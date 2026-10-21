from __future__ import annotations

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
from typing import Any, Callable

from .store import Store

DEBUG_URL = "http://127.0.0.1:9223/json/version"
STATUS_FILE = "launcher-health.json"
SUPERVISOR_INTERVAL_S = 15.0
START_TIMEOUT_S = 20.0
EXTENSION_STALE_S = 90.0
# A fresh fleet_list lease does not prove that the extension's 30s work loop finished.
# Reload only after several missed ticks; the existing reload grace provides escalation.
EXTENSION_TICK_STALE_S = 240.0
EXTENSION_BOOT_GRACE_S = 75.0
EXTENSION_RELOAD_GRACE_S = 45.0
EXTENSION_RECOVERY_BACKOFF_S = 300.0
_BACKOFF_S = (15.0, 30.0, 60.0, 120.0, 300.0)
# Жив процесс FleetChrome, а DevTools молчит: это загрузка или тормоз, а не «браузера нет».
# Второй запуск chrome.exe с тем же профилем открыл бы в живом браузере ЕЩЁ ОДНО окно
# (07.10: четыре окна, 16 минут простоя); по истечении срока дерево убивается и браузер поднимается заново.
ALIVE_SILENT_GRACE_S = 120.0
_STATUS_LOCK = threading.Lock()
# 16.10.2026 14–18 UTC: 117 перезапусков FleetChrome (каждые ~2 мин) — тик расширения не
# успевал завершиться после очередного рестарта. Больше этого числа рестартов «ради
# расширения» в час — не лечим браузер, а зовём оператора.
MAX_EXTENSION_RESTARTS_PER_HOUR = 4
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
        data = _read_json(path)
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

    @property
    def needed(self) -> bool:
        # ready/taken/submitted keeps the launcher alive even when dependencies or
        # an active claim temporarily make the exact runnable frontier empty.
        return (self.runnable > 0 or self.active_claims > 0 or self.unfinished > 0
                or self.owner_notices > 0)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status_path(store_or_path: Any) -> Path | None:
    raw = str(getattr(store_or_path, "path", store_or_path) or "")
    if not raw or raw == ":memory:":
        return None
    return Path(raw).with_name(STATUS_FILE)


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path | None, data: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def chrome_argv(binary: Path, local_appdata: str | None = None) -> list[str]:
    local = Path(local_appdata or os.environ.get("LOCALAPPDATA", ""))
    profile = local / "FleetChrome"
    return [
        str(binary),
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9223",
        "--start-minimized",
        "--restore-last-session",
        "--no-first-run",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
        "https://chatgpt.com/",
    ]


def start_priority_keeper(
    *,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> int | None:
    """Start the renderer-priority keeper; its named mutex makes this idempotent."""
    repo = Path(__file__).resolve().parents[3]
    script = repo / "extension" / "fleet-tabs" / "fleet-priority.ps1"
    if not script.is_file():
        return None
    proc = popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
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
) -> int:
    binary = binary or chrome_binary()
    if binary is None:
        raise FileNotFoundError("Google Chrome не найден")
    argv = chrome_argv(binary)
    if os.name == "nt":
        # 17.10: Chrome был потомком dev-mcp, и каждый перезапуск dev (taskkill /T по
        # дереву прокси) убивал браузер флота. `start` через cmd, который сразу выходит,
        # отрывает Chrome от дерева сервера.
        argv = ["cmd.exe", "/d", "/c", "start", "", *argv]
    proc = popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt"
                       else getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
    )
    # Chrome itself may hand the launcher process off and exit immediately.  The
    # DevTools readiness probe below is authoritative; renderer priority is kept
    # by a separate idempotent helper.
    try:
        start_priority_keeper(popen=popen)
    except Exception:
        pass
    return int(proc.pid)


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
    sessions intact; full FleetChrome restart remains a fallback.
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



