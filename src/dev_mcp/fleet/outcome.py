"""Versioned, additive outcome contract on the existing fleet Store.

This module does not alter the legacy queue or Store schema version. Its own
single-step migration runs inside Store.tx(), in the SAME SQLite database.
The controller and scheduling policy are deliberately outside this module.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .store import Store


OUTCOME_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 65536
STATES = frozenset({"open", "done", "dropped"})
EVIDENCE_KINDS = frozenset({"run", "measure", "screen", "delivery", "review", "external"})
OBSTACLE_TYPES = frozenset({"external_fact", "needs_result", "unknown_method", "defect", "owner_action"})
CAUSE_CODES = frozenset({
    "path_forbidden", "resource_down", "gate_red_foreign", "gate_red_own",
    "needs_file_outside_owns", "acceptance_impossible", "procedure_unknown",
    "apply_failed_schema", "apply_failed_transient", "producer_failed",
    "owner_physical", "owner_taste", "owner_money", "owner_private_data",
})

_DDL = (
    """CREATE TABLE outcomes (
        id TEXT PRIMARY KEY,
        parent_id TEXT REFERENCES outcomes(id) ON DELETE RESTRICT,
        project TEXT NOT NULL,
        title TEXT NOT NULL,
        intent_id TEXT NOT NULL,
        spec TEXT NOT NULL,
        evidence_spec TEXT NOT NULL,
        authority TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('open','done','dropped')),
        rev INTEGER NOT NULL CHECK (rev >= 1),
        created_ts TEXT NOT NULL,
        closed_ts TEXT
    )""",
    "CREATE INDEX outcomes_parent ON outcomes(parent_id)",
    """CREATE TABLE evidence (
        id TEXT PRIMARY KEY,
        outcome_id TEXT NOT NULL REFERENCES outcomes(id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK (kind IN ('run','measure','screen','delivery','review','external')),
        ref TEXT NOT NULL,
        subject_version TEXT NOT NULL CHECK (length(subject_version) > 0),
        env_version TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('pass','fail')),
        producer TEXT NOT NULL,
        ts TEXT NOT NULL
    )""",
    "CREATE INDEX evidence_outcome ON evidence(outcome_id)",
    """CREATE TABLE obstacles (
        id TEXT PRIMARY KEY,
        outcome_id TEXT NOT NULL REFERENCES outcomes(id) ON DELETE RESTRICT,
        type TEXT NOT NULL CHECK (type IN ('external_fact','needs_result','unknown_method','defect','owner_action')),
        cause_code TEXT NOT NULL,
        predicate TEXT NOT NULL,
        change_source TEXT,
        resolver_work TEXT,
        owner_action TEXT,
        state TEXT NOT NULL CHECK (state IN ('open','resolved')),
        ts TEXT NOT NULL
    )""",
    "CREATE INDEX obstacles_outcome ON obstacles(outcome_id, state)",
    """CREATE TABLE decisions (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        text TEXT NOT NULL,
        source TEXT NOT NULL,
        reversible INTEGER NOT NULL CHECK (reversible IN (0,1)),
        rollback TEXT,
        ts TEXT NOT NULL
    )""",
    """CREATE TABLE outcome_work (
        outcome_id TEXT NOT NULL REFERENCES outcomes(id) ON DELETE CASCADE,
        project TEXT NOT NULL,
        tz TEXT NOT NULL,
        PRIMARY KEY(outcome_id, project, tz)
    )""",
)


@dataclass(frozen=True)
class Outcome:
    id: str
    parent_id: str | None
    project: str
    title: str
    intent_id: str
    spec: dict[str, Any]
    evidence_spec: dict[str, Any]
    authority: dict[str, Any]
    state: str
    rev: int
    created_ts: str
    closed_ts: str | None


@dataclass(frozen=True)
class NextStep:
    kind: Literal["work", "wait", "owner", "done"]
    id: str | None = None


@dataclass(frozen=True)
class Violation:
    code: Literal["no_next_step", "self_wait", "wait_cycle", "prose_control"]
    outcome_id: str
    detail: str


class ServerEvidenceWriter:
    """Capability possessed by server-side verifiers, never supplied by agent reports.

    Python module access is not a security sandbox. The enforcement boundary is
    that MCP/agent report tools must NOT expose server_evidence_writer or this
    writer; trusted server code verifies an artifact before issuing a write.
    """

    def __init__(self, store: Store, *, _server_capability: object | None = None):
        if _server_capability is not _SERVER_CAPABILITY:
            raise PermissionError("only a trusted server verifier can issue evidence")
        self.store = store


_SERVER_CAPABILITY = object()


def server_evidence_writer(store: Store) -> ServerEvidenceWriter:
    """Server-only construction path; NEVER bind this as an agent-facing tool."""
    return ServerEvidenceWriter(store, _server_capability=_SERVER_CAPABILITY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def _text(value: str, field: str, *, max_size: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_size:
        raise ValueError(f"{field} must be a non-empty string of at most {max_size} characters")
    return value.strip()


def _json(value: dict[str, Any], field: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, dict) or (nonempty and not value):
        raise ValueError(f"{field} must be a {'non-empty ' if nonempty else ''}JSON object")
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError(f"{field} exceeds {MAX_JSON_BYTES} bytes")
    except (OverflowError, RecursionError, TypeError) as exc:
        raise ValueError(f"{field} is not safe JSON") from exc
    return serialized


def _outcome(row: Any) -> Outcome:
    return Outcome(
        id=row["id"], parent_id=row["parent_id"], project=row["project"],
        title=row["title"], intent_id=row["intent_id"],
        spec=json.loads(row["spec"]), evidence_spec=json.loads(row["evidence_spec"]),
        authority=json.loads(row["authority"]), state=row["state"], rev=row["rev"],
        created_ts=row["created_ts"], closed_ts=row["closed_ts"],
    )


def install_schema(store: Store) -> None:
    """Idempotent outcome schema migration via Store.tx; old tables stay untouched."""
    if getattr(store, "_outcome_schema_ready", False):
        return
    with store.tx() as tx:
        tx.change(
            "CREATE TABLE IF NOT EXISTS outcome_schema_version (version INTEGER PRIMARY KEY, applied_ts TEXT NOT NULL)",
            event="outcome.schema",
        )
        version = int(tx.scalar("SELECT COALESCE(MAX(version),0) FROM outcome_schema_version"))
        if version > OUTCOME_SCHEMA_VERSION:
            raise ValueError(f"future outcome schema version: {version}")
        if version < 1:
            tx.changes(((statement, ()) for statement in _DDL), event="outcome.migration")
            tx.change(
                "INSERT INTO outcome_schema_version(version, applied_ts) VALUES (?,?)",
                (1, _now()), event="outcome.migration",
            )
        required = {"outcomes", "evidence", "obstacles", "decisions", "outcome_work"}
        names = {r["name"] for r in tx.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('outcomes','evidence','obstacles','decisions','outcome_work')"
        )}
        if names != required:
            raise ValueError("incomplete outcome migration")
    store._outcome_schema_ready = True


def get_outcome(store: Store, outcome_id: str) -> Outcome:
    install_schema(store)
    with store.read() as tx:
        row = tx.one("SELECT * FROM outcomes WHERE id=?", (_text(outcome_id, "outcome_id"),))
    if row is None:
        raise ValueError("outcome does not exist")
    return _outcome(row)


def create_outcome(
    store: Store, *, project: str, title: str, intent_id: str,
    spec: dict[str, Any], evidence_spec: dict[str, Any], authority: dict[str, Any],
    parent_id: str | None = None, outcome_id: str | None = None,
) -> Outcome:
    project = _text(project, "project", max_size=256)
    title = _text(title, "title", max_size=1024)
    intent_id = _text(intent_id, "intent_id", max_size=256)
    spec_json = _json(spec, "spec")
    evidence_json = _json(evidence_spec, "evidence_spec")
    authority_json = _json(authority, "authority")
    identifier = _text(outcome_id, "outcome_id") if outcome_id is not None else _id()
    install_schema(store)
    with store.tx() as tx:
        if parent_id is not None:
            parent = tx.one("SELECT project,state FROM outcomes WHERE id=?", (parent_id,))
            if parent is None or parent["project"] != project or parent["state"] != "open":
                raise ValueError("parent must exist, be open, and belong to the same project")
        tx.change(
            """INSERT INTO outcomes
            (id,parent_id,project,title,intent_id,spec,evidence_spec,authority,state,rev,created_ts,closed_ts)
            VALUES (?,?,?,?,?,?,?,?,'open',1,?,NULL)""",
            (identifier, parent_id, project, title, intent_id, spec_json, evidence_json, authority_json, _now()),
            event={"kind": "outcome.created", "project": project, "payload": {"id": identifier}},
        )
    return get_outcome(store, identifier)


def close_outcome(store: Store, outcome_id: str, *, state: Literal["done", "dropped"] = "done") -> Outcome:
    if state not in {"done", "dropped"}:
        raise ValueError("outcome may only be closed as done or dropped")
    install_schema(store)
    with store.tx() as tx:
        item = tx.one("SELECT state,project FROM outcomes WHERE id=?", (outcome_id,))
        if item is None or item["state"] != "open":
            raise ValueError("outcome must exist and be open")
        if tx.scalar("SELECT COUNT(*) FROM outcomes WHERE parent_id=? AND state='open'", (outcome_id,)):
            raise ValueError("cannot close an outcome with open children")
        tx.change(
            "UPDATE outcomes SET state=?,rev=rev+1,closed_ts=? WHERE id=?",
            (state, _now(), outcome_id),
            event={"kind": "outcome.closed", "project": item["project"], "payload": {"id": outcome_id, "state": state}},
        )
    return get_outcome(store, outcome_id)


def add_evidence(
    store: Store, *, writer: ServerEvidenceWriter, outcome_id: str,
    kind: str, ref: str, subject_version: str, env_version: str,
    verdict: str, producer: str, evidence_id: str | None = None,
) -> str:
    if type(writer) is not ServerEvidenceWriter or writer.store is not store:
        raise PermissionError("evidence must originate from this store's server verifier")
    if kind not in EVIDENCE_KINDS or verdict not in {"pass", "fail"}:
        raise ValueError("invalid evidence kind or verdict")
    ref, subject_version = _text(ref, "ref"), _text(subject_version, "subject_version")
    env_version, producer = _text(env_version, "env_version"), _text(producer, "producer")
    identifier = _text(evidence_id, "evidence_id") if evidence_id is not None else _id()
    install_schema(store)
    with store.tx() as tx:
        if tx.one("SELECT id FROM outcomes WHERE id=?", (outcome_id,)) is None:
            raise ValueError("outcome does not exist")
        tx.change(
            """INSERT INTO evidence
            (id,outcome_id,kind,ref,subject_version,env_version,verdict,producer,ts)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (identifier, outcome_id, kind, ref, subject_version, env_version, verdict, producer, _now()),
            event={"kind": "outcome.evidence", "payload": {"id": identifier, "outcome": outcome_id}},
        )
    return identifier


