"""Post-acceptance delivery with trusted operator evidence and durable dedup.

This is a trusted-server entrypoint for the acceptance controller, NOT an MCP
agent tool. It never invokes shell, manipulates deployment paths, or reads keys.
The independently authorized fleet_op operator executes all effects.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .delivery_rules import DeliveryPolicy, DeliveryPolicyError, DeliveryStep, load_delivery_policy

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_EVIDENCE_FIELDS = frozenset({
    "kind", "ref", "subject_version", "env_version",
    "verdict", "producer", "operation", "request_fingerprint",
    "metrics", "observed_ts",
})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_attempts (
    event_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    receipt TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class AcceptedEvent:
    """Server-issued acceptance. Never construct this from an agent narrative."""
    event_id: str
    project: str
    subject_version: str
    env_version: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class DefectRequest:
    """Forwarded to the controller's actual defect-work scheduler."""
    project: str
    acceptance_event: str
    subject_version: str
    old_evidence: dict[str, Any]
    delivery_evidence: tuple[dict[str, Any], ...]
    failure_code: str
    known_good_revision: str


FleetOp = Callable[[str, dict[str, Any]], Mapping[str, Any]]
EnvironmentProbe = Callable[[Path, str], str]
RepairScheduler = Callable[[DefectRequest], str]


def _digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _accept(event: AcceptedEvent) -> dict[str, Any]:
    if not isinstance(event, AcceptedEvent):
        raise ValueError("accepted_event_required")
    if (not isinstance(event.event_id, str) or not _ID.fullmatch(event.event_id)
            or not isinstance(event.project, str) or not 1 <= len(event.project) <= 256
            or any(ord(c) < 32 for c in event.project)
            or not _valid_sha(event.subject_version) or not _valid_sha(event.env_version)):
        raise ValueError("invalid_accepted_event")
    e = event.evidence
    if (not isinstance(e, Mapping) or e.get("kind") != "review"
            or e.get("verdict") != "pass" or e.get("producer") != "server"
            or e.get("subject_version") != event.subject_version
            or e.get("env_version") != event.env_version or
            not isinstance(e.get("ref"), str) or not 1 <= len(e["ref"]) <= 256):
        raise ValueError("accepted_evidence_not_server_verified")
    return {key: e[key] for key in (
        "kind", "verdict", "producer", "subject_version", "env_version", "ref",
    )}


def _operator_proof(response: Mapping[str, Any], step: DeliveryStep,
                    *, expected_subject: str, require_pass: bool) -> dict[str, Any]:
    """Reject missing, inconsistent or agent-fabricated operator evidence."""
    if not isinstance(response, Mapping):
        raise ValueError("operator_result_invalid")
    proof = response.get("evidence")
    if not isinstance(proof, Mapping) or not _EVIDENCE_FIELDS.issuperset(proof):
        raise ValueError("operator_evidence_invalid")
    if (proof.get("producer") != "operator" or proof.get("operation") != step.operation
            or proof.get("kind") not in {"run", "measure", "screen", "delivery", "review", "external"}
            or proof.get("verdict") not in {"pass", "fail"}
            or not _valid_sha(proof.get("subject_version"))
            or proof["subject_version"] != expected_subject
            or not _valid_sha(proof.get("env_version"))
            or not _valid_sha(proof.get("request_fingerprint"))
            or not isinstance(proof.get("ref"), str)
            or re.fullmatch(r"opref:[0-9a-f]{64}", proof["ref"]) is None
            or type(proof.get("observed_ts")) is not int or proof["observed_ts"] < 0):
        raise ValueError("operator_evidence_mismatch")
    if (require_pass and (response.get("ok") is not True or proof["verdict"] != "pass")):
        raise ValueError("operator_operation_failed")
    metrics = proof.get("metrics", {})
    if not isinstance(metrics, Mapping) or len(metrics) > 12 or any(
        not isinstance(key, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) is None
        or type(value) not in (bool, int, float)
        or (type(value) is float and not -1e12 <= value <= 1e12)
        for key, value in metrics.items()
    ):
        raise ValueError("operator_metrics_invalid")
    return {
        "kind": proof["kind"], "ref": proof["ref"],
        "subject_version": proof["subject_version"], "env_version": proof["env_version"],
        "verdict": proof["verdict"], "producer": "operator",
        "operation": step.operation, "request_fingerprint": proof["request_fingerprint"],
        "metrics": dict(metrics), "observed_ts": proof["observed_ts"],
    }


def _state(db: Path, key: str, fingerprint: str) -> dict[str, Any] | None:
    """BEGIN IMMEDIATE reserves the event before the first external effect."""
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db, timeout=15, isolation_level=None) as conn:
        conn.execute(_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT fingerprint,receipt FROM delivery_attempts WHERE event_id=?",
                               (key,)).fetchone()
            if row:
                conn.commit()
                if row[0] != fingerprint:
                    return {"status": "event_conflict", "event_id": key}
                return json.loads(row[1])
            conn.execute("INSERT INTO delivery_attempts(event_id,fingerprint,receipt) VALUES (?,?,?)",
                         (key, fingerprint, json.dumps({"status": "running", "event_id": key})))
            conn.commit()
            return None
        except Exception:
            conn.rollback()
            raise


def _save(db: Path, key: str, fingerprint: str, receipt: dict[str, Any]) -> None:
    with sqlite3.connect(db, timeout=15, isolation_level=None) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT fingerprint FROM delivery_attempts WHERE event_id=?",
                           (key,)).fetchone()
        if row is None or row[0] != fingerprint:
            conn.rollback()
            raise ValueError("delivery_state_conflict")
        conn.execute("UPDATE delivery_attempts SET receipt=? WHERE event_id=?",
                     (json.dumps(receipt, sort_keys=True), key))
        conn.commit()


