"""Windows subprocess children never open an owner's console window."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dev_mcp import runner, server

NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200


class FakeProc:
    pid = 12345

    def __init__(self):
        self.returncode = None
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def terminate(self):
        self.returncode = -9


@pytest.fixture
def flags(monkeypatch):
    # Windows-only constants are not defined on Linux CI.
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", NO_WINDOW, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", NEW_GROUP, raising=False)
    monkeypatch.setattr(runner, "_CREATE_NO_WINDOW", NO_WINDOW)
    monkeypatch.setattr(runner, "_CREATE_NEW_PROCESS_GROUP", NEW_GROUP)


def test_server_synchronous_run_preserves_process_group(tmp_path, monkeypatch, flags):
    observed = []

    def fake_popen(argv, **kw):
        observed.append(kw)
        return FakeProc()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    rc, text, timed_out, job = server._run_capturing(
        [sys.executable, "-V"], cwd=str(tmp_path), env={},
        timeout_s=5, runs_dir=tmp_path,
    )
    assert rc == 0 and not timed_out and job is None
    assert observed[0]["creationflags"] & NO_WINDOW
    assert observed[0]["creationflags"] & NEW_GROUP
    assert observed[0]["shell"] is False
    assert not list(tmp_path.glob("sync-*.log"))


def test_runner_background_start_preserves_process_group(tmp_path, monkeypatch, flags):
    observed = []

    def fake_popen(argv, **kw):
        observed.append(kw)
        return FakeProc()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    jobs = runner.JobRegistry(tmp_path / "runs")
    job = jobs.start([sys.executable, "-V"], str(tmp_path), {})
    assert observed[0]["creationflags"] & NO_WINDOW
    assert observed[0]["creationflags"] & NEW_GROUP
    assert observed[0]["shell"] is False
    assert not job.stopped
    job.proc.returncode = 0
    assert jobs.get(job.job_id).returncode == 0


def test_stop_and_kill_tree_launch_taskkill_hidden(tmp_path, monkeypatch, flags):
    observed = []

    def fake_run(argv, **kw):
        observed.append((argv, kw))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    proc = FakeProc()
    server._kill_process_tree(proc)
    assert observed[0][0][:3] == ["taskkill", "/F", "/T"]
    assert observed[0][1]["creationflags"] & NO_WINDOW

    jobs = runner.JobRegistry(tmp_path / "runs")
    fake = FakeProc()
    job = runner.Job(
        job_id="fake", argv=[], cwd=str(tmp_path),
        log_path=tmp_path / "none.log", started_at=0, proc=fake,
    )
    jobs._jobs[job.job_id] = job
    assert jobs.stop(job.job_id)
    assert observed[-1][1]["creationflags"] & NO_WINDOW
    assert job.stopped and fake.wait_calls


@pytest.mark.asyncio
async def test_git_tool_uses_hidden_subprocess(tmp_path, monkeypatch, flags):
    from fastmcp import Client
    from dev_mcp.audit import Audit
    from dev_mcp.policy import Policy

    root = tmp_path / "workspace"
    root.mkdir()
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        f"roots = ['{root.as_posix()}']\nallow_write = false\n",
        encoding="utf-8",
    )
    observed = []

    def fake_run(argv, **kw):
        observed.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout="## main\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    app = server.build_server(Policy.load(policy_path), Audit(tmp_path / "audit.jsonl"))
    async with Client(app) as client:
        result = await client.call_tool("git_status", {"repo": str(root)})
    assert "main" in result.content[0].text
    assert observed and observed[0][1]["creationflags"] & NO_WINDOW


@pytest.mark.skipif(os.name != "nt", reason="real Windows Ctrl-Break process-group test")
def test_real_background_stop_ctrl_break_fallback(tmp_path, monkeypatch):
    """Real sleeping Python child is stopped despite taskkill failure.

    When CREATE_NO_WINDOW removes the console, sending CTRL_BREAK can fail.
    The existing terminate fallback must still stop the group leader.
    """
    jobs = runner.JobRegistry(tmp_path / "runs")
    job = jobs.start(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        str(tmp_path), dict(os.environ),
    )
    assert job.proc.poll() is None
    # Ensure the CTRL_BREAK branch is exercised rather than taskkill success.
    def fail_taskkill(argv, **kw):
        raise OSError("synthetic taskkill failure")

    monkeypatch.setattr(subprocess, "run", fail_taskkill)
    try:
        assert jobs.stop(job.job_id)
        assert job.proc.poll() is not None
        assert job.stopped
    finally:
        if job.proc.poll() is None:
            job.proc.kill()
            job.proc.wait(timeout=5)
