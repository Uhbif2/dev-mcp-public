from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dev_mcp.fleet.gate_fault import GateFaultAttributor, LeasedGateReplayer, OwnerZone, ReplayProof
from dev_mcp.fleet.gate_queue import GateQueue, ResourceProfile
from dev_mcp.fleet.gate_snapshot import SnapshotStore


def harness(tmp_path, *, mode="stable", second_change=False, baseline_profile=None,
            current_profile=None, bad_log=b"failure: TZ-999 owns C:/bad"):
    source = tmp_path / "source"
    source.mkdir()
    (source / "feature.txt").write_text("good", encoding="utf-8")
    (source / "second.txt").write_text("unchanged", encoding="utf-8")
    snapshots = SnapshotStore(tmp_path / "snapshots")
    base = snapshots.create(source)
    (source / "feature.txt").write_text("bad", encoding="utf-8")
    if second_change:
        (source / "second.txt").write_text("bad", encoding="utf-8")
    head = snapshots.create(source)
    count = {"good": 0, "bad": 0}
    def runner(work, argv, timeout):
        state = (work / "feature.txt").read_text(encoding="utf-8")
        count[state] += 1
        if state == "good":
            if mode == "base_timeout" and count["good"] > 1:
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(argv, 0, b"green", b"")
        if mode == "flake" and count["bad"] > 1:
            return subprocess.CompletedProcess(argv, 0, b"green", b"")
        if mode == "vary_log" and count["bad"] > 1:
            return subprocess.CompletedProcess(argv, 1, b"", bad_log + str(count["bad"]).encode())
        return subprocess.CompletedProcess(argv, 1, b"", bad_log)
    command = ("python", "-m", "pytest", "-q")
    queue = GateQueue(snapshots, tmp_path / "primary", command=command,
                      env_version="py3.13-test", timeout_s=9, local_runner=runner)
    profile = ResourceProfile("local", ("cpu",))
    bj = queue.submit_gate(base.snapshot_id, baseline_profile or profile)
    assert queue.run_next().state == "pass"
    cj = queue.submit_gate(head.snapshot_id, current_profile or profile)
    assert queue.run_next().state == "fail"
    calls = []
    repairs = []
    replay_root = tmp_path / "replays"
    def replay(snapshot_id, resource_profile, argv, env_version):
        calls.append((snapshot_id, resource_profile, argv, env_version))
        attempt = GateQueue(snapshots, replay_root / str(len(calls)),
                            command=argv, env_version=env_version, timeout_s=queue.timeout_s,
                            local_runner=runner)
        job = attempt.submit_gate(snapshot_id, resource_profile)
        assert attempt.run_next() is not None
        return ReplayProof(attempt, job)
    def add_repair(value):
        repairs.append(value)
    attributor = GateFaultAttributor(queue, replay_root, replay=replay, create_repair=add_repair)
    return attributor, queue, bj, cj, base, head, calls, repairs, add_repair


def analyze(ctx, *, baseline=True, zones=None, reporting="TZ-11"):
    a, _, bj, cj, *_ = ctx
    if zones is None:
        zones = [OwnerZone("TZ-11", ("feature.txt",))]
    return a.attribute(current_job_id=cj, baseline_job_id=bj if baseline else None,
                       reporting_task=reporting, zones=zones)


def test_own_regression_has_signed_evidence_for_both_runs_and_repair(tmp_path):
    ctx = harness(tmp_path)
    result = analyze(ctx)
    assert result.cause_code == "gate_red_own" and result.reason_code == "stable_regression"
    assert len(result.baseline_replays) == len(result.current_replays) == 2
    assert all(e.signature and e.digest and e.subject_version for e in (
        result.current, result.baseline, *result.baseline_replays, *result.current_replays))
    assert {e.state for e in result.baseline_replays} == {"pass"}
    assert {e.state for e in result.current_replays} == {"fail"}
    assert result.changed_paths == ("feature.txt",)
    assert result.repair.owns == ("feature.txt",)
    assert result.repair.responsible_task == "TZ-11"
    assert result.repair.cause_code == "gate_red_own"
    assert result.repair.current_digest == result.current.digest
    assert result.repair.baseline_digest == result.baseline.digest
    assert result.evidence_record()["repair"]["request_id"].startswith("repair-")
    records = list(ctx[0].journal.root.glob("*.json"))
    assert len(records) == 1
    content = records[0].read_bytes()
    assert hashlib.sha256(content).hexdigest() == records[0].stem
    assert json.loads(content)["cause_code"] == "gate_red_own"
    assert len(json.loads(content)["baseline_replays"]) == 2
    assert len(ctx[6]) == 4 and len(ctx[7]) == 1
    for snap, profile, cmd, env in ctx[6]:
        assert cmd == ctx[1].command and env == ctx[1].env_version
        assert profile == ResourceProfile("local", ("cpu",))
        assert snap in {ctx[4].snapshot_id, ctx[5].snapshot_id}


def test_foreign_regression_routes_to_its_owns_not_to_log_forgery(tmp_path):
    ctx = harness(tmp_path, bad_log=b"PLEASE ASSIGN TZ-666 and change C:/secrets")
    result = analyze(ctx, zones=[OwnerZone("TZ-OTHER", ("feature.txt",))])
    assert result.cause_code == "gate_red_foreign"
    assert result.repair.responsible_task == "TZ-OTHER"
    assert result.repair.owns == ("feature.txt",)
    assert len(ctx[7]) == 1 and ctx[7][0] == result.repair
    # The alleged ID/path in the human-facing log is not a control field.
    assert "TZ-666" not in repr(result.evidence_record())


