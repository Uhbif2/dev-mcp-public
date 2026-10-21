"""Scoped decision provenance and fail-closed owner-question resolver.

No direct access to memory_service or Obsidian: a Jev/Gemini broker supplies typed,
access-filtered observations. This is a library, not an agent-facing tool.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Sequence

from dev_mcp.fleet.outcome import install_schema as install_outcomes
from dev_mcp.fleet.store import Store

OWNER_SOURCES = frozenset({"owner_tz", "owner_reply", "owner_intent", "decision_file"})
MEMORY_SOURCES = frozenset({"memory_service_rule", "memory_service_organic", "obsidian"})
BROKERS = frozenset({"jev", "gemini"})
_KEY = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.:/#-]{0,255}$")


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError(f"{name} must be an opaque identifier (not source content)")
    return value


def _when(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(value: str) -> str:
    # ntpath.isabs(r"\root") is True on some Windows/Python releases,
    # but that path is relative to the process's current drive. Only a
    # drive-qualified absolute path or a complete UNC root is unambiguous.
    if not isinstance(value, str):
        raise ValueError("scope requires an absolute root, not an ambiguous project name")
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith(("\\", "/")) or not ntpath.isabs(value):
        raise ValueError("scope requires an absolute root, not an ambiguous project name")
    result = ntpath.normcase(ntpath.normpath(value))
    if not result or result.endswith(":"):
        raise ValueError("invalid root")
    return result


@dataclass(frozen=True)
class Scope:
    root: str
    project: str = "*"
    context: str = "*"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _root(self.root))
        for field in ("project", "context"):
            value = getattr(self, field)
            if value != "*":
                _identifier(value, field)

    def matches(self, other: "Scope") -> bool:
        return (self.root == other.root and
                (self.project == "*" or self.project == other.project) and
                (self.context == "*" or self.context == other.context))

    @property
    def specificity(self) -> int:
        return int(self.project != "*") + int(self.context != "*")


@dataclass(frozen=True)
class SourceRef:
    kind: str
    ref: str
    version: str

    def __post_init__(self) -> None:
        _identifier(self.ref, "source ref")
        _identifier(self.version, "source version")


@dataclass(frozen=True)
class Decision:
    id: str
    key: str
    choice: str
    scope: Scope
    source: SourceRef
    evidence: tuple[SourceRef, ...]
    reversible: bool
    rollback: str | None
    fresh_until: str
    revoked: bool
    derived: bool


@dataclass(frozen=True)
class KnowledgeHit:
    """Broker output. 'choice' is data only, never executable instruction."""
    key: str
    choice: str
    scope: Scope
    source: SourceRef
    confidence: float
    broker: str
    access_granted: bool
    observed_until: str
    revoked: bool = False


@dataclass(frozen=True)
class Question:
    key: str
    scope: Scope
    choices: tuple[str, ...]
    reversible: bool
    rollback: str | None
    owner_only: bool = False
    private_access: bool = False
    policy_change: bool = False

    def __post_init__(self) -> None:
        _identifier(self.key, "question key")
        if not self.choices or any(not isinstance(x, str) or not x or len(x) > 1024 for x in self.choices):
            raise ValueError("non-empty bounded choices are required")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("duplicate choices")


@dataclass(frozen=True)
class Resolution:
    status: Literal["decided", "owner_action", "capability_gap"]
    choice: str | None
    decision_id: str | None
    source: SourceRef | None
    escalation_id: str | None
    cause: str


class BrokerUnavailable(ConnectionError):
    """A broker's failed health/access probe with an addressable, versioned receipt."""

    def __init__(self, probe: SourceRef):
        if probe.kind != "connector_probe":
            raise ValueError("broker outage requires connector_probe evidence")
        self.probe = probe
        super().__init__("mediated knowledge provider unavailable")


def _ref_dict(source: SourceRef) -> dict[str, str]:
    return {"kind": source.kind, "ref": source.ref, "version": source.version}


