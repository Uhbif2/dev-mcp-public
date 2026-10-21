"""Finite, evidence-based progress ladder for outcome-driven fleet work.

The controller owns issuance; reports alone are never progress. This module does
not hook legacy fleet_take (the controller integration is TZ-03).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from . import outcome
from .store import Store

STEPS = ("retry", "change_method", "alternative", "cross_model", "owner")
TRANSIENT = frozenset({"apply_failed_transient", "resource_down"})
_NOTE_KEYS = frozenset({"note", "reason", "text", "message", "description"})
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS progress_methods (
        outcome_id TEXT PRIMARY KEY REFERENCES outcomes(id) ON DELETE CASCADE,
        method_id TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS progress_attempts (
        outcome_id TEXT NOT NULL REFERENCES outcomes(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL,
        basis TEXT NOT NULL,
        step TEXT NOT NULL,
        successful INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (outcome_id, fingerprint, step),
        UNIQUE (outcome_id, basis, step)
    )""",
    """CREATE TABLE IF NOT EXISTS progress_escalations (
        outcome_id TEXT PRIMARY KEY REFERENCES outcomes(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL,
        obstacle_id TEXT NOT NULL,
        platform_outcome_id TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS progress_change_events (
        obstacle_id TEXT NOT NULL REFERENCES obstacles(id) ON DELETE RESTRICT,
        event_id TEXT NOT NULL,
        source TEXT NOT NULL,
        cause_code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (obstacle_id, event_id)
    )""",
)


@dataclass(frozen=True)
class Snapshot:
    fingerprint: str
    basis: str
    method_id: str
    children: tuple[str, ...]
    obstacle_types: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Step:
    kind: Literal["retry", "change_method", "alternative", "cross_model", "owner", "wait", "done"]
    outcome_id: str
    fingerprint: str
    baseline: Snapshot
    params: tuple[tuple[str, str], ...] = ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def install_schema(store: Store) -> None:
    outcome.install_schema(store)
    if getattr(store, "_progress_schema_ready", False):
        return
    with store.tx() as tx:
        for ddl in _SCHEMA:
            tx.change(ddl, event="progress.schema")
    store._progress_schema_ready = True


def _snapshot(tx: Any, outcome_id: str) -> Snapshot:
    row = tx.one("SELECT id,rev,spec,state FROM outcomes WHERE id=?", (outcome_id,))
    if row is None:
        raise ValueError("outcome does not exist")
    spec = json.loads(row["spec"])
    method_row = tx.one("SELECT method_id FROM progress_methods WHERE outcome_id=?", (outcome_id,))
    method = method_row["method_id"] if method_row else str(spec.get("method_id") or "initial")
    evidence_rows = tx.query(
        "SELECT kind,ref,subject_version,env_version,verdict FROM evidence WHERE outcome_id=?",
        (outcome_id,),
    )
    evidence = sorted({(
        r["kind"], r["ref"], r["subject_version"], r["env_version"], r["verdict"]
    ) for r in evidence_rows})
    obstacles = tx.query(
        "SELECT type,cause_code,predicate,change_source FROM obstacles "
        "WHERE outcome_id=? AND state='open'", (outcome_id,),
    )
    # Structural state only. Prose and scheduling timestamps are deliberately absent.
    active = sorted({
        (r["type"], r["cause_code"], _canonical(json.loads(r["predicate"])),
         r["change_source"] or "")
        for r in obstacles
    })
    children = tuple(r["id"] for r in tx.query(
        "SELECT id FROM outcomes WHERE parent_id=? ORDER BY id", (outcome_id,),
    ))
    source_facts = tuple((r["source"], r["event_id"]) for r in tx.query(
        "SELECT e.source,e.event_id FROM progress_change_events e "
        "JOIN obstacles b ON b.id=e.obstacle_id "
        "WHERE b.outcome_id=? ORDER BY e.source,e.event_id", (outcome_id,),
    ))
    requirement = (row["rev"], spec.get("requirements_version", spec.get("version")))
    # A finite ladder per verifiable *basis*. Merely swapping method names,
    # creating children, or shuffling obstacle types cannot reset its budget.
    # A genuinely new verifier proof, requirement or source event can.
    basis = hashlib.sha256(_canonical({
        "requirements": requirement, "evidence": evidence, "events": source_facts,
    }).encode("utf-8")).hexdigest()
    material = {
        "requirements": requirement,
        "evidence": evidence,
        "obstacles": active,
        "method_id": method,
        "children": children,
        "source_facts": source_facts,
    }
    digest = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
    obstacle_types = tuple(sorted({(r["type"], r["cause_code"]) for r in obstacles}))
    return Snapshot(digest, basis, method, children, obstacle_types)


def fingerprint(store: Store, outcome_id: str) -> str:
    install_schema(store)
    with store.read() as tx:
        return _snapshot(tx, outcome_id).fingerprint