def open_obstacle(
    store: Store, *, outcome_id: str, type: str, cause_code: str,
    predicate: dict[str, Any], change_source: str | None = None,
    resolver_work: str | None = None, owner_action: str | None = None,
    obstacle_id: str | None = None,
) -> str:
    if type not in OBSTACLE_TYPES or cause_code not in CAUSE_CODES:
        raise ValueError("unknown obstacle type or cause_code")
    predicate_json = _json(predicate, "predicate", nonempty=False)
    if type == "owner_action":
        owner_action = _text(owner_action, "owner_action", max_size=1024)
    elif owner_action is not None:
        raise ValueError("owner_action text requires owner_action obstacle type")
    if change_source is not None:
        change_source = _text(change_source, "change_source")
    if resolver_work is not None:
        resolver_work = _text(resolver_work, "resolver_work")
    identifier = _text(obstacle_id, "obstacle_id") if obstacle_id is not None else _id()
    install_schema(store)
    with store.tx() as tx:
        if tx.one("SELECT id FROM outcomes WHERE id=?", (outcome_id,)) is None:
            raise ValueError("outcome does not exist")
        tx.change(
            """INSERT INTO obstacles
            (id,outcome_id,type,cause_code,predicate,change_source,resolver_work,owner_action,state,ts)
            VALUES (?,?,?,?,?,?,?,?,'open',?)""",
            (identifier, outcome_id, type, cause_code, predicate_json, change_source, resolver_work, owner_action, _now()),
            event={"kind": "outcome.obstacle_open", "payload": {"id": identifier, "outcome": outcome_id}},
        )
    return identifier


