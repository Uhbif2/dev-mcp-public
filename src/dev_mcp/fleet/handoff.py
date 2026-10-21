"""Trusted post-delivery owner handoff and durable, at-most-once toast outbox.

Never expose publish as an untrusted MCP agent tool: a delivery receipt must
originate from the server's delivery controller, not an agent's JSON report.
The full packet is owner-private; toast carries only fixed text and a local URL.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from dev_mcp.owner.actions import ActionRequest

from .outcome import Outcome
from .windows_notify import WindowsToast, gui_session_available


_SHA = re.compile(r"[0-9a-f]{64}\Z")
_EVENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_EXTENSION = re.compile(r"[a-p]{32}\Z")
_OPREF = re.compile(r"opref:[0-9a-f]{64}\Z")
_APPLY_OPERATIONS = frozenset({"cloud.deploy", "android.build_install", "second_pc.run"})
_VERIFY_OPERATIONS = frozenset({"ci.run", "eval.live", "device.screens", "browser.task"})
_OVERVIEW_PATHS = frozenset({"/dashboard/", "/dashboard/index.html", "/owner/overview"})
_SCHEMA = """CREATE TABLE IF NOT EXISTS handoff_receipts (
    event_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    notice_state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
)"""


def validate_overview_url(url: str) -> str:
    """Accept only a fixed local overview, never arbitrary/custom URI schemes.

    Query strings, fragments, credentials, percent-escapes, paths outside the
    overview and hosts other than loopback or a Chrome extension are rejected.
    """
    if not isinstance(url, str) or len(url) > 300 or any(
        ord(c) < 33 or ord(c) > 126 or c in "\\'\"<>%" for c in url
    ):
        raise ValueError("unsafe_overview_url")
    try:
        p = urlsplit(url)
        if p.query or p.fragment or p.username is not None or p.password is not None:
            raise ValueError("unsafe_overview_url")
        if p.scheme == "chrome-extension":
            if not _EXTENSION.fullmatch(p.netloc) or p.path != "/dashboard/index.html":
                raise ValueError("unsafe_overview_url")
        elif p.scheme == "http":
            if (p.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or not isinstance(p.port, int) or not 1 <= p.port <= 65535
                    or p.path not in _OVERVIEW_PATHS):
                raise ValueError("unsafe_overview_url")
        else:
            raise ValueError("unsafe_overview_url")
    except ValueError as exc:
        raise ValueError("unsafe_overview_url") from exc
    return url


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _safe_title(outcome: Outcome) -> str:
    title = outcome.title
    if (outcome.spec.get("dashboard_visibility") == "public"
            and isinstance(title, str) and 1 <= len(title) <= 160
            and not any(ord(c) < 32 or c in "<>&" for c in title)):
        return title
    return "Результат готов"


def _proof(raw: Any, *, stage: str, subject: str, env: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("stage") != stage:
        raise ValueError("unverified_delivery_path")
    proof = raw.get("evidence")
    if not isinstance(proof, Mapping) or (
        proof.get("producer") != "operator" or proof.get("verdict") != "pass"
        or proof.get("subject_version") != subject
        or proof.get("env_version") != env
        or proof.get("kind") not in {"run", "measure", "screen", "delivery", "review", "external"}
        or not isinstance(proof.get("ref"), str) or not _OPREF.fullmatch(proof["ref"])
        or not isinstance(proof.get("operation"), str)
        or not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", proof["operation"])
        or not _sha(proof.get("request_fingerprint"))
        or type(proof.get("observed_ts")) is not int or proof["observed_ts"] < 0
    ):
        raise ValueError("unverified_delivery_path")
    if proof["operation"] not in (_APPLY_OPERATIONS if stage == "apply" else _VERIFY_OPERATIONS):
        raise ValueError("unverified_delivery_path")
    return {"stage": stage, "kind": proof["kind"], "operation": proof["operation"],
            "ref": proof["ref"], "subject_version": subject, "env_version": env}


def build_packet(
    *, outcome: Outcome, delivery_receipt: Mapping[str, Any] | None,
    use_location: str, overview_url: str,
    physical_action: ActionRequest | None = None,
) -> dict[str, Any]:
    """Only a delivered, version-matched, non-cancelled outcome gets a packet."""
    if not isinstance(outcome, Outcome) or outcome.state != "done":
        raise ValueError("outcome_not_completed")
    if not isinstance(delivery_receipt, Mapping) or delivery_receipt.get("status") != "delivered":
        raise ValueError("delivery_proof_required")
    receipt = delivery_receipt
    version = receipt.get("subject_version")
    final_env = receipt.get("env_version")
    initial_env = receipt.get("expected_env_version")
    event = receipt.get("event_id")
    if (not _sha(version) or not _sha(final_env) or not _sha(initial_env)
            or not _sha(receipt.get("known_good_revision"))
            or not isinstance(event, str) or not _EVENT.fullmatch(event)
            or receipt.get("project") != outcome.project
            or outcome.evidence_spec.get("subject_version") != version
            or receipt.get("repair_work") is not None or receipt.get("error_code") is not None):
        raise ValueError("delivery_version_mismatch")
    accepted = receipt.get("accepted_evidence")
    if not isinstance(accepted, Mapping) or (
        accepted.get("producer") != "server" or accepted.get("kind") != "review"
        or accepted.get("verdict") != "pass" or accepted.get("subject_version") != version
        or accepted.get("env_version") != initial_env
        or not isinstance(accepted.get("ref"), str)
        or not 1 <= len(accepted["ref"]) <= 256
    ):
        raise ValueError("accepted_evidence_required")
    path = receipt.get("evidence")
    if not isinstance(path, (list, tuple)) or len(path) != 2:
        raise ValueError("unverified_delivery_path")
    stages = [_proof(item, stage=stage, subject=version, env=final_env)
              for item, stage in zip(path, ("apply", "verify"))]
    if not isinstance(use_location, str) or not 1 <= len(use_location) <= 512 or any(
        ord(c) < 32 for c in use_location
    ):
        raise ValueError("invalid_use_location")
    link = validate_overview_url(overview_url)
    action = None
    if physical_action is not None:
        if (not isinstance(physical_action, ActionRequest)
                or physical_action.cause != "owner_physical"
                or physical_action.outcome_id != outcome.id
                or physical_action.subject_version != version):
            raise ValueError("invalid_physical_action")
        action = {"id": hashlib.sha256((outcome.id + ":" + version + ":" +
                                        physical_action.cause).encode()).hexdigest(),
                  "what": physical_action.proposed_choice, "why": physical_action.reason}
    key = hashlib.sha256(json.dumps([outcome.id, event, version],
                                    separators=(",", ":")).encode()).hexdigest()
    return {
        "schema": 1, "packet_id": key, "event_id": event,
        "outcome_id": outcome.id, "intent_id": outcome.intent_id,
        "result": {"title": _safe_title(outcome), "version": version,
                   "outcome_rev": outcome.rev},
        "where_to_use": use_location, "overview_url": link,
        "delivery": {"status": "delivered", "verified_path": stages,
                     "before_env": initial_env, "after_env": final_env},
        "evidence_summary": {"acceptance": {"kind": "review", "ref": accepted["ref"],
                                                    "subject_version": version,
                                                    "env_version": initial_env},
                             "operator_steps": len(stages)},
        "rollback": {"known_good_revision": receipt["known_good_revision"],
                     "status": "configured_not_executed"},
        "physical_action": action,
    }


@dataclass(frozen=True)
class HandoffReceipt:
    packet: dict[str, Any]
    notice_state: str
    attempts: int


class HandoffService:
    """SQLite journal reserves the event before any external WinRT effect.

    A duplicate publish never replays. An absent desktop leaves a durable
    ``pending`` receipt with attempts=0; retry_pending may send that *once* when
    the owner logs in. A failed/ambiguous WinRT call remains pending_unknown and
    is never blindly retried (the toast may have appeared). ``submitted`` means
    WinRT accepted the call, not proof that the owner actually saw a toast.
    """

    def __init__(self, state_db: str | Path, *, notifier: Callable[[str], bool] | None = None,
                 session_ready: Callable[[], bool] = gui_session_available,
                 app_id: str | None = None):
        self.db = Path(state_db)
        self.session_ready = session_ready
        if notifier is not None:
            self.notifier = notifier
        elif app_id is not None:
            self.notifier = WindowsToast(app_id).show
        else:
            self.notifier = lambda _url: False
            # Missing registered AUMID is not an attempted side effect. Keep
            # pending so installing/configuring the transport can resume once.
            self.session_ready = lambda: False

    @contextmanager
    def _connect(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db, timeout=15, isolation_level=None)
        try:
            db.execute(_SCHEMA)
            yield db
        finally:
            db.close()

    @staticmethod
    def _record(row: tuple[str, str, int]) -> HandoffReceipt:
        return HandoffReceipt(json.loads(row[0]), row[1], row[2])

    def get(self, event_id: str) -> HandoffReceipt:
        with self._connect() as db:
            row = db.execute("SELECT packet_json,notice_state,attempts FROM handoff_receipts "
                             "WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError("unknown_handoff")
        return self._record(row)

    def publish(self, *, outcome: Outcome, delivery_receipt: Mapping[str, Any] | None,
                use_location: str, overview_url: str,
                physical_action: ActionRequest | None = None) -> HandoffReceipt:
        packet = build_packet(outcome=outcome, delivery_receipt=delivery_receipt,
                              use_location=use_location, overview_url=overview_url,
                              physical_action=physical_action)
        blob = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        fingerprint = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint FROM handoff_receipts WHERE event_id=?",
                             (packet["event_id"],)).fetchone()
            if row:
                db.commit()
                if row[0] != fingerprint:
                    raise ValueError("handoff_event_conflict")
                return self.get(packet["event_id"])
            db.execute("INSERT INTO handoff_receipts"
                       "(event_id,fingerprint,packet_json,notice_state,attempts) "
                       "VALUES (?,?,?,'pending',0)", (packet["event_id"], fingerprint, blob))
            db.commit()
        return self.retry_pending(packet["event_id"])

    def retry_pending(self, event_id: str) -> HandoffReceipt:
        """Single safe resume of offline queue, never an automatic toast retry."""
        record = self.get(event_id)
        if record.notice_state != "pending" or record.attempts != 0:
            return record
        try:
            available = self.session_ready()
        except Exception:
            available = False
        if not available:
            return record
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE handoff_receipts SET notice_state='uncertain',attempts=1 "
                "WHERE event_id=? AND notice_state='pending' AND attempts=0", (event_id,)
            ).rowcount
            db.commit()
        if changed != 1:
            return self.get(event_id)
        try:
            shown = self.notifier(record.packet["overview_url"]) is True
        except Exception:
            shown = False
        with self._connect() as db:
            db.execute("UPDATE handoff_receipts SET notice_state=? "
                       "WHERE event_id=? AND notice_state='uncertain'",
                       ("submitted" if shown else "pending_unknown", event_id))
        return self.get(event_id)
