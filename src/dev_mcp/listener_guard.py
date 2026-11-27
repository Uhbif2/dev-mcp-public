"""Recover only this backend's supervised dev proxy after a lost listener.

An alive proxy PID is insufficient: WinError 64 left port 8766 closed while
the existing launcher waited forever for that process to exit. No core restart,
new launcher, window control, or process-name scan is needed.
"""
from __future__ import annotations

from dev_mcp import machine

import ctypes
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading

STATE = machine.PROXY_STATE
PORT = 8766


def port_up() -> bool:
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


def recovery_pid(state: dict, ancestors: list[dict]) -> int | None:
    """The state PID must be our actual ancestor, a dev proxy, and not core."""
    if not isinstance(state, dict) or not all(isinstance(a, dict) for a in ancestors):
        return None
    instances = state.get('instances', {})
    if not isinstance(instances, dict):
        return None
    dev, core = instances.get('dev', {}), instances.get('core', {})
    if not isinstance(dev, dict) or not isinstance(core, dict):
        return None
    pid = dev.get('pid')
    if (type(pid) is not int or pid <= 0 or dev.get('port') != PORT
            or dev.get('state') != 'running' or pid == core.get('pid')
            or state.get('pid') not in {a.get('pid') for a in ancestors}):
        return None
    for item in ancestors:
        if item.get('pid') == pid:
            command = str(item.get('command') or '')
            if (re.search(r'\bmcp_proxy\b', command, re.I)
                    and re.search(r'(?:^|\s)--port\s+8766(?=\s|$)', command)):
                return pid
    return None


def _ancestors() -> list[dict]:
    # Inspect only our own ancestry. Never enumerate other applications' argv,
    # and never return command text to the agent audit or notification channel.
    script = (
        f'$current={os.getpid()}; $rows=@(); '
        'for($n=0;$n -lt 8 -and $current -gt 0;$n++){'
        '$p=Get-CimInstance Win32_Process -Filter "ProcessId=$current";'
        'if(-not $p){break};'
        '$rows+=@{pid=[int]$p.ProcessId;command=[string]$p.CommandLine};'
        '$current=[int]$p.ParentProcessId};'
        'ConvertTo-Json -InputObject @($rows) -Compress'
    )
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=12,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
    data = json.loads(result.stdout)
    return data if isinstance(data, list) else []


def recover(audit) -> bool:
    """Terminate one verified process handle; the existing launcher restarts it.

    Open before inspecting ancestry and retain that kernel handle throughout.
    A reused PID cannot redirect TerminateProcess to a different object.
    """
    if sys.platform != 'win32' or port_up():
        return False
    state = json.loads(STATE.read_text(encoding='utf-8'))
    if not isinstance(state, dict) or not isinstance(state.get('instances'), dict):
        return False
    dev = state['instances'].get('dev')
    if not isinstance(dev, dict):
        return False
    pid = dev.get('pid')
    if type(pid) is not int or pid <= 0:
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000 | 0x0001, False, pid)  # query + terminate
    if not handle:
        return False
    try:
        if recovery_pid(state, _ancestors()) != pid:
            return False
        current = json.loads(STATE.read_text(encoding='utf-8'))
        code = ctypes.c_uint32()
        if (current != state or port_up()
                or not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259):
            return False
        audit.record('dev_listener_recovery', {'pid': pid, 'port': PORT},
                     decision='allow', reason='three consecutive listener failures; verified own dev ancestor')
        return bool(kernel.TerminateProcess(handle, 1))
    finally:
        kernel.CloseHandle(handle)


def normalize_transport_priority(audit) -> bool:
    """The HTTP hop serves the same interactive requests as this backend.

    The transport inherited BelowNormal/low-I/O while only the backend restored
    its serving priority. Normalize only our verified dev ancestor so every
    local request hop has the same class; background children keep theirs.
    """
    if sys.platform != 'win32':
        return False
    import psutil
    state = json.loads(STATE.read_text(encoding='utf-8'))
    parents = psutil.Process().parents()[:8]
    ancestors = [{'pid': p.pid, 'command': subprocess.list2cmdline(p.cmdline())}
                 for p in parents]
    pid = recovery_pid(state, ancestors)
    process = next((p for p in parents if p.pid == pid), None)
    if process is None or not process.is_running():
        return False
    cpu, io = process.nice(), process.ionice()
    low_cpu = cpu in (psutil.IDLE_PRIORITY_CLASS, psutil.BELOW_NORMAL_PRIORITY_CLASS)
    low_io = io in (psutil.IOPRIO_VERYLOW, psutil.IOPRIO_LOW)
    if not low_cpu and not low_io:
        return False
    if json.loads(STATE.read_text(encoding='utf-8')) != state or not process.is_running():
        return False
    if low_cpu:
        process.nice(psutil.NORMAL_PRIORITY_CLASS)
    if low_io:
        process.ionice(psutil.IOPRIO_NORMAL)
    audit.record('dev_transport_priority',
                 {'pid': pid, 'before': {'cpu': int(cpu), 'io': int(io)},
                  'after': {'cpu': int(process.nice()), 'io': int(process.ionice())}},
                 decision='allow', reason='verified own dev transport serves interactive MCP requests')
    return True


def watch(stop: threading.Event, audit, *, probe=port_up, repair=recover,
          interval_s: float = 10, normalize=None) -> None:
    seen = False
    failures = 0
    while not stop.is_set():
        try:
            if probe():
                seen, failures = True, 0
                if normalize is not None:
                    try:
                        normalize(audit)
                    except Exception:
                        # Priority permissions must never disable listener recovery.
                        pass
            elif seen:
                failures += 1
                if failures >= 3:
                    if repair(audit):
                        return
                    failures = 0
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            failures = 0  # No verified identity => no process effect.
        stop.wait(interval_s)


def start(home: Path, policy_path: str, audit, stop: threading.Event) -> None:
    # A test DB/check run/foreign checkout cannot activate process management.
    installed = machine.LIVE_HOME
    if (sys.platform != 'win32' or installed is None or home.resolve() != installed.resolve()
            or Path(policy_path).resolve() != (installed/'policy.toml').resolve()):
        return
    threading.Thread(target=watch, args=(stop, audit),
                     kwargs={'normalize': normalize_transport_priority}, daemon=True,
                     name='dev-mcp-listener-guard').start()
