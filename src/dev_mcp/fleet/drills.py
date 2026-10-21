"""Daily self-recovery drills, restricted to ephemeral synthetic test projects.

The runner is NOT a production recovery probe. Real-fleet scheduling/wiring is
a separate owner of code; this module exports the safe daily callable contract.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .drill_fixtures import (
    EXPECTED, FIXTURES, FaultFixture, apply_recovery, diagnose, fingerprint, observe, recheck_domain,
    write_state,
)


class DrillSafetyError(ValueError):
    """The attempted drill is outside the disposable sandbox."""


@dataclass(frozen=True)
class RepairSignal:
    """Untrusted work report, which cannot by itself establish recovery."""
    claim: str
    subject_version: str
    wake_deadline: datetime | None = None
    route: tuple[str, ...] = ()


Repair = Callable[[Path, FaultFixture, str], RepairSignal | None]
Clock = Callable[[], float]
Cleaner = Callable[[Path], None]

SCHEDULER_CONTRACT = {
    "cadence": "daily",
    "timezone": "UTC",
    "entrypoint": "run_daily",
    "root": "isolated OS-temp/fleet-drill-sandbox directory",
    "deduplication": "one durable report per UTC date, atomic exclusive lease",
    "expired_lease": "do not steal; report operator-visible capability_gap",
    "effect": "synthetic fixture only; never run against live controller or state",
}


def _environment_version() -> str:
    """Content-address ALL code determining the drill's domain verdict."""
    from . import controller, drill_fixtures, gate_fault, gate_queue, gate_snapshot, outcome, world

    digest = hashlib.sha256()
    for module in (controller, outcome, drill_fixtures, gate_fault, gate_queue, gate_snapshot, world):
        digest.update(Path(module.__file__).read_bytes())
    digest.update(Path(__file__).read_bytes())
    return (f"synthetic-drill-v1/py{sys.version_info.major}.{sys.version_info.minor}/"
            f"sha256:{digest.hexdigest()}")


def _check_root(root: str | Path) -> Path:
    root = Path(root)
    temp = Path(tempfile.gettempdir()).resolve()
    if root.name != "fleet-drill-sandbox":
        raise DrillSafetyError("root must be named fleet-drill-sandbox")
    # Reject link traversal including a symlink at the requested final path.
    candidate = root.absolute()
    if candidate.is_symlink() or candidate.resolve() != candidate:
        raise DrillSafetyError("symlink/reparse point in drill root")
    if not candidate.is_relative_to(temp) or candidate == temp:
        raise DrillSafetyError("drill root must be inside OS temporary directory")
    for parent in (candidate, *candidate.parents):
        if parent == temp:
            break
        if parent.is_symlink():
            raise DrillSafetyError("symlink ancestor")
    return candidate


def _record_digest(record: dict[str, object]) -> str:
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("an aware timestamp is required")
    return now.astimezone(timezone.utc)


def _owned_cleanup(project: Path, root: Path, token: str, cleaner: Cleaner) -> None:
    # Only an exact newly created project with its original token is removable.
    if (project.parent != root or not project.name.startswith("case-")
            or project.is_symlink() or project.resolve().parent != root
            or (project / ".drill-owner").is_symlink()
            or (project / ".drill-owner").read_text(encoding="utf-8") != token):
        raise DrillSafetyError("cleanup ownership check failed")
    for child in project.rglob("*"):
        if child.is_symlink():
            raise DrillSafetyError("refuse cleanup with symlink")
    cleaner(project)


def _synthetic_cleaner(project: Path) -> None:
    """Remove only a previously owner-validated case, including read-only snapshots."""
    def writable_then_retry(fn, path, error):
        if isinstance(error, PermissionError):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            fn(path)
        else:
            raise error
    shutil.rmtree(project, onexc=writable_then_retry)


def _default_repair(state_root: Path, fixture: FaultFixture, version: str) -> RepairSignal | None:
    """Exercise the REAL controller against a disposable SQLite registry.

    The executor is synthetic: it consumes the work produced by the actual
    Controller and repairs only this fixture's private state directory.
    """
    from .store import Store

    project = state_root.parent
    store = Store.open(str(project / "fleet.sqlite"))
    try:
        return _drive_controller(store, state_root, fixture, version)
    finally:
        # Windows cannot remove a WAL database while Store's pooled connections
        # remain open; failed drills must still be able to clean up safely.
        store.close()