def _recorded(tx: Any, outcome_id: str, basis: str) -> set[str]:
    return {r["step"] for r in tx.query(
        "SELECT step FROM progress_attempts WHERE outcome_id=? AND basis=?",
        (outcome_id, basis),
    )}


def _actionable(raw: str) -> bool:
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(obj, dict) and bool(set(obj) - _NOTE_KEYS)


def _waits_on_self(row: Any, outcome_id: str) -> bool:
    """The predicate is authoritative even if change_source names another task.

    A misleading nonempty source must never turn
    needs_result(requires_outcome=self) into a wait. TZ-186 had exactly this
    self-dependency masked by an external-looking source.
    """
    if row["type"] != "needs_result":
        return False
    if row["change_source"] == outcome_id:
        return True
    try:
        predicate = json.loads(row["predicate"])
    except (TypeError, ValueError):
        return False
    return isinstance(predicate, dict) and predicate.get("requires_outcome") == outcome_id


def _step(kind: str, oid: str, before: Snapshot, **params: str) -> Step:
    return Step(kind, oid, before.fingerprint, before, tuple(sorted(params.items())))


def decide(store: Store, outcome_id: str, *, gemini_available: bool = False) -> Step:
    """Choose an unused rung. A decision is pure: record_attempt consumes it.

    An owner escalation latches even if writing its obstacle changes the hash.
    No scheduler timestamps or unrelated task completions can unlock it.
    """
    install_schema(store)
    with store.read() as tx:
        item = tx.one("SELECT state,spec FROM outcomes WHERE id=?", (outcome_id,))
        before = _snapshot(tx, outcome_id)
        if item["state"] != "open":
            return _step("done", outcome_id, before)
        escalation = tx.one(
            "SELECT obstacle_id,platform_outcome_id FROM progress_escalations WHERE outcome_id=?",
            (outcome_id,),
        )
        if escalation:
            return _step("owner", outcome_id, before,
                         obstacle_id=escalation["obstacle_id"],
                         platform_outcome_id=escalation["platform_outcome_id"])
        rows = tx.query(
            "SELECT id,type,cause_code,predicate,change_source FROM obstacles "
            "WHERE outcome_id=? AND state='open' ORDER BY id", (outcome_id,),
        )
        used = _recorded(tx, outcome_id, before.basis)
        spec = json.loads(item["spec"])
        transient = any(r["cause_code"] == "apply_failed_transient" for r in rows)
        recovered = bool(tx.scalar(
            "SELECT COUNT(*) FROM progress_change_events e JOIN obstacles b "
            "ON b.id=e.obstacle_id WHERE b.outcome_id=? AND e.cause_code='resource_down'",
            (outcome_id,),
        ))
    # Invariant diagnosis runs outside the read snapshot to avoid nested transactions.
    illegal_wait = {v.outcome_id for v in outcome.check_invariant(store)
                    if v.code in {"self_wait", "wait_cycle", "prose_control"}}
    if any(row["type"] == "owner_action" for row in rows):
        owner = next(row for row in rows if row["type"] == "owner_action")
        return _step("owner", outcome_id, before, obstacle_id=owner["id"])
    if "retry" not in used and (transient or recovered):
        return _step("retry", outcome_id, before)
    if outcome_id not in illegal_wait and not any(
        _waits_on_self(row, outcome_id) for row in rows
    ):
        for row in rows:
            if row["change_source"] and _actionable(row["predicate"]):
                return _step("wait", outcome_id, before,
                             obstacle_id=row["id"], change_source=row["change_source"])
    if "change_method" not in used:
        return _step("change_method", outcome_id, before)
    if "alternative" not in used and (
        spec.get("alternatives") or spec.get("decomposition_available")
    ):
        return _step("alternative", outcome_id, before)
    if "cross_model" not in used and gemini_available:
        return _step("cross_model", outcome_id, before, model="gemini")
    return _step("owner", outcome_id, before)


def accepts_change_method(before: Snapshot, after: Snapshot) -> bool:
    """Only a new method, child subtree, or obstacle *type* counts."""
    return (
        before.method_id != after.method_id
        or before.children != after.children
        or {kind for kind, _ in before.obstacle_types}
           != {kind for kind, _ in after.obstacle_types}
    )