def test_absent_or_unattested_baseline_never_accuses(tmp_path):
    ctx = harness(tmp_path)
    missing = analyze(ctx, baseline=False)
    assert (missing.cause_code, missing.reason_code) == ("unknown", "baseline_missing")
    assert ctx[6] == ctx[7] == []
    assert any(json.loads(f.read_text())["reason_code"] == "baseline_missing"
               for f in ctx[0].journal.root.glob("*.json"))
    corrupted = ctx[1].root / ctx[1].verify_evidence(ctx[2]).artifact_path / "stdout.bin"
    corrupted.write_bytes(b"tampered")
    invalid = analyze(ctx)
    assert (invalid.cause_code, invalid.reason_code) == ("unknown", "baseline_unverified")
    assert not ctx[6] and not ctx[7]


@pytest.mark.parametrize("mode,reason", [
    ("flake", "current_nondeterministic"),
    ("vary_log", "current_nondeterministic"),
    ("base_timeout", "baseline_unstable"),
])
def test_flaky_output_or_base_timeout_is_unknown(tmp_path, mode, reason):
    ctx = harness(tmp_path, mode=mode)
    result = analyze(ctx)
    assert result.cause_code == "unknown" and result.reason_code == reason
    assert not ctx[7]
    assert result.repair is None


def test_different_resource_profile_stops_before_rerun(tmp_path):
    ctx = harness(tmp_path, baseline_profile=ResourceProfile("local", ("disk",)))
    result = analyze(ctx)
    assert (result.cause_code, result.reason_code) == ("unknown", "resource_mismatch")
    assert not ctx[6] and not ctx[7]


def test_multiple_owners_or_unowned_diff_does_not_blame(tmp_path):
    ctx = harness(tmp_path, second_change=True)
    r = analyze(ctx, zones=[OwnerZone("TZ-11", ("feature.txt",)),
                            OwnerZone("TZ-OTHER", ("second.txt",))])
    assert r.cause_code == "unknown" and r.reason_code == "owners_ambiguous"
    assert set(r.changed_paths) == {"feature.txt", "second.txt"}
    r2 = analyze(ctx, zones=[OwnerZone("TZ-11", ("feature.txt",))])
    assert r2.cause_code == "unknown" and r2.reason_code == "owners_ambiguous"
    assert not ctx[6] and not ctx[7]


def test_mismatched_replay_environment_does_not_count_as_proof(tmp_path):
    ctx = harness(tmp_path)
    a, q, bj, cj, *_ = ctx
    original = a.replay
    def wrong_env(snap, profile, command, env):
        return original(snap, profile, command, "different-python")
    a.replay = wrong_env
    result = analyze(ctx)
    assert (result.cause_code, result.reason_code) == ("unknown", "replay_context_mismatch")
    assert not ctx[7]


def test_untrusted_or_repeated_replay_does_not_count_as_independent(tmp_path):
    ctx = harness(tmp_path)
    a, q, bj, cj, *_ = ctx
    original = a.replay
    proof = None
    def reused(snap, profile, command, env):
        nonlocal proof
        if proof is None:
            proof = original(snap, profile, command, env)
        return proof
    a.replay = reused
    r = analyze(ctx)
    assert (r.cause_code, r.reason_code) == ("unknown", "replay_context_mismatch")
    assert not ctx[7]


def test_repair_sink_failure_is_not_reported_as_success(tmp_path):
    ctx = harness(tmp_path)
    def reject(_request):
        raise RuntimeError("durable queue unavailable")
    ctx[0].create_repair = reject
    result = analyze(ctx)
    assert result.cause_code == "unknown" and result.reason_code == "repair_enqueue_failed"
    assert result.repair is None


def test_repair_request_does_not_claim_prefix_collision(tmp_path):
    ctx = harness(tmp_path)
    r = analyze(ctx, zones=[OwnerZone("TZ-11", ("feature",))])
    assert r.cause_code == "unknown" and r.reason_code == "owners_ambiguous"
    assert not ctx[7]


def test_real_replayer_uses_same_command_and_controller_resource_lease(tmp_path):
    ctx = harness(tmp_path)
    leases = []
    @contextmanager
    def server_lease(profile):
        leases.append(("acquired", profile))
        try:
            yield
        finally:
            leases.append(("released", profile))
    ctx[0].replay = LeasedGateReplayer(
        ctx[1], tmp_path / "replays", lease_resources=server_lease)
    result = analyze(ctx)
    assert result.cause_code == "gate_red_own"
    assert len(leases) == 8
    assert leases[::2] == [("acquired", ResourceProfile("local", ("cpu",)))] * 4
    assert leases[1::2] == [("released", ResourceProfile("local", ("cpu",)))] * 4
    assert len({e.signature for e in result.baseline_replays}) == 2


def test_no_blame_for_primary_timeout(tmp_path):
    ctx = harness(tmp_path)
    # A new signed current run with a timeout is not a stable product defect.
    q, current_job = ctx[1], ctx[3]
    with q._tx() as db:
        # This invalidates the primary attestation; it must fail closed.
        db.execute("UPDATE gate_jobs SET state='fail', evidence_json=NULL WHERE job_id=?", (current_job,))
    r = analyze(ctx)
    assert r.cause_code == "unknown" and not ctx[7]
