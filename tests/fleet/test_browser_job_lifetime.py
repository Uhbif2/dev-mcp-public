"""Real Windows lifetime contract; uses hidden disposable Python, never Chrome."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object lifetime")
@pytest.mark.parametrize("independent", [False, True], ids=["old-cmd-start", "wmi-broker"])
def test_browser_launch_lifetime_outlives_mcp_job_only_with_independent_broker(tmp_path, independent):
    win32job = pytest.importorskip("win32job")
    win32api = pytest.importorskip("win32api")
    import psutil

    pid_file = tmp_path / "child-pid.txt"
    child_code = (
        f"from pathlib import Path; import os,time; Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    wrapper_code = (
        "import sys,subprocess,time; sys.stdin.readline(); "
        f"argv={[sys.executable, '-c', child_code]!r}; "
        + ("from dev_mcp.fleet.supervisor import start_windows_independent_process; "
           "start_windows_independent_process(argv); " if independent else
           "subprocess.Popen(['cmd.exe','/d','/c','start','',*argv], "
           "creationflags=subprocess.CREATE_NO_WINDOW); ")
        + "time.sleep(30)"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    wrapper = subprocess.Popen(
        [sys.executable, "-c", wrapper_code], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    job = win32job.CreateJobObject(None, "")
    child = None
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(0x0101, False, wrapper.pid)  # quota + terminate
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        wrapper.stdin.write(b"go\n")
        wrapper.stdin.flush()
        deadline = time.monotonic() + 15
        while not pid_file.exists() and wrapper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "hidden disposable child did not start"
        child = psutil.Process(int(pid_file.read_text()))
        assert child.is_running()
        job.Close()  # exact MCP shutdown operation, after the cmd launcher has exited
        job = None
        wrapper.wait(timeout=5)
        time.sleep(0.3)
        assert child.is_running() is independent
    finally:
        if job is not None:
            job.Close()
        if child is not None and child.is_running():
            child.terminate()
            child.wait(timeout=5)
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.communicate(timeout=5)
