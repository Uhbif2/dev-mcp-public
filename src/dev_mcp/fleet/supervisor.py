from __future__ import annotations

import base64
import json
import math
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from dev_mcp.json_book import read_json, write_json
from typing import Any, Callable

from .store import Store
from .browser_storage import probe_extension_storage, repair_extension_storage

DEBUG_URL = "http://localhost:9223/json/version"
STATUS_FILE = "launcher-health.json"
SUPERVISOR_INTERVAL_S = 15.0
START_TIMEOUT_S = 20.0
EXTENSION_STALE_S = 90.0
# Transport receipts and work-cycle progress are separate evidence.
EXTENSION_TICK_STALE_S = 240.0
EXTENSION_BOOT_GRACE_S = 75.0
EXTENSION_WAKE_GRACE_S = 240.0
EXTENSION_RELOAD_COOLDOWN_S = 900.0
STORAGE_REPAIR_COOLDOWN_S = 3600.0
_BACKOFF_S = (15.0, 30.0, 60.0, 120.0, 300.0)
# Жив процесс FleetChrome, а DevTools молчит: это загрузка или тормоз, а не «браузера нет».
# Второй запуск chrome.exe с тем же профилем открыл бы в живом браузере ЕЩЁ ОДНО окно
# (07.10: четыре окна, 16 минут простоя). A missing diagnostic port is not
# evidence that a live browser is hung; leave its windows and sessions intact.
ALIVE_SILENT_GRACE_S = 120.0
_STATUS_LOCK = threading.Lock()
CONTROL_FILE = "browser-control.json"


def _control_path(store_or_path: Any) -> Path | None:
    status = _status_path(store_or_path)
    return None if status is None else status.with_name(CONTROL_FILE)


def browser_control(store_or_path: Any) -> dict[str, Any]:
    """Намерение владельца о браузере флота: autostart и аварийная остановка (halt).

    autostart=False — сервер не запускает, не перезапускает и не убивает FleetChrome.
    halt — расширение увидело «подозрительную активность из вашей сети»: вся работа
    стоит, пока владелец не снимет остановку (окно расширения или fleet.cmd browser resume).
    """
    data = _read_json(_control_path(store_or_path))
    halt = data.get("halt") if isinstance(data.get("halt"), dict) else None
    return {"autostart": data.get("autostart") is not False, "halt": halt,
            "updated_at": data.get("updated_at"), "by": data.get("by")}


def set_browser_control(store_or_path: Any, *, autostart: bool | None = None,
                        halt: dict[str, Any] | None | bool = False, by: str = "") -> dict[str, Any]:
    """halt=False — не менять; None — снять; dict — поставить."""
    path = _control_path(store_or_path)
    if path is None:
        raise ValueError("нет пути реестра флота")
    with _STATUS_LOCK:
        data = _read_json(path, strict=True)
        if autostart is not None:
            data["autostart"] = bool(autostart)
        if halt is None:
            data.pop("halt", None)
        elif isinstance(halt, dict):
            data["halt"] = {**halt, "at": halt.get("at") or _utcnow()}
        data["updated_at"] = _utcnow()
        data["by"] = str(by or "")
        _write_json(path, data)
    return browser_control(store_or_path)


@dataclass(frozen=True)
class FleetDemand:
    runnable: int
    active_claims: int
    unfinished: int
    owner_notices: int = 0
    quick: int = 0  # открытые быстрые агенты (fleet/quick.py): их чаты ведёт тот же браузер

    @property
    def needed(self) -> bool:
        # ready/taken/submitted keeps the launcher alive even when dependencies or
        # an active claim temporarily make the exact runnable frontier empty.
        return (self.runnable > 0 or self.active_claims > 0 or self.unfinished > 0
                or self.owner_notices > 0 or self.quick > 0)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status_path(store_or_path: Any) -> Path | None:
    raw = str(getattr(store_or_path, "path", store_or_path) or "")
    if not raw or raw == ":memory:":
        return None
    return Path(raw).with_name(STATUS_FILE)


