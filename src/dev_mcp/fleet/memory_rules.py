"""Pilot: read-only owner rules from the existing ambient-memory memory_service retrieval.

The warm ambientd already owns memory_service search and applicability judging.  Do not
import its Claude hooks or start a second memory_service writer.  Only *selected rules*
are passed to a fleet brief; organic/canon/other-project records are never shown.
An unavailable retrieval is explicit, not equivalent to an empty result.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from . import goals

TTL_S = 600
ERROR_TTL_S = 15  # never keep an unavailable task result for more than 60 seconds
CIRCUIT_RETRY_S = 60.0  # one shared recovery probe per minute after an upstream failure
# The warm ambientd performs memory_service search *and* Jev applicability judging.
# Real requests take 2-17 s under concurrent load; a 2.8 s ceiling dropped
# most valid rules. Stay below the ambientd HTTP handler's 25 s deadline.
TIMEOUT_S = 20.0
MAX_ITEMS = 6
MAX_CHARS = 1500
LOG_PATH = goals.DEFAULT_PATH.parent / "logs" / "memory-rules.jsonl"
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fleet-memory-rules")
_lock = threading.RLock()
_cache: dict[tuple[str, str, str], tuple[float, str, tuple[str, ...], str]] = {}
_circuit_open_until = 0.0
_circuit_probe = False
KEEPER_INTERVAL_S = 120.0
KEEPER_RETRY_S = 60.0
KEEPER_LOG_PATH = LOG_PATH.with_name("memory-rules-keeper.jsonl")
_keeper_lock = threading.Lock()
_keeper_next_attempt = 0.0
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|"
    r"Bearer\s+\S{12,}|\b(?:password|api[_ -]?key|secret)\s*[:=]\s*\S+)",
    re.I,
)
_PROJECT = re.compile(r"\b(?:проект|project)\s*[:=]\s*([^;.,|\n]+)", re.I)


def enabled(project: str, role: str, *, quick_owner: str = "") -> bool:
    """Operator-controlled settings only: project overlays cannot enable memory."""
    try:
        raw = goals._load_doc()  # same live mtime cache as fleet-projects.toml
        platform = raw.get("platform", {}).get("memory_rules", {})
        project_settings = raw.get("projects", {}).get(project, {})
        if isinstance(project_settings, dict) and isinstance(project_settings.get("memory_rules"), bool):
            return project_settings["memory_rules"]
        if not isinstance(platform, dict):
            platform = {}
        if role == "quick":
            claude = quick_owner == "claude" or quick_owner.startswith("claude:")
            return platform.get("quick_claude" if claude else "quick_other", claude) is True
        return platform.get("fleet_default", False) is True
    except (OSError, ValueError, TypeError, AttributeError):
        return role == "quick" and (quick_owner == "claude" or quick_owner.startswith("claude:"))


def _read_ambient(question: str, session: str) -> dict[str, Any]:
    """Existing ambientd HTTP hook transport, strictly read-only memory_service search.

    Never spawn ambientd here: cold imports can exceed the task-brief budget.
    The independent production controller keeper owns daemon availability.
    """
    port = int(os.environ.get("AM_DAEMON_PORT", "11437"))
    payload = json.dumps({
        "hook_event_name": "UserPromptSubmit", "session_id": "fleet-rules-" + session,
        "prompt": question, "transcript_path": "",
    }, ensure_ascii=False).encode("utf-8")
    request = Request(f"http://127.0.0.1:{port}/", payload,
                      {"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=TIMEOUT_S) as response:
        data = json.loads(response.read(128_000))
    if not isinstance(data, dict):
        raise ValueError("неверный ответ ambientd")
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:100])
    return data


def _extract(data: dict[str, Any], project: str) -> tuple[str, ...]:
    """Use the Jev-selected rules only; never leak organic/canon or raw candidates."""
    selected: list[str] = []
    trace = data.get("trace")
    if isinstance(trace, list):
        for row in trace:
            if isinstance(row, (list, tuple)) and len(row) > 4 and row[1] == "rul":
                line = row[4]
                if isinstance(line, str) and line.startswith("- [правило] "):
                    selected.append(line.removeprefix("- [правило] ").strip())
    if not selected:
        selected = [
            line.removeprefix("- [правило] ").strip()
            for line in str(data.get("context") or "").splitlines()
            if line.startswith("- [правило] ")
        ]
    result: list[str] = []
    seen: set[str] = set()
    for item in selected:
        if not item or _SECRET.search(item):
            continue
        # Rules explicitly limited to another project are not cross-delivered.
        match = _PROJECT.search(item)
        if match and match.group(1).strip().casefold() not in ("все", "all", "любой", project.casefold()):
            continue
        compact = " ".join(item.split())
        if compact not in seen:
            seen.add(compact)
            result.append(compact)
        if len(result) >= MAX_ITEMS:
            break
    return tuple(result)


def _record(project: str, role: str, digest: str, count: int, status: str,
            latency_ms: int, cached: bool, *, source: str = "ambient",
            failure_kind: str = "") -> None:
    # No prompt, rule body, secret or raw memory_service payload in the journal.
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": project[:100], "role": role[:32], "task_sha256": digest,
        "rules": count, "latency_ms": latency_ms, "unavailable": status != "ok",
        "status": status, "cache_hit": cached, "source": source,
        "failure_kind": failure_kind if status != "ok" else "",
    }
    try:
        with _lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # metrics cannot block or break task delivery


def render(project: str, task_text: str, role: str, *, quick_owner: str = "") -> str:
    """Return bounded task-brief block, or '' only when the pilot is disabled."""
    global _circuit_open_until, _circuit_probe
    if not enabled(project, role, quick_owner=quick_owner):
        return ""
    digest = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
    key = (project, role, digest)
    started = now = time.monotonic()
    cached = bypass = probe = False
    with _lock:
        hit = _cache.get(key)
        if hit and now < hit[0]:
            _, status, items, reason = hit
            cached = True
        elif _circuit_probe or now < _circuit_open_until:
            # Shared failure state: do not enqueue 20-second searches for every
            # distinct task while memory_service or ambientd is known to be unavailable.
            status, items, reason = "unavailable", (), "circuit breaker (ожидание проверки)"
            bypass = True
        elif _circuit_open_until:
            # After the cooldown permit exactly one caller to test recovery.
            # All other concurrent briefs fail quickly until that probe finishes.
            _circuit_probe = probe = True
    if not cached and not bypass:
        # Keep this a single focused query: ambient.process splits list/newline
        # prompts into up to eight expensive searches, and a full TZ body can
        # overwhelm the selection budget before the brief is even delivered.
        summary = " ".join(task_text.split())[:1000]
        question = f"Проект: {project}. Роль: {role}. Задача: {summary}"
        future = None
        try:
            # ambientd suppresses already-delivered rules within one session.
            # A cache refresh must be an independent read, not a follow-up turn
            # of the previous request (there is no real conversation here).
            session = digest[:16] + "-" + uuid.uuid4().hex[:12]
            future = _pool.submit(_read_ambient, question, session)
            data = future.result(timeout=TIMEOUT_S)
            # Honor an explicit failure/degradation signal when the upstream
            # supplies it. An empty context alone is a legitimate zero-match.
            if data.get("search_status") in ("failed", "degraded", "unavailable"):
                raise RuntimeError("graph_search_unavailable")
            items = _extract(data, project)
            status, reason = "ok", ""
        except FutureTimeout:
            # Under bursts, some futures have not started yet. Discard expired
            # queued searches instead of running stale, costly Jev work later.
            if future is not None:
                future.cancel()
            status, items, reason = "unavailable", (), f"таймаут recall (>{TIMEOUT_S:g} с)"
        except Exception as exc:
            status, items = "unavailable", ()
            reason = f"{type(exc).__name__}"[:80]
        with _lock:
            if status != "ok":
                _circuit_open_until = max(_circuit_open_until,
                                          time.monotonic() + CIRCUIT_RETRY_S)
            elif probe:
                _circuit_open_until = 0.0
            if probe:
                _circuit_probe = False
    if not cached:
        with _lock:
            _cache[key] = (time.monotonic() + (TTL_S if status == "ok" else ERROR_TTL_S),
                           status, items, reason)
            if len(_cache) > 1024:
                expired = [k for k, v in _cache.items() if v[0] < time.monotonic()]
                for k in expired:
                    _cache.pop(k, None)
    lines = ["Правила владельца для этой задачи:"]
    if status != "ok":
        lines.append(f"  правила памяти недоступны ({reason})")
    elif not items:
        lines.append("  применимые правила не выданы (проверка ambient-memory)")
    else:
        for item in items:
            line = f"  - {item}"
            if len("\n".join([*lines, line])) > MAX_CHARS:
                remaining = MAX_CHARS - len("\n".join(lines)) - 5
                if remaining >= 50:
                    lines.append(line[:remaining].rstrip() + "…")
                break
            lines.append(line)
    result = "\n".join(lines)[:MAX_CHARS]
    _record(project, role, digest, len(lines) - 1 if status == "ok" and items else 0,
            status, int((time.monotonic() - started) * 1000), cached,
            source="cache" if cached else ("circuit" if bypass else "ambient"),
            failure_kind=reason if status != "ok" else "")
    return result


def _daemon_ping(port: int) -> bool:
    """A ping renews ambientd's three-hour idle timer without searching memory."""
    req = Request(f"http://127.0.0.1:{port}/", data=b'{"ping": true}',
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=1.5) as response:
            return json.loads(response.read(256)).get("ok") is True
    except (OSError, ValueError, TypeError):
        return False