def on_accept(
    event: AcceptedEvent, *,
    project_root: str | Path,
    allowed_roots: tuple[str | Path, ...],
    state_db: str | Path,
    fleet_op: FleetOp,
    probe_environment: EnvironmentProbe,
    create_defect: RepairScheduler,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """One post-accept delivery attempt, with fail-to-repair on any operational failure.

    No rule file => no publication. The caller must be the trusted acceptance
    controller. state_db must be a trusted server-owned path, never agent-supplied.
    """
    old_proof = _accept(event)
    try:
        policy = load_delivery_policy(project_root, allowed_roots=allowed_roots)
    except DeliveryPolicyError as exc:
        return {"event_id": event.event_id, "status": "not_configured" if
                str(exc) == "deliver_toml_missing" else "invalid_policy",
                "error_code": str(exc), "evidence": [], "repair_work": None}
    if policy.apply.params.get("artifact_sha256") != event.subject_version:
        return {"event_id": event.event_id, "status": "invalid_policy",
                "error_code": "accepted_artifact_mismatch", "evidence": [], "repair_work": None}
    # A successful CI run of another commit is not evidence for this delivery.
    # Operator's generic proof validator does not bind ci.run.commit_sha itself.
    if (policy.verify.operation == "ci.run"
            and policy.verify.params.get("commit_sha") != event.subject_version):
        return {"event_id": event.event_id, "status": "invalid_policy",
                "error_code": "verify_subject_mismatch", "evidence": [], "repair_work": None}
    fp = _digest({
        "project": event.project, "event_id": event.event_id, "subject": event.subject_version,
        "env": event.env_version, "policy": {
            "target": policy.target, "workspace": str(policy.workspace),
            "timeout": policy.timeout_seconds, "known_good": policy.known_good_revision,
            "apply": [policy.apply.operation, policy.apply.params],
            "verify": [policy.verify.operation, policy.verify.params],
            "rollback": [policy.rollback.operation, policy.rollback.params],
        },
    })
    db = Path(state_db)
    existing = _state(db, event.event_id, fp)
    if existing is not None:
        return existing
    deadline = monotonic() + policy.timeout_seconds
    steps: list[dict[str, Any]] = []
    has_attempted_apply = False
    failure = ""
    status = "failed"
    final_env: str | None = None

    def env() -> str:
        version = probe_environment(policy.workspace, policy.target)
        if not _valid_sha(version):
            raise ValueError("environment_probe_invalid")
        return version

    def call(step: DeliveryStep, stage: str, *, subject: str, check_deadline: bool = True) -> dict[str, Any]:
        if check_deadline and monotonic() > deadline:
            raise ValueError("deadline_exceeded")
        response = fleet_op(step.operation, dict(step.params))
        proof = _operator_proof(response, step, expected_subject=subject, require_pass=False)
        steps.append({"stage": stage, "evidence": proof})
        if response.get("ok") is not True or proof["verdict"] != "pass":
            raise ValueError("operator_operation_failed")
        if check_deadline and monotonic() > deadline:
            raise ValueError("deadline_exceeded")
        return proof

    try:
        if env() != event.env_version:
            raise ValueError("environment_changed")
        if monotonic() > deadline:
            raise ValueError("deadline_exceeded")
        # Reserve idempotency row before reaching this mutation.
        has_attempted_apply = True
        applied = call(policy.apply, "apply", subject=event.subject_version)
        if env() != applied["env_version"]:
            raise ValueError("environment_changed")
        verified = call(policy.verify, "verify", subject=event.subject_version)
        final_env = env()
        if final_env != verified["env_version"] or final_env != applied["env_version"]:
            raise ValueError("environment_changed")
        status = "delivered"
    except Exception as exc:
        # Never leak upstream/adapter exception messages (possible credentials).
        failure = str(exc) if isinstance(exc, ValueError) and str(exc) in {
            "environment_changed", "environment_probe_invalid", "deadline_exceeded",
            "operator_operation_failed", "operator_evidence_mismatch",
            "operator_evidence_invalid", "operator_result_invalid", "operator_metrics_invalid",
        } else "delivery_operation_failed"
        if has_attempted_apply:
            try:
                rolled = call(policy.rollback, "rollback", subject=policy.known_good_revision,
                              check_deadline=False)
                if env() != rolled["env_version"]:
                    raise ValueError("environment_changed")
            except Exception:
                status = "rollback_failed"
        # Without an apply attempt, nothing should be rolled back.
        if status != "rollback_failed":
            status = "failed"
    if status != "delivered":
        request = DefectRequest(
            project=event.project, acceptance_event=event.event_id,
            subject_version=event.subject_version, old_evidence=old_proof,
            delivery_evidence=tuple(step["evidence"] for step in steps),
            failure_code=failure or "delivery_operation_failed",
            known_good_revision=policy.known_good_revision,
        )
        try:
            work_id = create_defect(request)
            if not isinstance(work_id, str) or not _ID.fullmatch(work_id):
                raise ValueError("invalid_defect_work")
        except Exception:
            work_id = None
            status = "repair_pending" if status == "failed" else "rollback_failed"
    else:
        work_id = None
    receipt = {
        "event_id": event.event_id, "project": event.project, "status": status,
        "subject_version": event.subject_version,
        "expected_env_version": event.env_version,
        "env_version": final_env if status == "delivered" else None,
        "known_good_revision": policy.known_good_revision,
        "evidence": steps, "accepted_evidence": old_proof,
        "error_code": failure if status != "delivered" else None,
        "repair_work": work_id,
    }
    _save(db, event.event_id, fp, receipt)
    return receipt