def record_attempt(
    store: Store, outcome_id: str, step: Step, *,
    successful: bool = False, method_id: str | None = None,
    owner_action: str = "Нужен новый проверяемый способ достижения результата",
) -> Step:
    """Consume exactly one rung under a SQLite write lock.

    Reject stale/mismatched decisions and duplicate attempts; a failed attempt
    still consumes its rung. Successful change_method must be structural.
    """
    if step.outcome_id != outcome_id or step.kind not in STEPS:
        raise ValueError("invalid or non-work step")
    if step.kind == "owner" and dict(step.params).get("obstacle_id"):
        raise ValueError("existing owner action is already waiting; do not create another")
    if len(step.fingerprint) != 64 or step.baseline.fingerprint != step.fingerprint:
        raise ValueError("invalid baseline")
    if method_id is not None and (step.kind != "change_method" or not successful):
        raise ValueError("method_id only allowed for successful change_method")
    if method_id is not None and (not isinstance(method_id, str) or not method_id.strip()):
        raise ValueError("method_id must be a non-empty string")
    install_schema(store)
    with store.tx() as tx:
        current = _snapshot(tx, outcome_id)
        if current.fingerprint != step.fingerprint:
            # Evidence/subtree/obstacle changes can be a real result of the
            # issued work, but a fresh issuance must use the new fingerprint.
            if not successful:
                raise ValueError("stale attempt; reevaluate changed outcome")
        if tx.one("SELECT state FROM outcomes WHERE id=?", (outcome_id,))["state"] != "open":
            raise ValueError("cannot attempt a closed outcome")
        if tx.one(
            "SELECT 1 FROM progress_escalations WHERE outcome_id=?", (outcome_id,),
        ):
            raise ValueError("owner escalation is latched")
        if method_id is not None:
            tx.change(
                "INSERT INTO progress_methods(outcome_id,method_id) VALUES (?,?) "
                "ON CONFLICT(outcome_id) DO UPDATE SET method_id=excluded.method_id",
                (outcome_id, method_id.strip()),
                event="progress.method",
            )
        after = _snapshot(tx, outcome_id)
        if step.kind == "change_method" and successful and not accepts_change_method(step.baseline, after):
            raise ValueError("change_method requires a new method_id, subtree, or obstacle type")
        tx.change(
            "INSERT INTO progress_attempts(outcome_id,fingerprint,basis,step,successful,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (outcome_id, step.fingerprint, step.baseline.basis,
             step.kind, int(successful and after.fingerprint != step.fingerprint), _now()),
            event={"kind": "progress.attempt", "payload": {
                "outcome": outcome_id, "fingerprint": step.fingerprint, "step": step.kind}},
        )
        if step.kind == "owner":
            # The final rung is irreversible on this basis until a verified owner
            # event. Exactly one capability-gap outcome is created transactionally.
            obstacle_id, gap_id = uuid.uuid4().hex, uuid.uuid4().hex
            if not owner_action.strip():
                raise ValueError("owner_action must be a concrete phrase")
            tx.change(
                "INSERT INTO obstacles(id,outcome_id,type,cause_code,predicate,"
                "change_source,resolver_work,owner_action,state,ts) "
                "VALUES (?,?,'owner_action','procedure_unknown',?,'owner',NULL,?,'open',?)",
                (obstacle_id, outcome_id, _canonical({"owner_event": True}),
                 owner_action.strip(), _now()), event="progress.owner_obstacle",
            )
            tx.change(
                "INSERT INTO outcomes(id,parent_id,project,title,intent_id,spec,evidence_spec,"
                "authority,state,rev,created_ts,closed_ts) "
                "VALUES (?,NULL,'Fleet Platform',?,?,?,?,?,'open',1,?,NULL)",
                (gap_id, "Не хватило способности", f"capability-gap:{outcome_id}",
                 _canonical({"source_outcome": outcome_id,
                             "reason": "finite progress ladder exhausted"}),
                 _canonical({"required": "new verified method"}),
                 _canonical({"origin": "progress.owner"}), _now()),
                event="progress.capability_gap",
            )
            tx.change(
                "INSERT INTO progress_escalations(outcome_id,fingerprint,obstacle_id,platform_outcome_id) "
                "VALUES (?,?,?,?)",
                (outcome_id, step.fingerprint, obstacle_id, gap_id),
                event="progress.owner_latch",
            )
    return decide(store, outcome_id)


def signal_change(
    store: Store, *, obstacle_id: str, source: str, event_id: str, predicate_true: bool,
) -> bool:
    """Resolve only a matching *new* source event AND verified predicate.

    The caller is a trusted event verifier, never a prose report parser. In
    particular, unrelated accepted work, mention of an ID, or time are ignored.
    """
    if not source or not event_id or not predicate_true:
        return False
    install_schema(store)
    with store.tx() as tx:
        row = tx.one(
            "SELECT outcome_id,state,change_source,cause_code,predicate "
            "FROM obstacles WHERE id=?", (obstacle_id,),
        )
        if (row is None or row["state"] != "open"
                or row["change_source"] != source or not _actionable(row["predicate"])):
            return False
        tx.change(
            "INSERT OR IGNORE INTO progress_change_events"
            "(obstacle_id,event_id,source,cause_code,created_at) VALUES (?,?,?,?,?)",
            (obstacle_id, event_id, source, row["cause_code"], _now()),
            event="progress.change_signal",
        )
        tx.change(
            "UPDATE obstacles SET state='resolved' WHERE id=? AND state='open'",
            (obstacle_id,), event="progress.unfreeze",
        )
        if source == "owner":
            tx.change(
                "DELETE FROM progress_escalations WHERE outcome_id=? AND obstacle_id=?",
                (row["outcome_id"], obstacle_id), event="progress.owner_changed",
            )
    return True
