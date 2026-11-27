"""№116 п. 7: долгоживущие процессы вне Job Object — со скрытой консолью, а не вовсе без неё.

С DETACHED_PROCESS флаг CREATE_NO_WINDOW игнорируется (WinAPI), и консольные потомки такого
процесса (git, ssh, powershell) открывают на рабочем столе владельца видимые окна.
"""
from __future__ import annotations

import shutil
import subprocess

from dev_mcp.fleet import overseer, world

DETACHED, NO_WINDOW, BREAKAWAY = 0x00000008, 0x08000000, 0x01000000


def test_adb_daemon_gets_a_hidden_console(monkeypatch, tmp_path):
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw["creationflags"])))
    monkeypatch.setattr(world, "_adb_devices", lambda **_: (world.UNKNOWN, "нет"))
    monkeypatch.setattr(world, "_adb_started_at", 0.0)
    world.ensure_adb_server(str(adb), wait_s=0)
    flags = calls[0][1]
    assert flags & NO_WINDOW and flags & BREAKAWAY and not flags & DETACHED


def test_relay_gets_a_hidden_console(monkeypatch, tmp_path):
    (tmp_path / "relay.mjs").write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw["creationflags"])))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(overseer, "_RELAY_DIR", tmp_path)
    monkeypatch.setattr(overseer, "_relay_started", [-1e9])
    monkeypatch.setattr(shutil, "which", lambda name: "node.exe")
    assert overseer._start_relay() is True
    flags = calls[0][1]
    assert calls[0][0] == ["node.exe", "relay.mjs"]
    assert flags & NO_WINDOW and flags & BREAKAWAY and not flags & DETACHED