def resolve_obstacle(store: Store, obstacle_id: str) -> None:
    install_schema(store)
    with store.tx() as tx:
        row = tx.one("SELECT state FROM obstacles WHERE id=?", (obstacle_id,))
        if row is None or row["state"] != "open":
            raise ValueError("obstacle must exist and be open")
        tx.change("UPDATE obstacles SET state='resolved' WHERE id=?", (obstacle_id,),
                  event={"kind": "outcome.obstacle_resolved", "payload": {"id": obstacle_id}})


def link_work(store: Store, *, outcome_id: str, project: str, tz: str) -> None:
    project, tz = _text(project, "project"), _text(tz, "tz")
    install_schema(store)
    with store.tx() as tx:
        item = tx.one("SELECT project FROM outcomes WHERE id=?", (outcome_id,))
        if item is None or item["project"] != project:
            raise ValueError("work must belong to the outcome's project")
        tx.change(
            "INSERT OR IGNORE INTO outcome_work(outcome_id,project,tz) VALUES (?,?,?)",
            (outcome_id, project, tz),
            event={"kind": "outcome.work_linked", "project": project, "tz": tz},
        )


def _actionable_predicate(raw: str) -> bool:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and bool(value) and bool(
        set(value) - {"note", "reason", "text", "message", "description"}
    )


