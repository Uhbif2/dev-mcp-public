"""TZ-47: one bounded, durable stall-watch step.

The observer consumes registry facts plus *verified* external sensor values.
Unknown is not down. It never executes a command from a frozen task, and it
never treats a guessed policy or deployment as authority to thaw one.

The tick is suitable for a controller loop or an independently supervised
autostart worker. Integration/notification belong to a trusted launcher,
not to a project spec or an agent-provided callback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import probe

STALL_MIN = 10
RED_GATE_MIN = 30
UNINTEGRATED_MIN = 30
PULSE_MIN = 5
EXTENSION_MIN = 5
METRIC_SAMPLE_MAX_AGE_S = 60
METRIC_SKEW_S = 10
METRIC_CONFIRM_MAX_GAP_S = 180
BOOT_NO_TAKE_MIN = 10
REPAIR_RETRY_MIN = 10
PROJECT = "Fleet Platform"
_INTERVENTION_LEDGERS: dict[tuple[str, int], Any] = {}
_INTERVENTION_LOCK = threading.Lock()
_GUARD_FAILURES = frozenset({
    "protected_path", "tests_red", "dirty_live", "not_ff",
    "unreviewed_paths", "merge_error", "validation_error",
    "restart_error", "probe_red", "canary_unavailable",
    "canary_unhealthy", "canary_skew", "safe_mode",
    "watch_error", "rollback_failed", "git_error",
})


def _dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str) and raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else None


def _old(raw: Any, now: datetime, minutes: int) -> bool:
    when = _dt(raw)
    return when is not None and (now - when) >= timedelta(minutes=minutes)


def _json(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _deps(raw: Any) -> tuple[str, ...]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ()
    return tuple(str(x) for x in data) if isinstance(data, list) else ()


@dataclass(frozen=True)
class Signals:
    """Signals supplied by trusted sensors. None means unmeasured, not failure.

    A satisfied blocker maps (project, tz) to a *specific verified fact*.
    policy_conflicts maps the same key to a statement proved by policy_info,
    never a substring guess. Deployment waits remain deploy_events' domain.
    """

    runnable: int | None = None  # authoritative fleet.cmd why figure
    extension_runnable: int | None = None
    extension_enabled: bool | None = None
    extension_last_tick: datetime | None = None
    chatgpt_up: bool | None = None
    boot_at: datetime | None = None
    guard_heartbeat: datetime | None = None
    guard_rejections: tuple[str, ...] = ()
    guard_events: tuple[str, ...] = ()
    satisfied_blockers: Mapping[tuple[str, str], str] = field(default_factory=dict)
    policy_conflicts: Mapping[tuple[str, str], str] = field(default_factory=dict)
    # Classifies why/online sensor for owner-facing repair instructions.
    stalled_reason: str = ""
    ambiguities: Mapping[str, str] = field(default_factory=dict)
    intervention_metrics: Mapping[str, Any] | None = None
    # Only comparable, completed supervisor/browser measurements may drive a
    # live metrics alert. None leaves explicit synthetic Signals compatible.
    metrics_comparable: bool | None = None
    metric_sample_id: str = ""
    metric_supervisor_at: datetime | None = None
    metric_project_breakdown: str = ""


@dataclass(frozen=True)
class Finding:
    kind: str
    project: str
    subject: str
    detail: str
    repair: str
    block_id: int = 0
    fingerprint_key: str = ""

    @property
    def fingerprint(self) -> str:
        # Intervention counts change throughout the day. They are evidence in
        # the alert, but not a new outage/cause on every increment.
        # Урок 38: detail несёт метки времени и счётчики — в отпечатке он делал
        # каждый тик «новым эпизодом» и новым тикетом (≈17 тыс. triage за час).
        raw = json.dumps([self.kind, self.project, self.subject, self.fingerprint_key],
                         ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _issue(kind: str, project: str, subject: str, detail: str, repair: str,
           block_id: int = 0, *, fingerprint_key: str = "") -> Finding:
    return Finding(kind, project, subject, detail[:600], repair, block_id,
                   fingerprint_key)


def _blocked(tx: Any, task: Any) -> tuple[int, dict[str, Any]] | None:
    last = tx.one(
        "SELECT id,kind,payload FROM events WHERE project=? AND tz=? "
        "AND kind IN ('block','freeze','thaw','reactivate','approve','auto_reactivate') "
        "ORDER BY id DESC LIMIT 1", (task["project"], task["tz"]),
    )
    if last is None or last["kind"] != "block":
        return None  # never override owner's manual freeze
    return int(last["id"]), _json(last["payload"])


def detect(store: Any, *, now: datetime, signals: Signals = Signals()) -> list[Finding]:
    """One read-only snapshot. Every finding has a bounded, classed repair."""
    if now.tzinfo is None:
        raise ValueError("watchdog needs aware time")
    now = now.astimezone(timezone.utc)
    findings: list[Finding] = []
    with store.read() as tx:
        active = int(tx.scalar(
            "SELECT COUNT(*) FROM claims c JOIN workers w ON w.id=c.worker_id "
            "WHERE c.state='active' AND w.revoked_at IS NULL AND w.kind='chatgpt'") or 0)
        take = tx.one("SELECT ts FROM events WHERE kind='take' ORDER BY id DESC LIMIT 1")
        earliest = tx.one("SELECT MIN(updated_at) AS ts FROM tasks WHERE state='ready'")
        start = take["ts"] if take is not None else (
            earliest["ts"] if earliest is not None else None)
        if (signals.runnable is not None and signals.runnable > 0
                and _old(start, now, STALL_MIN)):
            reason = signals.stalled_reason or (
                "расширение выключено" if signals.extension_enabled is False
                else "ChatGPT недоступен" if signals.chatgpt_up is False
                else "проверь why, сеть, расширение и ёмкость")
            findings.append(_issue("no_take", PROJECT, "runnable", reason, "restore_agents"))

        if signals.boot_at and _old(signals.boot_at, now, BOOT_NO_TAKE_MIN):
            boot = _dt(signals.boot_at)
            assert boot is not None
            recent_take = tx.one("SELECT id FROM events WHERE kind='take' AND ts>=? LIMIT 1",
                                 (boot.isoformat(),))
            if recent_take is None:
                findings.append(_issue("boot_no_take", PROJECT, boot.isoformat(),
                                       "10 мин после загрузки без fleet_take",
                                       "restore_agents"))

        if signals.runnable is not None and signals.runnable > 0:
            if signals.extension_enabled is False:
                findings.append(_issue("extension_off", PROJECT, "extension",
                                       "расширение отключено при runnable>0", "owner"))
            if signals.extension_last_tick and _old(signals.extension_last_tick, now, EXTENSION_MIN):
                findings.append(_issue("extension_stale", PROJECT, "extension",
                                       "расширение не тикает >5 мин", "owner"))
            if signals.chatgpt_up is False:
                findings.append(_issue("chatgpt_down", PROJECT, "chatgpt",
                                       "датчик сети: ChatGPT недоступен", "owner"))
        if (signals.metrics_comparable is not False
                and signals.runnable is not None and signals.extension_runnable is not None
                and signals.runnable != signals.extension_runnable):
            provenance = ""
            if signals.metric_supervisor_at and signals.extension_last_tick:
                skew = abs((signals.metric_supervisor_at - signals.extension_last_tick)
                           .total_seconds())
                provenance = (f"; supervisor_at={signals.metric_supervisor_at.isoformat()}"
                              f"; extension_at={signals.extension_last_tick.isoformat()}"
                              f"; skew_s={skew:.1f}")
                if signals.metric_project_breakdown:
                    provenance += f"; project_counts_now={signals.metric_project_breakdown}"
            findings.append(_issue("metrics_mismatch", PROJECT, "runnable",
                                   f"why={signals.runnable}; extension={signals.extension_runnable}"
                                   f"{provenance}",
                                   "observability"))
        # TZ-48: verified InterventionLedger.metrics() provides the only trend
        # authority. Don't substitute manual audit counts or infer a rise from
        # absent data; the owner gets one actionable alert per rising episode.
        metrics = signals.intervention_metrics
        if (isinstance(metrics, Mapping) and metrics.get("rising") is True
                and type(metrics.get("today")) is int and type(metrics.get("yesterday")) is int
                and metrics["today"] > metrics["yesterday"] >= 0):
            findings.append(_issue("interventions_rising", PROJECT, "interventions",
                                   f"Растёт число вмешательств владельца: сегодня {metrics['today']}, "
                                   f"вчера {metrics['yesterday']}", "owner"))

        gates = tx.query(
            "SELECT e.project,e.ts,e.rc FROM events e JOIN "
            "(SELECT project,MAX(id) id FROM events WHERE kind='gate' "
            "GROUP BY project) x ON x.id=e.id JOIN projects p ON p.name=e.project WHERE p.active=1")
        for gate in gates:
            project = str(gate["project"] or "")
            if gate["rc"] is None or int(gate["rc"]) == 0:
                continue
            if not _old(gate["ts"], now, RED_GATE_MIN):
                continue
            styk = tx.one(
                "SELECT 1 FROM tasks WHERE project=? AND tz LIKE 'styk:%' "
                "AND state IN ('ready','taken','submitted','frozen','draft') LIMIT 1",
                (project,),
            )
            if styk is None:
                findings.append(_issue("red_trunk", project, "gate",
                                       "красный гейт >30 мин без ведущего стыка", "reopen_styk"))
            if active == 0:
                # Agent restoration is a separate higher-priority obstacle.
                findings.append(_issue("no_agents_red", project, "workers",
                                       "красный ствол и ноль активных захватов",
                                       "restore_agents"))

        # Guard is a GLOBAL deployment sensor. A refused candidate is not a
        # causal receipt linking that candidate to a project's old acceptance.
        # Old accept events are context only: aggregate them into one platform
        # incident instead of minting N false project-specific incidents.
        if len(signals.guard_rejections) >= 2 and len(set(signals.guard_rejections)) == 1:
            code = signals.guard_rejections[0]
            if code:
                pending_total = 0
                affected_projects: list[str] = []
                examples: list[str] = []
                for gate in gates:
                    if gate["rc"] is None or int(gate["rc"]) != 0:
                        continue
                    project = str(gate["project"] or "")
                    accepts = tx.query(
                        "SELECT e.id,e.ts,e.tz FROM events e "
                        "JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
                        "WHERE e.kind='accept' AND e.project=? AND t.state='accepted' "
                        "AND e.id=(SELECT MAX(a.id) FROM events a "
                        "WHERE a.kind='accept' AND a.project=e.project AND a.tz=e.tz)",
                        (project,))
                    integrated = set()
                    for row in tx.query(
                        "SELECT payload FROM events WHERE kind='integrate' AND project=?",
                        (project,)):
                        data = _json(row["payload"])
                        if data.get("commit") and isinstance(data.get("accept_id"), int):
                            integrated.add(data["accept_id"])
                    pending = [
                        row for row in accepts
                        if int(row["id"]) not in integrated
                        and _old(row["ts"], now, UNINTEGRATED_MIN)
                    ]
                    if pending:
                        pending_total += len(pending)
                        affected_projects.append(project)
                        examples.extend(f"{project}/{row['tz']}#{row['id']}"
                                        for row in pending[:max(0, 5 - len(examples))])
                if pending_total:
                    # No candidate SHA/path + accept_id join was provided by
                    # the sensor. Never assert that one of these projects
                    # caused the global denial. Timestamp text is evidence,
                    # not fingerprint identity; different guard codes are
                    # independent incidents.
                    findings.append(_issue(
                        "guard_denial", PROJECT, "unknown_accept",
                        f"correlation=unknown_accept; "
                        f"unintegrated_accepted={pending_total}; "
                        f"projects={','.join(sorted(affected_projects))[:160]}; "
                        f"examples={', '.join(examples)[:140]}; "
                        f"guard refuses {code}; "
                        f"last events: {'; '.join(signals.guard_events[-5:])[:160]}",
                        "guard_diagnosis", fingerprint_key=f"guard-code:{code}"))

        last_controller = tx.one(
            "SELECT ts FROM events WHERE kind='controller_tick' ORDER BY id DESC LIMIT 1")
        if last_controller and _old(last_controller["ts"], now, PULSE_MIN):
            findings.append(_issue("controller_stale", PROJECT, "controller",
                                   "пульс контроллера >5 мин", "restart_trusted"))
        if signals.guard_heartbeat and _old(signals.guard_heartbeat, now, PULSE_MIN):
            findings.append(_issue("guard_stale", PROJECT, "guard",
                                   "пульс стража >5 мин", "restart_trusted"))

        frozen = tx.query(
            "SELECT project,tz,needs FROM tasks WHERE state='frozen' ORDER BY project,tz")
        nodes = {(str(t["project"]), str(t["tz"])): t for t in frozen}
        edges: dict[tuple[str, str], tuple[str, ...]] = {}
        blocks: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        for key, task in nodes.items():
            found = _blocked(tx, task)
            if found is None:
                continue
            bid, payload = found
            blocks[key] = found
            deps = list(_deps(task["needs"]))
            for name in ("needs", "dependencies", "depends_on"):
                deps.extend(_deps(payload.get(name)))
            edges[key] = tuple(d for d in dict.fromkeys(deps) if (key[0], d) in nodes)
            reason = str(payload.get("reason") or "")
            # Deployment is handled only through verified deploy_events receipts.
            deploy_wait = (payload.get("change_source") == "platform_deploy"
                           or "platform_deploy" in reason.lower())
            proof = signals.satisfied_blockers.get(key)
            conflict = signals.policy_conflicts.get(key)
            if not deploy_wait and not proof:
                passed, argv = probe.passed(tx, *key)
                if passed:
                    proof = "verified probe: " + " ".join(argv or [])
            if proof and not deploy_wait:
                findings.append(_issue("satisfied_frozen", key[0], key[1],
                                       str(proof), "thaw", bid))
            if conflict and not deploy_wait:
                findings.append(_issue("policy_conflict", key[0], key[1],
                                       str(conflict), "thaw_and_document", bid))

        # A<->B cycle: deterministically unfreeze the contract publisher only.
        for key in sorted(edges):
            for other in edges[key]:
                peer = (key[0], other)
                if peer <= key or key[1] not in edges.get(peer, ()):
                    continue
                publisher = min(key, peer)  # stable choice; explicit contract first
                block = blocks.get(publisher)
                if block:
                    findings.append(_issue(
                        "wait_cycle", publisher[0], publisher[1],
                        f"цикл ожидания {key[1]} ↔ {peer[1]}: контракт первым",
                        "thaw", block[0]))
        for name, description in sorted(signals.ambiguities.items()):
            findings.append(_issue("ambiguous", PROJECT, name, str(description),
                                   "gemini_hypothesis"))
        # 13.10: прогресс, а не очередь — циклы, стоящие интенции, принятое мимо ствола.
        from .progress_watch import findings as progress_findings
        findings.extend(progress_findings(tx, now, _issue))
    # Different diagnostics of the same block may arrive together. A single
    # state transition handles them; all reasons remain in the journal.
    return findings


TICKET_BACKPRESSURE = 5  # открытых тикетов сторожа на всё флот — дальше только события
# Наблюдательные находки (metrics_mismatch и т.п.) мигают: один тикет на отпечаток за окно.
# Ремонтные (красный ствол, нет агентов) — по эпизодам: повтор после восстановления — новая беда.
TICKET_COOLDOWN = timedelta(hours=6)
COOLDOWN_REPAIRS = frozenset({"observability"})


def _ticket(tx: Any, finding: Finding, episode: int, now: datetime) -> None:
    from . import platform_manual
    if platform_manual.enabled():
        files, checks = _repair_scope(finding)
        platform_manual.enqueue(tx,finding.fingerprint,title=f'Watchdog: {finding.kind}',
                                detail=finding.detail,source_project=finding.project,
                                source_tz=finding.subject,owns=files,acceptance=checks)
        return
    row = tx.one("SELECT root FROM projects WHERE name=?", (finding.project,))
    if row is None:
        return
    # Урок 38: сторож не должен сам становиться потоком. Пока прежние тикеты не
    # разобраны, новые не создаются; находка остаётся в журнале (watchdog_open).
    # Урок 43: author_worker меняется при выдаче — считать по имени тикета и по
    # активным состояниям (замороженный тикет агентов не занимает — урок 43, хвост).
    open_tickets = int(tx.scalar(
        "SELECT COUNT(*) FROM tasks WHERE (tz LIKE 'watch:%' OR tz LIKE 'styk:watchdog:%') "
        "AND state IN ('ready','taken','submitted')") or 0)
    if open_tickets >= TICKET_BACKPRESSURE:
        return
    since = (now - TICKET_COOLDOWN).isoformat()
    for recent in () if finding.repair not in COOLDOWN_REPAIRS else tx.query(
            "SELECT payload FROM events WHERE kind IN ('watchdog_ticket','styk_open') AND ts>=?",
            (since,)):
        if _json(recent["payload"]).get("fingerprint") == finding.fingerprint:
            return
    tag = hashlib.sha256(f"{finding.fingerprint}:{episode}".encode()).hexdigest()[:12]
    tz = f"styk:watchdog:{tag}" if finding.repair == "reopen_styk" else f"watch:{tag}"
    if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?", (finding.project, tz)):
        return
    root = str(row["root"])
    files, checks = _repair_scope(finding)
    owns = [str(Path(root) / rel) for rel in files]
    body = (f"Сторож потока: {finding.kind}\n{finding.detail}\n"
            f"Класс ремонта: {finding.repair}. Воспроизведи первопричину, исправь код в owns, "
            "добавь регрессионную проверку и докажи, что заблокированная работа снова проходит.\n"
            "Не выполняй перезапуск без доверенного TZ-37 и не снимай ручной freeze.")
    tx.change(
        "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
        "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (finding.project, tz, f"Watchdog: {finding.kind}", "ready", 1, "work",
         "", "", "[]", json.dumps(owns), "[]", json.dumps(checks), "watchdog", body,
         now.isoformat(), now.isoformat()),
        event={"kind": "styk_open" if finding.repair == "reopen_styk"
               else "watchdog_ticket", "project": finding.project, "tz": tz,
               "payload": {"fingerprint": finding.fingerprint, "class": finding.repair}},
    )


def _repair_scope(finding: Finding) -> tuple[list[str], list[str]]:
    if finding.kind == "intent_stall":
        modules = ["intents/stages.py", "intents/stage_check.py", "fleet/queue.py",
                   "fleet/progress_watch.py"]
        tests = ["tests/intents/test_stage_problem_kinds.py", "tests/fleet/test_progress_front.py"]
    elif finding.kind == "accepted_not_integrated" or finding.repair == "reopen_styk":
        modules = ["fleet/claim_accept_flow.py", "fleet/integrate.py", "fleet/watchdog.py"]
        tests = ["tests/fleet/test_integrate.py", "tests/fleet/test_watchdog.py"]
    elif finding.kind == "refusal_storm":
        modules = ["fleet/queue.py", "fleet/claim_tree.py", "fleet/claim_take_routing.py"]
        tests = ["tests/fleet/test_claim_preparation_concurrency.py", "tests/fleet/test_queue.py"]
    elif finding.kind in {"loop_task", "loop_project"}:
        modules = ["fleet/queue.py", "fleet/review.py", "fleet/tools.py", "fleet/judgment.py"]
        tests = ["tests/fleet/test_queue.py", "tests/fleet/test_claim_addressed_actions.py"]
    else:
        modules = ["fleet/watchdog.py", "fleet/supervisor.py", "fleet/queue.py", "fleet/wake.py"]
        tests = ["tests/fleet/test_watchdog.py", "tests/fleet/test_supervisor.py"]
    return ([f"src/dev_mcp/{rel}" for rel in modules] + tests,
            ["py -3 -m pytest -q " + " ".join(tests)])


_HISTORY_KINDS = (
    "'watchdog_open','watchdog_attempt','watchdog_recovered','watchdog_owner_signal'"
)


def _watchdog_history(store: Any) -> tuple[dict[str, list[dict[str, Any]]], int, int, bool]:
    """Read only events after a durable high-water mark, outside any write tx.

    On first deployment an old history is read once through WAL; subsequent
    ticks process only new IDs. Keep the last episode and at most two attempts.
    """
    with store.read() as rx:
        marker = rx.one(
            "SELECT id,payload FROM events WHERE kind='watchdog_cursor' "
            "ORDER BY id DESC LIMIT 1")
        state = _json(marker["payload"]) if marker else {}
        if rx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_state'"):
            saved_state = rx.one("SELECT cursor_id,through,history FROM watchdog_state WHERE id=1")
            if saved_state and marker and int(saved_state["cursor_id"]) == int(marker["id"]):
                state = {"through": int(saved_state["through"]),
                         "history": _json(saved_state["history"])}
            elif "history" not in state:
                # A missing/inconsistent new checkpoint must replay durable
                # incident events, not silently forget suppression history.
                state = {}
        after = int(state.get("through", 0)) if marker else 0
        saved = state.get("history", {})
        history: dict[str, list[dict[str, Any]]] = (
            {str(key): list(value) for key, value in saved.items()
             if isinstance(value, list)} if isinstance(saved, dict) else {}
        )
        rows = rx.query(
            "SELECT id,ts,kind,payload FROM events WHERE id>? AND kind IN ("
            + _HISTORY_KINDS + ") ORDER BY id", (after,))
        through = int(rx.scalar("SELECT COALESCE(MAX(id),0) FROM events") or 0)
    for row in rows:
        fingerprint = str(_json(row["payload"]).get("fingerprint") or "")
        if fingerprint:
            history.setdefault(fingerprint, []).append({
                "id": int(row["id"]), "ts": str(row["ts"]),
                "kind": str(row["kind"]),
            })
    # No history older than the last recovery is relevant to the next episode.
    # For an active episode the only required facts are its last two retries,
    # the existence of an owner signal and (for tickets) the recovery ID.
    for fingerprint, records in list(history.items()):
        if not records:
            del history[fingerprint]
            continue
        cleared = max((i for i, r in enumerate(records)
                       if r["kind"] == "watchdog_recovered"), default=-1)
        if cleared >= 0:
            records = records[cleared:]
        if records[-1]["kind"] != "watchdog_recovered":
            attempts = [r for r in records if r["kind"] == "watchdog_attempt"][-2:]
            opened = next((r for r in records if r["kind"] == "watchdog_open"), None)
            signalled = next((r for r in records
                              if r["kind"] == "watchdog_owner_signal"), None)
            recovered = records[0] if records[0]["kind"] == "watchdog_recovered" else None
            keep = [r for r in (recovered, opened, *attempts, signalled) if r is not None]
            records = sorted(keep, key=lambda r: r["id"])
        history[fingerprint] = records
    return history, through, int(marker["id"]) if marker else 0, bool(rows)


def watchdog_tick(store: Any, *, now: datetime | None = None,
                  signals: Signals = Signals()) -> list[Finding]:
    """Persist one incident per episode, retry twice, then queue one owner signal.

    The *owner signal* is an outbox event (not an unverified claim that a push
    was delivered). A trusted delivery worker can deduplicate by fingerprint.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("watchdog needs aware time")
    now = now.astimezone(timezone.utc)
    findings = detect(store, now=now, signals=signals)
    from .progress_watch import front_snapshot, write_front_report
    with store.read() as tx:
        fronts = front_snapshot(tx, now)
    raw_path = str(getattr(store, "path", ""))
    if raw_path and raw_path != ":memory:":
        write_front_report(Path(raw_path).with_name("progress-front.json"), now, fronts)
    # A fast-changing queue is sampled independently by the supervisor and
    # browser. Do not create a durable incident from one comparable but
    # non-atomic pair; require two different completed browser ticks. Cache
    # the confirmation per tick so controller retries cannot close an incident
    # merely because they read the same tick twice.
    if signals.metric_sample_id:
        mismatch = any(f.kind == "metrics_mismatch" for f in findings)
        with store.tx() as tx:
            previous = tx.one(
                "SELECT ts,payload FROM events WHERE kind='watchdog_metric_sample' "
                "ORDER BY id DESC LIMIT 1")
            last = _json(previous["payload"]) if previous else {}
            same_tick = last.get("sample_id") == signals.metric_sample_id
            when = _dt(previous["ts"]) if previous else None
            recent = (when is not None
                      and timedelta(0) <= now - when
                      <= timedelta(seconds=METRIC_CONFIRM_MAX_GAP_S))
            supervisor_id = (signals.metric_supervisor_at.isoformat()
                             if signals.metric_supervisor_at else "")
            independent_supervisor = last.get("supervisor_at") != supervisor_id
            confirmed = bool(mismatch and (
                last.get("confirmed") is True if same_tick else
                last.get("mismatch") is True and recent and independent_supervisor))
            if not same_tick:
                tx.record_event(
                    kind="watchdog_metric_sample", project=PROJECT, tz="runnable",
                    ts=now.isoformat(),
                    payload={"sample_id": signals.metric_sample_id,
                             "mismatch": mismatch, "confirmed": confirmed,
                             "why": signals.runnable,
                             "extension": signals.extension_runnable,
                             "supervisor_at": supervisor_id})
        if mismatch and not confirmed:
            findings = [f for f in findings if f.kind != "metrics_mismatch"]
    current = {f.fingerprint: f for f in findings}
    history, through, marker_id, has_new_events = _watchdog_history(store)
    # Serialize the bounded current state before acquiring SQLite's writer.
    history_json = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    with store.tx() as tx:
        latest = tx.scalar(
            "SELECT COALESCE(MAX(id),0) FROM events WHERE kind='watchdog_cursor'")
        if int(latest or 0) != marker_id:
            # A concurrent tick already advanced the cursor. Do not replay
            # observations from an outdated snapshot.
            return findings
        if not tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_attention'"):
            tx.change("CREATE TABLE watchdog_attention(fingerprint TEXT PRIMARY KEY,episode INTEGER NOT NULL,"
                      "summary TEXT NOT NULL,state TEXT NOT NULL)",event='watchdog_attention.schema')
        for fingerprint, records in history.items():
            if fingerprint in current or records[-1]["kind"] == "watchdog_recovered":
                continue
            tx.record_event(kind="watchdog_recovered", project=PROJECT, ts=now.isoformat(),
                            payload={"fingerprint": fingerprint, "predicate": "cleared"})
            tx.change("UPDATE watchdog_attention SET state='resolved' WHERE fingerprint=? AND state='active'",
                      (fingerprint,),event={'kind':'watchdog_attention.resolved','payload':{'fingerprint':fingerprint}})
        thawed: set[tuple[str, str]] = set()
        for f in findings:
            records = history.get(f.fingerprint, [])
            last_clear = max((i for i, r in enumerate(records)
                              if r["kind"] == "watchdog_recovered"), default=-1)
            episode = records[last_clear]["id"] if last_clear >= 0 else 0
            open_records = records[last_clear + 1:]
            previous = [r for r in open_records if r["kind"] == "watchdog_attempt"]
            if not open_records:
                tx.record_event(kind="watchdog_open", project=f.project, tz=f.subject,
                                ts=now.isoformat(),
                                payload={"fingerprint": f.fingerprint, "predicate": f.kind,
                                         "detail": f.detail, "repair": f.repair})
            attempt_due = not previous or _old(previous[-1]["ts"], now, REPAIR_RETRY_MIN)
            if not attempt_due:
                continue
            if f.repair == "owner" or f.kind == "boot_no_take":
                _attention(tx,f,int(episode))
                if not any(r["kind"] == "watchdog_owner_signal" for r in open_records):
                    tx.record_event(kind="watchdog_owner_signal", project=f.project,
                                    tz=f.subject, ts=now.isoformat(), payload={
                                        "fingerprint": f.fingerprint, "predicate": f.kind,
                                        "reason": f.detail,
                                        "action": f"Исправить {f.repair}: {f.project}/{f.subject}"})
                continue
            if len(previous) >= 2:
                _attention(tx,f,int(episode))
                if not any(r["kind"] == "watchdog_owner_signal" for r in open_records):
                    tx.record_event(kind="watchdog_owner_signal", project=f.project,
                                    tz=f.subject, ts=now.isoformat(), payload={
                                        "fingerprint": f.fingerprint, "predicate": f.kind,
                                        "reason": f.detail,
                                        "action": f"Исправить {f.repair}: {f.project}/{f.subject}"})
                continue
            tx.record_event(kind="watchdog_attempt", project=f.project, tz=f.subject,
                            ts=now.isoformat(),
                            payload={"fingerprint": f.fingerprint, "number": len(previous) + 1,
                                     "repair": f.repair})
            if f.repair == "gemini_hypothesis":
                tx.record_event(kind="watchdog_hypothesis_needed", project=f.project,
                                tz=f.subject, ts=now.isoformat(),
                                payload={"fingerprint": f.fingerprint,
                                         "facts": f.detail, "authority": "hypothesis_only"})
                continue
            if f.repair in ("thaw", "thaw_and_document") and f.block_id:
                key = (f.project, f.subject)
                if key not in thawed:
                    task = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", key)
                    last = tx.one(
                        "SELECT id,kind FROM events WHERE project=? AND tz=? "
                        "AND kind IN ('block','freeze','thaw','reactivate','approve','auto_reactivate') "
                        "ORDER BY id DESC LIMIT 1", key)
                    if (task and task["state"] == "frozen" and last
                            and int(last["id"]) == f.block_id and last["kind"] == "block"):
                        tx.change(
                            "UPDATE tasks SET state='ready',updated_at=? WHERE project=? "
                            "AND tz=? AND state='frozen'", (now.isoformat(), *key),
                            event={"kind": "thaw", "project": f.project, "tz": f.subject,
                                   "payload": {"why": f.detail, "predicate": f.kind,
                                               "block_id": f.block_id,
                                               "watchdog_fingerprint": f.fingerprint}},
                        )
                        thawed.add(key)
            else:
                _ticket(tx, f, int(episode), now)
            if f.repair == "thaw_and_document":
                _ticket(tx, f, int(episode), now)
        # Fence every completed tick, including one that creates its first
        # finding after an idle tick. Otherwise concurrent idle snapshots can
        # both emit the same incident before any history row advances the cursor.
        tx.record_event(
            kind="watchdog_cursor", project=PROJECT, ts=now.isoformat(),
            payload={"through": through, "history_size": len(history), "state_version": 2},
        )
        cursor_id = int(tx.scalar("SELECT MAX(id) FROM events WHERE kind='watchdog_cursor'"))
        if not tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_state'"):
            tx.change("CREATE TABLE watchdog_state (id INTEGER PRIMARY KEY CHECK(id=1), "
                      "cursor_id INTEGER NOT NULL, through INTEGER NOT NULL, history TEXT NOT NULL)",
                      event={"kind": "watchdog_state_schema"})
        tx.change("INSERT INTO watchdog_state(id,cursor_id,through,history) VALUES(1,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET cursor_id=excluded.cursor_id, "
                  "through=excluded.through,history=excluded.history",
                  (cursor_id, through, history_json),
                  event={"kind": "watchdog_state_advance", "payload": {"cursor_id": cursor_id}})
    return findings