def extension_tick_age_s(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> float | None:
    """Read the completed-tick age directly from FleetChrome's extension storage.

    None = cannot probe (keep legacy lease-based recovery).
    +inf = extension exists but has not completed a tick.
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
    return FleetDemand(runnable=runnable, active_claims=active_claims, unfinished=unfinished,owner_notices=notices)


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
    tick_age_finder: Callable[[], float | None] = extension_tick_age_s,
    alive_finder: Callable[..., list[int]] = fleet_chrome_main_pids,
    killer: Callable[..., None] = kill_pids,
) -> None:
    """Keep both FleetChrome *and its extension* healthy while work exists.

    Worker MCP heartbeats are deliberately irrelevant here: a server-side ChatGPT
    turn can keep calling MCP after the local browser has exited.  The browser is
    proved by DevTools :9223; the extension is proved by its separate fleet_list
    lease.  If either half disappears, dev-mcp owns the outer self-heal loop.
    """
    store = Store.open(str(db_path))
    failures = 0
    next_attempt = 0.0
    extension_retry_after = 0.0
    extension_reload_started_at = 0.0
    browser_ready_since: float | None = None
    previous_action = ""
    alive_silent_since: float | None = None
    restarts: list[float] = []

    while not stop_event.is_set():
        try:
            demand = fleet_demand(store)
            now_mono = time.monotonic()
            ready = bool(probe())
            control = browser_control(store)
            restarts = [t for t in restarts if now_mono - t < 3600]
            if control["halt"] or not control["autostart"]:
                # Намерение владельца сильнее спроса: ни запуска, ни reload, ни kill.
                failures = 0
                next_attempt = extension_retry_after = extension_reload_started_at = 0.0
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
                if browser_ready_since is None:
                    browser_ready_since = now_mono
            else:
                browser_ready_since = None

            action = "idle-no-work"
            error = ""

            if demand.needed and ready:
                lease_age = extension_lease_age_s(store)
                lease_alive = lease_age is not None and lease_age <= EXTENSION_STALE_S
                browser_age = now_mono - (browser_ready_since or now_mono)
                # Probe only after Chrome's startup grace: fresh lease and stalled
                # tick is the night-long failure that the old supervisor missed.
                tick_age = (
                    tick_age_finder()
                    if browser_age >= EXTENSION_BOOT_GRACE_S
                    else None
                )
                tick_stale = tick_age is not None and tick_age > EXTENSION_TICK_STALE_S

                direct_alive = tick_age is not None and tick_age <= EXTENSION_STALE_S
                if (lease_alive and not tick_stale) or direct_alive:
                    action = "healthy" if lease_alive else "control-unreachable"
                    if not lease_alive:
                        error = "локальный UI-цикл жив; MCP lease устарел — Chrome не перезапускаю"
                    failures = 0
                    next_attempt = 0.0
                    extension_retry_after = 0.0
                    extension_reload_started_at = 0.0
                elif browser_age < EXTENSION_BOOT_GRACE_S:
                    # A freshly opened browser needs time for the MV3 service worker
                    # to wake, read storage and make its first fleet_list call.
                    action = "waiting-extension"
                elif extension_reload_started_at and now_mono < extension_retry_after:
                    action = "waiting-extension-reload"
                elif now_mono < extension_retry_after:
                    action = "extension-backoff"
                else:
                    # First heal the lightest possible layer.  Chrome may keep an
                    # unpacked MV3 service worker compiled from older source even
                    # after a browser restart; chrome.runtime.reload() refreshes the
                    # extension in-place and preserves all ChatGPT tabs.
                    should_restart_browser = bool(extension_reload_started_at)
                    reload_error = ""
                    if not should_restart_browser:
                        try:
                            reloaded = bool(extension_reloader())
                        except Exception as exc:  # noqa: BLE001
                            reloaded = False
                            reload_error = f"{type(exc).__name__}: {exc}"
                        if reloaded:
                            extension_reload_started_at = now_mono
                            extension_retry_after = now_mono + EXTENSION_RELOAD_GRACE_S
                            action = "reloading-extension"
                            audit.record(
                                "fleet_chrome_supervisor",
                                {
                                    "runnable": demand.runnable,
                                    "active_claims": demand.active_claims,
                                    "extension_age_s": None if lease_age is None else int(lease_age),
                                    "tick_age_s": None if tick_age is None or not math.isfinite(tick_age) else int(tick_age),
                                    "tick_stale": tick_stale,
                                },
                                decision="allow",
                                reason=("fleet-tabs extension reload requested through DevTools (tick stale)"
                                        if tick_stale else "fleet-tabs extension reload requested through DevTools"),
                            )
                        else:
                            should_restart_browser = True

                    if should_restart_browser and len(restarts) >= MAX_EXTENSION_RESTARTS_PER_HOUR:
                        should_restart_browser = False
                        action = "restart-cap"
                        error = (f"{len(restarts)} перезапусков браузера за час не вылечили расширение — "
                                 "дальше не перезапускаю; нужна диагностика (fleet.cmd, лог расширения)")
                        extension_reload_started_at = 0.0
                        extension_retry_after = now_mono + EXTENSION_RECOVERY_BACKOFF_S
                    if should_restart_browser:
                        restarts.append(now_mono)
                        try:
                            old_pid = stopper()
                            ready = False
                            browser_ready_since = None
                            pid = int(starter())
                            deadline = time.monotonic() + start_timeout_s
                            while not stop_event.is_set() and time.monotonic() < deadline:
                                if probe():
                                    ready = True
                                    browser_ready_since = time.monotonic()
                                    break
                                stop_event.wait(0.5)
                            if not ready:
                                raise RuntimeError(
                                    f"DevTools :9223 не ответил после recovery за {start_timeout_s:.0f} с"
                                )
                            action = "restarted-for-extension"
                            failures = 0
                            next_attempt = 0.0
                            extension_reload_started_at = 0.0
                            # Browser restart is the heavier fallback.  Give its
                            # restored extension a long cooldown before another one.
                            extension_retry_after = time.monotonic() + EXTENSION_RECOVERY_BACKOFF_S
                            audit.record(
                                "fleet_chrome_supervisor",
                                {
                                    "old_pid": old_pid,
                                    "pid": pid,
                                    "runnable": demand.runnable,
                                    "active_claims": demand.active_claims,
                                    "extension_age_s": None if lease_age is None else int(lease_age),
                                    "reload_error": reload_error,
                                    "tick_stale": tick_stale,
                                },
                                decision="allow",
                                reason=("FleetChrome перезапущен: extension reload не восстановил tick"
                                        if tick_stale else "FleetChrome перезапущен: extension reload не восстановил lease"),
                            )
                        except Exception as exc:  # noqa: BLE001 - supervisor must stay alive
                            failures += 1
                            delay = _BACKOFF_S[min(failures - 1, len(_BACKOFF_S) - 1)]
                            extension_reload_started_at = 0.0
                            extension_retry_after = time.monotonic() + delay
                            action = "extension-restart-failed"
                            error = f"{type(exc).__name__}: {exc}"
                            audit.record(
                                "fleet_chrome_supervisor",
                                {
                                    "runnable": demand.runnable,
                                    "active_claims": demand.active_claims,
                                    "extension_age_s": None if lease_age is None else int(lease_age),
                                    "reload_error": reload_error,
                                },
                                decision="deny",
                                reason=f"{error}; retry extension recovery через {int(delay)} с",
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
                        killer(alive)
                        alive_silent_since = None
                        action = "killed-silent-browser"
                        audit.record(
                            "fleet_chrome_supervisor",
                            {"pids": alive, "runnable": demand.runnable, "active_claims": demand.active_claims},
                            decision="deny",
                            reason=f"FleetChrome жив, но DevTools молчит > {ALIVE_SILENT_GRACE_S:.0f} с: дерево убито, поднимется заново",
                        )
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
                extension_retry_after = 0.0
                extension_reload_started_at = 0.0

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
                "waiting-extension-reload",
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