def _step_for(tx: Any, outcome_id: str) -> NextStep:
    children = tx.query(
        "SELECT id FROM outcomes WHERE parent_id=? AND state='open' ORDER BY created_ts,id",
        (outcome_id,),
    )
    if children:
        for child in children:
            step = _step_for(tx, child["id"])
            if step.kind != "done":
                return step
        return NextStep("done")
    ready = tx.one(
        """SELECT w.project,w.tz FROM outcome_work w
        JOIN tasks t ON t.project=w.project AND t.tz=w.tz
        WHERE w.outcome_id=? AND t.state='ready'
        ORDER BY t.priority,w.project,w.tz LIMIT 1""", (outcome_id,),
    )
    if ready:
        return NextStep("work", f"{ready['project']}/{ready['tz']}")
    obstacles = tx.query(
        "SELECT * FROM obstacles WHERE outcome_id=? AND state='open' ORDER BY ts,id",
        (outcome_id,),
    )
    for row in obstacles:
        if row["type"] == "owner_action" and row["owner_action"]:
            return NextStep("owner", row["id"])
        if row["change_source"] and _actionable_predicate(row["predicate"]):
            return NextStep("wait", row["id"])
    # An invalid open leaf has no executable step. The invariant flags it.
    return NextStep("done")


def next_step(store: Store, outcome_id: str | Outcome) -> NextStep:
    install_schema(store)
    identifier = outcome_id.id if isinstance(outcome_id, Outcome) else outcome_id
    with store.read() as tx:
        row = tx.one("SELECT state FROM outcomes WHERE id=?", (identifier,))
        if row is None:
            raise ValueError("outcome does not exist")
        return NextStep("done") if row["state"] != "open" else _step_for(tx, identifier)


def check_invariant(store: Store) -> list[Violation]:
    """Pure diagnosis: no queue writes, no waiting for our own acceptance."""
    install_schema(store)
    violations: list[Violation] = []
    with store.read() as tx:
        open_rows = tx.query("SELECT id,parent_id FROM outcomes WHERE state='open' ORDER BY id")
        active = {r["id"] for r in open_rows}
        parents = {r["parent_id"] for r in open_rows if r["parent_id"] is not None}
        obstacles = tx.query("SELECT * FROM obstacles WHERE state='open' ORDER BY outcome_id,id")
        by_outcome: dict[str, list[Any]] = defaultdict(list)
        edges: dict[str, set[str]] = defaultdict(set)
        for row in obstacles:
            oid, kind = row["outcome_id"], row["type"]
            by_outcome[oid].append(row)
            if kind != "owner_action" and not _actionable_predicate(row["predicate"]):
                violations.append(Violation("prose_control", oid, f"obstacle {row['id']} has no machine predicate"))
            if kind == "needs_result":
                source = row["change_source"]
                dependency = json.loads(row["predicate"]).get("requires_outcome") if row["predicate"] else None
                # The machine predicate names the actual outcome being awaited.
                # A decoy/nonempty source must not mask self-wait (TZ-186).
                if source == oid or dependency == oid:
                    violations.append(Violation("self_wait", oid, f"obstacle {row['id']} waits on its own evidence"))
                else:
                    # The predicate and the source are independent claims.
                    # Include both in the dependency graph: a different
                    # external-looking source cannot hide an A↔B cycle.
                    if dependency in active:
                        edges[oid].add(dependency)
                    if source in active:
                        edges[oid].add(source)
        for row in open_rows:
            oid = row["id"]
            if oid in parents:
                continue
            ready = tx.scalar(
                """SELECT COUNT(*) FROM outcome_work w
                JOIN tasks t ON t.project=w.project AND t.tz=w.tz
                WHERE w.outcome_id=? AND t.state='ready'""", (oid,),
            )
            observable = any(
                (r["type"] == "owner_action" and bool(r["owner_action"]))
                or (bool(r["change_source"]) and _actionable_predicate(r["predicate"]))
                for r in by_outcome.get(oid, ())
            )
            if not ready and not observable:
                violations.append(Violation("no_next_step", oid, "open leaf has no ready work or observable obstacle"))
        visited: set[str] = set()
        on_stack: set[str] = set()
        flagged: set[str] = set()

        def visit(node: str, stack: list[str]) -> None:
            visited.add(node)
            on_stack.add(node)
            stack.append(node)
            for next_node in sorted(edges.get(node, ())):
                if next_node not in visited:
                    visit(next_node, stack)
                elif next_node in on_stack:
                    for member in stack[stack.index(next_node):]:
                        if member not in flagged:
                            flagged.add(member)
                            violations.append(Violation("wait_cycle", member, "cycle in needs_result graph"))
            stack.pop()
            on_stack.remove(node)

        for oid in sorted(active):
            if oid not in visited:
                visit(oid, [])
    return sorted(violations, key=lambda v: (v.code, v.outcome_id, v.detail))
