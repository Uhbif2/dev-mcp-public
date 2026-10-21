from __future__ import annotations
import subprocess
import sys
import threading
import time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dev_mcp.fleet.gate_snapshot import SnapshotStore
from dev_mcp.fleet.gate_queue import GateQueue, ResourceProfile


def setup(tmp_path, runner=None, remote=None):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("original")
    snapshots = SnapshotStore(tmp_path / "snapshots")
    ref = snapshots.create(source)
    q = GateQueue(snapshots, tmp_path / "gate-state", command=(sys.executable, "-c", "print('ok')"),
                  env_version="python-test-v1", local_runner=runner, remote_runners=remote)
    return q, ref, source


def light():
    return ResourceProfile("local", ("cpu",))


def test_success_signed_artifacts_and_request_idempotency(tmp_path):
    q, ref, source = setup(tmp_path)
    job = q.submit_gate(ref.snapshot_id, light())
    assert q.job(job)["state"] == "queued"
    evidence = q.run_next()
    assert evidence.state == evidence.verdict == "pass"
    assert evidence.subject_version == ref.snapshot_id
    assert evidence.env_version == "python-test-v1"
    assert evidence.runner == "server:local" and evidence.signature and evidence.digest
    assert q.verify_evidence(job) == evidence
    assert q.submit_gate(ref.snapshot_id, light()) == job
    assert q.run_next() is None
    after_restart = GateQueue(q.snapshots, q.root, command=q.command, env_version=q.env_version)
    assert after_restart.verify_evidence(job) == evidence
    (q.root / evidence.artifact_path / "stdout.bin").write_bytes(b"fake")
    with pytest.raises(ValueError, match="artifact"):
        q.verify_evidence(job)


def test_run_uses_snapshot_not_live_tree(tmp_path):
    def runner(work, argv, timeout):
        assert (work / "a.txt").read_text() == "original"
        (work / "a.txt").write_text("changes confined to workspace")
        return subprocess.CompletedProcess(argv, 0, b"success", b"")
    q, ref, source = setup(tmp_path, runner)
    (source / "a.txt").write_text("new live content")
    job = q.submit_gate(ref.snapshot_id, light())
    assert q.run_next().state == "pass"
    assert (source / "a.txt").read_text() == "new live content"
    assert q.verify_evidence(job).state == "pass"


def test_resource_serialization_and_multiple_requests(tmp_path):
    enter, release = threading.Event(), threading.Event()
    calls = []
    def runner(work, argv, timeout):
        calls.append(1)
        if len(calls) == 1:
            enter.set()
            assert release.wait(5)
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")
    q, first, source = setup(tmp_path, runner)
    (source / "a.txt").write_text("second version")
    second = q.snapshots.create(source)
    a = q.submit_gate(first.snapshot_id, light())
    b = q.submit_gate(second.snapshot_id, light())
    t = threading.Thread(target=q.run_next)
    t.start()
    assert enter.wait(5)
    try:
        assert q.job(a)["state"] == "running"
        assert q.run_next() is None
        assert q.job(b)["state"] == "queued"
    finally:
        release.set()
        t.join(5)
    assert not t.is_alive()
    assert q.run_next().state == "pass"
    assert len(calls) == 2
    assert q.job(b)["state"] == "pass"


def test_heavy_requires_remote_adapter_without_local_fallback(tmp_path):
    q, ref, source = setup(tmp_path)
    with pytest.raises(ValueError, match="heavy"):
        q.submit_gate(ref.snapshot_id, ResourceProfile("local", ("heavy-build",)))
    with pytest.raises(ValueError):
        q.submit_gate(ref.snapshot_id, {"target": "local", "resources": [], "heavy": False})
    remote_profile = ResourceProfile("ci", ("heavy-build",), heavy=True)
    job = q.submit_gate(ref.snapshot_id, remote_profile)
    assert q.run_next() is None
    assert q.job(job)["state"] == "queued"
    called = []
    def ci(work, argv, timeout):
        called.append(1)
        return subprocess.CompletedProcess(argv, 0, b"ci green", b"")
    active = GateQueue(q.snapshots, q.root, command=q.command,
                       env_version=q.env_version, remote_runners={"ci": ci})
    assert active.run_next().runner == "server:ci"
    assert called and active.verify_evidence(job).state == "pass"


def test_tampered_snapshot_during_run_fails_even_if_rc_zero(tmp_path):
    holder = []
    def runner(work, argv, timeout):
        stored = holder[0]
        stored.chmod(0o644)
        stored.write_text("tampered")
        return subprocess.CompletedProcess(argv, 0, b"green but fake", b"")
    q, ref, source = setup(tmp_path, runner)
    holder.append(q.snapshots.root / ref.snapshot_id / "tree" / "a.txt")
    job = q.submit_gate(ref.snapshot_id, light())
    assert q.run_next().state == "fail"
    assert q.job(job)["state"] == "fail"
    assert q.verify_evidence(job).state == "fail"  # signed failure survives corruption


def test_pass_evidence_invalidated_by_later_snapshot_corruption(tmp_path):
    q, ref, source = setup(tmp_path)
    job = q.submit_gate(ref.snapshot_id, light())
    assert q.run_next().state == "pass"
    stored = q.snapshots.root / ref.snapshot_id / "tree" / "a.txt"
    stored.chmod(0o644)
    stored.write_text("tampered later")
    with pytest.raises(ValueError):
        q.verify_evidence(job)


def test_worker_crash_expired_lease_and_cancel(tmp_path):
    q, ref, source = setup(tmp_path)
    job = q.submit_gate(ref.snapshot_id, light())
    with q._tx() as cx:
        cx.execute("UPDATE gate_jobs SET state='running', lease_until=?,lease_token='stale' WHERE job_id=?",
                   (time.time() - 1, job))
    assert q.recover_stale() == 1
    assert q.job(job)["state"] == "queued"
    assert q.cancel(job)
    assert q.job(job)["state"] == "cancelled"
    assert q.run_next() is None
    with pytest.raises(ValueError, match="no final"):
        q.verify_evidence(job)


def test_crashed_cancelled_runner_is_never_requeued(tmp_path):
    q, ref, source = setup(tmp_path)
    job = q.submit_gate(ref.snapshot_id, light())
    with q._tx() as cx:
        cx.execute("UPDATE gate_jobs SET state='running', lease_until=?, "
                   "lease_token='dead', cancel_requested=1 WHERE job_id=?",
                   (time.time() - 1, job))
    assert q.recover_stale() == 1
    assert q.job(job)["state"] == "cancelled"
    assert q.run_next() is None


def test_runner_timeout_creates_failure_evidence(tmp_path):
    def fail(work, argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    q, ref, source = setup(tmp_path, fail)
    job = q.submit_gate(ref.snapshot_id, light())
    result = q.run_next()
    assert result.state == "fail" and result.exit_code == -1
    assert b"timeout" in (q.root / result.artifact_path / "stderr.bin").read_bytes()
    assert q.verify_evidence(job).state == "fail"


def test_default_runner_does_not_inherit_server_secrets(tmp_path, monkeypatch):
    q, ref, source = setup(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "canary-secret-value")
    q = GateQueue(q.snapshots, q.root / "isolated",
                  command=(sys.executable, "-c",
                           "import os; print(os.environ.get('OPENROUTER_API_KEY', 'absent'))"),
                  env_version="isolated")
    job = q.submit_gate(ref.snapshot_id, light())
    result = q.run_next()
    assert result.state == "pass"
    assert (q.root / result.artifact_path / "stdout.bin").read_bytes().strip() == b"absent"
    assert q.verify_evidence(job).state == "pass"
