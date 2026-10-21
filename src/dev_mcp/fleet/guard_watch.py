"""Watch the independent deployment guard from the fleet controller.

Only the trusted controller supplies the live root.  Neither an agent task nor
a project manifest may select the executable.  Tests inject the process
boundary; no real processes are launched from the test suite.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

PULSE_MAX_AGE_S = 5 * 60
RESTART_WINDOW_S = 60 * 60
RESTART_LIMIT = 3
_LOCK = threading.Lock()


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows os.kill(pid, 0) can terminate a process: Python's Windows
        # implementation treats non-CTRL signals as TerminateProcess codes.
        # Use a query-only handle instead; never send a signal to a guard.
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            # Access denied is not proof of absence. Fail closed against
            # accidentally starting a duplicate process.
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
        try:
            return int(kernel.WaitForSingleObject(handle, 0)) == 0x00000102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _start(script: Path) -> int:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if os.name != "nt" or not pythonw.is_file() or not script.is_file():
        raise FileNotFoundError("trusted guard pythonw/runtime missing")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    child = subprocess.Popen(
        [str(pythonw), str(script)], cwd=str(script.parent),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
    )
    return int(child.pid)


def _pulse(live: Path, now: datetime, alive: Callable[[int], bool]) -> bool:
    try:
        heartbeat = json.loads((live / "data" / "guard-heartbeat.json").read_text(encoding="utf-8"))
        pid, stamp = heartbeat["pid"], heartbeat["time"]
        if type(pid) is not int or not isinstance(stamp, str):
            return False
        when = datetime.fromisoformat(stamp)
        age = (now - when).total_seconds()
        return 0 <= age <= PULSE_MAX_AGE_S and alive(pid)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return False


def _heartbeat_pid(live: Path) -> int:
    try:
        data = json.loads((live / "data" / "guard-heartbeat.json").read_text(encoding="utf-8"))
        return data["pid"] if type(data["pid"]) is int and data["pid"] > 0 else 0
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def _recent(store: Any, now: datetime) -> list[tuple[datetime, int]]:
    with store.read() as tx:
        rows = tx.query(
            "SELECT ts,payload FROM events WHERE kind='guard_restarted' ORDER BY id DESC LIMIT 100"
        )
    recent = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row["ts"]))
            if not now - timedelta(seconds=RESTART_WINDOW_S) <= stamp <= now:
                continue
            payload = json.loads(row["payload"])
            recent.append((stamp, int(payload.get("pid", 0))))
        except (TypeError, ValueError, KeyError):
            continue
    return recent


def _mark_recovered(store: Any) -> None:
    """A proven healthy pulse ends an obstacle episode exactly once."""
    with store.tx() as tx:
        blocked = tx.one(
            "SELECT id FROM events WHERE project=? AND kind=? ORDER BY id DESC LIMIT 1",
            ("Fleet Platform", "guard_watch_blocked"),
        )
        if blocked is None:
            return
        recovered = tx.one(
            "SELECT id FROM events WHERE project=? AND kind=? ORDER BY id DESC LIMIT 1",
            ("Fleet Platform", "guard_watch_recovered"),
        )
        if recovered is None or recovered["id"] < blocked["id"]:
            tx.record_event(kind="guard_watch_recovered", project="Fleet Platform",
                            payload={"source": "verified_heartbeat"})


def _obstacle(store: Any, live: Path, now: datetime, reason: str = "three_guard_restarts") -> None:
    """Keep one fix per guard/reason until a proven recovery starts a new episode."""
    with store.tx() as tx:
        recovered = tx.one(
            "SELECT id FROM events WHERE project=? AND kind=? ORDER BY id DESC LIMIT 1",
            ("Fleet Platform", "guard_watch_recovered"),
        )
        epoch = recovered["id"] if recovered is not None else 0
        guard_id = hashlib.sha256(os.path.normcase(str(live.resolve())).encode("utf-8")).hexdigest()[:10]
        key = f"fix:guard-watch:{guard_id}:{reason}:{epoch}"
        if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?",
                  ("Fleet Platform", key)) is not None:
            return
        project = tx.one("SELECT root FROM projects WHERE name=?", ("Fleet Platform",))
        if project is None:
            tx.record_event(kind="guard_watch_blocked",
                            payload={"reason": "fleet_platform_not_registered"})
            return
        root = Path(str(project["root"]))
        stamp = now.isoformat()
        owns = [str(root / "guard" / "core.py"),
                str(root / "src" / "dev_mcp" / "fleet" / "guard_watch.py")]
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("Fleet Platform", key, "Guard watcher crash loop", "ready", 1, "fix",
             "", "", "[]", json.dumps(owns), "[]", "[]", "guard_watch",
             "[guard_watch] reason=" + reason + ". Investigate the guard process "
             "and heartbeat. Do not start a second guard without proving the old "
             "one stopped under the deploy lock.",
             stamp, stamp),
            event={"kind": "guard_watch_blocked", "project": "Fleet Platform",
                   "tz": key, "payload": {"reason": reason}},
        )


def guard_watch_tick(
    store: Any, live: Path, *, now: datetime | None = None,
    alive: Callable[[int], bool] = _alive,
    start: Callable[[Path], int] = _start,
) -> str:
    """One idempotent controller maintenance step; returns a stable status."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("aware time required")
    with _LOCK:
        if _pulse(live, now, alive):
            _mark_recovered(store)
            return "healthy"
        stale_pid = _heartbeat_pid(live)
        if stale_pid and alive(stale_pid):
            # We have not proved the old process is dead. Restarting without
            # a trusted lock-aware stop could make two independent deployers.
            with store.tx() as tx:
                tx.record_event(kind="guard_watch_stale_alive", project="Fleet Platform",
                                payload={"pid": stale_pid})
            _obstacle(store, live, now, reason="stale_heartbeat_but_process_alive")
            return "stale_alive"
        recent = _recent(store, now)
        if not stale_pid and not recent:
            # No heartbeat and no prior supervised PID is not evidence of a
            # dead process. The existing independent boot watcher may be from
            # an older installation. Never start a second guard on this basis.
            _obstacle(store, live, now, reason="guard_pid_unproven")
            return "unknown"
        # A just-spawned living guard has five minutes to publish its first
        # heartbeat. Do not spawn a duplicate merely because the pulse is late.
        if recent and (now - recent[0][0]).total_seconds() <= PULSE_MAX_AGE_S:
            if alive(recent[0][1]):
                return "starting"
        if len(recent) >= RESTART_LIMIT:
            _obstacle(store, live, now)
            return "blocked"
        script = live / "data" / "guard-runtime" / "run_guard.pyw"
        try:
            pid = int(start(script))
            if pid <= 0:
                raise RuntimeError("guard launch did not return a PID")
        except (OSError, ValueError, RuntimeError) as exc:
            with store.tx() as tx:
                tx.record_event(kind="guard_watch_error", payload={
                    "error": type(exc).__name__,
                })
            return "launch_error"
        with store.tx() as tx:
            tx.record_event(kind="guard_restarted", project="Fleet Platform",
                            payload={"pid": pid, "script": str(script)})
        if len(recent) + 1 >= RESTART_LIMIT:
            _obstacle(store, live, now)
            return "blocked"
        return "restarted"
