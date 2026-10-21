"""Fail-closed clean-start CI/second-PC proof coordinator (TZ-19).

No direct shell, local credentials, human-PC access, or production calls here.
The only effect is a validated named fleet_op request to a trusted Operator.
Real clone/install/run and attestation are responsibilities of the remote adapter.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from dev_mcp.operator_catalog import CATALOG_VERSION, validate_operation
from dev_mcp.operator_core import verify_public_result

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
_SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,79}\Z")


def _digest(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def _sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


@dataclass(frozen=True)
class CleanStartSpec:
    """Pinned desired outcome. ID + versions come from the trusted controller."""
    task_id: str
    commit_sha: str
    source_sha: str
    lock_sha: str
    env_sha: str
    artifact_sha: str
    outcome_sha: str
    workflow: str
    second_pc_plan_id: str
    target: str
    min_ram_mib: int
    min_disk_mib: int
    required_secrets: tuple[str, ...] = ()

    def validate(self) -> None:
        if any(type(x) is not str or _NAME.fullmatch(x) is None
               for x in (self.task_id, self.workflow, self.second_pc_plan_id, self.target)):
            raise ValueError("invalid_task_or_plan")
        if type(self.commit_sha) is not str or not _COMMIT.fullmatch(self.commit_sha):
            raise ValueError("invalid_commit")
        if any(not _sha(v) for v in (
            self.source_sha, self.lock_sha, self.env_sha,
            self.artifact_sha, self.outcome_sha
        )):
            raise ValueError("invalid_version")
        if (type(self.min_ram_mib) is not int or not 128 <= self.min_ram_mib <= 1_048_576
                or type(self.min_disk_mib) is not int or not 128 <= self.min_disk_mib <= 10_485_760):
            raise ValueError("invalid_resource_requirements")
        if (type(self.required_secrets) is not tuple
                or len(self.required_secrets) > 16
                or any(type(s) is not str or _SECRET_NAME.fullmatch(s) is None
                       for s in self.required_secrets)):
            raise ValueError("invalid_secret_references")


@dataclass(frozen=True)
class ResourceProfile:
    """Snapshot from a trusted resource probe, NEVER provided through fleet_op."""
    resource_id: str
    operation: str
    workspace_base: str
    online: bool
    human_using: bool
    free_ram_mib: int
    free_disk_mib: int
    ready_secret_names: frozenset[str]

    def validate(self) -> None:
        if (type(self.resource_id) is not str or _NAME.fullmatch(self.resource_id) is None
                or self.operation not in {"ci.run", "second_pc.run"}):
            raise ValueError("invalid_resource_profile")
        if type(self.workspace_base) is not str:
            raise ValueError("invalid_workspace_base")
        p = Path(self.workspace_base)
        if (not p.is_absolute()
                or ".." in p.parts or p.resolve() != p):
            raise ValueError("invalid_workspace_base")
        if (type(self.online) is not bool or type(self.human_using) is not bool
                or type(self.free_ram_mib) is not int or self.free_ram_mib < 0
                or type(self.free_disk_mib) is not int or self.free_disk_mib < 0
                or type(self.ready_secret_names) is not frozenset
                or any(type(x) is not str or _SECRET_NAME.fullmatch(x) is None
                       for x in self.ready_secret_names)):
            raise ValueError("invalid_resource_profile")

    def available(self, spec: CleanStartSpec) -> bool:
        return (self.online and not self.human_using
                and self.free_ram_mib >= spec.min_ram_mib
                and self.free_disk_mib >= spec.min_disk_mib)


FleetOp = Callable[[str, dict[str, Any]], Mapping[str, Any]]
# Authentication and signature checking MUST be done by an independent trusted
# verifier with a pinned public key. None always fails closed.
AttestationVerifier = Callable[[Mapping[str, Any]], bool]
AttestationFetcher = Callable[[str, str, str], Mapping[str, Any]]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clean_start_attempts (
    task_id TEXT PRIMARY KEY,
    request_sha TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','done')),
    receipt_json TEXT,
    receipt_sha TEXT,
    nonce TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clean_start_leases (
    resource_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE
);
"""


