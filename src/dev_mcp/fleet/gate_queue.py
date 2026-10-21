"""Server-owned snapshot gate queue and signed evidence.

The caller supplies only a snapshot id and a resource profile. Command, runtime,
attestation key and runner are trusted server configuration, not agent reports.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from .gate_snapshot import SnapshotError, SnapshotStore

_RESOURCE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_TARGETS = frozenset({"local", "ci", "second_pc"})
_STATES = frozenset({"queued", "running", "pass", "fail", "cancelled"})
_HEAVY = frozenset({"heavy-build", "gpu", "android-build", "large-memory"})


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class ResourceProfile:
    target: str
    resources: tuple[str, ...]
    heavy: bool = False

    def __post_init__(self) -> None:
        if self.target not in _TARGETS:
            raise ValueError("unrecognized gate target")
        if not self.resources or any(not isinstance(r, str) or not _RESOURCE.fullmatch(r)
                                     for r in self.resources):
            raise ValueError("resources must be nonempty, typed identifiers")
        if tuple(sorted(set(self.resources))) != self.resources:
            raise ValueError("resource identifiers must be unique and sorted")
        if not isinstance(self.heavy, bool):
            raise ValueError("heavy must be a boolean")
        if self.target == "local" and (self.heavy or set(self.resources) & _HEAVY):
            raise ValueError("heavy gate requires ci or second_pc")

    @classmethod
    def parse(cls, value: ResourceProfile | Mapping[str, object]) -> ResourceProfile:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) != {"target", "resources", "heavy"}:
            raise ValueError("invalid resource profile schema")
        resources = value["resources"]
        if (not isinstance(value["target"], str) or
                not isinstance(value["heavy"], bool) or
                not isinstance(resources, (list, tuple)) or
                not all(isinstance(r, str) for r in resources)):
            raise ValueError("resources must be a sequence")
        return cls(value["target"], tuple(resources), value["heavy"])  # type: ignore[arg-type]


@dataclass(frozen=True)
class GateEvidence:
    job_id: str
    snapshot_id: str
    subject_version: str
    env_version: str
    target: str
    resources: tuple[str, ...]
    state: str
    verdict: str
    exit_code: int
    runner: str
    artifact_path: str
    stdout_sha256: str
    stderr_sha256: str
    created_ts: float
    finished_ts: float
    digest: str
    signature: str


Runner = Callable[[Path, tuple[str, ...], float], subprocess.CompletedProcess[bytes]]


def _local_runner(workdir: Path, argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[bytes]:
    # Never inherit credentials from the server process into untrusted gate code.
    allow = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
             "PYTHONIOENCODING", "PYTHONUTF8", "LANG")
    env = {key: os.environ[key] for key in allow if key in os.environ}
    return subprocess.run(argv, cwd=workdir, capture_output=True, timeout=timeout,
                          check=False, env=env)


class GateQueue:
    """Durable FIFO with SQLite resource leasing and HMAC attestation.

    No background thread is started: a trusted server scheduler calls run_next().
    External executors are adapters supplied by TZ-12; no fallback to local.
    """

    def __init__(self, snapshots: SnapshotStore, state_dir: str | Path, *,
                 command: Sequence[str], env_version: str,
                 timeout_s: float = 180.0,
                 local_runner: Runner | None = None,
                 remote_runners: Mapping[str, Runner] | None = None):
        if not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError("server gate command must be nonempty")
        if not isinstance(env_version, str) or not env_version.strip():
            raise ValueError("env_version is required")
        if timeout_s <= 0:
            raise ValueError("invalid timeout")
        self.snapshots = snapshots
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "gates.sqlite3"
        self.command = tuple(command)
        self.env_version = env_version
        self.timeout_s = timeout_s
        self.local_runner = local_runner or _local_runner
        self.remote_runners = dict(remote_runners or {})
        self.key_path = self.root / "attestation.key"
        if not self.key_path.exists():
            try:
                with self.key_path.open("xb") as out:
                    out.write(secrets.token_bytes(32))
            except FileExistsError:
                pass
            self.key_path.chmod(0o600)
        self._secret = self.key_path.read_bytes()
        if len(self._secret) != 32:
            raise ValueError("invalid attestation key")
        with self._tx() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS gate_jobs (
                job_id TEXT PRIMARY KEY, request_digest TEXT UNIQUE NOT NULL,
                snapshot_id TEXT NOT NULL, profile_json TEXT NOT NULL,
                env_version TEXT NOT NULL, state TEXT NOT NULL,
                created_ts REAL NOT NULL, lease_until REAL, lease_token TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT
            )""")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def submit_gate(self, snapshot_id: str,
                    resource_profile: ResourceProfile | Mapping[str, object]) -> str:
        """submit-gate(snapshot_id, resource_profile), idempotent by request digest."""
        self.snapshots.verify(snapshot_id)
        profile = ResourceProfile.parse(resource_profile)
        request = {"snapshot_id": snapshot_id, "profile": asdict(profile),
                   "env_version": self.env_version, "command": self.command}
        digest = hashlib.sha256(_json(request)).hexdigest()
        job_id = "gate-" + digest[:24]
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO gate_jobs("
                         "job_id,request_digest,snapshot_id,profile_json,env_version,state,created_ts)"
                         " VALUES(?,?,?,?,?,'queued',?)",
                         (job_id, digest, snapshot_id, _json(asdict(profile)).decode(),
                          self.env_version, time.time()))
        return job_id

    def job(self, job_id: str) -> dict[str, object]:
        if not isinstance(job_id, str) or not re.fullmatch(r"gate-[0-9a-f]{24}", job_id):
            raise ValueError("invalid job id")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM gate_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def recover_stale(self, *, now: float | None = None) -> int:
        """Only expired leases are requeued; still-running gates retain resources."""
        with self._tx() as conn:
            result = conn.execute(
                "UPDATE gate_jobs SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' "
                "ELSE 'queued' END, lease_until=NULL, lease_token=NULL "
                "WHERE state='running' AND lease_until < ?",
                (time.time() if now is None else now,))
            return result.rowcount

    def cancel(self, job_id: str) -> bool:
        with self._tx() as conn:
            row = conn.execute("SELECT state FROM gate_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["state"] in {"pass", "fail", "cancelled"}:
                return False
            if row["state"] == "queued":
                conn.execute("UPDATE gate_jobs SET state='cancelled' WHERE job_id=?", (job_id,))
            else:
                # Keep the resource leased until the runner returns or lease expires.
                conn.execute("UPDATE gate_jobs SET cancel_requested=1 WHERE job_id=?", (job_id,))
        return True

    def _reserve(self) -> tuple[dict[str, object], ResourceProfile, str] | None:
        self.recover_stale()
        with self._tx() as conn:
            rows = conn.execute("SELECT * FROM gate_jobs ORDER BY created_ts, job_id").fetchall()
            busy = set()
            for row in rows:
                if row["state"] == "running":
                    busy.update(json.loads(row["profile_json"])["resources"])
            for row in rows:
                if row["state"] != "queued":
                    continue
                profile = ResourceProfile.parse(json.loads(row["profile_json"]))
                if set(profile.resources) & busy:
                    continue
                if profile.target != "local" and profile.target not in self.remote_runners:
                    continue  # TZ-12 adapter not installed: never silently run heavy locally
                token = uuid.uuid4().hex
                conn.execute("UPDATE gate_jobs SET state='running', lease_token=?, lease_until=? "
                             "WHERE job_id=? AND state='queued'",
                             (token, time.time() + self.timeout_s + 60, row["job_id"]))
                return dict(row), profile, token
        return None

    def run_next(self) -> GateEvidence | None:
        selected = self._reserve()
        if selected is None:
            return None
        row, profile, token = selected
        snapshot_id, job_id = str(row["snapshot_id"]), str(row["job_id"])
        rc = -1
        stdout = b""
        stderr = b""
        integrity_ok = False
        runner = self.local_runner if profile.target == "local" else self.remote_runners[profile.target]
        try:
            self.snapshots.verify(snapshot_id)
            with tempfile.TemporaryDirectory(prefix="gate-exec-") as temp:
                work = self.snapshots.materialize(snapshot_id, Path(temp) / "work")
                try:
                    completed = runner(work, self.command, self.timeout_s)
                    rc = int(completed.returncode)
                    stdout = _as_bytes(completed.stdout)
                    stderr = _as_bytes(completed.stderr)
                except subprocess.TimeoutExpired as exc:
                    stderr = b"gate timeout: " + _as_bytes(str(exc))
            self.snapshots.verify(snapshot_id)
            integrity_ok = True
        except (Exception) as exc:
            stderr += (" gate execution/integrity failure: " + type(exc).__name__ + ": " + str(exc)).encode()
        # Artifacts are scoped by lease token so a stale runner cannot overwrite retry evidence.
        relative = f"artifacts/{job_id}/{token}"
        folder = self.root / relative
        folder.mkdir(parents=True, exist_ok=False)
        stdout, stderr = stdout[:1_000_000], stderr[:1_000_000]
        (folder / "stdout.bin").write_bytes(stdout)
        (folder / "stderr.bin").write_bytes(stderr)
        with self._tx() as conn:
            live = conn.execute("SELECT state, lease_token, cancel_requested FROM gate_jobs "
                                "WHERE job_id=?", (job_id,)).fetchone()
            if live is None or live["state"] != "running" or live["lease_token"] != token:
                return None  # lease expired and a new runner owns the request
            status = ("cancelled" if live["cancel_requested"] else
                      "pass" if integrity_ok and rc == 0 else "fail")
            now = time.time()
            unsigned = {
                "job_id": job_id, "snapshot_id": snapshot_id,
                "subject_version": snapshot_id, "env_version": self.env_version,
                "target": profile.target, "resources": profile.resources,
                "state": status, "verdict": status if status in {"pass", "fail"} else "cancelled",
                "exit_code": rc, "runner": "server:" + profile.target,
                "artifact_path": relative,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                "created_ts": float(row["created_ts"]), "finished_ts": now,
            }
            digest = hashlib.sha256(_json(unsigned)).hexdigest()
            signature = hmac.new(self._secret, digest.encode("ascii"), hashlib.sha256).hexdigest()
            evidence = GateEvidence(**unsigned, digest=digest, signature=signature)
            (folder / "evidence.json").write_bytes(_json(asdict(evidence)))
            conn.execute("UPDATE gate_jobs SET state=?, evidence_json=?, lease_until=NULL "
                         "WHERE job_id=? AND lease_token=?",
                         (status, _json(asdict(evidence)).decode(), job_id, token))
        return evidence

    def verify_evidence(self, job_id: str) -> GateEvidence:
        row = self.job(job_id)
        if row["state"] not in {"pass", "fail", "cancelled"} or not row["evidence_json"]:
            raise ValueError("no final gate evidence")
        record = json.loads(row["evidence_json"])
        evidence = GateEvidence(**{**record, "resources": tuple(record["resources"])})
        unsigned = {key: value for key, value in record.items() if key not in {"digest", "signature"}}
        digest = hashlib.sha256(_json(unsigned)).hexdigest()
        signature = hmac.new(self._secret, digest.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(evidence.digest, digest) or not hmac.compare_digest(evidence.signature, signature):
            raise ValueError("gate attestation mismatch")
        if evidence.snapshot_id != row["snapshot_id"] or evidence.env_version != row["env_version"]:
            raise ValueError("gate version mismatch")
        profile = ResourceProfile.parse(json.loads(row["profile_json"]))
        if evidence.target != profile.target or evidence.resources != profile.resources:
            raise ValueError("gate resource profile mismatch")
        if evidence.state != row["state"] or not re.fullmatch(
                r"artifacts/gate-[0-9a-f]{24}/[0-9a-f]{32}", evidence.artifact_path):
            raise ValueError("gate state or artifact path mismatch")
        folder = self.root / evidence.artifact_path
        if (folder / "evidence.json").read_bytes() != _json(record):
            raise ValueError("evidence artifact modified")
        for name, expected in [("stdout.bin", evidence.stdout_sha256),
                               ("stderr.bin", evidence.stderr_sha256)]:
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
                raise ValueError("gate log artifact modified")
        # A failed gate remains a verifiable failure even when its snapshot was
        # corrupted. A passing gate can only be trusted while bytes still match.
        if evidence.state == "pass":
            self.snapshots.verify(evidence.snapshot_id)
        return evidence


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if value is None:
        return b""
    return str(value).encode("utf-8", errors="replace")