def _read_json(path: Path | None, *, strict: bool = False) -> dict[str, Any]:
    if path is None:
        return {}
    data = read_json(path, default={}, strict=strict)
    return data if isinstance(data, dict) else {}


def _write_json(path: Path | None, data: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data, indent=2, trailing_newline=True)


def record_extension_heartbeat(
    store_or_path: Any,
    *,
    version: str = "",
    tabs: int = -1,
    pending: int = -1,
    runnable: int = -1,
    enabled: bool | None = None,
    tick_completed: bool = False,
) -> None:
    """Persist a browser-extension lease without touching worker liveness.

    A ChatGPT inference may continue calling MCP after the local browser has closed.
    Therefore this lease is intentionally separate from workers.last_seen.
    """
    path = _status_path(store_or_path)
    if path is None:
        return
    with _STATUS_LOCK:
        data = _read_json(path)
        previous = data.get("extension")
        previous = previous if isinstance(previous, dict) else {}
        entry = {**previous, "seen_at": _utcnow()}
        if version:
            entry['version']=str(version)
        if type(tabs) is int and tabs >= 0:
            entry['tabs']=tabs
        if type(pending) is int and pending >= 0:
            entry['pending']=pending
        if type(enabled) is bool:
            entry["enabled"] = enabled
        if type(runnable) is int and runnable >= 0:
            entry["runnable"] = runnable
        # Server-stamped evidence, not a browser-supplied timestamp or lease.
        if tick_completed is True and enabled is True:
            entry["tick_at"] = entry["seen_at"]
        elif isinstance(previous.get("tick_at"), str):
            entry["tick_at"] = previous["tick_at"]
        data["extension"] = entry
        _write_json(path, data)


def browser_ready(url: str = DEBUG_URL, *, timeout_s: float = 0.7) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    browser = str(payload.get("Browser") or "")
    return bool(browser)


def chrome_binary() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


PRIORITY_KEEPER = False  # 17.11.2026: держатель приоритета вкладок выключен (см. start_priority_keeper)


def chrome_argv(binary: Path, local_appdata: str | None = None) -> list[str]:
    local = Path(local_appdata or os.environ.get("LOCALAPPDATA", ""))
    profile = local / "FleetChrome"
    argv = [
        str(binary),
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9223",
        "--start-minimized",
        "--restore-last-session",
        "--no-first-run",
        # 17.11 (владелец): флаги против троттлинга фоновых вкладок убраны — скрытые вкладки
        # ChatGPT без них рисовали ответы на полной скорости (Chrome 65% ЦП). Отправка
        # больше не зависит от фона: расширение выводит вкладку вперёд (fleet-tabs 0.19.33).
    ]
    # Restoring a profile must not append another unowned homepage on every
    # recovery. Cold pages receive new tab IDs and otherwise exhaust UI seats.
    sessions = profile / 'Default/Sessions'
    if not sessions.is_dir() or not any(sessions.glob('Session_*')):
        argv.append('https://chatgpt.com/')
    return argv