def _gate_replay_proof(project: Path, state_root: Path, fixture: FaultFixture):
    """Run signed baseline/current gate and 2+2 leased replays in private paths.

    Returns a callable for a second, independently signed gate after the fix.
    The runner is a fake supplier, while GateQueue/GateFaultAttributor and their
    attested evidence pipeline are the actual production implementation.
    """
    from .gate_snapshot import SnapshotStore
    from .gate_queue import GateQueue, ResourceProfile
    from .gate_fault import GateFaultAttributor, LeasedGateReplayer, OwnerZone

    healthy = project / "gate-baseline"
    healthy.mkdir()
    write_state(healthy, fixture.healthy)
    snapshots = SnapshotStore(project / "gate-snapshots")
    base = snapshots.create(healthy, include=("gate/status",))
    head = snapshots.create(state_root, include=("gate/status",))
    def runner(work, argv, timeout):
        val = (work / "gate/status").read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0 if val == "pass\n" else 1,
                                           b"gate-pass" if val == "pass\n" else b"",
                                           b"" if val == "pass\n" else b"gate-red")
    command = ("synthetic-gate", "--verify")
    gates = GateQueue(snapshots, project / "gate-primary", command=command,
                      env_version="drill-signed-gate-v1", local_runner=runner)
    profile = ResourceProfile("local", ("cpu",))
    baseline_job = gates.submit_gate(base.snapshot_id, profile)
    if gates.run_next() is None or gates.verify_evidence(baseline_job).state != "pass":
        return None
    current_job = gates.submit_gate(head.snapshot_id, profile)
    if gates.run_next() is None or gates.verify_evidence(current_job).state != "fail":
        return None
    @contextmanager
    def fake_global_lease(_profile):
        yield
    replay_root = project / "gate-replays"
    repairs = []
    fault = GateFaultAttributor(
        gates, replay_root,
        replay=LeasedGateReplayer(gates, replay_root, lease_resources=fake_global_lease),
        create_repair=repairs.append,
    ).attribute(current_job_id=current_job, baseline_job_id=baseline_job,
                reporting_task="drill", zones=[OwnerZone("drill", ("gate/status",))])
    if (fault.cause_code != "gate_red_own" or len(repairs) != 1 or
            repairs[0].owns != ("gate/status",) or
            len(fault.baseline_replays) != 2 or len(fault.current_replays) != 2):
        return None

    def recheck() -> bool:
        restored = snapshots.create(state_root, include=("gate/status",))
        if restored.snapshot_id != base.snapshot_id:
            return False
        after_queue = GateQueue(snapshots, project / "gate-after",
                                command=command, env_version="drill-signed-gate-v1",
                                local_runner=runner)
        job_id = after_queue.submit_gate(restored.snapshot_id, profile)
        return (after_queue.run_next() is not None and
                after_queue.verify_evidence(job_id).state == "pass")
    return recheck


