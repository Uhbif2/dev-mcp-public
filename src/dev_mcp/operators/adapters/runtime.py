"""Operator-only effect gateway. Never execute arbitrary shell or accept credentials.

Drivers live in a separate restricted OS process and are injected by its trusted
launcher. They expose named operations rather than arbitrary commands. A signed,
version-pinned backend receipt is mandatory; absence is a failed AdapterEvidence,
not a successful operation.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from dev_mcp.operator_catalog import NAMES, validate_operation
from dev_mcp.operator_core import AdapterEvidence

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}\Z")
_KIND = {
    "android.build_install": "run", "device.screens": "screen",
    "cloud.deploy": "delivery", "eval.live": "measure",
    "ci.run": "run", "second_pc.run": "run", "browser.task": "external",
}


def _digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class ResourceSnapshot:
    resource_id: str
    online: bool
    human_using: bool = False
    free_ram_mib: int = 0
    free_disk_mib: int = 0

    def validate(self) -> None:
        if (not isinstance(self.resource_id, str) or not _ID.fullmatch(self.resource_id)
                or type(self.online) is not bool or type(self.human_using) is not bool
                or type(self.free_ram_mib) is not int or self.free_ram_mib < 0
                or type(self.free_disk_mib) is not int or self.free_disk_mib < 0):
            raise ValueError("invalid_resource_snapshot")


@dataclass(frozen=True)
class BackendReceipt:
    """Driver-attested observation; verifier authenticates ALL fields."""
    operation: str
    request_sha: str
    resource_id: str
    subject_version: str
    env_version: str
    ref: str
    outcome: str  # pass | fail | needs_human
    issuer: str = ""
    signature: str = ""
    commit_sha: str = ""
    artifact_sha256: str = ""
    corpus_sha256: str = ""
    fresh: bool = False
    user_visible: bool = False
    metrics: Mapping[str, int | float | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class BrowserPlan:
    task_id: str
    host: str
    mode: str
    profile_id: str
    instruction: str = ""
    vault_slot: str = ""

    def validate(self) -> None:
        if (not _ID.fullmatch(self.task_id) or not _ID.fullmatch(self.profile_id)
                or self.mode not in {"inspect", "configure"}
                or self.host != self.host.lower()
                or not re.fullmatch(r"[a-z0-9.-]{1,253}", self.host)
                or type(self.instruction) is not str or len(self.instruction) > 1024
                or (self.mode == "configure" and not self.instruction.strip())
                or type(self.vault_slot) is not str
                or (self.vault_slot and not _ID.fullmatch(self.vault_slot))):
            raise ValueError("invalid_browser_plan")


class NamedBackend(Protocol):
    """Trusted, operation-specific implementation; no freeform command channel.

    probe(action) must query an external resource sensor without starting adb
    clients, using the owner's Windows environment, or disrupting the human.
    run(action, params, browser_profile) is bound to this finite operation list
    by the launcher; browser_profile is non-null only for browser.task.
    """

    def probe(self, action: str) -> ResourceSnapshot: ...

    def run(self, action: str, params: Mapping[str, Any],
            browser_profile: Path | None) -> BackendReceipt: ...

    def readback(self, params: Mapping[str, Any]) -> str: ...

    def rollback(self, params: Mapping[str, Any]) -> bool: ...


_DDL = """
CREATE TABLE IF NOT EXISTS adapter_leases(
    resource_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS adapter_receipts(
    fingerprint TEXT PRIMARY KEY, receipt TEXT NOT NULL, receipt_sha TEXT NOT NULL
);
"""


class AdapterRuntime:
    """Fail-closed domain adapters with durable, cross-process resource leases."""

    def __init__(
        self, lease_db: str | Path, backends: Mapping[str, NamedBackend],
        verify_remote: Callable[[BackendReceipt], bool],
        *, profile_root: str | Path, browser_plans: Mapping[str, BrowserPlan] | None = None,
        minimum_ram_mib: int = 512, minimum_disk_mib: int = 512,
    ) -> None:
        self.db = Path(lease_db)
        self.profile_root = Path(profile_root)
        if (not self.db.parent.is_dir() or not self.profile_root.is_absolute()
                or not self.profile_root.is_dir() or self.profile_root.is_symlink()):
            raise ValueError("operator_private_roots_missing")
        if (type(minimum_ram_mib) is not int or minimum_ram_mib < 0
                or type(minimum_disk_mib) is not int or minimum_disk_mib < 0):
            raise ValueError("invalid_resource_limits")
        self.min_ram = minimum_ram_mib
        self.min_disk = minimum_disk_mib
        self.backends = dict(backends)
        if set(self.backends) != NAMES or any(not callable(getattr(b, "probe", None))
                                                or not callable(getattr(b, "run", None))
                                                for b in self.backends.values()):
            raise ValueError("missing_named_backend")
        if not callable(verify_remote):
            raise ValueError("independent_verifier_required")
        self.verify_remote = verify_remote
        self.browser_plans = dict(browser_plans or {})
        for key, plan in self.browser_plans.items():
            plan.validate()
            if key != plan.task_id:
                raise ValueError("plan_key_mismatch")
        with self._db() as conn:
            conn.executescript(_DDL)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _reserve(self, resource_id: str, fingerprint: str) -> tuple[str, AdapterEvidence | None]:
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cached = conn.execute("SELECT receipt,receipt_sha FROM adapter_receipts WHERE fingerprint=?",
                                  (fingerprint,)).fetchone()
            if cached is not None:
                conn.commit()
                try:
                    obj = json.loads(cached[0])
                    if (_digest(obj) != cached[1]
                            or set(obj) != set(AdapterEvidence.__dataclass_fields__)):
                        raise ValueError("bad_cached_receipt")
                    return "cached", AdapterEvidence(**obj)
                except (ValueError, TypeError):
                    return "corrupt", None
            occupied = conn.execute("SELECT 1 FROM adapter_leases WHERE resource_id=? OR fingerprint=?",
                                    (resource_id, fingerprint)).fetchone()
            if occupied:
                conn.commit()
                return "occupied", None
            conn.execute("INSERT INTO adapter_leases VALUES (?,?)", (resource_id, fingerprint))
            conn.commit()
            return "fresh", None

    def _complete(self, resource_id: str, fingerprint: str, proof: AdapterEvidence) -> None:
        obj = asdict(proof)
        wire = json.dumps(obj, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if len(wire) > 8192:
            raise ValueError("receipt_too_large")
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM adapter_leases WHERE resource_id=? AND fingerprint=?",
                                (resource_id, fingerprint)).fetchone():
                conn.rollback()
                raise ValueError("reservation_lost")
            conn.execute("INSERT INTO adapter_receipts VALUES (?,?,?)",
                         (fingerprint, wire, _digest(obj)))
            conn.execute("DELETE FROM adapter_leases WHERE resource_id=? AND fingerprint=?",
                         (resource_id, fingerprint))
            conn.commit()

    @staticmethod
    def _failure(operation: str, fp: str, *, human=False) -> AdapterEvidence:
        return AdapterEvidence(kind=_KIND[operation], ref="failure:" + fp,
                               subject_version=fp,
                               env_version=hashlib.sha256(b"operator-adapter-v1").hexdigest(),
                               verdict="fail", metrics={"needs_human": human})

    @staticmethod
    def _valid_receipt(operation: str, params: Mapping[str, Any], snap: ResourceSnapshot,
                       fp: str, receipt: BackendReceipt) -> bool:
        if (not isinstance(receipt, BackendReceipt) or receipt.operation != operation
                or receipt.request_sha != fp or receipt.resource_id != snap.resource_id
                or receipt.outcome not in {"pass", "fail", "needs_human"}
                or not isinstance(receipt.issuer, str) or not _ID.fullmatch(receipt.issuer)
                or not isinstance(receipt.signature, str) or len(receipt.signature) < 64
                or len(receipt.signature) > 512
                or any(type(value) is not str or not _SHA.fullmatch(value)
                       for value in (receipt.subject_version, receipt.env_version))
                or not isinstance(receipt.ref, str) or not 1 <= len(receipt.ref) <= 256
                or not isinstance(receipt.metrics, Mapping) or len(receipt.metrics) > 12):
            return False
        expected = params.get("artifact_sha256", params.get("corpus_sha256"))
        if expected and receipt.subject_version != expected:
            return False
        if "artifact_sha256" in params and receipt.artifact_sha256 != expected:
            return False
        if "corpus_sha256" in params and receipt.corpus_sha256 != expected:
            return False
        if operation == "ci.run" and (receipt.commit_sha != params["commit_sha"]
                                      or not receipt.fresh or not receipt.user_visible):
            return False
        if operation == "second_pc.run" and (not receipt.fresh or not receipt.user_visible):
            return False
        if operation == "device.screens" and receipt.metrics.get("screens") != params["count"]:
            return False
        if operation == "eval.live" and receipt.metrics.get("samples") != params["sample_count"]:
            return False
        if operation == "browser.task" and receipt.outcome == "pass":
            if receipt.metrics.get("readback") is not True:
                return False
        return True

    def _profile(self, params: Mapping[str, Any]) -> Path:
        plan = self.browser_plans.get(params["task_id"])
        if (plan is None or plan.mode != params["mode"]
                or urlsplit(params["url"]).hostname != plan.host):
            raise ValueError("browser_plan_not_approved")
        profile = self.profile_root / ("isolated-" + plan.profile_id)
        if profile.exists() and profile.is_symlink():
            raise ValueError("profile_symlink")
        if profile.resolve().parent != self.profile_root.resolve():
            raise ValueError("profile_out_of_scope")
        return profile

    def execute(self, operation: str, params: Mapping[str, Any]) -> AdapterEvidence:
        """All errors produce safe failure; exceptions never reveal backend text."""
        validated = validate_operation(operation, dict(params))
        args = dict(validated.params)
        fp = _digest({"operation": operation, "params": args})
        backend = self.backends[operation]
        profile = None
        try:
            if operation == "browser.task":
                profile = self._profile(args)
            snap = backend.probe(operation)
            snap.validate()
            if (not snap.online or snap.human_using or snap.free_ram_mib < self.min_ram
                    or snap.free_disk_mib < self.min_disk):
                return self._failure(operation, fp, human=snap.human_using)
            state, cached = self._reserve(snap.resource_id, fp)
            if state == "cached":
                assert cached is not None
                return cached
            if state != "fresh":
                return self._failure(operation, fp)
        except Exception:
            return self._failure(operation, fp)

        try:
            receipt = backend.run(operation, MappingProxyType(args), profile)
            if not self._valid_receipt(operation, args, snap, fp, receipt):
                raise ValueError("bad_remote_receipt")
            if self.verify_remote(receipt) is not True:
                raise ValueError("unsigned_remote_result")
            if operation == "cloud.deploy":
                # A signed failure can still mean the remote deployment changed
                # state. Route every non-pass receipt through the rollback path.
                if receipt.outcome != "pass":
                    raise ValueError("cloud_remote_deploy_failed")
                if backend.readback(MappingProxyType(args)) != args["artifact_sha256"]:
                    raise ValueError("cloud_readback_mismatch")
            outcome_ok = receipt.outcome == "pass"
            metrics = dict(receipt.metrics)
            if receipt.outcome == "needs_human":
                metrics["needs_human"] = True
            proof = AdapterEvidence(
                kind=_KIND[operation], ref=receipt.ref,
                subject_version=receipt.subject_version,
                env_version=receipt.env_version, verdict="pass" if outcome_ok else "fail",
                metrics=metrics,
            )
        except Exception:
            # No backend errors/HTTP bodies, device logs, tokens or stack traces.
            if operation == "cloud.deploy":
                try:
                    backend.rollback(MappingProxyType(args))
                except Exception:
                    pass  # Never infer success from a failed rollback.
            proof = self._failure(operation, fp)
        try:
            self._complete(snap.resource_id, fp, proof)
        except Exception:
            # Keep lease charged; prevents unsafe re-execution after a DB outage.
            return self._failure(operation, fp)
        return proof


def make_adapters(runtime: AdapterRuntime) -> dict[str, Callable[[Mapping[str, Any]], AdapterEvidence]]:
    """Register only finite known operations with the TZ-12 Operator."""
    return {name: (lambda params, op=name: runtime.execute(op, params)) for name in sorted(NAMES)}