def _daemon_listening(port: int) -> bool:
    """A busy or stalled listener must never trigger a competing daemon."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _keeper_log(status: str, *, detail: str = "") -> None:
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "status": status, "detail": detail[:80]}
    try:
        KEEPER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with KEEPER_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _start_ambient_daemon(root: Path) -> int:
    """Use the exact trusted Python environment installed for Claude's hook.

    The hook's command is an operator-managed path, but only the memory_service venv
    interpreter is accepted: an arbitrary command in hooks.json is not an
    execution grant. The script is fixed, not read from that config.
    """
    hooks = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    if (not isinstance(command, str) or
            not command.replace("\\", "/").casefold().endswith(
                "/memory_service/mcp_server/.venv/scripts/python.exe")):
        raise ValueError("untrusted_ambient_interpreter")
    python = Path(command)
    script = root / "ambientd.py"
    if not python.is_file() or not script.is_file() or script.is_symlink():
        raise FileNotFoundError("ambient_daemon_or_python_missing")
    pythonw = python.with_name("pythonw.exe")
    executable = pythonw if pythonw.is_file() else python
    base = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    last_error: OSError | None = None
    for flags in (base | 0x01000000, base):  # break away from the MCP job object
        try:
            process = subprocess.Popen([str(executable), str(script)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, close_fds=True,
                                       creationflags=flags, cwd=str(root))
            return int(process.pid)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("ambient_spawn_failed")


def daemon_keeper_tick(*, root: Path | None = None, port: int | None = None) -> str:
    """Production-only maintenance: bounded ping/spawn, never called from render.

    The single controller worker owns this operation. A competing Claude hook
    can attempt startup concurrently; ambientd's port bind is single-instance.
    """
    global _keeper_next_attempt
    port = int(port if port is not None else os.environ.get("AM_DAEMON_PORT", "11437"))
    root = root or Path(__file__).resolve().parents[4] / "ambient-memory"
    with _keeper_lock:
        if _daemon_ping(port):
            return "alive"
        if _daemon_listening(port):
            _keeper_log("unresponsive")
            return "unresponsive"
        if not root.is_dir():
            return "disabled: ambient-memory missing"
        if time.monotonic() < _keeper_next_attempt:
            return "cooldown"
        _keeper_next_attempt = time.monotonic() + KEEPER_RETRY_S
        try:
            pid = _start_ambient_daemon(root)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            _keeper_log("spawn_failed", detail=type(exc).__name__)
            return "spawn_failed"
        _keeper_log("spawn_requested", detail=f"pid={pid}")
        return "spawn_requested"


def run_daemon_keeper(stop_event: threading.Event, *,
                      interval_s: float = KEEPER_INTERVAL_S) -> None:
    """Runs independently of brief latency and the controller's 30s DB tick."""
    while not stop_event.is_set():
        try:
            daemon_keeper_tick()
        except Exception as exc:
            _keeper_log("tick_failed", detail=type(exc).__name__)
        stop_event.wait(interval_s)
