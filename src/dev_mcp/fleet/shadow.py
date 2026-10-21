"""Shadow-first control for rules which could stop the fleet.

State lives in the existing fleet events table, not in a new schema/table.
Callers must pass every potentially blocking decision through check().  The
caller remains responsible for carrying out the returned "blocked" decision.
Integrations in the guard and scheduler belong to their respective owners.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .store import Store

FAMILIES = frozenset({"project_gate", "guard", "freeze", "zone", "watchdog"})
STATE_EVENTS = frozenset({"shadow_rule_registered", "shadow_rule_promoted", "shadow_rule_demoted"})
SUCCESS = frozenset({"accept", "integrate", "deployed", "platform_deployed", "known_good"})
EDITS = frozenset({"write", "edit", "apply_patch", "file_write", "file_edit"})
SHADOW_HOURS = 4
REPLAY_HOURS = 24
BLOCK_HOURS = 1
FALSE_BLOCK_THRESHOLD = 0.05


def _at(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc(now: datetime | None) -> datetime:
    return _at(now or datetime.now(timezone.utc))


def _payload(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    raw = row["payload"]
    if isinstance(raw, dict):
        return raw
    try:
        result = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    family: str
    fingerprint: str
    mode: str
    changed_at: datetime
    enabled_at: datetime | None = None


@dataclass(frozen=True)
class HistoryEvent:
    at: datetime
    kind: str
    subject: str = ""
    revision: str = ""
    rule_id: str = ""
    fingerprint: str = ""
    reason: str = ""
    unchanged: bool = False


@dataclass(frozen=True)
class Replay:
    attempts: int
    false_blocks: int
    examples: tuple[str, ...]

    @property
    def false_rate(self) -> float:
        return self.false_blocks / self.attempts if self.attempts else 0.0


@dataclass(frozen=True)
class Decision:
    blocked: bool
    mode: str
    reason: str


def history_event(row: Mapping[str, Any]) -> HistoryEvent:
    """Convert either a fleet event row or a guard JSONL record."""
    data = _payload(row) if "payload" in row else dict(row)
    code = str(row.get("code") or "")
    kind = str(row.get("kind") or code)
    if code and kind == code:
        # Only a completed guard deploy is successful; a preflight isn't.
        kind = "deployed" if code == "deployed" and data.get("stage") == "after" else f"guard:{code}"
    project, tz = str(row.get("project") or ""), str(row.get("tz") or "")
    candidate = str(data.get("candidate") or row.get("candidate") or "")
    subject = str(data.get("subject") or row.get("subject") or "")
    if not subject:
        subject = f"deploy:{candidate}" if candidate else (
            f"{project}/{tz}" if project and tz else ""
        )
    return HistoryEvent(
        at=_at(str(row.get("ts"))), kind=kind, subject=subject,
        revision=str(data.get("revision") or row.get("revision") or candidate),
        rule_id=str(data.get("rule_id") or row.get("rule_id") or ""),
        fingerprint=str(data.get("fingerprint") or row.get("fingerprint") or ""),
        reason=str(data.get("reason") or row.get("reason") or ""),
        unchanged=data.get("unchanged") is True,
    )


def replay(
    rule: Rule,
    history: Iterable[HistoryEvent],
    *,
    now: datetime | None = None,
    window: timedelta = timedelta(hours=REPLAY_HOURS),
) -> Replay:
    """Count decisions which would falsely block later unchanged success.

    Only exact subject matches count.  A changed revision or any intervening
    write invalidates the no-edit proof; unknown outcomes aren't called false.
    """
    end = _utc(now)
    timeline = sorted(
        (e for e in history if end - window <= e.at <= end),
        key=lambda e: e.at,
    )
    attempts = [e for e in timeline if e.kind == "would_block"
                and e.rule_id == rule.rule_id
                and e.fingerprint == rule.fingerprint]
    examples: list[str] = []
    for attempt in attempts:
        if not attempt.subject:
            continue
        for outcome in timeline:
            if outcome.at <= attempt.at or outcome.subject != attempt.subject:
                continue
            if outcome.kind not in SUCCESS:
                continue
            if attempt.revision and outcome.revision and attempt.revision != outcome.revision:
                continue
            if attempt.revision and not outcome.revision and not outcome.unchanged:
                continue
            if any(edit.kind in EDITS and edit.subject == attempt.subject
                   and attempt.at < edit.at < outcome.at for edit in timeline):
                continue
            examples.append(
                f"{attempt.subject}: would_block {attempt.at.isoformat()} "
                f"({attempt.reason}) -> {outcome.kind} {outcome.at.isoformat()} "
                "without edits"
            )
            break
    return Replay(len(attempts), len(examples), tuple(examples))


class ShadowRules:
    """Durable registry and deterministic policy engine; never starts workers."""

    def __init__(
        self, store: Store, *, guard_journal: Path | None = None,
        min_shadow: timedelta = timedelta(hours=SHADOW_HOURS),
        window: timedelta = timedelta(hours=REPLAY_HOURS),
        false_threshold: float = FALSE_BLOCK_THRESHOLD,
        max_block: timedelta = timedelta(hours=BLOCK_HOURS),
        min_attempts: int = 1,
    ) -> None:
        if min_shadow.total_seconds() < 0 or window.total_seconds() <= 0:
            raise ValueError("invalid shadow or replay duration")
        if max_block.total_seconds() <= 0 or not 0 < false_threshold <= 1 or min_attempts < 1:
            raise ValueError("invalid enforcement policy")
        self.store, self.guard_journal = store, guard_journal
        self.min_shadow, self.window = min_shadow, window
        self.false_threshold, self.max_block = false_threshold, max_block
        self.min_attempts = min_attempts

    @staticmethod
    def _rules(tx: Any) -> dict[str, Rule]:
        states: dict[str, Rule] = {}
        for row in tx.query(
            "SELECT ts,kind,payload FROM events "
            "WHERE kind IN ('shadow_rule_registered','shadow_rule_promoted',"
            "'shadow_rule_demoted') ORDER BY id"
        ):
            data = _payload(row)
            key = data.get("rule_id")
            if not isinstance(key, str) or not key:
                continue
            stamp = _at(row["ts"])
            if row["kind"] == "shadow_rule_registered":
                states[key] = Rule(key, str(data["family"]), str(data["fingerprint"]),
                                   "shadow", stamp)
            elif key in states and states[key].fingerprint == data.get("fingerprint"):
                previous = states[key]
                if row["kind"] == "shadow_rule_promoted":
                    states[key] = Rule(key, previous.family, previous.fingerprint,
                                       "enforce", previous.changed_at, stamp)
                else:
                    states[key] = Rule(key, previous.family, previous.fingerprint,
                                       "shadow", stamp)
        return states

    def all(self) -> dict[str, Rule]:
        with self.store.read() as tx:
            return self._rules(tx)

    def get(self, rule_id: str) -> Rule:
        try:
            return self.all()[rule_id]
        except KeyError:
            raise KeyError(f"rule not registered: {rule_id}") from None

    def register(
        self, rule_id: str, family: str, fingerprint: str, *, now: datetime | None = None,
    ) -> Rule:
        """New/modified fingerprint *always* starts in shadow."""
        if not rule_id.strip() or not fingerprint.strip() or family not in FAMILIES:
            raise ValueError("rule id, supported family and fingerprint required")
        stamp = _utc(now)
        with self.store.tx() as tx:
            old = self._rules(tx).get(rule_id)
            if old and old.fingerprint == fingerprint and old.family == family:
                return old
            tx.record_event(kind="shadow_rule_registered", ts=stamp.isoformat(),
                            payload={"rule_id": rule_id, "family": family,
                                     "fingerprint": fingerprint, "mode": "shadow"})
            return Rule(rule_id, family, fingerprint, "shadow", stamp)

    def history(self, *, now: datetime | None = None) -> list[HistoryEvent]:
        """Read the preceding 24h (or configured window) of fleet and guard evidence."""
        end = _utc(now)
        with self.store.read() as tx:
            rows = tx.query("SELECT ts,kind,project,tz,payload FROM events WHERE ts>=? AND ts<=? "
                            "ORDER BY id", ((end-self.window).isoformat(), end.isoformat()))
        result: list[HistoryEvent] = []
        for row in rows:
            try:
                result.append(history_event(dict(row)))
            except (ValueError, TypeError, KeyError):
                continue
        if self.guard_journal is not None:
            try:
                with self.guard_journal.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            raw = json.loads(line)
                            if isinstance(raw, dict):
                                event = history_event(raw)
                                if end-self.window <= event.at <= end:
                                    result.append(event)
                        except (ValueError, TypeError, KeyError):
                            continue  # malformed historical line cannot abort replay
            except FileNotFoundError:
                pass
        return sorted(result, key=lambda x: x.at)

    def observe_deadlocks(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Journal TZ-54 proposals without changing scheduling or claims.

        A separate queue integration must observe these suggestions for at
        least 24 hours before a reviewer can choose to enable exemptions.
        Calling this method never promotes a rule or repairs a styk.
        """
        from .deadlock import cycles, exempt_reviews, recovery_plan, wait_graph

        stamp = _utc(now)
        with self.store.tx() as tx:
            graph = wait_graph(tx)
            recovery = recovery_plan(tx)
            report: dict[str, Any] = {
                "mode": "shadow",
                "minimum_observation_hours": 24,
                "cycles": [list(group) for group in cycles(graph)],
                "exempt_reviews": [list(key) for key in sorted(exempt_reviews(tx))],
                "stale_styks": [list(key) for key in recovery.stale_styks],
                "repair_projects": list(recovery.repair_projects),
            }
            tx.record_event(kind="deadlock_shadow", ts=stamp.isoformat(),
                            project="Fleet Platform", tz="TZ-54", payload=report)
        return report

    def _ticket(self, tx: Any, rule: Rule, reason: str, examples: tuple[str, ...],
                stamp: datetime) -> None:
        """One actionable triage per rule version and reason, not one per tick."""
        episode = rule.enabled_at.isoformat() if reason == "stalled_block" and rule.enabled_at else ""
        key = hashlib.sha256((rule.rule_id + "\0" + rule.fingerprint + "\0" +
                              reason + "\0" + episode)
                             .encode()).hexdigest()[:16]
        tz = f"triage:shadow:{key}"
        project = "Fleet Platform"
        if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?", (project, tz)):
            return
        if not tx.one("SELECT 1 FROM projects WHERE name=?", (project,)):
            tx.record_event(kind="shadow_ticket_unavailable", ts=stamp.isoformat(),
                            payload={"rule_id": rule.rule_id, "reason": reason})
            return
        body = ("[shadow-rule] rule=" + rule.rule_id + ", family=" + rule.family +
                ", fingerprint=" + rule.fingerprint + ", problem=" + reason +
                ". Verify and correct the rule before re-enabling. Examples:\n" +
                "\n".join(examples[:5]))
        ts = stamp.isoformat()
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, tz, f"Rule falsely blocks: {rule.rule_id}", "ready", 2, "triage",
             "", "", "[]", "[]", "[]", "[]", None, body, ts, ts),
            event={"kind": "shadow_ticket", "project": project, "tz": tz,
                   "ts": ts, "payload": {"rule_id": rule.rule_id, "reason": reason}},
        )

    def advance(
        self, rule_id: str, *, now: datetime | None = None,
        history: Iterable[HistoryEvent] | None = None,
        predicate: Callable[[list[HistoryEvent]], Iterable[HistoryEvent]] | None = None,
    ) -> Replay:
        """Replay, then automatically promote after enough shadow observation."""
        stamp = _utc(now)
        rule = self.get(rule_id)
        evidence = list(history) if history is not None else self.history(now=stamp)
        if predicate is not None:
            evidence.extend(predicate(list(evidence)))
        report = replay(rule, evidence, now=stamp, window=self.window)
        with self.store.tx() as tx:
            current = self._rules(tx).get(rule_id)
            if current != rule or current is None or current.mode != "shadow":
                return report
            tx.record_event(kind="shadow_replay", ts=stamp.isoformat(),
                            payload={"rule_id": rule_id, "fingerprint": rule.fingerprint,
                                     "attempts": report.attempts,
                                     "false_blocks": report.false_blocks,
                                     "false_rate": report.false_rate,
                                     "window_hours": self.window.total_seconds()/3600})
            if report.attempts and report.false_rate >= self.false_threshold:
                self._ticket(tx, rule, "false_blocks", report.examples, stamp)
            elif (stamp - rule.changed_at >= self.min_shadow
                  and report.attempts >= self.min_attempts
                  # Guard is an independent process; omitting its journal is
                  # not evidence that its historical decisions were safe.
                  and (rule.family != "guard" or history is not None or
                       (self.guard_journal is not None and self.guard_journal.is_file()))
                  and report.false_rate < self.false_threshold):
                tx.record_event(kind="shadow_rule_promoted", ts=stamp.isoformat(),
                                payload={"rule_id": rule_id, "fingerprint": rule.fingerprint,
                                         "mode": "enforce", "false_rate": report.false_rate})
        return report

    def check(
        self, rule_id: str, *, would_block: bool, subject: str, reason: str,
        revision: str = "", code: str = "", defect_found: bool = False,
        now: datetime | None = None,
    ) -> Decision:
        """Evaluate every call; shadow decisions log would_block but never stop."""
        stamp = _utc(now)
        rule = self.get(rule_id)
        if not subject.strip():
            raise ValueError("stable blocking subject is required")
        payload = {"rule_id": rule_id, "fingerprint": rule.fingerprint,
                   "subject": subject, "reason": reason, "revision": revision,
                   "code": code or reason, "defect_found": defect_found}
        kind = ("would_block" if would_block else "shadow_observed"
                ) if rule.mode == "shadow" else (
                    "shadow_enforced_block" if would_block else "shadow_enforced_clear"
                )
        with self.store.tx() as tx:
            fresh = self._rules(tx).get(rule_id)
            if fresh != rule:
                rule = fresh if fresh is not None else rule
                payload["fingerprint"] = rule.fingerprint
                kind = ("would_block" if would_block else "shadow_observed"
                        ) if rule.mode == "shadow" else (
                            "shadow_enforced_block" if would_block else "shadow_enforced_clear"
                        )
            tx.record_event(kind=kind, ts=stamp.isoformat(), payload=payload)
        if rule.mode == "enforce" and would_block and not defect_found:
            if self.unstick(rule_id, now=stamp):
                # The decision which trips the circuit breaker must also be
                # observable as a non-blocking shadow decision.
                with self.store.tx() as tx:
                    tx.record_event(kind="would_block", ts=stamp.isoformat(),
                                    payload=payload)
                return Decision(False, "shadow", reason)
        return Decision(bool(would_block and rule.mode == "enforce"), rule.mode, reason)

    def unstick(self, rule_id: str, *, now: datetime | None = None) -> bool:
        """Demote an uninterrupted, defect-free >1h block of one code/subject."""
        stamp = _utc(now)
        with self.store.tx() as tx:
            rule = self._rules(tx).get(rule_id)
            if not rule or rule.mode != "enforce":
                return False
            rows = tx.query(
                "SELECT ts,kind,payload FROM events WHERE kind IN "
                "('shadow_enforced_block','shadow_enforced_clear') ORDER BY id"
            )
            active: dict[str, tuple[str, datetime, bool]] = {}
            for row in rows:
                data = _payload(row)
                if data.get("rule_id") != rule_id or data.get("fingerprint") != rule.fingerprint:
                    continue
                # An older enforcement episode is not part of this one: after
                # demotion and a fresh replay, promotion starts a new streak.
                if rule.enabled_at is not None and _at(row["ts"]) < rule.enabled_at:
                    continue
                subject = str(data.get("subject") or "")
                code = str(data.get("code") or "")
                if row["kind"] == "shadow_enforced_clear":
                    active.pop(subject, None)
                else:
                    prior = active.get(subject)
                    defect = data.get("defect_found") is True
                    if prior and prior[0] == code:
                        active[subject] = (code, prior[1], prior[2] or defect)
                    else:
                        active[subject] = (code, _at(row["ts"]), defect)
            offenders = [(s, c, first) for s, (c, first, defect) in active.items()
                         if not defect and stamp - first > self.max_block]
            if not offenders:
                return False
            offender = min(offenders, key=lambda x: x[2])
            reason = (f"stalled_block code={offender[1]} subject={offender[0]} "
                      f"since={offender[2].isoformat()}")
            tx.record_event(kind="shadow_rule_demoted", ts=stamp.isoformat(),
                            payload={"rule_id": rule_id, "fingerprint": rule.fingerprint,
                                     "mode": "shadow", "reason": reason})
            self._ticket(tx, rule, "stalled_block", (reason,), stamp)
            return True
