"""05.11: демон adb — свой, долгоживущий, вне Job Object команды; run adb — закреплённым бинарём."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import world  # noqa: E402


def test_sensor_starts_missing_daemon_once_outside_the_job(monkeypatch, tmp_path):
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"")
    states = iter([(world.UNKNOWN, "нет"), (world.UNKNOWN, "нет"), (world.UP, "устройства: X"),
                   (world.UP, "устройства: X")])
    monkeypatch.setattr(world, "_adb_devices", lambda **_: next(states))
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append((argv, kw["creationflags"])))
    monkeypatch.setattr(world, "_adb_started_at", 0.0)
    monkeypatch.setattr(world.time, "sleep", lambda _s: None)
    assert world._adb_sensor({"sensor": "adb", "adb": str(adb)}) == (world.UP, "устройства: X")
    assert started and started[0][0] == [str(adb), "start-server"]
    assert started[0][1] & 0x01000000  # CREATE_BREAKAWAY_FROM_JOB


def test_daemon_restart_is_throttled(monkeypatch, tmp_path):
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"")
    monkeypatch.setattr(world, "_adb_devices", lambda **_: (world.UNKNOWN, "нет"))
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.setattr(world, "_adb_started_at", 0.0)
    world.ensure_adb_server(str(adb), wait_s=0)
    world.ensure_adb_server(str(adb), wait_s=0)
    assert len(started) == 1


def test_run_adb_uses_pinned_binary(monkeypatch, tmp_path):
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"")
    monkeypatch.setattr(world, "config", lambda: {"телефон": {"sensor": "adb", "adb": str(adb)}})
    monkeypatch.setattr(world, "ensure_adb_server", lambda *_a, **_k: True)
    assert world.pin_adb(["adb", "devices", "-l"]) == [str(adb), "devices", "-l"]
    assert world.pin_adb(["C:/x/ADB.EXE", "shell"]) == [str(adb), "shell"]
    assert world.pin_adb(["py", "-3"]) == ["py", "-3"]


def test_agent_cannot_kill_or_replace_shared_daemon():
    assert "запрещён" in world.adb_refusal(["adb", "kill-server"])
    assert world.adb_refusal(["C:/sdk/adb.exe", "-P", "5038", "devices"])
    assert world.adb_refusal(["adb", "reconnect"])
    assert world.adb_refusal(["adb", "devices", "-l"]) == ""
    assert world.adb_refusal(["adb", "shell", "getprop"]) == ""
    assert world.adb_refusal(["py", "-3", "x.py", "kill-server"]) == ""