def _drive_controller(store, state_root: Path, fixture: FaultFixture,
                      version: str) -> RepairSignal | None:
    from . import outcome, world
    from .controller import Controller

    diagnosis = diagnose(state_root, fixture)
    if diagnosis is None:
        return None
    gate_recheck = (_gate_replay_proof(state_root.parent, state_root, fixture)
                    if fixture.name == "red_gate" else None)
    if fixture.name == "red_gate" and gate_recheck is None:
        return None

    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("drill", str(state_root)), event="drill.seed")
    outcome.create_outcome(
        store, project="drill", title=f"Recover {fixture.name}", intent_id="synthetic-drill",
        spec={"expected": fixture.expected_outcome, "fixture": fixture.name,
              "cause_code": diagnosis.cause_code, "fault_marker": diagnosis.fault_marker},
        evidence_spec={"kind": diagnosis.evidence_kind, "subject_version": version},
        authority={"root": str(state_root), "synthetic": True},
        outcome_id="drill-outcome",
    )
    obstacle = outcome.open_obstacle(
        store, outcome_id="drill-outcome", type=diagnosis.obstacle_type,
        cause_code=diagnosis.cause_code,
        predicate={"resource": "drill-worker"} if fixture.name == "resource_missing"
                  else {"fault": diagnosis.fault_marker, "version": version},
        change_source=world.path_key("drill-worker") if fixture.name == "resource_missing" else None,
        obstacle_id="drill-obstacle",
    )
    with store.read() as tx:
        row = tx.one("SELECT cause_code,state FROM obstacles WHERE id=?", (obstacle,))
    if row["cause_code"] != diagnosis.cause_code or row["state"] != "open":
        return None
    route = [f"detect:{fixture.name}:{diagnosis.cause_code}", "controller:typed_obstacle"]
    controller = Controller(store, maintenance=lambda: None)
    if fixture.name == "resource_missing":
        # Exercise the real resource-transition event and source-action path.
        world.record(store, {"drill-worker": (world.DOWN, "synthetic-supplier-absent")})
        with store.read() as tx:
            if world.last_state(tx, "drill-worker")[0] != world.DOWN:
                return None
        controller.tick()
        if outcome.next_step(store, "drill-outcome").kind != "wait":
            return None
        if not apply_recovery(state_root, fixture, diagnosis):
            return None
        world.record(store, {"drill-worker": (world.UP, "synthetic-supplier-restored")})
        controller.tick()
        with store.read() as tx:
            state = tx.scalar("SELECT state FROM obstacles WHERE id=?", (obstacle,))
        if state != "resolved":
            return None
        route.append("world:resource_down_to_up")
    else:
        # For gate/format/blocker/delivery the actual Controller must create
        # executable work from the typed defect; no synthetic task is seeded.
        controller.tick()
    # A resolved resource wait needs one further tick to plan the remaining proof.
    controller.tick()
    with store.read() as tx:
        tasks = tx.query(
            "SELECT t.project,t.tz,t.state FROM outcome_work w JOIN tasks t "
            "ON w.project=t.project AND w.tz=t.tz WHERE w.outcome_id='drill-outcome'"
        )
    if len(tasks) != 1 or tasks[0]["state"] != "ready":
        return None
    route.append(f"controller:routed_work:{tasks[0]['tz']}")
    # Synthetic effect only after a real Controller task is ready. Red-gate
    # recheck below invokes signed GateQueue, not just a textual status claim.
    if fixture.name != "resource_missing" and not apply_recovery(state_root, fixture, diagnosis):
        return None
    actual, correct = observe(state_root, fixture)
    if (not recheck_domain(state_root, fixture, diagnosis) or
            not correct or actual != fingerprint(fixture.healthy) or
            (gate_recheck is not None and not gate_recheck())):
        return None
    if fixture.name != "resource_missing":
        outcome.resolve_obstacle(store, obstacle)
    with store.read() as tx:
        if tx.scalar("SELECT state FROM obstacles WHERE id=?", (obstacle,)) != "resolved":
            return None
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project=? AND tz=?",
                  (tasks[0]["project"], tasks[0]["tz"]), event="drill.synthetic_executor")
    outcome.add_evidence(
        store, writer=outcome.server_evidence_writer(store), outcome_id="drill-outcome",
        kind=diagnosis.evidence_kind, ref=f"drill://{fixture.name}/{actual}",
        subject_version=version, env_version=_environment_version(),
        verdict="pass", producer="independent_fixture_probe",
    )
    controller.tick()
    if outcome.get_outcome(store, "drill-outcome").state != "done":
        return None
    route.extend((f"synthetic_supplier:{fixture.name}", f"independent_domain_probe:{fixture.name}",
                  f"server_evidence:{diagnosis.evidence_kind}", "controller:close_verified_outcome"))
    return RepairSignal("repaired", version, route=tuple(route))


