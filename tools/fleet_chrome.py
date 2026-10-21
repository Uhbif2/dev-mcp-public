from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

DEBUG_URL = "http://127.0.0.1:9223/json/version"
DEBUG_PORT = 9223
START_TIMEOUT_S = 20.0


def browser_ready(timeout_s: float = 0.7) -> bool:
    try:
        with urllib.request.urlopen(DEBUG_URL, timeout=timeout_s) as response:
            if response.status != 200:
                return False
            data = json.loads(response.read().decode("utf-8", "replace"))
            return bool(data.get("Browser"))
    except Exception:
        return False


def chrome_binary() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("Google Chrome not found")


def chrome_args() -> list[str]:
    profile = Path(os.environ["LOCALAPPDATA"]) / "FleetChrome"
    return [
        str(chrome_binary()),
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={DEBUG_PORT}",
        "--start-minimized",
        "--restore-last-session",
        "--new-window",
        "--no-first-run",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
        "https://chatgpt.com/",
    ]


def start_priority_keeper() -> None:
    script = Path(__file__).resolve().parents[1] / "extension" / "fleet-tabs" / "fleet-priority.ps1"
    if not script.is_file():
        return
    subprocess.Popen(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-File", str(script),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def start() -> str:
    if browser_ready():
        start_priority_keeper()
        return "fleet_chrome=already_ready devtools=9223"
    proc = subprocess.Popen(
        chrome_args(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    start_priority_keeper()
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if browser_ready():
            # A process merely appearing is not success; require the dedicated
            # browser's DevTools endpoint to remain reachable for a short settle.
            time.sleep(2)
            if browser_ready():
                return f"fleet_chrome=started pid={proc.pid} devtools=9223 ready=1"
        time.sleep(0.5)
    raise RuntimeError("FleetChrome did not become stably ready on DevTools :9223")


def listener_pid() -> int | None:
    done = subprocess.run(
        ["netstat.exe", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for raw in done.stdout.splitlines():
        cols = raw.split()
        if len(cols) < 5 or cols[0].upper() != "TCP":
            continue
        local, state, pid = cols[1], cols[3].upper(), cols[4]
        if state != "LISTENING":
            continue
        if local.endswith(f":{DEBUG_PORT}") and pid.isdigit():
            return int(pid)
    return None


def stop() -> str:
    pid = listener_pid()
    if pid is None:
        return "fleet_chrome=already_stopped"
    done = subprocess.run(
        ["taskkill.exe", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not browser_ready():
            return f"fleet_chrome=stopped pid={pid}"
        time.sleep(0.3)
    raise RuntimeError(
        f"FleetChrome DevTools :9223 still responds after taskkill pid={pid}; "
        f"rc={done.returncode}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "restart", "status"))
    args = parser.parse_args()
    try:
        if args.action == "start":
            result = start()
        elif args.action == "stop":
            result = stop()
        elif args.action == "restart":
            stop()
            result = start()
        else:
            result = f"fleet_chrome={'ready' if browser_ready() else 'down'} devtools=9223"
        print(result)
        return 0
    except Exception as exc:
        print(f"fleet_chrome=error type={type(exc).__name__} detail={exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