def _ref(data: dict[str, str]) -> SourceRef:
    return SourceRef(**data)


def _safe_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("metadata exceeds allowed limit")
    return encoded


class DecisionBook:
    """Adds versioned metadata beside TZ-02's common decisions table."""

    def __init__(self, store: Store):
        self.store = store
        install_outcomes(store)
        with store.tx() as tx:
            tx.change("""CREATE TABLE IF NOT EXISTS decision_provenance (
                id TEXT PRIMARY KEY REFERENCES decisions(id),
                decision_key TEXT NOT NULL,
                root TEXT NOT NULL,
                project TEXT NOT NULL,
                context TEXT NOT NULL,
                source_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                fresh_until TEXT NOT NULL,
                derived INTEGER NOT NULL CHECK (derived IN (0,1)),
                revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0,1))
            )""", event="owner.schema")
            tx.change("""CREATE INDEX IF NOT EXISTS decision_lookup
                ON decision_provenance(decision_key,root,revoked)""", event="owner.schema")
            tx.change("""CREATE TABLE IF NOT EXISTS decision_escalations (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('owner_action','capability_gap')),
                cause TEXT NOT NULL,
                decision_key TEXT NOT NULL,
                root TEXT NOT NULL,
                project TEXT NOT NULL,
                context TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                ts TEXT NOT NULL
            )""", event="owner.schema")

    def record(
        self, *, key: str, choice: str, scope: Scope, source: SourceRef,
        evidence: Sequence[SourceRef], reversible: bool, rollback: str | None,
        fresh_until: str, derived: bool = False,
    ) -> Decision:
        _identifier(key, "decision key")
        if not isinstance(choice, str) or not choice or len(choice) > 1024:
            raise ValueError("invalid choice")
        if source.kind not in (MEMORY_SOURCES | {"previous_choice"} if derived else OWNER_SOURCES):
            raise ValueError("wrong authority: owner and mediated observations are distinct")
        if not evidence or any(not isinstance(item, SourceRef) for item in evidence):
            raise ValueError("versioned evidence required")
        if derived and (not reversible or not rollback or not rollback.strip()):
            raise ValueError("derived decisions need a reversible rollback")
        if reversible and (not isinstance(rollback, str) or not rollback.strip()):
            raise ValueError("reversible decisions require a rollback recipe")
        if rollback is not None and len(rollback) > 2048:
            raise ValueError("rollback recipe too long")
        if _when(fresh_until) <= _when(_now()):
            raise ValueError("fresh_until is already expired")
        item_id = uuid.uuid4().hex
        src = _safe_json(_ref_dict(source))
        ev = _safe_json([_ref_dict(x) for x in evidence])
        scope_json = _safe_json({"root": scope.root, "project": scope.project, "context": scope.context})
        with self.store.tx() as tx:
            tx.change(
                "INSERT INTO decisions(id,scope,text,source,reversible,rollback,ts) VALUES (?,?,?,?,?,?,?)",
                (item_id, scope_json, choice, source.kind, int(reversible), rollback, _now()),
                event={"kind": "owner.decision", "payload": {"id": item_id}},
            )
            tx.change(
                """INSERT INTO decision_provenance
                (id,decision_key,root,project,context,source_json,evidence_json,fresh_until,derived)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (item_id, key, scope.root, scope.project, scope.context, src, ev, fresh_until, int(derived)),
                event="owner.provenance",
            )
        return self.get(item_id)

    @staticmethod
    def _decode(row: object) -> Decision:
        return Decision(
            id=row["id"], key=row["decision_key"], choice=row["text"],
            scope=Scope(row["root"], row["project"], row["context"]),
            source=_ref(json.loads(row["source_json"])),
            evidence=tuple(_ref(x) for x in json.loads(row["evidence_json"])),
            reversible=bool(row["reversible"]), rollback=row["rollback"],
            fresh_until=row["fresh_until"], revoked=bool(row["revoked"]),
            derived=bool(row["derived"]),
        )

    def get(self, decision_id: str) -> Decision:
        with self.store.read() as tx:
            row = tx.one("""SELECT d.*,p.* FROM decisions d
                JOIN decision_provenance p ON p.id=d.id WHERE d.id=?""", (_identifier(decision_id, "decision id"),))
        if row is None:
            raise KeyError(decision_id)
        return self._decode(row)

    def revoke(self, decision_id: str) -> None:
        with self.store.tx() as tx:
            if tx.one("SELECT id FROM decision_provenance WHERE id=?", (decision_id,)) is None:
                raise KeyError(decision_id)
            tx.change("UPDATE decision_provenance SET revoked=1 WHERE id=?", (decision_id,),
                      event={"kind": "owner.revoke", "payload": {"id": decision_id}})

    def applicable(
        self, key: str, scope: Scope, *, now: str | None = None,
        verify_version: Callable[[SourceRef], bool] | None = None,
    ) -> list[Decision]:
        stamp = _when(now or _now())
        with self.store.read() as tx:
            rows = tx.query("""SELECT d.*,p.* FROM decisions d
                JOIN decision_provenance p ON p.id=d.id
                WHERE p.decision_key=? AND p.root=? AND p.revoked=0""",
                (_identifier(key, "decision key"), scope.root))
        found = []
        for row in rows:
            decision = self._decode(row)
            if not decision.scope.matches(scope) or _when(decision.fresh_until) <= stamp:
                continue
            # Owner TЗ/intents/decision files can be superseded just like
            # derived knowledge. Immutable owner replies need only the book's
            # explicit revocation/freshness checks when no verifier is present.
            refs = (decision.source, *decision.evidence)
            if verify_version is None and (
                decision.derived or any(ref.kind != "owner_reply" for ref in refs)
            ):
                continue
            if verify_version is not None:
                try:
                    if not all(verify_version(ref) is True for ref in refs):
                        continue
                except Exception:
                    # A failing freshness probe is never consent or a decision.
                    continue
            found.append(decision)
        found.sort(key=lambda d: d.scope.specificity, reverse=True)
        return found

    def escalate(self, *, kind: str, cause: str, key: str, scope: Scope,
                 evidence: Sequence[SourceRef] = ()) -> str:
        if kind not in {"owner_action", "capability_gap"}:
            raise ValueError("invalid escalation kind")
        _identifier(cause, "cause")
        _identifier(key, "decision key")
        digest = hashlib.sha256(_safe_json([
            kind, cause, key, scope.root, scope.project, scope.context,
            [_ref_dict(x) for x in evidence],
        ]).encode()).hexdigest()
        with self.store.tx() as tx:
            tx.change("""INSERT OR IGNORE INTO decision_escalations
                (id,kind,cause,decision_key,root,project,context,evidence_json,ts)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (digest, kind, cause, key, scope.root, scope.project, scope.context,
                 _safe_json([_ref_dict(x) for x in evidence]), _now()),
                event={"kind": "owner.escalation", "payload": {"id": digest, "kind": kind, "cause": cause}},
            )
        return digest


