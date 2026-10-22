"""TZ-51: реальный TCP listener гаснет, независимый guard — нет."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from tools import restart


@pytest.mark.parametrize("argument,code", [("--help", 0), ("--typo", 2)])
def test_help_and_unknown_arguments_never_restart(monkeypatch, argument, code):
    monkeypatch.setattr(sys, "argv", ["restart.py", argument])
    def forbidden(*args, **kwargs):
        pytest.fail("argument parsing must finish before policy checks or effects")
    monkeypatch.setattr(restart, "step_check_policy", forbidden)
    monkeypatch.setattr(restart, "step_restart_proxy", forbidden)
    with pytest.raises(SystemExit) as stopped:
        restart.main()
    assert stopped.value.code == code


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _await_file(path: Path, proc: subprocess.Popen) -> None:
    for _ in range(100):
        if path.exists():
            return
        if proc.poll() is not None:
            pytest.fail(f"child exited prematurely: {proc.returncode}")
        time.sleep(0.05)
    pytest.fail(f"child did not signal readiness: {path}")


@pytest.mark.skipif(os.name != "nt", reason="uses Win32 TCP PID and taskkill")
def test_live_listener_stops_but_guard_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_port()
    server_path = tmp_path / "dummy_dev.py"
    server_path.write_text(
        "import pathlib,socket,sys,time\n"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        "s.bind(('127.0.0.1',int(sys.argv[1])));s.listen()\n"
        "pathlib.Path(sys.argv[2]).write_text('ready')\n"
        "time.sleep(90)\n", encoding="utf-8",
    )
    guard_dir = tmp_path / "dev-mcp" / "data" / "guard-runtime"
    guard_dir.mkdir(parents=True)
    guard_path = guard_dir / "run_guard.pyw"
    guard_path.write_text(
        "import pathlib,sys,time\n"
        "pathlib.Path(sys.argv[1]).write_text('ready')\n"
        "time.sleep(90)\n", encoding="utf-8",
    )
    ready_dev = tmp_path / "dev.ready"
    ready_guard = tmp_path / "guard.ready"
    dev = subprocess.Popen([sys.executable, str(server_path), str(port), str(ready_dev)])
    # Последние аргументы специально делают строку guard похожей на старую
    # подстроку поиска `mcp_proxy --port N` — старый _pids_of убил бы guard.
    guard = subprocess.Popen([sys.executable, str(guard_path), str(ready_guard),
                              "mcp_proxy", "--port", str(port)])
    try:
        _await_file(ready_dev, dev)
        _await_file(ready_guard, guard)
        state = tmp_path / "state.json"
        state.write_text(json.dumps({"instances": {"dev": {"port": port, "pid": dev.pid}}}),
                         encoding="utf-8")
        monkeypatch.setattr(restart, "PROXY_STATE", state)
        listening = restart._port_listener_pids(port)
        assert listening, "the dummy dev process must own the TCP listener"
        victims = restart._stop_proxy_instance("dev", port)
        assert listening.issubset(victims)
        assert guard.pid not in victims
        dev.wait(timeout=10)
        assert guard.poll() is None, "guard must survive dev-only restart"
    finally:
        for child in (dev, guard):
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)


def test_stale_state_pid_and_protected_guard_are_never_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restart, "_port_listener_pids", lambda port: {51})
    monkeypatch.setattr(restart, "_proxy_state", lambda: {"dev": {"pid": 52, "port": 8766}})
    commands = {
        51: r"pythonw C:\dev-mcp\data\guard-runtime\run_guard.pyw mcp_proxy --port 8766",
        52: "python something_unrelated.py",
    }
    monkeypatch.setattr(restart, "_process_command_line", lambda pid: commands[pid])
    assert restart._proxy_victims("dev", 8766) == []


def test_state_pid_is_checked_against_proxy_and_exact_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restart, "_port_listener_pids", lambda port: set())
    monkeypatch.setattr(restart, "_proxy_state", lambda: {"dev": {"pid": 123, "port": 8766}})
    monkeypatch.setattr(restart, "_process_command_line",
                        lambda pid: r"C:\Python\python.exe -m mcp_proxy --port 8766 --host 127.0.0.1")
    assert restart._proxy_victims("dev", 8766) == [123]


def test_occupied_port_without_safe_pid_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restart, "_ensure_launcher", lambda: True)
    monkeypatch.setattr(restart, "_stop_proxy_instance", lambda name, port: [])
    monkeypatch.setattr(restart, "_port_open", lambda port: True)
    monkeypatch.setattr(restart, "_ok", True)
    restart.step_restart_proxy(["dev"])
    assert restart._ok is False