def run_scenario(
    root: str | Path,
    fixture: FaultFixture,
    *,
    repair: Repair | None = None,
    baseline_hook: Callable[[Path], None] | None = None,
    cleaner: Cleaner = _synthetic_cleaner,
    clock: Clock = time.monotonic,
    now: datetime | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """Exercise one fault; only the independent probe may produce a pass.

    Repair is synchronous and must return promptly; orchestration must enforce
    an external worker deadline when integrating actual agents. Timeouts after
    a returning callback are recorded as gaps, never as success.
    """
    root = _check_root(root)
    if not root.is_dir():
        raise DrillSafetyError("sandbox directory must already exist")
    if fixture not in FIXTURES:
        raise DrillSafetyError("unregistered fixture")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    now = _utc(now)
    project = Path(tempfile.mkdtemp(prefix="case-", dir=root))
    state_root = project / "state"
    state_root.mkdir()
    token = uuid.uuid4().hex
    (project / ".drill-owner").write_text(token, encoding="utf-8")
    start = clock()
    env_version = _environment_version()
    baseline_version = fingerprint(fixture.healthy)
    result: dict[str, object] = {
        "scenario": fixture.name,
        "synthetic": True,
        "expected_outcome": fixture.expected_outcome,
        "route": list(fixture.route),
        "baseline_version": baseline_version,
        "fault_version": None,
        "subject_version": None,
        "env_version": env_version,
        "evidence": [],
        "status": "capability_gap",
        "capability_gap": None,
        "elapsed_seconds": 0.0,
        "cleanup": "pending",
    }
    try:
        write_state(state_root, fixture.healthy)
        if baseline_hook is not None:
            baseline_hook(state_root)
        observed, healthy = observe(state_root, fixture)
        if not healthy or observed != baseline_version:
            result["capability_gap"] = "baseline_failed"
        else:
            write_state(state_root, fixture.broken)
            fault_version = fingerprint(fixture.broken)
            recorded, repaired = observe(state_root, fixture)
            result["fault_version"] = fault_version
            result["subject_version"] = fault_version
            diagnosis = diagnose(state_root, fixture)
            if repaired or recorded != fault_version or diagnosis is None:
                result["capability_gap"] = "fault_injection_failed"
            else:
                result["evidence"] = [
                    {"stage": "baseline", "digest": baseline_version, "subject_version": baseline_version,
                     "env_version": env_version, "producer": "independent_probe"},
                    {"stage": "fault", "digest": recorded, "subject_version": fault_version,
                     "env_version": env_version, "producer": "independent_probe"},
                ]
                signal = (repair or _default_repair)(state_root, fixture, fault_version)
                elapsed = max(0.0, clock() - start)
                actual, healthy = observe(state_root, fixture)
                if signal is not None and signal.route:
                    result["route"] = list(signal.route)
                result["evidence"].append({
                    "stage": "after_repair",
                    "digest": actual,
                    "subject_version": fault_version,
                    "env_version": env_version,
                    "producer": "independent_probe",
                })
                if _environment_version() != env_version:
                    result["capability_gap"] = "environment_changed"
                elif elapsed > timeout_seconds:
                    result["capability_gap"] = "agent_timed_out"
                elif signal is None:
                    result["capability_gap"] = "agent_stuck"
                elif signal.subject_version != fault_version:
                    result["capability_gap"] = "stale_subject_version"
                elif signal.wake_deadline is not None and _utc(signal.wake_deadline) <= now:
                    result["capability_gap"] = "expired_wake"
                elif not healthy or actual != baseline_version:
                    result["capability_gap"] = "repair_not_proven"
                elif not recheck_domain(state_root, fixture, diagnosis):
                    result["capability_gap"] = "domain_recheck_failed"
                else:
                    result["status"] = "recovered"
    except Exception as exc:
        result["capability_gap"] = f"exercise_error:{type(exc).__name__}"
    finally:
        result["elapsed_seconds"] = round(max(0.0, clock() - start), 6)
        try:
            _owned_cleanup(project, root, token, cleaner)
            result["cleanup"] = "removed"
        except Exception as exc:
            result["cleanup"] = f"failed:{type(exc).__name__}"
            result["status"] = "capability_gap"
            result["capability_gap"] = "cleanup_failed"
    result["report_digest"] = _record_digest(result)
    return result


def _persist_report(path: Path, report: dict[str, object]) -> None:
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_verified_report(path: Path, day: str) -> dict[str, object]:
    """Do not treat a cached status string as an independent proof."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("not an object")
        claimed = report.get("report_digest")
        payload = {key: value for key, value in report.items() if key != "report_digest"}
        if (not isinstance(claimed, str) or
                not hmac.compare_digest(claimed, _record_digest(payload)) or
                report.get("date") != day or report.get("synthetic") is not True):
            raise ValueError("invalid report envelope")
        scenarios = report.get("scenarios")
        if not isinstance(scenarios, list) or len(scenarios) != len(FIXTURES):
            raise ValueError("missing scenario")
        for scenario, fixture in zip(scenarios, FIXTURES):
            if not isinstance(scenario, dict):
                raise ValueError("invalid scenario")
            entry_hash = scenario.get("report_digest")
            content = {key: value for key, value in scenario.items() if key != "report_digest"}
            if (not isinstance(entry_hash, str) or
                    not hmac.compare_digest(entry_hash, _record_digest(content)) or
                    scenario.get("scenario") != fixture.name or
                    scenario.get("synthetic") is not True or
                    scenario.get("baseline_version") != fingerprint(fixture.healthy)):
                raise ValueError("incorrect scenario version")
            if scenario.get("status") == "recovered":
                evidence = scenario.get("evidence")
                route = scenario.get("route")
                if (scenario.get("fault_version") != fingerprint(fixture.broken) or
                        scenario.get("subject_version") != fingerprint(fixture.broken) or
                        scenario.get("cleanup") != "removed" or
                        scenario.get("capability_gap") is not None or
                        not isinstance(route, list) or
                        not route or route[0] != f"detect:{fixture.name}:{EXPECTED[fixture.name].cause_code}" or
                        f"server_evidence:{EXPECTED[fixture.name].evidence_kind}" not in route or
                        not isinstance(evidence, list) or len(evidence) != 3 or
                        [entry.get("stage") for entry in evidence] !=
                        ["baseline", "fault", "after_repair"] or
                        [entry.get("digest") for entry in evidence] !=
                        [fingerprint(fixture.healthy), fingerprint(fixture.broken),
                         fingerprint(fixture.healthy)] or
                        any(entry.get("producer") != "independent_probe" or
                            entry.get("env_version") != scenario.get("env_version") for
                            entry in evidence) or
                        [entry.get("subject_version") for entry in evidence] !=
                        [fingerprint(fixture.healthy), fingerprint(fixture.broken),
                         fingerprint(fixture.broken)]):
                    raise ValueError("recovery without matching independent proof")
            elif scenario.get("status") != "capability_gap":
                raise ValueError("unknown scenario status")
        gaps = [f"{entry['scenario']}:{entry['capability_gap']}"
                for entry in scenarios if entry["status"] != "recovered"]
        if (report.get("capability_gaps") != gaps or
                report.get("status") != ("capability_gap" if gaps else "recovered")):
            raise ValueError("contradictory aggregate status")
        return report
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise DrillSafetyError("cached drill report failed verification") from exc


def run_daily(
    root: str | Path,
    *,
    now: datetime | None = None,
    repair: Repair | None = None,
    cleaner: Cleaner = _synthetic_cleaner,
    clock: Clock = time.monotonic,
) -> dict[str, object]:
    """Idempotent daily entrypoint. Caller supplies a dedicated temp sandbox.

    A crashed process leaves a lease: fail closed rather than steal ownership
    from a potentially live worker. Only case-* projects created by this call
    are cleaned; reports and leases are retained for audit/deduplication.
    """
    root = _check_root(root)
    root.mkdir(parents=True, exist_ok=True)
    day = _utc(now).date().isoformat()
    reports = root / "reports"
    leases = root / "leases"
    for folder in (reports, leases):
        if folder.is_symlink():
            raise DrillSafetyError("symlink in reports/leases")
        folder.mkdir(exist_ok=True)
    report_path = reports / f"{day}.json"
    if report_path.is_symlink():
        raise DrillSafetyError("symlink report")
    if report_path.exists():
        return _read_verified_report(report_path, day)
    lease = leases / f"{day}.lock"
    try:
        lease.mkdir()
    except FileExistsError:
        if report_path.exists():
            return _read_verified_report(report_path, day)
        return {"date": day, "status": "capability_gap", "capability_gaps": ["daily_lease_busy"],
                "synthetic": True, "scenarios": []}
    # Do not unlink lease until the report is on disk; never delete a foreign lease.
    result = {
        "date": day,
        "synthetic": True,
        "scheduler_contract": dict(SCHEDULER_CONTRACT),
        "scenarios": [
            run_scenario(root, fixture, repair=repair, cleaner=cleaner, clock=clock, now=now)
            for fixture in FIXTURES
        ],
    }
    result["capability_gaps"] = [
        f"{entry['scenario']}:{entry['capability_gap']}" for entry in result["scenarios"]
        if entry["status"] != "recovered"
    ]
    result["status"] = "recovered" if not result["capability_gaps"] else "capability_gap"
    result["report_digest"] = _record_digest(result)
    _persist_report(report_path, result)
    lease.rmdir()
    return result