def resolve(
    book: DecisionBook, question: Question, *,
    broker: Callable[[Question], Sequence[KnowledgeHit]] | None = None,
    previous_choices: Callable[[Question], Sequence[KnowledgeHit]] | None = None,
    verify_version: Callable[[SourceRef], bool] | None = None,
    now: str | None = None,
    minimum_confidence: float = 0.85,
) -> Resolution:
    """Ordered authority: owner decision -> mediated memory -> previous choices.

    Every selected value must be an already-authorized option. Never parse prose
    from notes as instructions, access grants, executable commands or consent.
    """
    now = now or _now()

    def fail(kind: str, cause: str, refs: Sequence[SourceRef] = ()) -> Resolution:
        if not refs:
            query_id = hashlib.sha256(_safe_json([
                question.key, question.scope.root, question.scope.project,
                question.scope.context, question.choices,
            ]).encode()).hexdigest()
            refs = (SourceRef("decision_query", f"request:{query_id}", "schema-v1"),)
        action_id = book.escalate(kind=kind, cause=cause, key=question.key,
                                  scope=question.scope, evidence=refs)
        return Resolution(kind, None, None, None, action_id, cause)

    if question.owner_only or question.private_access or question.policy_change:
        return fail("owner_action", "owner_authority_required")
    if not question.reversible or not question.rollback or not question.rollback.strip():
        return fail("owner_action", "rollback_required")

    existing = [x for x in book.applicable(question.key, question.scope, now=now,
                  verify_version=verify_version) if x.choice in question.choices and x.reversible]
    if existing:
        top = [d for d in existing if d.scope.specificity == existing[0].scope.specificity]
        if len({d.choice for d in top}) > 1:
            return fail("owner_action", "conflicting_decisions", [d.source for d in top])
        chosen = top[0]
        return Resolution("decided", chosen.choice, chosen.id, chosen.source, None, "owner_decision")

    unavailable = broker is None
    failed_probes: list[SourceRef] = []
    for provider, stage in ((broker, "memory"), (previous_choices, "previous")):
        if provider is None:
            continue
        try:
            incoming = provider(question)
        except BrokerUnavailable as exc:
            failed_probes.append(exc.probe)
            if stage == "memory":
                unavailable = True
            continue
        except (OSError, ConnectionError, TimeoutError) as exc:
            # A local, versioned error receipt is journaled without any exception
            # message (which could contain credentials or private note content).
            probe_id = hashlib.sha256(_safe_json([
                stage, question.key, question.scope.root,
                question.scope.project, question.scope.context,
                type(exc).__name__,
            ]).encode()).hexdigest()
            probe = SourceRef("broker_attempt", f"attempt:{probe_id}", "failure-v1")
            with book.store.tx() as tx:
                tx.record_event(kind="owner.broker_failure", payload={
                    "probe": probe.ref, "kind": type(exc).__name__, "stage": stage,
                })
            failed_probes.append(probe)
            if stage == "memory":
                unavailable = True
            continue
        candidates = []
        for hit in incoming:
            if not isinstance(hit, KnowledgeHit) or hit.key != question.key:
                continue
            if hit.source.kind not in (MEMORY_SOURCES if stage == "memory" else {"previous_choice"}):
                continue
            if not hit.scope.matches(question.scope) or hit.choice not in question.choices:
                continue
            if ((stage == "memory" and hit.broker not in BROKERS)
                    or (stage == "previous" and hit.broker != "local")
                    or hit.access_granted is not True or hit.revoked
                    or not 0 <= hit.confidence <= 1 or hit.confidence < minimum_confidence):
                continue
            try:
                fresh = _when(hit.observed_until) > _when(now)
            except ValueError:
                # External broker data cannot break owner-question resolution.
                continue
            if not fresh:
                continue
            if verify_version is None or not verify_version(hit.source):
                continue
            candidates.append(hit)
        if candidates:
            max_specificity = max(x.scope.specificity for x in candidates)
            top = [x for x in candidates if x.scope.specificity == max_specificity]
            if len({x.choice for x in top}) > 1:
                return fail("owner_action", "conflicting_evidence", [x.source for x in top])
            chosen = top[0]
            decision = book.record(
                key=question.key, choice=chosen.choice, scope=question.scope,
                source=chosen.source, evidence=(chosen.source,), derived=True,
                reversible=True, rollback=question.rollback,
                fresh_until=chosen.observed_until,
            )
            return Resolution("decided", chosen.choice, decision.id, chosen.source, None, stage)
    return fail("capability_gap" if unavailable else "owner_action",
                "knowledge_source_unavailable" if unavailable else "no_sufficient_evidence",
                failed_probes)
