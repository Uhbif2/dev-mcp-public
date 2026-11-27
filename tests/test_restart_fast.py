"""21.11.2026: restart.py — кэш стражей, один снимок процессов, netstat, без второго лончера."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from tools import restart


class _Policy:
    roots, exec_roots, exec_allowed_bin, _exec_bin_missing = ["r"], ["e"], ["b"], []

    @classmethod
    def load(cls, path):
        return cls()


@pytest.fixture
def stub_policy(monkeypatch):
    monkeypatch.setattr(restart, "Policy", _Policy)


@pytest.fixture(autouse=True)
def fresh_table():
    restart._invalidate_table()
    yield
    restart._invalidate_table()


def test_guard_cache_skips_unchanged_and_reruns_after_change(tmp_path, monkeypatch):
    stamp = tmp_path / "ok.json"
    monkeypatch.setattr(restart, "GUARD_STAMP", stamp)
    assert restart._guard_cached("abc") is None
    stamp.write_text(json.dumps({"fingerprint": "abc", "at": time.time(), "last": "23 passed"}), encoding="utf-8")
    assert restart._guard_cached("abc")["last"] == "23 passed"
    assert restart._guard_cached("changed") is None                      # код/конфиги поменялись
    stamp.write_text(json.dumps({"fingerprint": "abc", "at": time.time() - 25 * 3600}), encoding="utf-8")
    assert restart._guard_cached("abc") is None                          # старше суток


def test_fingerprint_is_stable_and_cheap():
    started = time.monotonic()
    first = restart._guard_fingerprint()
    assert first == restart._guard_fingerprint()
    assert time.monotonic() - started < 20


def test_step_check_policy_uses_cache_without_running_pytest(monkeypatch, stub_policy):
    monkeypatch.setattr(restart, "_guard_fingerprint", lambda: "fp")
    monkeypatch.setattr(restart, "_guard_cached", lambda fp: {"at": time.time() - 60, "last": "23 passed"})

    def forbidden(*a, **k):
        pytest.fail("pytest must not run when nothing changed")
    monkeypatch.setattr(restart.subprocess, "run", forbidden)
    assert restart.step_check_policy(True) is not None


def test_force_tests_ignores_cache(monkeypatch, tmp_path, stub_policy):
    monkeypatch.setattr(restart, "_guard_fingerprint", lambda: "fp")
    monkeypatch.setattr(restart, "_guard_cached", lambda fp: {"at": time.time(), "last": "ok"})
    monkeypatch.setattr(restart, "REPO", restart.REPO)
    calls = []

    class Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        kwargs["stdout"].write("=== 23 passed ===\n")
        return Done()
    monkeypatch.setattr(restart.subprocess, "run", fake_run)
    monkeypatch.setattr(restart, "GUARD_STAMP", tmp_path / "ok.json")
    monkeypatch.setattr(restart, "GUARD_LOG", tmp_path / "restart-guards.log")  # не трогать боевой журнал
    assert restart.step_check_policy(True, force_tests=True) is not None
    assert calls and "-k" in calls[0] and "production" in calls[0]
    assert json.loads((tmp_path / "ok.json").read_text(encoding="utf-8"))["fingerprint"] == "fp"


def test_guard_files_cover_every_production_selected_file():
    files = restart._guard_files()
    assert files and files != ["tests"]
    assert all("production" in open(restart.REPO / f, encoding="utf-8", errors="ignore").read()
               or "production" in f for f in files)
    assert any(f.replace("\\", "/").endswith("tests/test_policy.py") for f in files)


def test_unavailable_process_table_never_starts_second_launcher(monkeypatch):
    monkeypatch.setattr(restart, "_process_table", lambda max_age=15.0: None)
    monkeypatch.setattr(restart.subprocess, "Popen", lambda *a, **k: pytest.fail("no launcher start"))
    monkeypatch.setattr(restart, "_ok", True)
    assert restart._ensure_launcher() is False
    assert restart._ok is False


def test_pids_of_matches_python_command_lines_only(monkeypatch):
    rows = [{"pid": 10, "name": "pythonw.exe", "cmd": r"pythonw C:\x\mcp_launcher.py"},
            {"pid": 11, "name": "powershell.exe", "cmd": "powershell ... mcp_launcher.py"},
            {"pid": 12, "name": "py-3.13.exe", "cmd": "py mcp_launcher.py"}]
    monkeypatch.setattr(restart, "_process_table", lambda max_age=15.0: rows)
    assert restart._pids_of("mcp_launcher.py") == [10, 12]
    assert restart._proc_alive("mcp_launcher.py")
    assert not restart._proc_alive("poll_worker.py")


def test_process_table_sees_this_python():
    rows = restart._process_table()
    assert rows is not None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "marker-restart-fast"])
    try:
        restart._invalidate_table()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if child.pid in restart._pids_of("marker-restart-fast"):
                break
            restart._invalidate_table()
            time.sleep(0.3)
        assert child.pid in restart._pids_of("marker-restart-fast")
        assert "marker-restart-fast" in restart._process_command_line(child.pid)
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="netstat -ano is Windows")
def test_netstat_listener_pid_is_this_process():
    table = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=30).stdout
    if b"TCP" not in table:
        pytest.skip("netstat не показывает соединения (изолированная песочница)")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert os.getpid() in restart._port_listener_pids(port)
    assert os.getpid() not in restart._port_listener_pids(port)



LOG = ("\r\n=== mcp-proxy[dev] CRASHED/STOPPED 2026-11-21 10:58:51 — rc=1 ===\r\n"
       "=== mcp-proxy[dev] START 2026-11-21 10:59:22 (port 8766) ===\r\n"
       "[I 2026-11-21 11:00:40,191 mcp_proxy.mcp_server] Setting up named server 'dev': cmd /c run.cmd\r\n")


def _proxy_files(tmp_path, text):
    (tmp_path / "mcp_proxy_dev.log").write_text(text, encoding="utf-8", newline="")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"instances": {"dev": {"port": 8766, "log": "mcp_proxy_dev.log"}}}), encoding="utf-8")
    return state


def test_start_progress_reads_stages_of_the_last_start_only(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "PROXY_STATE", _proxy_files(tmp_path, LOG))
    progress = restart._start_progress("dev")
    assert progress["stage"] == "child"                     # CRASHED относится к прошлому старту
    assert time.strftime("%H:%M:%S", time.localtime(progress["started"])) == "10:59:22"
    (tmp_path / "mcp_proxy_dev.log").write_text(LOG + "[11/21/26 11:08:56] INFO Starting MCP server 'dev-mcp'\r\n",
                                               encoding="utf-8")
    assert restart._start_progress("dev")["stage"] == "server"
    (tmp_path / "mcp_proxy_dev.log").write_text(LOG + "=== mcp-proxy[dev] CRASHED/STOPPED 2026-11-21 11:16:33 ===\r\n",
                                               encoding="utf-8")
    assert restart._start_progress("dev")["stage"] == "crashed"


def test_slow_start_is_awaited_not_failed(monkeypatch):
    opens = iter([False, False, False, False, True])
    monkeypatch.setattr(restart, "_ensure_launcher", lambda: True)
    monkeypatch.setattr(restart, "_stop_proxy_instance", lambda name, port: [1])
    monkeypatch.setattr(restart, "_port_open", lambda port: next(opens, True))
    stages = iter(["proxy", "child", "server"])
    monkeypatch.setattr(restart, "_start_progress",
                        lambda name: {"stage": next(stages, "server"), "size": time.time_ns(), "started": 0})
    monkeypatch.setattr(restart, "_ok", True)
    monkeypatch.setattr(restart.time, "sleep", lambda s: None)
    restart.step_restart_proxy(["dev"])
    assert restart._ok is True


def test_no_progress_in_log_fails_after_stall_limit(monkeypatch):
    monkeypatch.setattr(restart, "_ensure_launcher", lambda: True)
    monkeypatch.setattr(restart, "_stop_proxy_instance", lambda name, port: [1])
    monkeypatch.setattr(restart, "_port_open", lambda port: False)
    monkeypatch.setattr(restart, "_start_progress", lambda name: {"stage": "child", "size": 10, "started": 0})
    monkeypatch.setattr(restart, "START_STALL_S", 1)
    monkeypatch.setattr(restart, "_ok", True)
    restart.step_restart_proxy(["dev"])
    assert restart._ok is False
    assert any("без движения" in line for line in restart._lines)


def test_instance_already_starting_after_last_edit_is_not_killed(monkeypatch):
    monkeypatch.setattr(restart, "_ensure_launcher", lambda: True)
    monkeypatch.setattr(restart, "_stop_proxy_instance", lambda name, port: pytest.fail("must not kill a fresh start"))
    opens = iter([False, False, True])
    monkeypatch.setattr(restart, "_port_open", lambda port: next(opens, True))
    monkeypatch.setattr(restart, "_start_progress",
                        lambda name: {"stage": "child", "size": time.time_ns(), "started": time.time() - 60})
    monkeypatch.setattr(restart, "_newest_code_mtime", lambda: time.time() - 600)
    monkeypatch.setattr(restart, "_ok", True)
    monkeypatch.setattr(restart.time, "sleep", lambda s: None)
    restart.step_restart_proxy(["dev"])
    assert restart._ok is True
    assert any("не убиваю" in line for line in restart._lines)


def test_start_from_before_the_last_edit_is_restarted(monkeypatch):
    killed = []
    monkeypatch.setattr(restart, "_ensure_launcher", lambda: True)
    monkeypatch.setattr(restart, "_stop_proxy_instance", lambda name, port: killed.append(name) or [1])
    opens = iter([False, False, True])
    monkeypatch.setattr(restart, "_port_open", lambda port: next(opens, True))
    monkeypatch.setattr(restart, "_start_progress",
                        lambda name: {"stage": "child", "size": time.time_ns(), "started": time.time() - 600})
    monkeypatch.setattr(restart, "_newest_code_mtime", lambda: time.time() - 60)
    monkeypatch.setattr(restart, "_ok", True)
    monkeypatch.setattr(restart.time, "sleep", lambda s: None)
    restart.step_restart_proxy(["dev"])
    assert killed == ["dev"]


def test_free_ram_probe_returns_a_number_on_windows():
    value = restart._free_ram_gib()
    assert value is None or value >= 0
