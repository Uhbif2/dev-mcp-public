"""Conservative, evidence-backed fault attribution for snapshot integration gates.

The caller is the trusted server controller, not an agent report. In particular:
* baseline_job_id is a server-pinned, previously attested gate;
* replay executes through the server's resource-leased gate scheduler;
* zones come from the server's owns registry, never stdout/stderr;
* create_repair is a durable idempotent enqueue function.

A single failed run is insufficient to blame any zone: two independent replay
attempts per snapshot must reproduce the exact signed output fingerprints.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, ContextManager, Sequence
import uuid

from .gate_queue import GateEvidence, GateQueue, ResourceProfile


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class OwnerZone:
    """Trusted path ownership, expressed relative to the snapshot root."""

    task_id: str
    owns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.owns:
            raise ValueError("owner task and paths required")
        for name in self.owns:
            if (not isinstance(name, str) or not name or name.startswith("/")
                    or "\\" in name or ":" in name
                    or any(part in {"", ".", ".."} for part in name.split("/"))):
                raise ValueError("invalid relative owns path")


@dataclass(frozen=True)
class ReplayProof:
    """A real, newly scheduled gate run; verified against its own attestation."""

    queue: GateQueue
    job_id: str


@dataclass(frozen=True)
class RepairRequest:
    request_id: str
    cause_code: str
    responsible_task: str
    owns: tuple[str, ...]
    current_job_id: str
    baseline_job_id: str
    current_digest: str
    baseline_digest: str


@dataclass(frozen=True)
class FaultAttribution:
    cause_code: str  # unknown | gate_red_own | gate_red_foreign
    reason_code: str
    current: GateEvidence | None
    baseline: GateEvidence | None
    baseline_replays: tuple[GateEvidence, ...]
    current_replays: tuple[GateEvidence, ...]
    changed_paths: tuple[str, ...]
    repair: RepairRequest | None

    def evidence_record(self) -> dict[str, object]:
        """Serializable evidence for the controller/journal, not prose parsing."""
        return asdict(self)


class FaultEvidenceJournal:
    """Content-addressed, atomically written verdicts including unknowns."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def write(self, attribution: FaultAttribution) -> Path:
        payload = _canonical(attribution.evidence_record())
        digest = hashlib.sha256(payload).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        final = self.root / (digest + ".json")
        if final.exists():
            if final.read_bytes() != payload:
                raise ValueError("fault journal digest collision or corruption")
            return final
        stage = self.root / ("." + uuid.uuid4().hex + ".tmp")
        try:
            with stage.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stage, final)
        finally:
            stage.unlink(missing_ok=True)
        return final


Replay = Callable[[str, ResourceProfile, tuple[str, ...], str], ReplayProof]
RepairSink = Callable[[RepairRequest], None]


class LeasedGateReplayer:
    """Actually execute fresh signed trials, holding the controller's lease.

    The lease adapter is mandatory: independent GateQueue databases must never
    be mistaken for a global resource lock. This class has no subprocess
    fallback and cannot run an unavailable remote target on the local machine.
    """

    def __init__(self, gates: GateQueue, root: Path, *,
                 lease_resources: Callable[[ResourceProfile], ContextManager[object]]) -> None:
        self.gates = gates
        self.root = Path(root)
        self.lease_resources = lease_resources

    def __call__(self, snapshot_id: str, profile: ResourceProfile,
                 command: tuple[str, ...], env_version: str) -> ReplayProof:
        if command != self.gates.command or env_version != self.gates.env_version:
            raise ValueError("cannot replay a different gate command or environment")
        with self.lease_resources(profile):
            attempt = GateQueue(
                self.gates.snapshots, self.root / ("attempt-" + uuid.uuid4().hex),
                command=command, env_version=env_version,
                timeout_s=self.gates.timeout_s, local_runner=self.gates.local_runner,
                remote_runners=self.gates.remote_runners,
            )
            job_id = attempt.submit_gate(snapshot_id, profile)
            if attempt.run_next() is None:
                raise RuntimeError("requested runner/resource unavailable")
            attempt.verify_evidence(job_id)
        return ReplayProof(attempt, job_id)


def _fingerprint(ev: GateEvidence) -> tuple[object, ...]:
    # Signed structured fields only. Log contents are never read or tokenized.
    return ev.state, ev.exit_code, ev.stdout_sha256, ev.stderr_sha256


def _profile(queue: GateQueue, job_id: str) -> ResourceProfile:
    row = queue.job(job_id)
    profile = ResourceProfile.parse(json.loads(str(row["profile_json"])))
    request = {"snapshot_id": row["snapshot_id"], "profile": asdict(profile),
               "env_version": row["env_version"], "command": queue.command}
    if hashlib.sha256(_canonical(request)).hexdigest() != row["request_digest"]:
        raise ValueError("gate command or profile differs from attested request")
    return profile