def _attention(tx: Any, finding: Finding, episode: int) -> None:
    old = tx.one("SELECT episode,state FROM watchdog_attention WHERE fingerprint=?",(finding.fingerprint,))
    if old and old['state']=='active' and int(old['episode']) == episode:
        return
    tx.change("INSERT INTO watchdog_attention VALUES(?,?,?,'active') ON CONFLICT(fingerprint) "
              "DO UPDATE SET episode=excluded.episode,summary=excluded.summary,state='active'",
              (finding.fingerprint,episode,(finding.project+': '+finding.detail)[:600]),
              event={'kind':'watchdog_attention.open','payload':{'fingerprint':finding.fingerprint,'episode':episode}})


def _intervention_metrics(store: Any, now: datetime) -> Mapping[str, Any] | None:
    """Read actual TZ-48 metrics only after its ledger is installed.

    Cache the ledger handle: its initializer ensures SQLite schema and should
    not write two redundant schema events on every 30-second watchdog step.
    """
    from . import platform_manual
    if platform_manual.enabled():
        return None
    with store.read() as tx:
        if tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intervention_log'") is None:
            return None
    from .interventions import InterventionLedger
    key = (str(store.path), id(store))
    with _INTERVENTION_LOCK:
        ledger = _INTERVENTION_LEDGERS.get(key)
        if ledger is None:
            ledger = InterventionLedger(store, PROJECT, Path(store.path).parent)
            _INTERVENTION_LEDGERS[key] = ledger
    return ledger.metrics(now=now)