def start_windows_independent_process(
    argv: list[str],
    *,
    show_window: int = 0,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    """Launch via local WMI, outside the caller's MCP kill-on-close job.

    cmd/start and CREATE_NEW_PROCESS_GROUP do not escape Windows Job Objects.
    WMI's local provider is independent of the MCP parent; BREAKAWAY also avoids
    the provider's own quota job. Never fall back to an inherited child process.
    """
    command = subprocess.list2cmdline(argv).replace("'", "''")
    directory = str(Path(__file__).resolve().parents[3]).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; "
        "$startup=([wmiclass]'Win32_ProcessStartup').CreateInstance(); "
        f"$startup.ShowWindow={int(show_window)}; "
        "$startup.WinstationDesktop='winsta0\\default'; "
        "$startup.CreateFlags=150994944; "  # BREAKAWAY_FROM_JOB | NO_WINDOW
        f"$result=([wmiclass]'Win32_Process').Create('{command}','{directory}',$startup); "
        "if($result.ReturnValue -ne 0){throw ('Win32_Process.Create rc='+$result.ReturnValue)}; "
        "[Console]::Out.WriteLine($result.ProcessId)"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    value = str(done.stdout or "").strip()
    if done.returncode or not value.isdigit() or int(value) <= 0:
        raise RuntimeError(f"independent Windows launch failed: {str(done.stderr or value)[:500]}")
    return int(value)


def start_priority_keeper(
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    independent_starter: Callable[..., int] = start_windows_independent_process,
) -> int | None:
    """Start the renderer-priority keeper; its named mutex makes this idempotent.

    17.11.2026: выключен — он снимал с фоновых вкладок EcoQoS и Idle, и скрытый ChatGPT
    ел ЦП на полной скорости. Вкладка нужна только на время отправки (выводится вперёд).
    """
    if not PRIORITY_KEEPER:
        return None
    repo = Path(__file__).resolve().parents[3]
    script = repo / "extension" / "fleet-tabs" / "fleet-priority.ps1"
    if not script.is_file():
        return None
    argv = [
        shutil.which("powershell.exe") or "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    if os.name == "nt":
        return independent_starter(argv)
    proc = popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return int(proc.pid)


def start_fleet_chrome(
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    binary: Path | None = None,
    independent_starter: Callable[..., int] = start_windows_independent_process,
) -> int:
    binary = binary or chrome_binary()
    if binary is None:
        raise FileNotFoundError("Google Chrome не найден")
    argv = chrome_argv(binary)
    if os.name == "nt":
        pid = independent_starter(argv, show_window=7)  # minimized, no focus
    else:
        proc = popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        pid = int(proc.pid)
    # Chrome itself may hand the launcher process off and exit immediately.  The
    # DevTools readiness probe below is authoritative; renderer priority is kept
    # by a separate idempotent helper.
    try:
        start_priority_keeper(popen=popen, independent_starter=independent_starter)
    except Exception:
        pass
    return pid


def fleet_chrome_main_pids(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[int]:
    """Главные процессы FleetChrome (профиль FleetChrome, без --type=): живой браузер или нет."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*FleetChrome*' -and $_.CommandLine -notlike '*--type=*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    done = run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return [int(x) for x in str(done.stdout or "").split() if x.isdigit()]


def kill_pids(
    pids: list[int],
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    for pid in pids:
        run(
            ["taskkill.exe", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def listener_pid(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int | None:
    """Return the PID bound to the dedicated DevTools port, if any."""
    done = run(
        ["netstat.exe", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for raw in str(done.stdout or "").splitlines():
        cols = raw.split()
        if len(cols) < 5 or cols[0].upper() != "TCP":
            continue
        local, state, pid = cols[1], cols[3].upper(), cols[4]
        if state == "LISTENING" and local.endswith(":9223") and pid.isdigit():
            return int(pid)
    return None


def stop_fleet_chrome(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int | None:
    """Kill only the browser tree that owns the dedicated DevTools listener.

    The ordinary user Chrome has no :9223 listener and is never targeted here.
    """
    pid = listener_pid(run=run)
    if pid is None:
        return None
    run(
        ["taskkill.exe", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return pid


def reload_fleet_extension(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Ask the loaded unpacked fleet extension to reload its current source.

    Restarting Chrome is not sufficient for unpacked MV3 extensions: Chrome may
    restore a registered service worker whose compiled modules predate the files
    on disk.  The helper talks to that service worker through the dedicated
    DevTools port and calls chrome.runtime.reload().  This keeps browser tabs and
    sessions intact. A failed request never authorizes restarting Chrome.
    """
    node = shutil.which("node") or shutil.which("node.exe")
    script = Path(__file__).resolve().parents[3] / "extension" / "fleet-tabs" / "reload-extension.mjs"
    if not node or not script.is_file():
        return False
    done = run(
        [node, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=12,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.returncode == 0 and "extension_reload=requested" in str(done.stdout or "")



def wake_fleet_extension(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Repair the extension's alarm without reloading it or touching user tabs."""
    node = shutil.which("node") or shutil.which("node.exe")
    script = Path(__file__).resolve().parents[3] / "extension" / "fleet-tabs" / "reload-extension.mjs"
    if not node or not script.is_file():
        return False
    done = run(
        [node, str(script), "--wake"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=12,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.returncode == 0 and "extension_wake=requested" in str(done.stdout or "")


def extension_tick_age_s(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> float | None:
    """Read the cycle-progress age directly from FleetChrome's extension storage.

    None = cannot probe (unknown, never evidence of a dead extension).
    +inf = extension exists but has no recorded cycle progress.
    This is a read-only CDP probe; no user tabs or extension state are modified.
    """
    node = shutil.which("node") or shutil.which("node.exe")
    script = Path(__file__).resolve().parents[3] / "extension" / "fleet-tabs" / "reload-extension.mjs"
    if not node or not script.is_file():
        return None
    try:
        done = run(
            [node, str(script), "--tick-age"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    for line in str(done.stdout or "").splitlines():
        if not line.startswith("extension_tick_age_s="):
            continue
        value = line.partition("=")[2].strip()
        if value == "missing":
            return float("inf")
        try:
            seconds = float(value)
        except ValueError:
            return None
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    return None


def _record_recovery(store: Store, **changes: Any) -> None:
    # Persist attempts independently of receipts and supervisor restarts. Otherwise
    # a dev deployment or a partial heartbeat resets the recovery budget.
    with _STATUS_LOCK:
        path = _status_path(store)
        data = _read_json(path)
        recovery = data.get("recovery") if isinstance(data.get("recovery"), dict) else {}
        data["recovery"] = {**recovery, **changes}
        _write_json(path, data)


def _recovery_age(recovery: dict[str, Any], key: str) -> float | None:
    stamp = _parse_stamp(recovery.get(key))
    return None if stamp is None else max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def fleet_demand(store: Store) -> FleetDemand:
    from . import queue

    try:
        _, runnable = queue.peek_next(store)
    except Exception:
        runnable = 0
    with store.read() as tx:
        active_claims = int(
            tx.scalar(
                "SELECT COUNT(*) FROM claims c JOIN workers w ON w.id=c.worker_id "
                "WHERE c.state='active' AND w.revoked_at IS NULL AND w.kind='chatgpt'"
            )
            or 0
        )
        unfinished = int(
            tx.scalar(
                "SELECT COUNT(*) FROM tasks t JOIN projects p ON p.name=t.project "
                "WHERE p.active=1 AND t.state IN ('ready','taken','submitted')"
            )
            or 0
        )
        notices = int(tx.scalar("SELECT COUNT(*) FROM owner_notices WHERE state='active' "
                               "AND (kind IN ('needs','attention') OR delivery_state NOT IN "
                               "('api_accepted','clicked','browser_attention'))") or 0
                      ) if tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_notices'") else 0
    try:
        from . import quick as quick_agents
        raw = str(getattr(store, "path", "") or "")
        quick = 0 if not raw or raw == ":memory:" else len(
            quick_agents.listing(quick_agents.Book(Path(raw).parent)))
    except Exception:  # noqa: BLE001 — книга быстрых агентов не должна ломать спрос
        quick = 0
    return FleetDemand(runnable=runnable, active_claims=active_claims, unfinished=unfinished,
                       owner_notices=notices, quick=quick)


def _parse_stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def extension_lease_age_s(
    store_or_path: Any,
    *,
    now: datetime | None = None,
) -> float | None:
    """Age of the last extension lease, separate from worker MCP heartbeats."""
    data = _read_json(_status_path(store_or_path))
    extension = data.get("extension") if isinstance(data.get("extension"), dict) else {}
    seen = _parse_stamp(extension.get("seen_at"))
    if seen is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - seen).total_seconds())


def status_line(store_or_path: Any, *, now: datetime | None = None) -> str:
    path = _status_path(store_or_path)
    data = _read_json(path)
    if not data:
        return "launcher: supervisor ещё не оставлял health-снимок"

    supervisor = data.get("supervisor") if isinstance(data.get("supervisor"), dict) else {}
    extension = data.get("extension") if isinstance(data.get("extension"), dict) else {}
    browser = "up" if supervisor.get("browser_ready") else "down"
    action = str(supervisor.get("action") or "unknown")
    if action in {"waiting-devtools", "devtools-unreachable"}:
        browser = "unobserved"
    demand = supervisor.get("demand") if isinstance(supervisor.get("demand"), dict) else {}
    bits = [
        f"launcher: browser={browser}",
        f"supervisor={action}",
        f"runnable={int(demand.get('runnable') or 0)}",
        f"active_claims={int(demand.get('active_claims') or 0)}",
    ]

    seen = _parse_stamp(extension.get("seen_at"))
    if seen is None:
        bits.append("extension=never-seen")
    else:
        current = now or datetime.now(timezone.utc)
        age = max(0, int((current - seen).total_seconds()))
        version = str(extension.get("version") or "?")
        state = "alive" if age <= EXTENSION_STALE_S else "stale"
        bits.append(f"extension={state} v{version} age={age}s")
        try:
            tabs = int(extension.get("tabs", -1))
            pending = int(extension.get("pending", -1))
        except (TypeError, ValueError):
            tabs, pending = -1, -1
        if tabs >= 0:
            bits.append(f"tabs={tabs}")
        if pending >= 0:
            bits.append(f"pending={pending}")
    error = str(supervisor.get("last_error") or "").strip()
    if error:
        bits.append(f"error={error[:160]}")
    return "; ".join(bits)


def _write_supervisor_status(
    store: Store,
    *,
    demand: FleetDemand,
    ready: bool,
    action: str,
    last_error: str = "",
) -> None:
    path = _status_path(store)
    if path is None:
        return
    with _STATUS_LOCK:
        data = _read_json(path)
        data["supervisor"] = {
            "checked_at": _utcnow(),
            "browser_ready": bool(ready),
            "action": str(action),
            "last_error": str(last_error),
            "demand": {
                "runnable": demand.runnable,
                "active_claims": demand.active_claims,
                "unfinished": demand.unfinished,
            "owner_notices": demand.owner_notices,
                "quick": demand.quick,
            },
        }
        _write_json(path, data)


def supervise(
    db_path: str | Path,
    audit: Any,
    stop_event: threading.Event,
    *,
    interval_s: float = SUPERVISOR_INTERVAL_S,
    start_timeout_s: float = START_TIMEOUT_S,
    probe: Callable[..., bool] = browser_ready,
    starter: Callable[..., int] = start_fleet_chrome,
    stopper: Callable[..., int | None] = stop_fleet_chrome,
    extension_reloader: Callable[..., bool] = reload_fleet_extension,
    extension_waker: Callable[..., bool] = wake_fleet_extension,
    tick_age_finder: Callable[[], float | None] = extension_tick_age_s,
    storage_probe: Callable[[], dict[str, Any]] = probe_extension_storage,
    storage_repairer: Callable[[], dict[str, Any]] = repair_extension_storage,
    alive_finder: Callable[..., list[int]] = fleet_chrome_main_pids,
    killer: Callable[..., None] = kill_pids,
) -> None:
    """Start an absent FleetChrome and repair positively stalled extension cycles.

    Worker MCP heartbeats are deliberately irrelevant here: a server-side ChatGPT
    turn can keep calling MCP after the local browser has exited.  The browser is
    proved by DevTools :9223 OR its dedicated process; extension transport receipts
    are not work progress. Unobservable state never authorizes a browser restart.
    A measured sticky storage I/O error has a separate backup + graceful reopen
    path. Lease age and an unavailable probe never authorize a browser restart.
    stopper/killer remain injectable for compatibility; this loop never uses them.
    """
    store = Store.open(str(db_path))
    failures = 0
    next_attempt = 0.0
    browser_ready_since: float | None = None
    previous_action = ""
    alive_silent_since: float | None = None

    while not stop_event.is_set():
        try:
            demand = fleet_demand(store)
            now_mono = time.monotonic()
            ready = bool(probe())
            control = browser_control(store)
            if control["halt"] or not control["autostart"]:
                # Намерение владельца сильнее спроса: ни запуска, ни reload, ни kill.
                failures = 0
                next_attempt = 0.0
                alive_silent_since = None
                _write_supervisor_status(
                    store, demand=demand, ready=ready,
                    action="halted" if control["halt"] else "owner-off",
                    last_error=(f"остановлено: {control['halt'].get('reason', '')}"[:200]
                                if control["halt"] else "автозапуск браузера выключен владельцем"),
                )
                stop_event.wait(interval_s)
                continue
            if ready:
                alive_silent_since = None
                if browser_ready_since is None:
                    browser_ready_since = now_mono
            else:
                browser_ready_since = None

            action = "idle-no-work"
            error = ""

            if demand.needed and ready:
                lease_age = extension_lease_age_s(store)
                lease_alive = lease_age is not None and lease_age <= EXTENSION_STALE_S
                browser_age = now_mono - (browser_ready_since if browser_ready_since is not None else now_mono)
                # Probe only after Chrome's startup grace: fresh lease and stalled
                # tick is the night-long failure that the old supervisor missed.
                tick_age = (
                    tick_age_finder()
                    if browser_age >= EXTENSION_BOOT_GRACE_S
                    else None
                )
                tick_stale = tick_age is not None and tick_age > EXTENSION_TICK_STALE_S

                direct_alive = tick_age is not None and tick_age <= EXTENSION_TICK_STALE_S
                snapshot = _read_json(_status_path(store))
                recovery = snapshot.get("recovery") if isinstance(snapshot.get("recovery"), dict) else {}
                wake_age = _recovery_age(recovery, "wake_at")
                reload_age = _recovery_age(recovery, "reload_at")
                inspect_storage = tick_stale or (
                    tick_age is None and not lease_alive and browser_age >= EXTENSION_BOOT_GRACE_S)
                storage = storage_probe() if inspect_storage else {}
                if storage.get("state") == "io-error":
                    repair_age = _recovery_age(recovery, "storage_repair_at")
                    error = "хранилище FleetChrome: " + str(storage.get("detail", "I/O failure"))[:500]
                    if repair_age is not None and repair_age < STORAGE_REPAIR_COOLDOWN_S:
                        action = "storage-repair-backoff"
                    else:
                        # Persist the budget BEFORE closing anything; never spin
                        # on restart after an interrupted repair or lost receipt.
                        _record_recovery(store, storage_repair_at=_utcnow())
                        result = storage_repairer()
                        state = result.get("state", "unknown")
                        action = "storage-repaired" if state == "repaired" else "storage-" + str(state)
                        if state in {"waiting-space", "not-required", "unobserved"}:
                            _record_recovery(store, storage_repair_at=None)
                        if state == "repaired":
                            error = ""
                            _record_recovery(store, wake_at=None)
                        else:
                            error += "; " + str(result.get("detail", state))[:300]
                        audit.record("fleet_browser_storage_repair", result,
                                     decision="allow" if state == "repaired" else "deny",
                                     reason="verified storage write/readback" if state == "repaired" else error)
                elif (lease_alive and not tick_stale) or direct_alive:
                    action = "healthy" if lease_alive else "control-unreachable"
                    if not lease_alive:
                        error = "локальный UI-цикл жив; MCP lease устарел — Chrome не перезапускаю"
                    failures = 0
                    next_attempt = 0.0
                    if recovery.get("wake_at"):
                        _record_recovery(store, wake_at=None)
                elif browser_age < EXTENSION_BOOT_GRACE_S:
                    # A freshly opened browser needs time for the MV3 service worker
                    # to wake, read storage and make its first fleet_list call.
                    action = "waiting-extension"
                elif tick_age is None:
                    action = "extension-unobserved"
                    error = "нет прямого наблюдения цикла; старый MCP lease не доказывает зависание"
                elif reload_age is not None and reload_age < EXTENSION_RELOAD_COOLDOWN_S:
                    action = "extension-backoff"
                    error = "повторный reload ограничен сохранённым cooldown; браузер сохранён"
                elif wake_age is not None and wake_age < EXTENSION_WAKE_GRACE_S:
                    action = "waiting-extension-wake"
                else:
                    # Wake the normal serialized loop first. Only a second direct
                    # stale observation AFTER that grace permits extension reload.
                    reload_due = wake_age is not None
                    _record_recovery(store, **({"reload_at": _utcnow(), "wake_at": None}
                                              if reload_due else {"wake_at": _utcnow()}))
                    try:
                        requested = bool(extension_reloader() if reload_due else extension_waker())
                    except Exception as exc:  # noqa: BLE001
                        requested = False
                        error = f"{type(exc).__name__}: {exc}"
                    action = (
                        ("reloading-extension" if reload_due else "waking-extension")
                        if requested else "extension-recovery-failed"
                    )
                    audit.record(
                        "fleet_chrome_supervisor",
                        {"extension_age_s": None if lease_age is None else int(lease_age),
                         "tick_age_s": None if not math.isfinite(tick_age) else int(tick_age)},
                        decision="allow" if requested else "deny",
                        reason=f"fleet-tabs {'reload' if reload_due else 'alarm wake'} "
                               f"{'requested' if requested else 'failed'} through DevTools (tick stale); browser preserved",
                    )
            elif demand.needed:
                now_mono = time.monotonic()
                alive = [] if now_mono < next_attempt else list(alive_finder() or [])
                if now_mono < next_attempt:
                    action = "backoff"
                elif alive:
                    # Не запускать второй chrome.exe в живом браузере — он откроет лишнее окно.
                    alive_silent_since = alive_silent_since or now_mono
                    if now_mono - alive_silent_since < ALIVE_SILENT_GRACE_S:
                        action = "waiting-devtools"
                    else:
                        action = "devtools-unreachable"
                        error = "процесс FleetChrome жив; недоступный DevTools не разрешает kill или второй запуск"
                else:
                    alive_silent_since = None
                    action = "starting"
                    try:
                        pid = int(starter())
                        deadline = time.monotonic() + start_timeout_s
                        while not stop_event.is_set() and time.monotonic() < deadline:
                            if probe():
                                ready = True
                                browser_ready_since = time.monotonic()
                                break
                            stop_event.wait(0.5)
                        if ready:
                            action = "started"
                            failures = 0
                            next_attempt = 0.0
                            audit.record(
                                "fleet_chrome_supervisor",
                                {"pid": pid, "runnable": demand.runnable, "active_claims": demand.active_claims},
                                decision="allow",
                                reason="FleetChrome поднят по живому спросу флота",
                            )
                        else:
                            raise RuntimeError(f"DevTools :9223 не ответил за {start_timeout_s:.0f} с")
                    except Exception as exc:  # noqa: BLE001 - supervisor must stay alive
                        failures += 1
                        delay = _BACKOFF_S[min(failures - 1, len(_BACKOFF_S) - 1)]
                        next_attempt = time.monotonic() + delay
                        action = "start-failed"
                        error = f"{type(exc).__name__}: {exc}"
                        audit.record(
                            "fleet_chrome_supervisor",
                            {"runnable": demand.runnable, "active_claims": demand.active_claims},
                            decision="deny",
                            reason=f"{error}; retry через {int(delay)} с",
                        )
            else:
                failures = 0
                next_attempt = 0.0

            _write_supervisor_status(
                store,
                demand=demand,
                ready=ready,
                action=action,
                last_error=error,
            )
            if action != previous_action and action in {
                "healthy",
                "idle-no-work",
                "backoff",
                "waiting-extension",
                "waiting-extension-wake",
                "extension-backoff",
            }:
                previous_action = action
        except Exception as exc:  # noqa: BLE001
            try:
                audit.record(
                    "fleet_chrome_supervisor",
                    {},
                    decision="deny",
                    reason=f"цикл supervisor: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        stop_event.wait(interval_s)
