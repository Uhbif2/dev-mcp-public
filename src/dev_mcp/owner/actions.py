"""Owner actions and capability gaps tied to versioned fleet outcomes.

No API invocation unless an operator has explicitly enabled the adapter.
Raw credentials, private notes and untrusted callback bodies never enter SQLite.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store

from .decision_book import SourceRef
from .ticktick import TickTickAdapter, TickTickUnavailable, check_task_url

OWNER_CAUSES = frozenset({"owner_physical", "owner_taste", "owner_money", "owner_private_data"})
GAP_CAUSES = frozenset({"missing_tool", "missing_capability", "missing_integration", "unknown_method"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def _id(*values: str) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ActionRequest:
    outcome_id: str
    subject_version: str
    cause: str
    title: str
    proposed_choice: str
    reason: str
    evidence: tuple[SourceRef, ...]
    reversible: bool = False
    rollback: str | None = None
    deadline: str | None = None
    default_choice: str | None = None

    def __post_init__(self) -> None:
        for field in ("outcome_id", "subject_version", "title", "proposed_choice", "reason"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError(f"invalid {field}")
        if self.cause not in OWNER_CAUSES | GAP_CAUSES:
            raise ValueError("unknown action cause")
        if not self.evidence or any(not isinstance(e, SourceRef) for e in self.evidence):
            raise ValueError("versioned evidence required")
        if self.reversible and (not self.rollback or not self.rollback.strip()):
            raise ValueError("reversible choice requires rollback")
        if self.default_choice is not None:
            if not self.reversible or not self.rollback or not self.deadline:
                raise ValueError("default requires deadline and rollback")
            if _date(self.deadline) <= _date(_now()):
                raise ValueError("default deadline already expired")


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    status: str
    remote_id: str | None
    url: str | None
    outcome_id: str
    subject_version: str


def classify(req: ActionRequest) -> Literal["owner_only", "capability_gap"]:
    if req.cause in OWNER_CAUSES:
        return "owner_only"
    return "capability_gap"


class OwnerActions:
    """Persistent outbox: a poll only reads; never sends a second task."""

    def __init__(self, store: Store, adapter: TickTickAdapter | None = None):
        self.store = store
        self.adapter = adapter or TickTickAdapter()
        outcome.install_schema(store)
        with store.tx() as tx:
            tx.change("""CREATE TABLE IF NOT EXISTS owner_action_requests(
                id TEXT PRIMARY KEY, outcome_id TEXT NOT NULL,
                subject_version TEXT NOT NULL, cause TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL,
                remote_id TEXT UNIQUE, remote_url TEXT, deadline TEXT,
                rollback TEXT, default_choice TEXT, evidence_json TEXT NOT NULL,
                FOREIGN KEY(outcome_id) REFERENCES outcomes(id))""",
                event="owner.actions_schema")

    def _check_version(self, req: ActionRequest) -> outcome.Outcome:
        current = outcome.get_outcome(self.store, req.outcome_id)
        if current.state != "open":
            raise ValueError("outcome no longer open")
        expected = str(current.evidence_spec.get("subject_version") or f"rev:{current.rev}")
        if req.subject_version != expected:
            raise ValueError("stale outcome version")
        return current

    def _action_id(self, req: ActionRequest) -> str:
        # One request per outcome/version/cause. Rephrasing a proposal during
        # retries must not create additional owner tasks or platform outcomes.
        return _id(req.outcome_id, req.subject_version, req.cause)

    def _record(self, row) -> ActionReceipt:
        return ActionReceipt(row["id"], row["state"], row["remote_id"],
                             row["remote_url"], row["outcome_id"], row["subject_version"])

    def get(self, action_id: str) -> ActionReceipt:
        with self.store.read() as tx:
            row = tx.one("SELECT * FROM owner_action_requests WHERE id=?", (action_id,))
        if row is None:
            raise KeyError("unknown owner action")
        return self._record(row)

    def route(self, req: ActionRequest) -> ActionReceipt | str:
        current = self._check_version(req)
        route = classify(req)
        if route == "capability_gap":
            gap_id = "capability-gap-" + self._action_id(req)[:24]
            try:
                outcome.get_outcome(self.store, gap_id)
            except ValueError:
                outcome.create_outcome(
                    self.store, project=current.project, title="Закрыть пробел возможностей",
                    intent_id=current.intent_id, outcome_id=gap_id, parent_id=current.id,
                    spec={"kind": "capability_gap", "cause": req.cause,
                          "parent": current.id, "subject_version": req.subject_version,
                          "evidence": [
                              {"kind": e.kind, "ref": e.ref, "version": e.version}
                              for e in req.evidence]},
                    evidence_spec={"kind": "run", "subject_version": req.subject_version},
                    authority={"level": "A1", "from": "owner-action-router"},
                )
            return gap_id
        action_id = self._action_id(req)
        with self.store.tx() as tx:
            tx.change("""INSERT OR IGNORE INTO owner_action_requests
                (id,outcome_id,subject_version,cause,state,created_at,deadline,rollback,default_choice,evidence_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (action_id, req.outcome_id, req.subject_version, req.cause,
                 "default_pending" if req.default_choice is not None else "prepared", _now(),
                 req.deadline, req.rollback, req.default_choice,
                 json.dumps([{"kind": e.kind, "ref": e.ref, "version": e.version}
                             for e in req.evidence], sort_keys=True)),
                event={"kind": "owner.action_open", "payload": {"id": action_id,
                       "outcome": req.outcome_id, "version": req.subject_version,
                       "kind": route}},
            )
        return self.get(action_id)

    def publish(self, req: ActionRequest) -> ActionReceipt:
        receipt = self.route(req)
        if isinstance(receipt, str) or receipt.status == "default_pending":
            raise ValueError("only owner-only action may be published")
        if receipt.status != "prepared":
            return receipt  # published/uncertain/completed/refused are never reposted
        # Disabled or absent operator credentials cannot have sent anything.
        # Leave the durable prepared receipt replayable after authorization.
        try:
            self.adapter.require_authorization()
        except TickTickUnavailable:
            return receipt
        marker = "owner-action:" + receipt.action_id
        payload = {
            "title": req.title,
            "content": (req.proposed_choice + "\nПричина: " + req.reason +
                        "\nOutcome: " + req.outcome_id + "\nVersion: " +
                        req.subject_version + "\n" + marker),
            "due": req.deadline,
        }
        # Mark uncertain BEFORE invoking a network effect. A timeout may mean
        # the remote create succeeded; never blind-retry that create on poll.
        with self.store.tx() as tx:
            reserved = tx.change(
                "UPDATE owner_action_requests SET state='uncertain' WHERE id=? AND state='prepared'",
                (receipt.action_id,), event="owner.delivery_started")
            if reserved.rowcount != 1:
                return self.get(receipt.action_id)
        try:
            remote = self.adapter.create(payload, idempotency_key=receipt.action_id)
        except (TickTickUnavailable, OSError, TimeoutError, ConnectionError):
            return self.get(receipt.action_id)
        with self.store.tx() as tx:
            collision = tx.one("SELECT id FROM owner_action_requests WHERE remote_id=?", (remote.task_id,))
            if collision is not None and collision["id"] != receipt.action_id:
                raise ValueError("foreign remote task ID collision")
            tx.change("""UPDATE owner_action_requests SET state='published',remote_id=?,remote_url=?
                WHERE id=? AND state='uncertain'""",
                (remote.task_id, check_task_url(remote.url), receipt.action_id),
                event={"kind": "owner.action_published", "payload": {"id": receipt.action_id,
                       "outcome": req.outcome_id, "version": req.subject_version}},
            )
        return self.get(receipt.action_id)

    def advance_default(self, action_id: str, *, now: str) -> ActionReceipt:
        """Offer a reversible choice to the controller only after its deadline.

        This emits a durable proposed command for authorized application, not
        an external effect or manufactured owner consent.
        """
        moment = _date(now)
        with self.store.tx() as tx:
            row = tx.one("SELECT * FROM owner_action_requests WHERE id=?", (action_id,))
            if row is None:
                raise KeyError("unknown owner action")
            if row["state"] != "default_pending":
                return self._record(row)
            if not row["default_choice"] or not row["rollback"] or not row["deadline"]:
                raise ValueError("default has no rollback/deadline")
            if moment < _date(row["deadline"]):
                return self._record(row)
            active = tx.one("SELECT state,rev,evidence_spec FROM outcomes WHERE id=?",
                            (row["outcome_id"],))
            if active is None or active["state"] != "open":
                raise ValueError("outcome no longer open")
            version = json.loads(active["evidence_spec"]).get("subject_version") or f"rev:{active['rev']}"
            if version != row["subject_version"]:
                raise ValueError("default no longer matches outcome version")
            tx.change("UPDATE owner_action_requests SET state='default_ready' WHERE id=?",
                      (action_id,), event={"kind": "owner.default_ready",
                      "payload": {"id": action_id, "outcome": row["outcome_id"],
                      "version": version, "choice": row["default_choice"],
                      "rollback": row["rollback"]}})
        return self.get(action_id)

    def complete(self, action_id: str, remote_id: str) -> ActionReceipt:
        receipt = self.get(action_id)
        if receipt.remote_id != remote_id or not remote_id:
            raise ValueError("foreign or unknown remote task")
        if receipt.status in {"completed", "refused"}:
            return receipt
        if receipt.status != "published":
            raise ValueError("action not yet published")
        observed = self.adapter.get(remote_id)
        if observed.task_id != remote_id or observed.marker != "owner-action:" + action_id:
            raise ValueError("remote task does not match this action")
        if observed.status not in {"completed", "refused"}:
            return receipt
        with self.store.tx() as tx:
            row = tx.one("SELECT * FROM owner_action_requests WHERE id=?", (action_id,))
            if row["state"] != "published":
                return self._record(row)
            # A remote refusal/cancellation is verified by its exact task ID,
            # action marker and current status. TickTick may omit completedAt
            # for those states: do not manufacture a completion timestamp.
            # An actual completion always needs a timestamp; when a refusal
            # does supply one it must not predate this action either.
            if observed.status == "completed" or observed.completed_at:
                if _date(observed.completed_at) < _date(row["created_at"]):
                    raise ValueError("remote completion predates action creation")
            current = tx.one("SELECT rev,state,evidence_spec FROM outcomes WHERE id=?", (row["outcome_id"],))
            if current is None or current["state"] != "open":
                raise ValueError("outcome is not open")
            expected = json.loads(current["evidence_spec"]).get("subject_version") or f"rev:{current['rev']}"
            if row["subject_version"] != expected:
                raise ValueError("stale outcome version")
            accepted = observed.status == "completed"
            if accepted:
                evid = "owner-completion-" + action_id
                tx.change("""INSERT OR IGNORE INTO evidence
                    (id,outcome_id,kind,ref,subject_version,env_version,verdict,producer,ts)
                    VALUES (?,?,'external',?,?,?,'pass','ticktick-verified',?)""",
                    (evid, row["outcome_id"], "owner-action:" + action_id,
                     row["subject_version"], "ticktick:verified", _now()),
                    event={"kind": "outcome.evidence", "payload": {"id": evid,
                           "outcome": row["outcome_id"]}},
                )
            tx.change("UPDATE owner_action_requests SET state=? WHERE id=?",
                      ("completed" if accepted else "refused", action_id),
                      event={"kind": "owner.action_resolution", "payload": {"id": action_id,
                             "outcome": row["outcome_id"], "version": row["subject_version"],
                             "status": observed.status,
                             "time_source": ("remote_completed_at" if observed.completed_at
                                             else "remote_status_observed_on_poll")}},
            )
        return self.get(action_id)
