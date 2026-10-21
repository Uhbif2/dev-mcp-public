"""Trusted Operator boundary: validate -> authorize -> reserve -> adapter -> proof.

Instantiate only in an operator OS process with a separately restricted identity.
The fleet process has access to the public fleet_op request, not to credentials,
budget paths or handler registration. No direct subprocess/shell execution here.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .operator_budget import BudgetBook, BudgetError, BudgetLedger
from .operator_catalog import CATALOG_VERSION, CatalogError, NAMES, validate_operation


@dataclass(frozen=True)
class AdapterEvidence:
    kind: str
    ref: str
    subject_version: str
    env_version: str
    verdict: str
    metrics: Mapping[str, int | float | bool] = field(default_factory=dict)


Adapter = Callable[[Mapping[str, Any]], AdapterEvidence]


class CredentialVault:
    """Read protected files solely from an operator-owned directory.

    The directory's OS ACL and the policy deny_patterns MUST exclude fleet agents;
    no secret is accepted over fleet_op, stdout, environment or logging.
    """
    def __init__(self, directory: Path | str):
        self.directory = Path(directory).resolve()

    def read(self, name: str) -> bytes:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name, flags=re.ASCII):
            raise ValueError("invalid_secret_name")
        path = (self.directory / name).resolve()
        if path.parent != self.directory or not path.is_file():
            raise ValueError("secret_not_available")
        return path.read_bytes()


def _safe_evidence(proof: AdapterEvidence, fingerprint: str, operation: str,
                   params: Mapping[str, Any], observed_ts: int) -> dict[str, Any]:
    if not isinstance(proof, AdapterEvidence):
        raise ValueError("invalid_evidence")
    if proof.kind not in {"run", "measure", "screen", "delivery", "review", "external"}:
        raise ValueError("invalid_evidence_kind")
    if proof.verdict not in {"pass", "fail"}:
        raise ValueError("invalid_evidence_verdict")
    for digest in (proof.subject_version, proof.env_version):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("missing_version")
    if not isinstance(proof.ref, str) or not 1 <= len(proof.ref) <= 256:
        raise ValueError("invalid_evidence_ref")
    # Where the request names a content digest, the adapter must attest exactly
    # that artifact/corpus, not a successful but unrelated build/deployment.
    requested_version = params.get("artifact_sha256", params.get("corpus_sha256"))
    if requested_version is not None and proof.subject_version != requested_version:
        raise ValueError("subject_version_mismatch")
    metrics: dict[str, int | float | bool] = {}
    if not isinstance(proof.metrics, Mapping) or len(proof.metrics) > 12:
        raise ValueError("invalid_metrics")
    for key, value in proof.metrics.items():
        if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) is None:
            raise ValueError("invalid_metric_name")
        if type(value) not in (int, float, bool) or (type(value) is float and not -1e12 <= value <= 1e12):
            raise ValueError("invalid_metric_value")
        metrics[key] = value
    return {
        # Adapter-provided ref is not a trusted public string: it might contain
        # an Authorization header, a signed URL, or a token unknown to regexes.
        # Return only its opaque stable identifier, never raw driver output.
        "kind": proof.kind, "ref": "opref:" + hashlib.sha256(proof.ref.encode("utf-8")).hexdigest(),
        "subject_version": proof.subject_version, "env_version": proof.env_version,
        "verdict": proof.verdict, "producer": "operator", "operation": operation,
        "request_fingerprint": fingerprint, "metrics": metrics, "observed_ts": observed_ts,
    }


def verify_public_result(result: Any, operation: str) -> dict[str, Any]:
    """Sanitize an IPC response again before exposing it through MCP.

    Never pass through unrecognized metadata, driver stdout or error messages.
    Transport authentication and OS ACLs remain responsibilities of launcher.
    """
    if type(result) is not dict or type(result.get("ok")) is not bool:
        raise ValueError("invalid_operator_result")
    filtered: dict[str, Any] = {"ok": result["ok"]}
    if "error" in result:
        error = result["error"]
        if (type(error) is not dict or type(error.get("code")) is not str
                or re.fullmatch(r"[a-z_]{1,64}", error["code"]) is None):
            raise ValueError("invalid_operator_error")
        filtered["error"] = {"code": error["code"]}
    if "evidence" not in result:
        if result["ok"] or "error" not in filtered:
            raise ValueError("missing_evidence")
        return filtered
    proof = result["evidence"]
    if not isinstance(proof, dict) or proof.get("producer") != "operator":
        raise ValueError("invalid_operator_proof")
    if proof.get("operation") != operation:
        raise ValueError("wrong_operator_proof")
    if proof.get("kind") not in {"run", "measure", "screen", "delivery", "review", "external"}:
        raise ValueError("invalid_operator_proof_kind")
    if proof.get("verdict") not in {"pass", "fail"} or result["ok"] != (proof["verdict"] == "pass"):
        raise ValueError("operator_verdict_mismatch")
    for key in ("subject_version", "env_version", "request_fingerprint"):
        if type(proof.get(key)) is not str or re.fullmatch(r"[0-9a-f]{64}", proof[key]) is None:
            raise ValueError("invalid_operator_proof_version")
    if type(proof.get("ref")) is not str or re.fullmatch(r"opref:[0-9a-f]{64}", proof["ref"]) is None:
        raise ValueError("invalid_operator_proof_ref")
    if type(proof.get("observed_ts")) is not int or proof["observed_ts"] < 0:
        raise ValueError("invalid_operator_proof_timestamp")
    metrics = proof.get("metrics")
    if not isinstance(metrics, dict) or len(metrics) > 12:
        raise ValueError("invalid_operator_metrics")
    clean_metrics: dict[str, int | float | bool] = {}
    for k, v in metrics.items():
        if (type(k) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", k) is None
                or type(v) not in (int, float, bool)
                or (type(v) is float and not -1e12 <= v <= 1e12)):
            raise ValueError("invalid_operator_metric")
        clean_metrics[k] = v
    filtered["evidence"] = {
        "kind": proof["kind"], "ref": proof["ref"],
        "subject_version": proof["subject_version"], "env_version": proof["env_version"],
        "request_fingerprint": proof["request_fingerprint"], "observed_ts": proof["observed_ts"],
        "verdict": proof["verdict"], "producer": "operator", "operation": operation,
        "metrics": clean_metrics,
    }
    return filtered


class Operator:
    """Public handle(operation, params); constructor is trusted/server-only."""

    def __init__(self, budget_path: Path | str, ledger_path: Path | str,
                 adapters: Mapping[str, Adapter] | None = None,
                 clock: Callable[[], float] = time.time):
        self.budgets = BudgetBook.load(budget_path)
        self.ledger = BudgetLedger(ledger_path)
        self.adapters = dict(adapters or {})
        if set(self.adapters) - NAMES:
            raise ValueError("unregistered_adapter")
        self.clock = clock

    def handle(self, operation: str, params: Any) -> dict[str, Any]:
        try:
            validated = validate_operation(operation, params)
            # Inputs are already bounded and contain no arbitrary executable/secret.
            canonical = json.dumps({"version": CATALOG_VERSION, "operation": operation,
                                    "params": dict(validated.params)},
                                   sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            fingerprint = hashlib.sha256(canonical.encode("ascii")).hexdigest()
            now = self.clock()
            grant = self.budgets.authorize(operation, validated.params, now)
            adapter = self.adapters.get(operation)
            if adapter is None:
                raise BudgetError("adapter_unavailable")
            reservation = self.ledger.reserve(fingerprint, grant, self.budgets, now)
            if reservation.cached is not None:
                return reservation.cached
        except (CatalogError, BudgetError) as exc:
            return {"ok": False, "error": {"code": str(exc)}}
        except (OSError, sqlite3.Error):
            # No backend error strings: OS and SQLite messages may include
            # protected paths and are not part of the wire contract.
            return {"ok": False, "error": {"code": "budget_store_failed"}}

        try:
            proof = _safe_evidence(adapter(validated.params), fingerprint, operation,
                                   validated.params, int(now))
            result: dict[str, Any] = {"ok": proof["verdict"] == "pass", "evidence": proof}
        except Exception:
            # Never expose adapter exceptions: they can include access tokens, URLs,
            # HTTP response bodies or full device logs.
            result = {"ok": False, "error": {"code": "adapter_failed"}, "evidence": {
                "kind": "external", "ref": "opref:" + hashlib.sha256(fingerprint.encode("ascii")).hexdigest(),
                "subject_version": fingerprint,
                "env_version": hashlib.sha256(b"operator-v1").hexdigest(), "verdict": "fail",
                "producer": "operator", "operation": operation,
                "request_fingerprint": fingerprint, "metrics": {}, "observed_ts": int(now),
            }}
        try:
            self.ledger.complete(fingerprint, result)
        except (BudgetError, OSError, sqlite3.Error):
            # Fail closed. A running reservation remains charged; no double execution.
            return {"ok": False, "error": {"code": "ledger_write_failed"}}
        return result