def _recent_guard_events(db: Path, now: datetime) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Bounded, read-only inspection of the actual guard's JSONL protocol.

    Only an installed data/fleet DB may point at its adjacent guard log. A
    successful deploy resets the denial streak; old events are not a current
    refusal. Never derive authority from a task-supplied path or message.
    """
    if db.parent.name != "fleet" or db.parent.parent.name != "data":
        return (), ()
    path = db.parents[2] / "logs" / "platform-guard.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 131072))
            blob = handle.read(131072)
        lines = blob.splitlines()
        if size > 131072:
            lines = lines[1:]  # first line may have been cut mid-JSON
    except OSError:
        return (), ()
    denials: list[str] = []
    events: list[str] = []
    for line in lines[-256:]:
        entry = _json(line.decode("utf-8", "replace"))
        code = entry.get("code")
        ts = _dt(entry.get("ts"))
        if not isinstance(code, str) or ts is None or not (now - timedelta(minutes=15) <= ts <= now):
            continue
        events.append(f"{ts.isoformat()} {code}")
        if code in ("known_good", "deployed", "rollback_ok"):
            denials.clear()
        elif code in _GUARD_FAILURES:
            denials.append(code)
    return tuple(denials[-4:]), tuple(events[-5:])


def _verified_blockers(store: Any) -> dict[tuple[str, str], str]:
    """Use only the recorded successful probe for the last live block."""
    proven: dict[tuple[str, str], str] = {}
    with store.read() as tx:
        for task in tx.query("SELECT project,tz FROM tasks WHERE state='frozen'"):
            key = str(task["project"]), str(task["tz"])
            last = _blocked(tx, task)
            if not last:
                continue
            reason = str(last[1].get("reason") or "")
            if last[1].get("change_source") == "platform_deploy" or "platform_deploy" in reason:
                continue
            passed, argv = probe.passed(tx, *key)
            if passed:
                proven[key] = "verified probe: " + " ".join(argv or [])
    return proven


def _verified_policy_conflicts(store: Any, db: Path) -> dict[tuple[str, str], str]:
    """Verify an explicit document/policy_info denial against the server policy.

    The source format is a line in the actual fleet_block(reason), which the
    queue already persists in the last block event:
      policy_info denied write: <absolute path>
    A worker's prose alone is not proof: the path must belong to its original
    task owns and the installed policy must now positively permit the write.
    Other access restrictions are not overridden.
    """
    if db.parent.name != "fleet" or db.parent.parent.name != "data":
        return {}
    claims = []
    with store.read() as tx:
        for task in tx.query("SELECT project,tz,owns FROM tasks WHERE state='frozen'"):
            blocked = _blocked(tx, task)
            if not blocked:
                continue
            payload = blocked[1]
            reason = str(payload.get("reason") or "")
            if payload.get("change_source") == "platform_deploy" or "platform_deploy" in reason:
                continue
            match = re.search(r"(?m)^policy_info denied write: (.+?)\s*$", reason)
            if match is None:
                continue
            raw = match.group(1)
            owns = _deps(task["owns"])
            if not Path(raw).is_absolute() or raw not in owns:
                continue
            claims.append((str(task["project"]), str(task["tz"]), raw))
    if not claims:
        return {}
    from ..policy import Policy, PolicyError
    try:
        policy = Policy.load(db.parents[2] / "policy.toml")
    except (PolicyError, OSError, ValueError):
        return {}
    results: dict[tuple[str, str], str] = {}
    for project, tz, raw in claims:
        try:
            policy.resolve(raw, for_write=True)
        except (PolicyError, OSError, ValueError):
            continue
        results[(project, tz)] = f"policy_info: confirmed writable own path {raw}"
    return results


def local_signals(store: Any, *, now: datetime | None = None) -> Signals:
    """Read verified local observations; no subprocess, network probe or guess.

    The supervisor's runnable count is derived from queue.peek_next. Extension
    lease seen_at is NOT a completed extension tick; use tick_at only when a
    sensor publishes that separate timestamp. Unknown fields remain None.
    """
    now = now or datetime.now(timezone.utc)
    raw_db = str(getattr(store, "path", ""))
    if not raw_db or raw_db == ":memory:":
        return Signals()
    db = Path(raw_db)
    status_path = db.with_name("launcher-health.json")
    try:
        status = _json(status_path.read_text(encoding="utf-8"))
    except OSError:
        status = {}
    supervisor = status.get("supervisor")
    supervisor = supervisor if isinstance(supervisor, dict) else {}
    ext = status.get("extension")
    ext = ext if isinstance(ext, dict) else {}
    demand = supervisor.get("demand")
    demand = demand if isinstance(demand, dict) else {}
    runnable = None
    checked = _dt(supervisor.get("checked_at"))
    if not _old(supervisor.get("checked_at"), now, PULSE_MIN):
        if checked and checked <= now and type(demand.get("runnable")) is int:
            runnable = max(0, demand["runnable"])
    extension_runnable = (ext["runnable"] if type(ext.get("runnable")) is int
                          and ext["runnable"] >= 0 else None)
    browser_tick = _dt(ext.get("tick_at"))
    comparable = bool(
        runnable is not None and extension_runnable is not None
        and checked is not None and browser_tick is not None
        and timedelta(0) <= now - checked <= timedelta(seconds=METRIC_SAMPLE_MAX_AGE_S)
        and timedelta(0) <= now - browser_tick <= timedelta(seconds=METRIC_SAMPLE_MAX_AGE_S)
        and abs((checked - browser_tick).total_seconds()) <= METRIC_SKEW_S
        and ext.get("enabled") is True
    )
    project_breakdown = ""
    if comparable:
        # The supervisor reports global peek_next(), while the extension sums
        # independently filtered peek_projects(). Those are NOT mathematically
        # identical when global conflict reservation spans projects. In that
        # state there is no valid common metric to compare: fail closed.
        try:
            from . import queue
            global_count = queue.peek_next(store)[1]
            project_counts = queue.peek_projects(store)
            project_total = sum(count for _name, _task, count in project_counts)
            comparable = global_count == project_total
            project_breakdown = ", ".join(
                f"{name}:{count}" for name, _task, count in project_counts if count
            )[:240]
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            comparable = False
    enabled = ext.get("enabled") if type(ext.get("enabled")) is bool else None
    # TZ-45 is the authority for ChatGPT availability. Use its persisted
    # resource transitions; never run a second network probe from the watchdog.
    # In particular absence of a transition is unknown, not "ChatGPT down".
    from . import world
    with store.read() as tx:
        net_state, _ = world.last_state(tx, "chatgpt-reachable")
    chatgpt_up = (True if net_state == world.UP else
                  False if net_state == world.DOWN else None)
    stalled_reason = ("ChatGPT недоступен по датчику TZ-45"
                      if chatgpt_up is False else "")
    guard_heartbeat = None
    from . import platform_manual
    automatic_deployment = not platform_manual.enabled()
    guard_rejections, guard_events = _recent_guard_events(db, now) if automatic_deployment else ((), ())
    if automatic_deployment and len(db.parents) >= 3:
        try:
            guard = _json((db.parents[2] / "data" / "guard-heartbeat.json")
                          .read_text(encoding="utf-8"))
            guard_heartbeat = _dt(guard.get("time"))
        except OSError:
            pass
    boot_at = None
    if os.name == "nt":
        try:
            import ctypes
            kernel = ctypes.WinDLL("kernel32")
            kernel.GetTickCount64.restype = ctypes.c_ulonglong
            uptime_ms = int(kernel.GetTickCount64())
            boot_at = now - timedelta(milliseconds=uptime_ms)
        except (AttributeError, OSError, ValueError):
            pass
    try:
        metrics = _intervention_metrics(store, now)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        metrics = None  # unavailable ledger is unknown, not a rising trend
    try:
        satisfied = _verified_blockers(store)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        satisfied = {}
    try:
        policy_conflicts = _verified_policy_conflicts(store, db)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        policy_conflicts = {}
    return Signals(runnable=runnable, extension_runnable=extension_runnable,
                   extension_enabled=enabled, extension_last_tick=_dt(ext.get("tick_at")),
                   metrics_comparable=comparable,
                   metric_sample_id=browser_tick.isoformat() if comparable and browser_tick else "",
                   metric_supervisor_at=checked if comparable else None,
                   metric_project_breakdown=project_breakdown if comparable else "",
                   boot_at=boot_at, guard_heartbeat=guard_heartbeat,
                   chatgpt_up=chatgpt_up, stalled_reason=stalled_reason,
                   guard_rejections=guard_rejections, guard_events=guard_events,
                   satisfied_blockers=satisfied, policy_conflicts=policy_conflicts,
                   intervention_metrics=metrics)


def _lease(path: Path) -> Any:
    """Cross-process singleton: an autostart restart cannot duplicate a watcher."""
    from contextlib import contextmanager

    @contextmanager
    def hold():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            try:
                handle.seek(0)
                if not handle.read(1):
                    handle.seek(0)
                    handle.write(b"\0")
                    handle.flush()
            except PermissionError:
                # Windows denies even *reading* another process's locked byte.
                yield False
                return
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    yield False
                    return
                try:
                    yield True
                finally:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    yield False
                    return
                try:
                    yield True
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    return hold()


def run_service(store: Any, stop: threading.Event, *, interval_s: float = 30.0,
                read_signals: Any = local_signals) -> str:
    """Independent autostart loop, no Claude timers; same tick may run in controller.

    A unique interprocess lease prevents two autostart copies. The SQLite
    journal provides deduplication against an in-controller copy as well.
    """
    if interval_s <= 0:
        raise ValueError("interval_s must be positive")
    raw = str(getattr(store, "path", ""))
    if not raw or raw == ":memory:":
        raise ValueError("service requires persistent registry")
    with _lease(Path(raw).with_name("watchdog.lock")) as leader:
        if not leader:
            return "already_running"
        while not stop.is_set():
            try:
                now = datetime.now(timezone.utc)
                watchdog_tick(store, now=now, signals=read_signals(store, now=now))
                # The controller also calls watchdog_tick. Only a completed
                # cycle *under the daemon lease* proves the independent
                # autostart worker is alive; process/cmdline and controller
                # events must not satisfy its future freshness check.
                with store.tx() as tx:
                    tx.record_event(kind="watchdog_daemon_tick", project=PROJECT,
                                    ts=now.isoformat(), payload={"source": "run_service"})
            except Exception as exc:
                # A malformed sensor must not permanently kill a boot watchdog.
                try:
                    with store.tx() as tx:
                        tx.record_event(kind="watchdog_tick_error", project=PROJECT,
                                        payload={"error": type(exc).__name__})
                except Exception:
                    pass  # next tick retries, never bypass SQLite permissions
            stop.wait(interval_s)
    return "stopped"


def main() -> None:
    from .store import Store

    # Derive the registry from the *installed package*, not an arbitrary
    # project manifest, shell argument or agent-supplied executable.
    root = Path(__file__).resolve().parents[3]
    db = root / "data" / "fleet" / "fleet.sqlite"
    if not db.is_file():
        raise SystemExit("no installed fleet registry")
    store = Store.open(str(db))
    if not store.healthy():
        raise SystemExit("fleet registry unhealthy")
    try:
        run_service(store, threading.Event())
    finally:
        store.close()


if __name__ == "__main__":
    main()