class CleanStartCoordinator:
    def __init__(self, db_path: str | Path,
                 *, fleet_op: FleetOp,
                 fetch_attestation: AttestationFetcher | None = None,
                 verify_attestation: AttestationVerifier | None = None):
        self.db_path = Path(db_path)
        if not self.db_path.parent.is_dir():
            raise ValueError("state_directory_missing")
        self.fleet_op = fleet_op
        self.fetch_attestation = fetch_attestation
        self.verify_attestation = verify_attestation
        with self._db() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _existing(self, task_id: str, request_sha: str) -> dict[str, Any] | None:
        """Replay reservations even when the remote resource is now offline."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT request_sha,state,receipt_json,receipt_sha "
                "FROM clean_start_attempts WHERE task_id=?", (task_id,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != request_sha:
            return {"status": "defect", "error_code": "spec_changed"}
        if row[1] != "done":
            return {"status": "waiting", "error_code": "in_progress"}
        try:
            receipt = json.loads(row[2])
            if (type(receipt) is not dict or _digest(receipt) != row[3]
                    or receipt.get("task_id") != task_id
                    or receipt.get("request_sha") != request_sha):
                raise ValueError("journal_digest_mismatch")
        except (TypeError, ValueError, OverflowError):
            return {"status": "defect", "error_code": "journal_digest_mismatch"}
        return receipt

    def _reserve(self, spec: CleanStartSpec, resource: ResourceProfile,
                 request_sha: str) -> tuple[str, str | dict[str, Any]]:
        """BEGIN IMMEDIATE: reserve slot before dispatch; no implicit retries."""
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_sha,state,receipt_json,receipt_sha FROM clean_start_attempts WHERE task_id=?",
                (spec.task_id,),
            ).fetchone()
            if row:
                conn.commit()
                if row[0] != request_sha:
                    return "conflict", "spec_changed"
                if row[1] == "running":
                    return "waiting", "in_progress"
                try:
                    receipt = json.loads(row[2])
                    if (type(receipt) is not dict or _digest(receipt) != row[3]
                            or receipt.get("task_id") != spec.task_id
                            or receipt.get("request_sha") != request_sha):
                        raise ValueError("journal_digest_mismatch")
                except (TypeError, ValueError, OverflowError):
                    return "defect", "journal_digest_mismatch"
                return "cached", receipt
            locked = conn.execute("SELECT 1 FROM clean_start_leases WHERE resource_id=?",
                                  (resource.resource_id,)).fetchone()
            if locked:
                conn.commit()
                return "waiting", "resource_reserved"
            nonce = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO clean_start_attempts(task_id,request_sha,resource_id,state,receipt_json,receipt_sha,nonce)"
                " VALUES (?,?,?,'running',NULL,NULL,?)",
                (spec.task_id, request_sha, resource.resource_id, nonce),
            )
            conn.execute("INSERT INTO clean_start_leases(resource_id,task_id) VALUES (?,?)",
                         (resource.resource_id, spec.task_id))
            conn.commit()
            return "fresh", nonce

    def _finish(self, task_id: str, receipt: dict[str, Any]) -> None:
        wire = json.dumps(receipt, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if len(wire) > 16384:
            raise ValueError("receipt_too_large")
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM clean_start_attempts WHERE task_id=?",
                               (task_id,)).fetchone()
            if row is None or row[0] != "running":
                conn.rollback()
                raise ValueError("reservation_missing")
            conn.execute(
                "UPDATE clean_start_attempts SET state='done',receipt_json=?,receipt_sha=? WHERE task_id=?",
                (wire, _digest(receipt), task_id),
            )
            conn.execute("DELETE FROM clean_start_leases WHERE task_id=?", (task_id,))
            conn.commit()

    @staticmethod
    def _public_error(spec: CleanStartSpec, status: str, code: str,
                      *, predicate: dict[str, str] | None = None) -> dict[str, Any]:
        return {"task_id": spec.task_id, "status": status, "error_code": code,
                "subject_version": spec.source_sha, "evidence": [],
                "predicate": predicate or {}}

    @staticmethod
    def _operator_proof(result: Mapping[str, Any], operation: str, params: dict[str, Any],
                        version: str, env_sha: str) -> dict[str, Any]:
        safe = verify_public_result(result, operation)
        if safe.get("ok") is not True:
            raise ValueError("operator_failed")
        proof = safe["evidence"]
        canonical = json.dumps({"version": CATALOG_VERSION, "operation": operation,
                                "params": params}, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=True)
        fingerprint = hashlib.sha256(canonical.encode("ascii")).hexdigest()
        if (proof["verdict"] != "pass" or proof["request_fingerprint"] != fingerprint
                or proof["subject_version"] != version
                or proof["env_version"] != env_sha):
            raise ValueError("operator_proof_mismatch")
        return proof

    @staticmethod
    def _witness(spec: CleanStartSpec, resource: ResourceProfile,
                 observation: Mapping[str, Any], *, nonce: str, workspace: str,
                 operation: str, operator_ref: str,
                 verifier: AttestationVerifier | None) -> dict[str, Any]:
        """Check independently signed clean clone -> install -> user outcome.

        The verifier authenticates the *entire* observation, including issuer,
        nonce, workspace, step digests and operator proof reference. Callers
        never get to submit their own verifier through fleet_op.
        """
        if verifier is None or not isinstance(observation, Mapping):
            raise ValueError("attestation_unavailable")
        if verifier(observation) is not True:
            raise ValueError("attestation_invalid")
        expected = {
            "nonce": nonce,
            "workspace": workspace,
            "resource_id": resource.resource_id,
            "operation": operation,
            "operator_ref": operator_ref,
            "commit_sha": spec.commit_sha,
            "source_sha": spec.source_sha,
            "lock_sha": spec.lock_sha,
            "env_sha": spec.env_sha,
            "artifact_sha": spec.artifact_sha,
            "outcome_sha": spec.outcome_sha,
        }
        if any(observation.get(key) != value for key, value in expected.items()):
            raise ValueError("attested_versions_mismatch")
        if (observation.get("fresh_clone") is not True
                or observation.get("empty_workspace_before_clone") is not True
                or observation.get("no_previous_artifacts") is not True
                or observation.get("real_e2e") is not True):
            raise ValueError("dirty_or_incomplete_workspace")
        stages = observation.get("stages")
        if not isinstance(stages, list) or len(stages) != 3:
            raise ValueError("incomplete_fresh_install")
        names = ("clone", "install", "run")
        versions = (spec.commit_sha, spec.lock_sha, spec.outcome_sha)
        for index, (name, version) in enumerate(zip(names, versions)):
            event = stages[index]
            if (not isinstance(event, Mapping) or event.get("stage") != name
                    or event.get("verdict") != "pass" or event.get("version") != version
                    or type(event.get("duration_ms")) is not int or event["duration_ms"] < 0):
                raise ValueError("fresh_stage_failed")
        if stages[2].get("user_visible") is not True:
            raise ValueError("user_outcome_not_observed")
        if (type(observation.get("observed_ts")) is not int
                or observation["observed_ts"] < 1):
            raise ValueError("attestation_timestamp_missing")
        # Never echo raw attestations, which may contain URLs, logs or hidden
        # credentials. Return a bounded cryptographic commitment and stage facts.
        return {
            "kind": "external", "producer": "trusted_clean_start_attestor",
            "ref": "attest:" + _digest(dict(observation)), "subject_version": spec.source_sha,
            "env_version": spec.env_sha, "operator_ref": operator_ref,
            "stages": [{"stage": s["stage"], "version": s["version"],
                        "verdict": "pass", "duration_ms": s["duration_ms"]}
                       for s in stages],
            "observed_ts": observation["observed_ts"], "verdict": "pass",
        }

    def run_once(self, spec: CleanStartSpec, resources: tuple[ResourceProfile, ...]) -> dict[str, Any]:
        """Use an already-authorized fleet_op, never launch a runner here.

        'verified' requires operator proof AND an independently authenticated
        fresh-workspace clone/install/E2E witness. Missing input => typed wait
        or defect, never a successful verdict.
        """
        spec.validate()
        request_sha = _digest(asdict(spec))
        existing = self._existing(spec.task_id, request_sha)
        if existing is not None:
            if existing["status"] in {"waiting", "defect"} and "request_sha" not in existing:
                return self._public_error(spec, existing["status"], existing["error_code"])
            return existing
        for resource in resources:
            resource.validate()
        if len({r.resource_id for r in resources}) != len(resources):
            return self._public_error(spec, "defect", "ambiguous_resources")
        available = [r for r in resources if r.available(spec)]
        eligible = [r for r in available
                    if all(name in r.ready_secret_names for name in spec.required_secrets)]
        if not eligible:
            if available:
                return self._public_error(spec, "defect", "required_secret_missing")
            return self._public_error(spec, "waiting", "clean_resource_unavailable", predicate={
                "ci": "online_and_capacity", "second_pc": "online_idle_and_capacity",
            })
        if self.fetch_attestation is None or self.verify_attestation is None:
            return self._public_error(spec, "waiting", "independent_attestor_unavailable")
        # The lease is checked atomically inside _reserve: a preliminary SELECT
        # outside its transaction would race another coordinator. Busy CI must
        # not prevent using a free second PC. A same-task running reservation
        # or changed spec must never be retried on a different resource.
        eligible.sort(key=lambda r: 0 if r.operation == "ci.run" else 1)
        for resource in eligible:
            status, value = self._reserve(spec, resource, request_sha)
            if status == "waiting" and value == "resource_reserved":
                continue
            break
        else:
            return self._public_error(spec, "waiting", "resource_reserved")
        if status == "cached":
            assert isinstance(value, dict)
            return value
        if status != "fresh":
            kind = "waiting" if status == "waiting" else "defect"
            assert isinstance(value, str)
            return self._public_error(spec, kind, value)
        assert isinstance(value, str)
        nonce = value
        workspace = str(Path(resource.workspace_base) / ("clean-" + nonce))
        operation = resource.operation
        params = {
            "workspace": workspace,
            "target": spec.target,
            "execution_id": nonce,
        }
        if operation == "ci.run":
            params.update({"commit_sha": spec.commit_sha, "workflow": spec.workflow})
            version = spec.source_sha
        else:
            params.update({"artifact_sha256": spec.artifact_sha,
                           "plan_id": spec.second_pc_plan_id})
            version = spec.artifact_sha
        evidence: list[dict[str, Any]] = []
        status = "defect"
        error_code: str | None = "unexpected_failure"
        try:
            validate_operation(operation, params)
            raw_proof = self.fleet_op(operation, params)
            operator_proof = self._operator_proof(raw_proof, operation, params,
                                                  version, spec.env_sha)
            evidence.append(operator_proof)
            observed = self.fetch_attestation(resource.resource_id, nonce,
                                              operator_proof["ref"])
            witness = self._witness(spec, resource, observed, nonce=nonce,
                                    workspace=workspace, operation=operation,
                                    operator_ref=operator_proof["ref"],
                                    verifier=self.verify_attestation)
            evidence.append(witness)
            status = "verified"
            error_code = None
        except Exception as exc:
            # Adapter/remote exceptions could expose secret strings. Only
            # internally enumerated safe codes may cross the boundary.
            code = str(exc)
            if type(exc) is ValueError and code in {
                "operator_failed", "operator_proof_mismatch", "attestation_unavailable",
                "attestation_invalid", "attested_versions_mismatch",
                "dirty_or_incomplete_workspace", "incomplete_fresh_install",
                "fresh_stage_failed", "user_outcome_not_observed",
                "attestation_timestamp_missing",
            }:
                error_code = code
            else:
                error_code = "clean_start_operation_failed"
        receipt = {
            "task_id": spec.task_id, "status": status, "error_code": error_code,
            "subject_version": spec.source_sha, "env_version": spec.env_sha,
            "source_commit": spec.commit_sha,
            "dependency_lock_sha": spec.lock_sha,
            "artifact_sha": spec.artifact_sha,
            "outcome_sha": spec.outcome_sha,
            "resource_id": resource.resource_id, "operation": operation,
            "workspace": workspace,
            "request_sha": request_sha, "evidence": evidence,
        }
        try:
            self._finish(spec.task_id, receipt)
        except (OSError, ValueError, sqlite3.Error):
            # A running reservation remains charged, forbidding repeated
            # execution after a journal outage.
            return self._public_error(spec, "defect", "journal_write_failed")
        return receipt