def _changed_files(queue: GateQueue, before: str, after: str) -> tuple[str, ...]:
    store = queue.snapshots
    store.verify(before)
    store.verify(after)
    def entries(snapshot_id: str) -> dict[str, str]:
        manifest = json.loads((store.root / snapshot_id / "manifest.json").read_text(encoding="utf-8"))
        return {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    a, b = entries(before), entries(after)
    return tuple(sorted(name for name in a.keys() | b.keys() if a.get(name) != b.get(name)))


def _owner_for(changes: tuple[str, ...], zones: Sequence[OwnerZone]) -> OwnerZone | None:
    if not changes:
        return None
    owners: set[str] = set()
    selected: OwnerZone | None = None
    for path in changes:
        candidates = [z for z in zones
                      if any(path == prefix or path.startswith(prefix + "/") for prefix in z.owns)]
        if len(candidates) != 1:
            return None
        selected = candidates[0]
        owners.add(selected.task_id)
    return selected if len(owners) == 1 else None


class GateFaultAttributor:
    """Runs attested baseline/current replays before assigning a repair.

    replay is a trusted, resource-aware executor which creates a new GateQueue
    attempt per call under replay_root; never a direct subprocess fallback.
    The distinct queue roots defeat GateQueue's idempotent job reuse. The
    controller must hold the requested resources across each replay.
    """

    def __init__(self, gates: GateQueue, replay_root: Path,
                 *, replay: Replay, create_repair: RepairSink) -> None:
        self.gates = gates
        self.replay_root = Path(replay_root).resolve()
        self.replay = replay
        self.create_repair = create_repair
        self.journal = FaultEvidenceJournal(self.replay_root / "_fault_records")

    def attribute(self, *, current_job_id: str, baseline_job_id: str | None,
                  reporting_task: str, zones: Sequence[OwnerZone]) -> FaultAttribution:
        current: GateEvidence | None = None
        baseline: GateEvidence | None = None
        b_runs: list[GateEvidence] = []
        c_runs: list[GateEvidence] = []
        changes: tuple[str, ...] = ()

        def result(code: str, reason: str, repair: RepairRequest | None = None) -> FaultAttribution:
            finding = FaultAttribution(code, reason, current, baseline, tuple(b_runs),
                                       tuple(c_runs), changes, repair)
            self.journal.write(finding)
            return finding

        try:
            current = self.gates.verify_evidence(current_job_id)
            current_profile = _profile(self.gates, current_job_id)
        except (ValueError, KeyError, OSError, TypeError):
            return result("unknown", "current_unverified")
        if current.state != "fail":
            return result("unknown", "current_not_red")
        if current.exit_code < 0:
            return result("unknown", "current_execution_failure")
        if not baseline_job_id:
            return result("unknown", "baseline_missing")
        try:
            baseline = self.gates.verify_evidence(baseline_job_id)
            baseline_profile = _profile(self.gates, baseline_job_id)
        except (ValueError, KeyError, OSError, TypeError):
            return result("unknown", "baseline_unverified")
        if baseline.subject_version == current.subject_version:
            return result("unknown", "baseline_not_distinct")
        if baseline.finished_ts > current.created_ts:
            return result("unknown", "baseline_not_prior")
        if baseline.env_version != current.env_version:
            return result("unknown", "environment_mismatch")
        if baseline_profile != current_profile:
            return result("unknown", "resource_mismatch")
        if baseline.state != "pass":
            return result("unknown", "baseline_not_green")

        # Fail closed on unsupported/ambiguous ownership before expensive work.
        try:
            changes = _changed_files(self.gates, baseline.snapshot_id, current.snapshot_id)
        except (ValueError, OSError, TypeError, KeyError):
            return result("unknown", "snapshot_diff_unverified")
        owner = _owner_for(changes, zones)
        if owner is None:
            return result("unknown", "owners_ambiguous")
        if not reporting_task or not reporting_task.strip():
            return result("unknown", "reporting_task_missing")

        # Replays must be independently signed and run after the original
        # evidence. Require distinct attestation-state roots and matching
        # command, environment, resources, and subject version.
        used = {(str(self.gates.root.resolve()), baseline_job_id),
                (str(self.gates.root.resolve()), current_job_id)}
        for snap, target in ((baseline.snapshot_id, b_runs),
                             (current.snapshot_id, c_runs)):
            for _ in range(2):
                try:
                    proof = self.replay(snap, current_profile, self.gates.command, self.gates.env_version)
                    q = proof.queue
                    identity = (str(q.root.resolve()), proof.job_id)
                    if (identity in used or q.root.resolve().parent != self.replay_root
                            or q.command != self.gates.command or q.env_version != self.gates.env_version
                            or q.timeout_s != self.gates.timeout_s
                            or q.snapshots.root.resolve() != self.gates.snapshots.root.resolve()):
                        return result("unknown", "replay_context_mismatch")
                    used.add(identity)
                    ev = q.verify_evidence(proof.job_id)
                    if (ev.snapshot_id != snap or ev.subject_version != snap
                            or _profile(q, proof.job_id) != current_profile
                            or ev.created_ts < current.finished_ts):
                        return result("unknown", "replay_context_mismatch")
                    target.append(ev)
                except (ValueError, KeyError, OSError, TypeError, AttributeError, RuntimeError):
                    return result("unknown", "replay_unverified")

        if any(ev.state != "pass" or ev.exit_code != 0 for ev in b_runs):
            return result("unknown", "baseline_unstable")
        if any(_fingerprint(ev) != _fingerprint(b_runs[0]) for ev in (baseline, *b_runs)):
            return result("unknown", "baseline_nondeterministic")
        if any(_fingerprint(ev) != _fingerprint(current) for ev in c_runs):
            return result("unknown", "current_nondeterministic")

        code = "gate_red_own" if owner.task_id == reporting_task else "gate_red_foreign"
        request = {
            "cause_code": code, "responsible_task": owner.task_id,
            "owns": changes, "current_job_id": current_job_id,
            "baseline_job_id": baseline_job_id, "current_digest": current.digest,
            "baseline_digest": baseline.digest,
        }
        key = hashlib.sha256(_canonical(request)).hexdigest()
        repair = RepairRequest("repair-" + key[:24], **request)
        try:
            self.create_repair(repair)  # durable sink must deduplicate request_id
        except Exception:
            return result("unknown", "repair_enqueue_failed")
        return result(code, "stable_regression", repair)
