"""Text owner console for dev-mcp fleet.

Default command shows exceptions only.  This process is intentionally not exposed by MCP.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- bootstrap из пульта владельца в dev-mcp (fleet_owner) -----------------------------------
# Сервер зовёт `fleet.py --db БАЗА bootstrap ...` со сроком 30 с (tools.OWNER_PULT_TIMEOUT_S) и
# убивает процесс по сроку. Bootstrap с пробой приёмки идёт 1–3 мин на ТЗ, и 20.11.2026 его
# обрывало на стадии snapshot, до записи в реестр: ТЗ не регистрировались вовсе. Поэтому в
# этом режиме пульт до тяжёлых импортов переносит работу в отдельный процесс без срока, ждёт
# его не дольше OWNER_BOOTSTRAP_WAIT_S и отдаёт ход; повтор с теми же аргументами показывает
# ход или итог того же процесса, а не запускает второй. Локальный fleet.cmd в консоли
# (stdout — терминал) и вызовы main() из кода работают как раньше, синхронно.
_STARTED = time.monotonic()
FOREGROUND_ENV = "FLEET_BOOTSTRAP_FOREGROUND"
RC_FILE_ENV = "FLEET_BOOTSTRAP_RC_FILE"
OWNER_BOOTSTRAP_WAIT_S = 15.0


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def _bootstrap_tail(path: str, limit: int = 4000) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").rstrip()[-limit:]
    except OSError:
        return ""


def _bootstrap_progress(err: str) -> str:
    lines = [line.strip() for line in _bootstrap_tail(err, 20000).splitlines()
             if line.strip().startswith("bootstrap")]
    return "\n".join(lines[-6:])


def _bootstrap_finished(marker: Path, job: dict[str, Any], rc: int) -> int:
    text = "\n".join(part for part in (_bootstrap_tail(job["out"]), _bootstrap_tail(job["err"]))
                     if part)
    print(text)
    try:
        marker.unlink()
    except OSError:
        pass
    return rc


def _owner_bootstrap_detach(args: list[str], *, command: list[str] | None = None,
                            folder: Path | None = None,
                            wait_s: float | None = None) -> int | None:
    """None — не режим сервера, работать как обычно; иначе код выхода для сервера."""
    if os.environ.get(FOREGROUND_ENV) == "1":
        return None
    if command is None and not (len(args) >= 3 and args[0] == "--db" and args[2] == "bootstrap"):
        return None
    if command is None and sys.stdout is not None and sys.stdout.isatty():
        return None
    import hashlib
    import subprocess

    folder = folder or REPO / "logs" / "owner-bootstrap"
    folder.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps(args, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    marker = folder / f"{key}.json"
    job: dict[str, Any] | None = None
    try:
        job = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        job = None
    if job is not None:
        rc_path = Path(job["rc"])
        if rc_path.exists():
            rc_text = rc_path.read_text(encoding="utf-8").strip()
            print(f"bootstrap завершён фоном (запущен {job.get('at', '?')}), итог:")
            return _bootstrap_finished(marker, job, int(rc_text) if rc_text.lstrip("-").isdigit() else 1)
        if _pid_alive(int(job.get("pid", 0))):
            progress = _bootstrap_progress(job["err"])
            print(f"bootstrap ещё идёт фоном (запущен {job.get('at', '?')}; второй процесс не "
                  "запущен). ТЗ попадут в реестр сами; итог — тот же вызов через 1–2 мин."
                  + (f"\nХод:\n{progress}" if progress else ""))
            return 0
        print(f"прошлый фоновый bootstrap оборвался без итога; журнал: {job['err']}. Запускаю заново.",
              file=sys.stderr)
        try:
            marker.unlink()
        except OSError:
            pass

    stem = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + key
    out, err, rc = (folder / f"{stem}.{ext}" for ext in ("out", "err", "rc"))
    env = {**os.environ, FOREGROUND_ENV: "1", RC_FILE_ENV: str(rc), "PYTHONIOENCODING": "utf-8"}
    argv = command or [sys.executable, str(Path(__file__).resolve()), *args]
    base = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    with out.open("wb") as fout, err.open("wb") as ferr:
        try:
            proc = subprocess.Popen(argv, cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                                    stdout=fout, stderr=ferr,
                                    creationflags=base | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
        except OSError:
            proc = subprocess.Popen(argv, cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                                    stdout=fout, stderr=ferr, creationflags=base)
    job = {"pid": proc.pid, "out": str(out), "err": str(err), "rc": str(rc), "args": args,
           "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, marker)
    budget = OWNER_BOOTSTRAP_WAIT_S if wait_s is None else wait_s
    try:
        code = proc.wait(timeout=max(0.5, budget - (time.monotonic() - _STARTED)))
    except subprocess.TimeoutExpired:
        progress = _bootstrap_progress(str(err))
        print("bootstrap запущен фоном и не прерывается сроком вызова (проба приёмки — 1–3 мин "
              "на ТЗ). ТЗ попадут в реестр сами по завершении. Итог — тот же fleet_owner bootstrap "
              "с теми же аргументами через 1–2 мин (второй процесс не запустится); "
              "проверка — active ПРОЕКТ." + (f"\nХод:\n{progress}" if progress else ""))
        return 0
    rc_text = rc.read_text(encoding="utf-8").strip() if rc.exists() else str(code)
    return _bootstrap_finished(marker, job, int(rc_text) if rc_text.lstrip("-").isdigit() else code)


if __name__ == "__main__":
    _detached_rc = _owner_bootstrap_detach(sys.argv[1:])
    if _detached_rc is not None:
        sys.stdout.flush()
        raise SystemExit(_detached_rc)

from dev_mcp.fleet.report import (  # noqa: E402
    _age_label, claimed_vs_done, claim_write_status, exception_items, exceptions, metrics,
)
from dev_mcp.fleet.spec import sync_selected  # noqa: E402
from dev_mcp.fleet import flow as flow_metrics  # noqa: E402
from dev_mcp.audit import iter_entries  # noqa: E402
from dev_mcp.fleet.location import fleet_db_path  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402
from dev_mcp.fleet.model import task_root  # noqa: E402
from dev_mcp.fleet import world  # noqa: E402
from dev_mcp.policy import Policy  # noqa: E402
from dev_mcp import machine  # noqa: E402

# None — место реестра выбирает fleet_db_path() при запуске (и переносит его из logs/,
# если нужно); вычислять на импорте нельзя: импорт в тестах двигал бы боевую базу.
DEFAULT_DB: Path | None = None


def chatgpt_owner_alert(db_path: Path) -> str:
    """Latest unresolved egress alert, reclassified using *current* Chrome evidence."""
    parent = db_path.parent
    path = (parent.parent if parent.name.lower() == "fleet" else parent) / "owner-alerts.jsonl"
    message = ""
    active = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("kind") == "chatgpt_unreachable" and item.get("state") == "active":
                    message = str(item.get("message") or "")[:500]
                    active = item
                elif item.get("kind") == "chatgpt_reachable" and item.get("state") == "resolved":
                    message = ""
                    active = None
    except (OSError, UnicodeError):
        pass
    if active is None:
        return message
    # Old alerts may contain an imperative from before the routes were separated.
    # Never replay an old VPN command without checking live Chrome receipts.
    from dev_mcp.fleet import net_sensor
    browser = net_sensor.BrowserRoute("unknown")
    if db_path.is_file():
        browser = net_sensor._browser_route(db_path)
    egress = str(active.get("server_egress") or world.UNKNOWN)
    result = net_sensor.Result(
        world.DOWN, str(active.get("cause") or "network_error"),
        bool(active.get("proxy_enabled")), bool(active.get("proxy_listening")),
        str(active.get("exit_ip") or ""), str(active.get("loc") or ""),
        server_egress_state=egress)
    return net_sensor._message(result, browser)
# 08.10.2026 проект Oracle переезжает в пульт; старый путь — пока переезд не завершён.
PROXY_STATE = machine.PROXY_STATE
GATEWAY_HEALTH = "https://gateway.example.net/healthz"
AUDIT_LOG = REPO / "logs" / "audit.jsonl"
MEMORY_URL = "http://127.0.0.1:8765/servers/memory_service/mcp"
MEMORY_PROBE_CACHE = REPO / "data" / "fleet" / "memory-probe.json"
MEMORY_PROBE_TTL_S = 600
# Общий срок пробы памяти: сама проба — три POST по 20 с, при висящей memory_service 18.10 сводка
# стояла на ней минуту. Не успела — та же «НЕ ОТВЕЧАЕТ», ответ кэшируется на минуту.
MEMORY_PROBE_DEADLINE_S = 12.0


def _gateway_health_by_ip(timeout: float = 6.0) -> dict[str, Any] | None:
    """/healthz шлюза по IP из имени sslip.io (203-0-113-10 → 203.0.113.10), SNI — имя."""
    import socket
    import ssl
    from urllib.parse import urlsplit

    host = urlsplit(GATEWAY_HEALTH).hostname or ""
    head = host.split(".", 1)[0]
    ip = head.replace("-", ".")
    if not host.endswith("sslip.io") or ip.count(".") != 3:
        return None
    try:
        with socket.create_connection((ip, 443), timeout=timeout) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                tls.sendall(f"GET /healthz HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                data = b""
                while chunk := tls.recv(4096):
                    data += chunk
        return json.loads(data.split(b"\r\n\r\n", 1)[1].decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — недоступность и есть ответ
        return None


def _port_open(port: int, timeout: float = 0.7) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _recent(since: Any, hours: float = 1.0) -> bool:
    """`since` из state.json (местное время лончера) не старше hours; непонятное — считаем свежим."""
    try:
        started = datetime.fromisoformat(str(since))
    except (TypeError, ValueError):
        return True
    if started.tzinfo is not None:
        started = started.astimezone().replace(tzinfo=None)
    return (datetime.now() - started).total_seconds() < hours * 3600


def _mcp_call(url: str, tool: str, arguments: dict[str, Any], timeout: float) -> Any:
    """Один вызов инструмента MCP по streamable HTTP, мимо системного прокси."""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    session = ""

    def post(body: dict[str, Any]) -> Any:
        nonlocal session
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            session = resp.headers.get("Mcp-Session-Id") or session
            raw = resp.read().decode("utf-8", "replace")
        data = "\n".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:")) or raw
        return json.loads(data) if data.strip() else None

    post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "fleet.cmd", "version": "0"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    reply = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}})
    result = (reply or {}).get("result") or {}
    if result.get("structuredContent") is not None:
        content = result["structuredContent"]
        return content.get("result", content) if isinstance(content, dict) else content
    for item in result.get("content") or []:
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except ValueError:
                return item["text"]
    raise ValueError(f"пустой ответ {tool}: {str(reply)[:200]}")


_BOOTSTRAP_LOCK = REPO / "data" / "fleet" / "bootstrap.lock"
# Bootstrap is an OS-lock queue, not a time-limited try-lock: a busy host may
# legitimately have several queued projects. Keep the process waiting its turn.
_BOOTSTRAP_WAIT_S = None


def _bootstrap_slot() -> float:
    """One bootstrap per host at a time; others wait their turn.

    Each bootstrap preflights acceptance in an in-memory snapshot (0.2–0.8 GB). 19.10.2026
    several Claude sessions ran 8 at once (3.7 GB) on a 14 GB host with ~0.6 GB free:
    core could not start memory_service and memory was down for every agent. The lock is an OS
    byte-range lock held for the life of this process, so a crashed caller frees it.
    """
    import msvcrt

    started = time.monotonic()
    _BOOTSTRAP_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = open(_BOOTSTRAP_LOCK, "a+b")  # noqa: SIM115 — kept open until exit
    told = False
    while True:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            globals()["_BOOTSTRAP_HANDLE"] = handle
            return time.monotonic() - started
        except OSError:
            if not told:
                print("bootstrap: жду очереди — на хосте уже идёт другой bootstrap (по одному)",
                      file=sys.stderr, flush=True)
                told = True
            time.sleep(3)


def _with_deadline(fn: Callable[[], Any], seconds: float) -> Any:
    """fn() в фоновом потоке; дольше seconds — TimeoutError (поток умрёт вместе с процессом)."""
    import threading

    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — переносим в вызывающий поток
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"дольше {seconds:g} с")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def memory_state(core_up: bool, *, now: float | None = None, probe: Callable[[], Any] | None = None) -> str:
    """Память — по доказательству: probe_health memory_service (Neo4j `RETURN 1`, эмбеддер и поиск
    по фиксированному запросу), а не порт core.

    26.09.2026 Neo4j был остановлен сутки, а пульт писал «core:8765 жив»: он проверял прокси,
    а не базу за ним. Проба стоит до ~6 с, поэтому результат кэшируется на 10 минут.
    """
    if not core_up:
        return "память ЛЕЖИТ (core не слушает)"
    now = time.time() if now is None else now
    try:
        cached = json.loads(MEMORY_PROBE_CACHE.read_text(encoding="utf-8"))
        # Сбой держим минуту, а не 10: 07.10 проба, снятая при 194 МБ свободной ОЗУ, ещё 10 минут
        # писала «НЕ ОТВЕЧАЕТ» о уже живой памяти.
        ttl = MEMORY_PROBE_TTL_S if str(cached.get("line", "")).startswith("память ок") else 60
        if now - float(cached.get("at", 0)) < ttl:
            return str(cached["line"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        res = _with_deadline(probe or (lambda: _mcp_call(MEMORY_URL, "probe_health",
                                            {"timeout_s": 3, "llm_timeout_s": 6, "include_guide": False}, 20)),
                             MEMORY_PROBE_DEADLINE_S)
        status = str(res.get("status")) if isinstance(res, dict) else "?"
        checks = res.get("checks", {}) if isinstance(res, dict) else {}
        bad = [k for k, v in checks.items() if isinstance(v, dict) and v.get("status") not in ("ok", None)]
        line = {"ok": "память ок", "degraded": "память: чтение ок, запись без LLM"}.get(
            status, "память НЕ РАБОТАЕТ" + (f" ({', '.join(bad)})" if bad else ""))
    except Exception as exc:  # noqa: BLE001 — недоступность пробы и есть ответ
        line = f"память НЕ ОТВЕЧАЕТ ({type(exc).__name__})"
    try:
        MEMORY_PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PROBE_CACHE.write_text(json.dumps({"at": now, "line": line}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return line


def fleet_rules_state(*, now: datetime | None = None,
                      probe: Callable[[], bool] | None = None,
                      path: Path | None = None) -> str:
    """Separate fleet *delivery* signal from the memory_service/core health probe.

    A green core/Neo4j probe does not prove that the ambientd hook delivered
    applicable rules to actual fleet briefs. Metadata only, no rule bodies.
    """
    up = (probe or (lambda: _port_open(11437)))()
    source = path if path is not None else REPO / "logs" / "memory-rules.jsonl"
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=24)
    attempts = with_rules = unavailable = 0
    try:
        with source.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                    ts = datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                    attempts += 1
                    with_rules += int(entry.get("rules", 0) > 0)
                    unavailable += int(bool(entry.get("unavailable")))
                except (ValueError, TypeError, KeyError):
                    continue
    except OSError:
        pass
    transport = "ambientd:11437 жив" if up else "ambientd:11437 НЕ СЛУШАЕТ"
    state = ("правила флота НЕДОСТУПНЫ" if not up else
             "правила флота СБОЙ ДОСТАВКИ" if attempts and unavailable > attempts // 2
             else "правила флота")
    if not attempts:
        return f"{state}: {transport}, нет измерений за 24 ч"
    return (f"{state}: {transport}, за 24 ч {with_rules}/{attempts} с правилами, "
            f"{unavailable} недоступны")


def platform_line(health_timeouts: tuple[float, ...] = (4, 8)) -> str:
    """Одна строка про ПЛАТФОРМУ поверх отчёта о работе.

    Смысл: молчащий агент и лежащий транспорт выглядят в отчёте одинаково —
    «писал: —, молчит 3ч». 18-19.09.2026 это стоило нескольких часов: крэш-луп
    mcp-proxy был виден только агентам в чате, а пульт владельца показывал
    обычную тишину. Теперь состояние транспорта печатается там же, где итоги
    работы, и «тихо, потому что всё лежит» отличается от «тихо, потому что
    агент думает».
    """
    import urllib.request

    parts = []
    dev_up, core_up = _port_open(8766), _port_open(8765)
    state = {}
    try:
        state = json.loads(PROXY_STATE.read_text(encoding="utf-8")).get("instances", {})
    except (OSError, ValueError):
        pass
    for name, up, port in (("dev", dev_up, 8766), ("core", core_up, 8765)):
        st = state.get(name, {})
        mark = "жив" if up else "ЛЕЖИТ"
        extra = ""
        if st.get("state") == "crashloop":
            extra = f" КРЭШ-ЛУП ({st.get('consecutive_fast_fails')} падений подряд)"
        elif up and int(st.get("restarts_last_hour") or 0) > 3 and _recent(st.get("since")):
            # Лончер пишет state.json только при смене состояния: счётчик — это «за час до
            # последнего рестарта». Живёт дольше часа — за последний час рестартов не было.
            extra = f" (рестартов за час: {st['restarts_last_hour']})"
        parts.append(f"{name}:{port} {mark}{extra}")

    # Один таймаут — ещё не «шлюз лежит»: 22.09.2026 первый запрос не уложился в 4с, второй
    # через минуту прошёл за 0.4с, а пульт успел крикнуть «агенты работать НЕ МОГУТ»
    # при двух работающих агентах. Ложная тревога в первой строке учит её не читать.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    health = None
    for timeout in health_timeouts:
        try:
            with opener.open(GATEWAY_HEALTH, timeout=timeout) as resp:
                health = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except Exception:  # noqa: BLE001 — недоступность шлюза и есть ответ
            continue
    dns_note = ""
    if health is None:
        # Имя не резолвится (07.10: DNS через tun0 молчал, шлюз при этом жив и воркер брал
        # задачи) — это поломка DNS этой машины, а не шлюза: проверяем по IP с тем же SNI.
        health = _gateway_health_by_ip()
        if health is not None:
            dns_note = ", DNS этой машины не резолвит имя шлюза"
    if health is not None:
        parts.append(
            f"шлюз ок (очередь {health.get('queue_depth')}{dns_note})"
        )
    else:
        parts.append("шлюз НЕ ОТВЕЧАЕТ")
    # Independent paths: core/memory_service can be green while the ambientd-based
    # fleet rule delivery is timing out. Do not present one as proof of the other.
    memory = f"memory_service/core: {memory_state(core_up)}; {fleet_rules_state()}"

    # The controller has its own heartbeat. A green transport is not enough if
    # queue maintenance has stopped for more than five minutes.
    agents = "агенты: нет данных сервера"
    try:
        db = Path(DEFAULT_DB) if DEFAULT_DB is not None else REPO / "data" / "fleet" / "fleet.sqlite"
        with _open_existing(db) as conn:
            counts = flow_metrics.agent_states(conn)
            agents = flow_metrics.format_agent_states(counts)
            if counts.get("silent"):
                agents += " (" + flow_metrics.format_silent_breakdown(counts).split(": ", 1)[1] + ")"
            row = conn.execute("SELECT ts,payload FROM events WHERE kind='controller_tick' "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            controller = "контроллер: НЕТ ТИКОВ"
        else:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(row["ts"]))).total_seconds()
            controller = (f"контроллер: ПРОПУСК >5 мин ({int(age)} с)" if age > 300
                          else f"контроллер ок ({max(0, int(age))} с)")
    except (OSError, sqlite3.Error, ValueError, TypeError):
        controller = "контроллер: НЕТ ТИКОВ"
    parts.append(agents)
    parts.append(controller)

    bad = ((not dev_up) or health is None or "КРЭШ-ЛУП" in " ".join(parts)
           or "ПРОПУСК" in controller or "НЕТ ТИКОВ" in controller)
    head = "[!] платформа" if bad else "платформа"
    tail = "\n    агенты сейчас работать НЕ МОГУТ — чини транспорт, а не задачи" if bad else ""
    return f"{head}: " + " · ".join(parts) + f" · {memory}" + tail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open_existing(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise sqlite3.OperationalError(f"файл не найден: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _open_write(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise sqlite3.OperationalError(f"файл не найден: {path}")
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    worker_id: str | None = None,
    project: str | None = None,
    tz: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload)
           VALUES(?,?,?,?,?,'','',NULL,?)""",
        (_now(), kind, worker_id, project, tz, json.dumps(payload or {}, ensure_ascii=False)),
    )


def _mutate(
    path: Path, operation: Callable[[sqlite3.Connection], str], *,
    intervention_command: str = "",
) -> tuple[int, str]:
    conn = _open_write(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        message = operation(conn)
        if intervention_command:
            # Owner CLI invocations bypass the MCP JSONL audit. Only the
            # fixed command name is persisted (never the free-form reason,
            # message body, target or command-line arguments).
            _event(conn, "owner_cli_manual", payload={
                "command": intervention_command, "actor": "console",
            })
        conn.execute("COMMIT")
        return 0, message
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _resolve_task(conn: sqlite3.Connection, ident: str, *, state: str | None = None) -> sqlite3.Row:
    if "/" in ident:
        project, tz = ident.rsplit("/", 1)
        sql = "SELECT project,tz,state FROM tasks WHERE project=? AND tz=?"
        params: tuple[Any, ...] = (project, tz)
        if state:
            sql += " AND state=?"
            params += (state,)
        rows = conn.execute(sql, params).fetchall()
    else:
        sql = "SELECT project,tz,state FROM tasks WHERE tz=?"
        params = (ident,)
        if state:
            sql += " AND state=?"
            params += (state,)
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        suffix = f" в состоянии {state}" if state else ""
        raise ValueError(f"задача {ident!r}{suffix} не найдена")
    if len(rows) > 1:
        raise ValueError(f"задача {ident!r} неоднозначна; укажи PROJECT/TZ")
    return rows[0]


def _cmd_revoke(conn: sqlite3.Connection, worker: str, reason: str) -> str:
    row = conn.execute("SELECT id FROM workers WHERE id=?", (worker,)).fetchone()
    if row is None:
        raise ValueError(f"воркер {worker} не найден")
    conn.execute("UPDATE workers SET revoked_at=?, revoke_reason=? WHERE id=?", (_now(), reason, worker))
    _event(conn, "revoke", worker_id=worker, payload={"reason": reason})
    return f"{worker}: отозван — {reason or 'без причины'}"


def _cmd_resume(conn: sqlite3.Connection, worker: str) -> str:
    row = conn.execute("SELECT id FROM workers WHERE id=?", (worker,)).fetchone()
    if row is None:
        raise ValueError(f"воркер {worker} не найден")
    conn.execute("UPDATE workers SET revoked_at=NULL, revoke_reason='' WHERE id=?", (worker,))
    _event(conn, "resume", worker_id=worker)
    return f"{worker}: отзыв снят"


def _cmd_recover(
    conn: sqlite3.Connection,
    project: str,
    tz: str,
    reason: str,
    min_silence_min: int = 120,
) -> str:
    """Вернуть брошенный taken-claim в ready, сохранив все файлы и отозвав старый worker.

    Обычный requeue_stale намеренно не трогает claim после первой записи. Для owner-side
    восстановления это слишком жёстко: работа сохраняется на диске, но claim может навсегда
    заблокировать зависимости и owns. Команда требует длительной фактической тишины и пишет
    отдельные requeue/revoke события, чтобы восстановление не выглядело обычным finish.
    """
    if min_silence_min < 30:
        raise ValueError("recover: min-silence-min должен быть не меньше 30")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("recover: нужна причина владельца")
    row = conn.execute(
        """SELECT c.id AS claim_id,c.worker_id,c.started_at,t.state AS task_state,
                  w.last_seen,w.revoked_at
             FROM claims c
             JOIN tasks t ON t.project=c.project AND t.tz=c.tz
             JOIN workers w ON w.id=c.worker_id
            WHERE c.project=? AND c.tz=? AND c.state='active'""",
        (project, tz),
    ).fetchone()
    if row is None:
        raise ValueError(f"{project}/{tz}: активный claim не найден")
    if row["task_state"] != "taken":
        raise ValueError(f"{project}/{tz}: recover допустим только из taken, сейчас {row['task_state']}")
    try:
        last_seen = datetime.fromisoformat(str(row["last_seen"]))
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        silence_min = int((datetime.now(timezone.utc) - last_seen.astimezone(timezone.utc)).total_seconds() // 60)
    except (TypeError, ValueError):
        raise ValueError(f"{project}/{tz}: last_seen старого worker не читается") from None
    # Уже revoked worker не является живым владельцем claim. Ждать обычный stale-timeout
    # здесь не только бессмысленно, но и опасно: такой claim невидим в fleet_list и может
    # держать задачу taken после duplicate-label dedupe. Для live worker защита остаётся.
    revoked_orphan = row["revoked_at"] is not None
    if not revoked_orphan and silence_min < min_silence_min:
        raise ValueError(
            f"{project}/{tz}: worker {row['worker_id']} молчит только {silence_min} мин; "
            f"recover требует не меньше {min_silence_min}"
        )

    now = _now()
    revoke_reason = f"owner recover {project}/{tz}: {reason}"
    conn.execute(
        "UPDATE claims SET state='requeued',released_at=? WHERE id=?",
        (now, row["claim_id"]),
    )
    conn.execute(
        "UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=?",
        (now, project, tz),
    )
    conn.execute(
        "UPDATE workers SET revoked_at=?,revoke_reason=? WHERE id=?",
        (now, revoke_reason, row["worker_id"]),
    )
    payload = {
        "reason": reason,
        "owner_recovery": True,
        "preserved_work": True,
        "silence_min": silence_min,
        "old_worker": row["worker_id"],
    }
    _event(conn, "requeue", worker_id=row["worker_id"], project=project, tz=tz, payload=payload)
    _event(conn, "revoke", worker_id=row["worker_id"], project=project, tz=tz,
           payload={"reason": revoke_reason, "owner_recovery": True})
    return (
        f"{project}/{tz}: abandoned claim {row['claim_id']} от {row['worker_id']} "
        f"(тишина {silence_min} мин) → ready; файлы сохранены, старый worker отозван"
    )


def _cmd_quick(db_path: Path, ns: Any) -> tuple[int, str]:
    """Быстрые агенты Claude Code (fleet/quick.py, владелец 15.11): new | say | show | list | close | wait."""
    from dev_mcp.fleet import overseer, quick
    book = quick.Book(Path(db_path).parent)
    owner = quick.claude_owner(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    words = list(ns.words or [])
    text = " ".join(words)
    if len(words) == 1 and text.startswith("@") and ns.action in ("new", "run"):
        source = Path(text[1:])
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return 2, f"quick {ns.action}: не удалось прочитать файл {source}: {exc}"
    dependencies = getattr(ns, "after", None) if ns.action == "new" else None
    if ns.action == "new" and dependencies:
        dependencies = [item.strip() for item in dependencies.split(",")]
        if not all(dependencies):
            return 2, "quick new --after: нужны ID через запятую"
    if ns.action == "wait" and getattr(ns, "after", None) is not None:
        try:
            ns.after = int(ns.after)
            if ns.after < 0:
                raise ValueError()
        except ValueError:
            return 2, "quick wait --after: нужен неотрицательный номер ответа"
    if ns.action == "run" and getattr(ns, "after", None) is not None:
        return 2, "quick run --after не поддерживается; используй quick new --after и quick wait ID"
    ns.wait_answer = ns.action == "run"  # run = new + wait: один вызов, ответ на выходе
    if ns.action in ("new", "run") and text:
        root = ""
        if ns.project:
            with _open_existing(db_path) as conn:
                row = conn.execute("SELECT root FROM projects WHERE name=?", (ns.project,)).fetchone()
            if row is None:
                return 2, f"проекта {ns.project!r} во флоте нет"
            root = str(row["root"])

        def register(tag: str, label: str) -> None:
            now = _now()
            conn = _open_write(db_path)
            try:
                if conn.execute("SELECT 1 FROM workers WHERE id=?", (tag,)).fetchone() is None:
                    conn.execute("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason)"
                                 " VALUES (?,?,'quick',?,?,NULL,'')", (tag, label, now, now))
                    _event(conn, "quick_new", worker_id=tag, payload={"label": label, "owner": owner})
            finally:
                conn.close()

        job = quick.create(book, owner=owner, prompt=text, project=ns.project, root=root,
                           scope=list(ns.scope or []), register=register, home=ns.home,
                           after=dependencies)
        head = (f"{job['id']} (agent {job['tag']}) поставлен: " +
                ("ожидает " + ", ".join(job["after"]) + "; чат пока не открывается."
                 if job["state"] == "waiting" else "чат откроет расширение fleet-tabs.")
                + (f" Права оператора: {', '.join(map(str, quick.operator_roots(job)))}."
                   if quick.operator_roots(job) else ""))
        if not ns.wait_answer:
            return 0, (f"{head}\nждать ответ: fleet.cmd quick wait {job['id']}; "
                       f"ход: fleet.cmd quick show {job['id']}")
        print(head, flush=True)
        words, ns.after = [job["id"]], 0
    elif ns.action in ("new", "run"):
        return 2, f"quick {ns.action}: нужен текст задачи"
    if ns.action == "adopt":
        if len(words) != 1:
            return 2, "quick adopt: нужен один полный ID qN-сессия"
        job = quick.adopt(book, words[0], owner=owner, force=getattr(ns, "force", False))
        return 0, f"{job['id']}: владелец {job['owner']}; передача записана в книгу"
    if ns.action == "say":
        if len(words) < 2:
            return 2, "quick say ID ТЕКСТ|@файл (для многострочного текста нужен @файл)"
        message = " ".join(words[1:])
        if len(words) == 2 and words[1].startswith("@"):
            source = Path(words[1][1:])
            try:
                message = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                return 2, f"quick say: не удалось прочитать файл {source}: {exc}"
        job = quick.say(book, words[0], message, owner=owner)
        return 0, f"{job['id']}: в очереди чата {len(job['outbox'])}"
    if ns.action == "close":
        job = quick.close(book, text, owner=owner)
        return 0, f"{job['id']} закрыт"
    if ns.action == "list":
        rows = quick.listing(book, owner="*" if ns.all else owner, include_closed=ns.all)
        return 0, "\n".join(quick.render(j, full=False).split("\n")[0] for j in rows) or "быстрых агентов нет"
    if ns.action == "show":
        job = quick.get(book, text, owner=owner)
        actions = quick.first_actions(REPO / "logs", [job])
        return 0, quick.render(quick.with_action(job, actions),
                               overseer.agent_history(job["tag"], job["created"], any_tool=True))
    # wait: один ответ из набора (по умолчанию) или все (--all).
    if not words:
        return 2, "quick wait ID [ID ...] [--all]"
    jids = list(dict.fromkeys(quick.get(book, word, owner=owner)["id"] for word in words))
    # None uses the durable per-job owner cursor; --after N overrides it.
    after = ns.after if isinstance(getattr(ns, "after", None), int) else None
    deadline = time.monotonic() + ns.timeout
    while True:
        completed = quick.collect_wait(book, jids, owner=owner,
                                       after=after,
                                       all_answers=ns.all)
        if completed:
            actions = quick.first_actions(REPO / "logs", completed)
            return 0, "\n\n".join(quick.render(quick.with_action(job, actions)) for job in completed)
        snapshot = book.jobs()
        current = [dict(quick.owned(snapshot.get(jid), owner)) for jid in jids]
        closed = [job["id"] for job in current if job["state"] == "closed"]
        if closed and (ns.all or len(closed) == len(current)):
            return 1, "закрыты без нового ответа: " + ", ".join(closed)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 1, (f"нового ответа за {ns.timeout} с нет; ожидание: {', '.join(jids)}\n" +
                       "\n".join(quick.render(job, full=False).split("\n")[0] for job in current))
        time.sleep(min(3, remaining))


def _cmd_overseer(db_path: Path, ns: Any) -> tuple[int, str]:
    """Надзиратель хода (_todo/59 пульта): чаты под надзором, режимы, журнал."""
    from dev_mcp.fleet import overseer
    book = overseer.Book(Path(db_path).parent)
    if ns.action == "add":
        if not ns.project:
            return 2, "нужен --project (имя проекта флота, корень берётся из реестра)"
        entry = overseer.add(book, ns.target, ns.project, "send" if ns.send else "shadow", ns.engine)
        return 0, (f"под надзором: {entry['url']} проект {entry['project']} режим {entry['mode']} "
                   f"движок {entry.get('engine') or 'semantic'} тег агента {entry['tag']}")
    if ns.action == "mode":
        entry = overseer.set_mode(book, ns.target, ns.value, ns.engine if ns.value != "off" else None)
        return 0, f"{entry['url']}: режим {entry['mode']}, движок {entry.get('engine') or 'semantic'}"
    if ns.action == "list":
        rows = []
        for cid, e in book.chats().items():
            card = book.card(cid) or {}
            segs = card.get("segments") or []
            done = sum(1 for s in segs if s.get("status") == "done")
            rows.append(f"{e['mode']:6} {e.get('engine') or 'semantic':8} {e['url']}  {e.get('project', '')}  "
                        f"ходов {card.get('turns', 0)}  "
                        f"сделано {done}/{len(segs)}  последний вердикт {card.get('last_verdict') or '—'}")
        return 0, "\n".join(rows) or "чатов под надзором нет"
    n = int(ns.target or 20)
    try:
        lines = book.journal.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return 0, "журнал пуст"
    out = []
    for line in lines:
        d = json.loads(line)
        out.append(f"{d['ts']} {d['chat'][:8]} {d['event']} {d.get('verdict', '')} "
                   f"{(d.get('text') or '')[:160]!r}")
    return 0, "\n".join(out)


def _cmd_say(conn: sqlite3.Connection, body: str) -> str:
    # Только тем, кто держит задачу: отработавшие вкладки закрыты, сообщение они не
    # прочтут, а неподтверждённые копии засоряли бы пульт строками «БЕЗ ACK».
    workers = conn.execute(
        "SELECT DISTINCT w.id FROM workers w JOIN claims c ON c.worker_id=w.id AND c.state='active' "
        "WHERE w.revoked_at IS NULL ORDER BY w.id"
    ).fetchall()
    ids: list[str] = []
    now = _now()
    for row in workers:
        mid = "m-" + uuid.uuid4().hex[:8]
        conn.execute(
            """INSERT INTO messages(
                   id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,body,
                   created_at,delivered_at,acked_at
               ) VALUES(?,'',?,NULL,0,'owner','','broadcast',?,?,NULL,NULL)""",
            (mid, str(row["id"]), body, now),
        )
        ids.append(mid)
    _event(conn, "broadcast", payload={"message_ids": ids, "workers": len(ids)})
    if not ids:
        return "broadcast: задачу сейчас никто не держит — рассылать некому"
    return f"broadcast сохранён для {len(ids)} воркеров: " + ", ".join(ids)


def _cmd_tell(conn: sqlite3.Connection, to: str, body: str) -> str:
    """Адресное сообщение одному воркеру (W123) или держателю задачи (Проект/TZ-7).

    Руль владельца и Claude Code. Сообщение приходит в конверте следующего ответа любого
    инструмента, а агент, ждущий на парковке fleet_take, получает его за несколько секунд.
    """
    target = to.strip()
    if not target or not body.strip():
        raise ValueError("нужны адресат и текст")
    if any(line.strip().endswith(("?", "？")) for line in body.splitlines()):
        raise ValueError("вопросы агенту — через задачу/request, в сообщении только указание")
    if "/" in target:
        project, tz = target.split("/", 1)
        row = conn.execute(
            "SELECT worker_id FROM claims WHERE project=? AND tz=? AND state='active'", (project, tz)
        ).fetchone()
        to_worker, to_task, who = None, target, (str(row["worker_id"]) if row else "никто сейчас не держит")
    else:
        row = conn.execute("SELECT id,revoked_at FROM workers WHERE id=?", (target,)).fetchone()
        if row is None:
            raise ValueError(f"воркер {target!r} не найден")
        if row["revoked_at"] is not None:
            raise ValueError(f"воркер {target} отозван")
        to_worker, to_task, who = target, None, target
    mid = "m-" + uuid.uuid4().hex[:8]
    conn.execute(
        """INSERT INTO messages(
               id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,body,
               created_at,delivered_at,acked_at
           ) VALUES(?,'',?,?,0,'owner','','note',?,?,NULL,NULL)""",
        (mid, to_worker, to_task, body, _now()),
    )
    _event(conn, "tell", worker_id=to_worker, payload={"message_id": mid, "to": target})
    return f"сообщение {mid} → {target} (получатель: {who})"


def _cmd_instruct(conn: sqlite3.Connection, project: str, text: str, done_when: str,
                  want: str = "", priority: int = 5, source: str = "") -> str:
    """Поручение ведущему проекта — объект реестра, а не записка (LEAD-SYSTEM-DESIGN фаза 0.3).

    05–06.11: ~30 писем ведущему Nova доставлены, ни одно не записано в книгу; новый чат вехи
    о них не знал. Здесь: письмо держателю устава + событие `instruction` (что сделать, зачем,
    критерий «сделано»). Ведущий видит его в брифе, пока не закроет fleet_lead(action='instruction')
    статусом accepted(O-*) / declined(reason) / done(evidence).
    """
    text, done_when, want = text.strip(), done_when.strip(), want.strip()
    if len(text) < 10 or len(done_when) < 5:
        raise ValueError("нужны текст поручения (от 10 знаков) и --done-when (критерий «сделано»)")
    if conn.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone() is None:
        raise ValueError(f"проект {project!r} не найден")
    mid = "m-" + uuid.uuid4().hex[:8]
    iid = "i-" + mid[2:]
    body = (text + (f"\nРезультат для человека: {want}" if want else "") + f"\nСделано, когда: {done_when}"
            + f"\nПоручение {iid}: закрой fleet_lead(action='instruction') статусом accepted/declined/done.")
    conn.execute(
        """INSERT INTO messages(
               id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,body,
               created_at,delivered_at,acked_at
           ) VALUES(?,'',NULL,?,0,'owner','','note',?,?,NULL,NULL)""",
        (mid, f"{project}/TZ-LEAD", body, _now()),
    )
    _event(conn, "instruction", project=project, tz="TZ-LEAD", payload={
        "id": iid, "author": "owner", "text": text, "want": want, "done_when": done_when,
        "priority": int(priority), "source": source or "fleet.cmd instruct", "msg_id": mid})
    return f"поручение {iid} → {project}/TZ-LEAD (письмо {mid}); статус — в брифе ведущего"


def _cmd_focus(conn: sqlite3.Connection, project: str, iid: str, tz: str) -> str:
    """Link an open owner instruction to a real task without changing admission."""
    from dev_mcp.fleet.instructions import CLOSED, KIND, STATUS_KIND, TARGET_KIND
    from dev_mcp.fleet.queue import family_root

    if not conn.execute("SELECT 1 FROM tasks WHERE project=? AND tz=?", (project, tz)).fetchone():
        raise ValueError(f"{project}/{tz}: задача не найдена")
    rows = conn.execute(
        "SELECT kind,payload FROM events WHERE project=? AND tz='TZ-LEAD' AND kind IN (?,?) "
        "AND json_extract(payload,'$.id')=? ORDER BY id",
        (project, KIND, STATUS_KIND, iid),
    ).fetchall()
    if not rows or rows[0]["kind"] != KIND or json.loads(rows[0]["payload"]).get("author") != "owner":
        raise ValueError(f"{project}: поручение владельца {iid} не найдено")
    if json.loads(rows[-1]["payload"]).get("status") in CLOSED:
        raise ValueError(f"{project}: поручение {iid} уже закрыто")
    target = family_root(tz)
    if not conn.execute("SELECT 1 FROM tasks WHERE project=? AND tz=?", (project, target)).fetchone():
        raise ValueError(f"{project}/{target}: исходная задача не найдена")
    _event(conn, TARGET_KIND, project=project, tz="TZ-LEAD", payload={"id": iid, "task": target})
    return f"{project}/{target}: приоритет поручения {iid}"


def _cmd_unblock(conn: sqlite3.Connection, project: str, oid: str, answer: str) -> str:
    """Ответ владельца на карточку препятствия ведущего (LEAD-SYSTEM-DESIGN фаза 1.1).

    Препятствие закрывается, ответ становится поручением ведущему — устав вернётся сам,
    когда появится продвигаемое O-*."""
    answer = answer.strip()
    if len(answer) < 2:
        raise ValueError("нужен ответ владельца")
    row = conn.execute(
        "SELECT id,payload FROM events WHERE kind='obstacle_open' AND project=? AND "
        "json_extract(payload,'$.id')=? ORDER BY id DESC LIMIT 1", (project, oid)).fetchone()
    if row is None:
        raise ValueError(f"препятствие {oid} в {project!r} не найдено")
    if conn.execute("SELECT 1 FROM events WHERE kind='obstacle_closed' AND project=? AND "
                    "json_extract(payload,'$.id')=? AND id>?", (project, oid, row["id"])).fetchone():
        raise ValueError(f"{oid} уже снято")
    ask = str(json.loads(row["payload"] or "{}").get("ask") or "")
    _event(conn, "obstacle_closed", project=project, tz="TZ-LEAD",
           payload={"id": oid, "by": "owner", "answer": answer})
    text = f"Решение владельца по {oid} («{ask[:200]}»): {answer}"
    return f"{oid} снято. " + _cmd_instruct(
        conn, project, text, done_when=f"решение по {oid} учтено в книге (accepted → O-*)",
        source=f"fleet.cmd unblock {oid}")


def _cmd_priority(conn: sqlite3.Connection, project: str, priority: int) -> str:
    cur = conn.execute("UPDATE projects SET priority=? WHERE name=?", (priority, project))
    if cur.rowcount != 1:
        raise ValueError(f"проект {project!r} не найден")
    _event(conn, "priority", project=project, payload={"priority": priority})
    return f"{project}: priority={priority}"


def _cmd_freeze(conn: sqlite3.Connection, project: str, tz: str) -> str:
    row = conn.execute("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz)).fetchone()
    if row is None:
        raise ValueError(f"{project}/{tz}: задача не найдена")
    state = str(row["state"])
    if state in {"accepted", "taken"}:
        raise ValueError(f"{project}/{tz}: нельзя freeze из состояния {state}")
    active = conn.execute(
        "SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active' LIMIT 1",
        (project, tz),
    ).fetchone()
    if active is not None:
        raise ValueError(f"{project}/{tz}: есть active claim; сначала освободи его")
    conn.execute("UPDATE tasks SET state='frozen', updated_at=? WHERE project=? AND tz=?", (_now(), project, tz))
    _event(conn, "freeze", project=project, tz=tz, payload={"previous_state": state})
    return f"{project}/{tz}: frozen"


def _cmd_reactivate(conn: sqlite3.Connection, project: str, tz: str, reason: str) -> str:
    """Return one exact frozen/rejected task to ready, including generated fix:* tasks."""
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reactivate: нужна причина владельца")
    row = conn.execute(
        "SELECT state FROM tasks WHERE project=? AND tz=?",
        (project, tz),
    ).fetchone()
    if row is None:
        raise ValueError(f"{project}/{tz}: задача не найдена")
    state = str(row["state"])
    if state not in {"frozen", "rejected"}:
        raise ValueError(
            f"{project}/{tz}: reactivate допустим только из frozen/rejected, сейчас {state}"
        )
    active = conn.execute(
        "SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active' LIMIT 1",
        (project, tz),
    ).fetchone()
    if active is not None:
        raise ValueError(f"{project}/{tz}: есть active claim; сначала освободи его")
    now = _now()
    conn.execute(
        "UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=?",
        (now, project, tz),
    )
    _event(
        conn,
        "reactivate",
        project=project,
        tz=tz,
        payload={"previous_state": state, "reason": reason},
    )
    return f"{project}/{tz}: {state} → ready — {reason}"


def _cmd_grant(conn: sqlite3.Connection, project: str, text: str, tzs: list[str]) -> str:
    """Owner decision as a registry fact (owner_grants.py): shown verbatim in every task
    briefing of the project (or of the named tasks) and in decide-triage; named frozen
    tasks without an active claim go back to ready right away."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("grant: нужен текст решения владельца")
    if conn.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone() is None:
        raise ValueError(f"проект {project!r} не найден")
    tzs = [t.strip() for t in tzs if t and t.strip()]
    for tz in tzs:
        if conn.execute("SELECT 1 FROM tasks WHERE project=? AND tz=?", (project, tz)).fetchone() is None:
            raise ValueError(f"{project}/{tz}: задача не найдена")
    _event(conn, "owner_grant", project=project, tz=tzs[0] if len(tzs) == 1 else None,
           payload={"text": text[:4000], "tzs": tzs})
    thawed = []
    for tz in tzs:
        row = conn.execute("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz)).fetchone()
        busy = conn.execute("SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active' LIMIT 1",
                            (project, tz)).fetchone()
        if str(row["state"]) == "frozen" and busy is None:
            _cmd_reactivate(conn, project, tz, f"решение владельца: {text[:600]}")
            thawed.append(tz)
    scope = ", ".join(tzs) if tzs else "весь проект"
    tail = f"; разморожено: {', '.join(thawed)}" if thawed else ""
    return f"{project}: решение владельца записано ({scope}){tail}"


def _cmd_set_acceptance(
    conn: sqlite3.Connection,
    project: str,
    tz: str,
    commands: list[str],
) -> str:
    """Owner-side acceptance migration for a task whose execution contract changed.

    This is intentionally narrower than arbitrary task editing: it only changes the
    acceptance JSON, refuses active/taken/accepted tasks, and records old/new values
    in the event log. File-backed specs remain the source of truth; generated fix/review
    tasks need this when they inherited acceptance before the source spec was corrected.
    """
    cleaned = [str(cmd).strip() for cmd in commands if str(cmd).strip()]
    if not cleaned:
        raise ValueError("set-acceptance: нужна хотя бы одна непустая --command")
    row = conn.execute(
        "SELECT state,acceptance FROM tasks WHERE project=? AND tz=?",
        (project, tz),
    ).fetchone()
    if row is None:
        raise ValueError(f"{project}/{tz}: задача не найдена")
    if row["state"] in {"accepted", "taken"}:
        raise ValueError(f"{project}/{tz}: нельзя менять acceptance из состояния {row['state']}")
    active = conn.execute(
        "SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active' LIMIT 1",
        (project, tz),
    ).fetchone()
    if active is not None:
        raise ValueError(f"{project}/{tz}: есть active claim; сначала освободи его")
    try:
        old = json.loads(str(row["acceptance"] or "[]"))
    except (TypeError, ValueError):
        old = []
    if not isinstance(old, list):
        old = []
    if old == cleaned:
        return f"{project}/{tz}: acceptance уже совпадает; без изменений"
    now = _now()
    conn.execute(
        "UPDATE tasks SET acceptance=?,updated_at=? WHERE project=? AND tz=?",
        (json.dumps(cleaned, ensure_ascii=False), now, project, tz),
    )
    _event(
        conn,
        "acceptance_update",
        project=project,
        tz=tz,
        payload={"old": old, "new": cleaned},
    )
    return f"{project}/{tz}: acceptance обновлена ({len(old)} → {len(cleaned)} команд)"


def _fmt_minutes(value: Any) -> str:
    if value is None:
        return "—"
    value = float(value)
    if value < 60:
        return f"{value:.1f} мин"
    return f"{value / 60.0:.1f} ч"


def _fmt_ratio(item: dict[str, Any]) -> str:
    n, N = int(item["n"]), int(item["N"])
    pct = "—" if not N else f"{100.0*n/N:.1f}%"
    return f"{n}/{N} ({pct})"


def _print_metrics(data: dict[str, Any]) -> None:
    if data.get("error"):
        print(data["error"])
        return
    print(f"с {data['since']}")
    # Первой строкой — принятие протокола: сколько записей пришло с названным
    # воркером, а не было приписано стражем по владению путём. Пока эта доля
    # низкая, остальные метрики описывают работу, которая идёт мимо реестра.
    print("записей с названным agent= (принятие протокола): "
          + _fmt_ratio(data["protocol_declared_rate"]))
    print("вмешательства владельца / принятые задачи: "
          + _fmt_ratio(data["owner_interventions_per_accepted"]))
    req = data["request_to_execution_min"]
    print(f"заявка → исполнение: n={req['n']}, медиана {_fmt_minutes(req['median'])}")
    review = data["review_wait_age_min"]
    print(f"готовое ждёт приёмки: n={review['n']}, медиана {_fmt_minutes(review['median'])}, "
          f"максимум {_fmt_minutes(review['max'])}")
    print("вернулись в очередь: " + _fmt_ratio(data["requeue_rate"]))
    print("попытки записи вне зоны: " + _fmt_ratio(data["foreign_path_rate"]))
    attempt = data.get("claim_attempt_time_min") or data["task_time_min"]
    print(f"одна claim-попытка: n={attempt['n']}, медиана {_fmt_minutes(attempt['median'])}, "
          f"IQR {_fmt_minutes(attempt['p25'])}…{_fmt_minutes(attempt['p75'])}")
    work = data.get("work_task_effort_min") or {}
    print(f"уникальная work-ТЗ, суммарное время её claims: n={work.get('n', 0)}, "
          f"медиана {_fmt_minutes(work.get('median'))}, "
          f"IQR {_fmt_minutes(work.get('p25'))}…{_fmt_minutes(work.get('p75'))}")
    by_pos = data.get("verdict_by_chat_position") or {}
    if by_pos:
        # Портится ли качество к концу длинного чата: доля reject у сдач по номеру задачи в чате.
        print("первый вердикт по номеру задачи в чате (reject / всего с вердиктом, ждут):")
        for pos, row in sorted(by_pos.items(), key=lambda kv: int(kv[0])):
            decided = row["accept"] + row["reject"]
            share = f"{row['reject'] / decided:.0%}" if decided else "—"
            print(f"  №{pos}: reject {row['reject']}/{decided} ({share}), ждут {row['pending']}")


def active_claims(conn: sqlite3.Connection, project: str | None = None) -> str:
    """Кто прямо сейчас держит какие пути. Нужна тем, кто пишет мимо стражи dev-mcp
    (Claude Code, человек): файл в чужом активном захвате править нельзя."""
    sql = (
        "SELECT c.id,c.project,c.tz,c.worker_id,w.label,w.last_seen FROM claims c "
        "JOIN workers w ON w.id=c.worker_id WHERE c.state='active'"
    )
    args: tuple = ()
    if project:
        sql += " AND c.project=?"
        args = (project,)
    rows = conn.execute(sql + " ORDER BY c.project,c.started_at", args).fetchall()
    if not rows:
        return "активных захватов нет" + (f" в {project}" if project else "") + " — файлы никто из флота не держит"
    now = datetime.now(timezone.utc)
    lines = []
    for row in rows:
        try:
            silent = int((now - datetime.fromisoformat(str(row["last_seen"]))).total_seconds() // 60)
        except ValueError:
            silent = -1
        lines.append(f"{row['project']}/{row['tz']} — {row['worker_id']} ({row['label']}), молчит {silent} мин")
        for path in conn.execute("SELECT path_norm FROM claim_paths WHERE claim_id=? ORDER BY path_norm", (row["id"],)):
            lines.append(f"    {path['path_norm']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude Code водит агентов флота как свои субагенты: result / watch / decisions.
# Все три только читают реестр (mode=ro) и ничего не пишут.
# ---------------------------------------------------------------------------

UNFINISHED = ("ready", "taken", "submitted", "frozen", "draft")
VERDICT_KINDS = ("accept", "reject", "escalate")
CHAIN_KINDS = (
    "take", "submit", "accept", "reject", "escalate", "block", "freeze", "thaw",
    "requeue", "finish", "reactivate", "integrate", "gate",
)
WATCH_KINDS = (
    "accept", "reject", "escalate", "block", "freeze", "thaw", "submit",
    "integrate", "gate", "owner_command", "park",
)
RESULT_CAP = 8000
STALE_ALARM_H = 24.0
# «автоблок: ждём request:c2719966; …» — зависимость, которой нет в needs.
_WAIT_FOR = re.compile(r"ждём\s+((?:review:|fix:)*(?:request:[0-9a-fA-F]+|TZ-\d+))")


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_ts(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_min(raw: Any, now: datetime | None = None) -> float:
    value = _parse_ts(raw)
    if value is None:
        return 0.0
    return max(0.0, ((now or datetime.now(timezone.utc)) - value).total_seconds() / 60.0)


def _local_hm(raw: Any, *, with_date: bool = False) -> str:
    value = _parse_ts(raw)
    if value is None:
        return "--:--"
    return value.astimezone().strftime("%d.%m %H:%M" if with_date else "%H:%M")


def _one_line(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)] + "…"


def _event_text(kind: str, payload: dict[str, Any], rc: Any = None) -> str:
    """Человеческий текст события: заметка ревьюера, причина блокировки, итог гейта."""
    if kind == "gate":
        code = payload.get("rc", rc)
        parts = [f"rc={code}"]
        if payload.get("dur_s") is not None:
            parts.append(f"{payload['dur_s']}с")
        summary = payload.get("summary") or ""
        if not summary and code not in (0, "0", None):
            summary = payload.get("log_tail") or ""
        return " ".join(parts) + (f" {summary}" if summary else "")
    if kind == "integrate":
        paths = payload.get("paths") or []
        count = len(paths) if isinstance(paths, list) else paths
        if payload.get("commit"):
            return f"коммит {str(payload['commit'])[:12]}, путей {count}"
        # 16.11: «коммит ?» скрывал, почему принятое не дошло до git.
        why = next((str(payload[k]) for k in ("reason", "reason_code", "error", "skipped")
                    if payload.get(k)), "причина не записана")
        return f"НЕ закоммичено: {why[:200]}, путей {count}"
    if kind == "owner_command":
        args = payload.get("args") or []
        return " ".join([str(payload.get("command") or "")] + [str(a) for a in args]).strip()
    for key in ("note", "reason", "summary", "why", "block_reason", "outcome"):
        if payload.get(key):
            return str(payload[key])
    if payload.get("previous_state"):
        return f"было {payload['previous_state']}"
    return ""


def _base_tz(tz: str) -> str:
    """review:fix:TZ-7:2 → TZ-7; request:ab12cd34 и styk:4 остаются собой."""
    return task_root(tz)


def _split_ident(conn: sqlite3.Connection, ident: str) -> tuple[str, str]:
    if "/" in ident:
        project, tz = ident.rsplit("/", 1)
        return project, tz
    rows = conn.execute("SELECT DISTINCT project FROM tasks WHERE tz=?", (ident,)).fetchall()
    if len(rows) == 1:
        return str(rows[0]["project"]), ident
    if not rows:
        raise ValueError(f"задача {ident!r} не найдена")
    raise ValueError(f"задача {ident!r} есть в нескольких проектах; укажи Проект/TZ")


def _cmd_tokens(command: Any) -> str:
    items = [str(x) for x in command] if isinstance(command, list) else str(command or "").split()
    return " ".join(x.strip("\"'") for x in items)


_CHAIN_RUN_MIN = 4


def _chain_label(tasks: list[dict]) -> str:
    """Цепочка звеньев; подряд идущие повторы одного семейства (review:X:2…:266) — одной строкой.

    05.11: у TZ-LEAD 266 ревью revalidate съедали весь бюджет вывода, итог задачи не был виден.
    """
    parts: list[str] = []
    i = 0
    while i < len(tasks):
        family = re.sub(r":\d+$", "", str(tasks[i]["tz"]))
        j = i
        while j + 1 < len(tasks) and re.sub(r":\d+$", "", str(tasks[j + 1]["tz"])) == family:
            j += 1
        run = tasks[i:j + 1]
        if len(run) >= _CHAIN_RUN_MIN:
            counts: dict[str, int] = {}
            for t in run:
                counts[str(t["state"])] = counts.get(str(t["state"]), 0) + 1
            states = ", ".join(f"{s} ×{n}" for s, n in counts.items())
            parts.append(f"{run[0]['tz']} … {run[-1]['tz']} ({len(run)} звеньев: {states})")
        else:
            parts.extend(f"{t['tz']} ({t['state']})" for t in run)
        i = j + 1
    return " → ".join(parts)


def task_result(conn: sqlite3.Connection, ident: str, cap: int = RESULT_CAP) -> str:
    """Итог задачи одним блоком — как финальный отчёт субагента: вердикт, приёмка, ход, файлы."""
    project, tz = _split_ident(conn, ident)
    base = _base_tz(tz)
    tasks = [
        dict(r) for r in conn.execute(
            "SELECT tz,title,state,kind,acceptance,created_at FROM tasks WHERE project=? ORDER BY created_at,tz",
            (project,),
        )
        if _base_tz(str(r["tz"])) == base
    ]
    if not tasks:
        raise ValueError(f"задача {project}/{tz} не найдена")
    names = [str(t["tz"]) for t in tasks]
    marks = ",".join("?" for _ in names)
    kinds = (*CHAIN_KINDS, "run", "write")
    events = [
        dict(r) for r in conn.execute(
            f"SELECT id,ts,kind,worker_id,project,tz,tool,path_norm,rc,payload FROM events "
            f"WHERE project=? AND tz IN ({marks}) AND kind IN ({','.join('?' for _ in kinds)}) ORDER BY id",
            (project, *names, *kinds),
        )
    ]
    first_seen: dict[str, int] = {}
    for event in events:
        first_seen.setdefault(str(event["tz"]), int(event["id"]))
    # Цепочка — в порядке, в котором звенья реально пошли в работу; created_at у звеньев бывает одинаковым.
    tasks.sort(key=lambda t: (first_seen.get(str(t["tz"]), 1 << 62), str(t["created_at"]), str(t["tz"])))
    main = next((t for t in tasks if t["tz"] == base), tasks[0])
    head = [f"{project}/{base} — {main['title'] or 'без названия'}"]
    head.append(f"состояние: {main['state']} · цепочка: " + _chain_label(tasks))
    from types import SimpleNamespace
    from dev_mcp.fleet.results import available
    result_reader = SimpleNamespace(query=lambda sql, args=(): conn.execute(sql,args).fetchall())
    # The final accepted change often belongs to fix:TZ rather than TZ itself.
    # Preserve the whole-chain view for both root and repair identifiers.
    published = [result for name in names
                 for result in available(result_reader, project=project, tz=name)]
    published.sort(key=lambda result: (int(result.get('accept_id') or 0),int(result['event_id'])),
                   reverse=True)
    delivery_lines: list[str] = []
    if published:
        result = published[0]
        delivery_lines.append(f"результат доступен: {result['artifact']} · SHA-256 {result['sha256']}")
        delivery_lines.append(f"версия результата: {result['revision']} · приёмка владельцем отдельно")
    elif main['state'] == 'accepted':
        delivery_lines.append('передача результата: подтверждённого артефакта пока нет')

    takes = [e for e in events if e["kind"] == "take"]
    verdicts = [e for e in events if e["kind"] in VERDICT_KINDS]
    if takes:
        end = verdicts[-1]["ts"] if verdicts else None
        start_dt = _parse_ts(takes[0]["ts"])
        end_dt = _parse_ts(end) if end else datetime.now(timezone.utc)
        minutes = (end_dt - start_dt).total_seconds() / 60.0 if start_dt and end_dt else 0.0
        tail = f"последний вердикт {_local_hm(end, with_date=True)}" if end else "вердикта ещё нет"
        head.append(f"время: {_age_label(minutes)} (первый take {_local_hm(takes[0]['ts'], with_date=True)}, {tail})")
    else:
        head.append("время: задачу ещё никто не брал")
    holders = conn.execute(
        f"SELECT c.worker_id,c.tz,c.started_at,w.last_seen FROM claims c LEFT JOIN workers w ON w.id=c.worker_id "
        f"WHERE c.project=? AND c.state='active' AND c.tz IN ({marks})",
        (project, *names),
    ).fetchall()
    if holders:
        head.append("держит сейчас: " + "; ".join(
            f"{h['worker_id']} ({h['tz']}, {_age_label(_age_min(h['started_at']))}, "
            f"молчит {_age_label(_age_min(h['last_seen']))})" for h in holders))
    else:
        head.append("держит сейчас: никто")

    # Приёмка: для каждой команды — rc последнего запуска того же argv в пределах цепочки.
    runs = [e for e in events if e["kind"] == "run" and e["rc"] is not None]
    acceptance: list[str] = []
    seen_cmd: set[str] = set()
    for task in tasks:
        try:
            commands = json.loads(task["acceptance"] or "[]")
        except ValueError:
            commands = []
        for command in commands if isinstance(commands, list) else []:
            key = _cmd_tokens(command)
            if not key or key in seen_cmd:
                continue
            seen_cmd.add(key)
            last = None
            for run in runs:
                argv = _payload(run["payload"]).get("argv")
                if argv and _cmd_tokens(argv) == key:
                    last = run
            if last is None:
                acceptance.append(f"  rc=—  {_one_line(command, 200)} (не запускалась)")
            else:
                acceptance.append(
                    f"  rc={last['rc']}  {_one_line(command, 200)} "
                    f"({last['tz']} {last['worker_id'] or '?'}, {_local_hm(last['ts'], with_date=True)})"
                )
    # Writes belong to a *take*, not merely a worker or a task label. Use the
    # immutable claim window and its captured owns, never today's tasks.owns.
    claims = [dict(r) for r in conn.execute(
        f"SELECT id,project,tz,worker_id,started_at,released_at FROM claims "
        f"WHERE project=? AND tz IN ({marks}) ORDER BY id", (project, *names)
    )]
    owns_by_claim = {int(c["id"]): [str(r["path_norm"]) for r in conn.execute(
        "SELECT path_norm FROM claim_paths WHERE claim_id=?", (c["id"],)
    )] for c in claims}
    files: set[str] = set()
    outside_owns: set[str] = set()
    unverified_files: set[str] = set()
    for event in events:
        if event["kind"] != "write" or not event["path_norm"]:
            continue
        path = str(event["path_norm"])
        statuses = [claim_write_status(event, c, owns_by_claim[int(c["id"])])
                    for c in claims if c["worker_id"] == event["worker_id"]
                    and c["tz"] == event["tz"]]
        if "owned" in statuses:
            files.add(path)
        elif "outside_owns" in statuses:
            outside_owns.add(path)
        else:
            unverified_files.add(path)
    files = sorted(files)
    outside_owns = sorted(outside_owns - set(files))
    unverified_files = sorted(unverified_files - set(files) - set(outside_owns))

    key_kinds = ("submit", *VERDICT_KINDS, "block", "gate")
    lifecycle = [e for e in events if e["kind"] not in ("run", "write", "take")]
    latest = lifecycle[-1] if lifecycle else None

    def render(key_cap: int, other_cap: int, max_files: int, max_events: int = 0) -> str:
        lines = list(head)
        if verdicts:
            last = verdicts[-1]
            text = _event_text(last["kind"], _payload(last["payload"]))
            lines.append(
                f"ИТОГ: {last['kind']} {last['tz']} {last['worker_id'] or '?'} "
                f"{_local_hm(last['ts'], with_date=True)} — {_one_line(text, max(key_cap, 1500))}"
            )
        if latest is not None and (not verdicts or latest["id"] > verdicts[-1]["id"]):
            text = _event_text(latest["kind"], _payload(latest["payload"]), latest["rc"])
            lines.append(
                f"СЕЙЧАС: {latest['kind']} {latest['tz']} {latest['worker_id'] or ''} "
                f"{_local_hm(latest['ts'], with_date=True)}" + (f" — {_one_line(text, max(key_cap, 600))}" if text else "")
            )
        lines.extend(delivery_lines)
        if acceptance:
            lines.append("приёмка (последний запуск в цепочке):")
            lines.extend(acceptance)
        lines.append("ход работы:")
        shown = [e for e in events if e["kind"] not in ("run", "write")]
        # Длинная цепочка: важнее свежий ход, чем начало недельной давности.
        if max_events and len(shown) > max_events:
            lines.append(f"  … раньше ещё {len(shown) - max_events} событий")
            shown = shown[-max_events:]
        for event in shown:
            text = _event_text(event["kind"], _payload(event["payload"]), event["rc"])
            if verdicts and event["id"] == verdicts[-1]["id"]:
                text = "(текст — в ИТОГ выше)"
            elif latest is not None and event["id"] == latest["id"] and text and (
                    not verdicts or latest["id"] > verdicts[-1]["id"]):
                text = "(текст — в СЕЙЧАС выше)"
            who = f" {event['worker_id']}" if event["worker_id"] else ""
            line = f"  {_local_hm(event['ts'], with_date=True)} {event['kind']} {event['tz']}{who}"
            if text:
                line += " — " + _one_line(text, key_cap if event["kind"] in key_kinds else other_cap)
            lines.append(line)
        for label, paths in (("файлы записаны", files),
                             ("вне owns", outside_owns),
                             ("без подтверждённого claim/owns", unverified_files)):
            if paths:
                lines.append(f"{label} ({len(paths)}):")
                lines.extend(f"  {path}" for path in paths[:max_files])
                if len(paths) > max_files:
                    lines.append(f"  … и ещё {len(paths) - max_files}")
        return "\n".join(lines)

    text = ""
    for key_cap, other_cap, max_files, max_events in ((4000, 1000, 40, 0), (2000, 300, 30, 0),
                                                      (1000, 160, 20, 120), (500, 100, 15, 60),
                                                      (250, 70, 10, 30), (250, 70, 10, 12)):
        text = render(key_cap, other_cap, max_files, max_events)
        if len(text) <= cap:
            return text
    return text[: cap - 40] + f"\n… обрезано до {cap} символов"


def _watch_line(row: Mapping[str, Any]) -> str | None:
    kind = str(row["kind"])
    payload = _payload(row["payload"])
    if kind == "park" and payload.get("outcome") != "task":
        return None
    target = "/".join(str(x) for x in (row["project"], row["tz"]) if x) or "—"
    note = _one_line(_event_text(kind, payload, row["rc"]), 160)
    line = f"{_local_hm(row['ts'])} {kind} {target} {row['worker_id'] or '-'}"
    return f"{line} — {note}" if note else line


def _since_utc(raw: str | None, hours: float = 2.0) -> str:
    if not raw:
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:  # без пояса — это местное время владельца
        value = value.astimezone()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def watch_events(
    conn: sqlite3.Connection, *, after_id: int = 0, since: str | None = None, project: str | None = None,
) -> tuple[list[str], int]:
    """Значимые события после after_id (и не раньше since): строки и последний просмотренный id."""
    sql = (f"SELECT id,ts,kind,worker_id,project,tz,rc,payload FROM events WHERE id>? "
           f"AND kind IN ({','.join('?' for _ in WATCH_KINDS)})")
    args: list[Any] = [after_id, *WATCH_KINDS]
    if since:
        sql += " AND ts>=?"
        args.append(since)
    if project:
        sql += " AND project=?"
        args.append(project)
    last = after_id
    lines = []
    for row in conn.execute(sql + " ORDER BY id", args).fetchall():
        last = max(last, int(row["id"]))
        line = _watch_line(row)
        if line:
            lines.append(line)
    return lines, last


def _cmd_watch(path: Path, stream: bool, since: str | None, project: str | None, poll_s: float = 2.0) -> int:
    conn = _open_existing(path)
    try:
        if not stream:
            lines, _ = watch_events(conn, since=_since_utc(since), project=project)
            print("\n".join(lines) if lines else "значимых событий нет")
            return 0
        last = -1
        while True:
            try:
                if last < 0:  # старое не печатаем: поток начинается с «сейчас»
                    last = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
                    lines = []
                else:
                    lines, last = watch_events(conn, after_id=last, project=project)
            except sqlite3.OperationalError as exc:  # database is locked / busy — просто следующий круг
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                lines = []
            for line in lines:
                print(line, flush=True)
            time.sleep(poll_s)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()


def _unfinished_dependents(
    tasks: dict[str, dict[str, Any]], extra_edges: dict[str, set[str]],
) -> dict[str, set[str]]:
    """dependents[X] — незавершённые задачи проекта, прямо ждущие X (needs или «ждём X» в блокировке)."""
    out: dict[str, set[str]] = {}
    for tz, task in tasks.items():
        if task["state"] not in UNFINISHED:
            continue
        for dep in task["needs"]:
            if tasks.get(dep, {}).get("state") != "accepted":
                out.setdefault(dep, set()).add(tz)
    for dep, waiting in extra_edges.items():
        for tz in waiting:
            if tasks.get(tz, {}).get("state") in UNFINISHED:
                out.setdefault(dep, set()).add(tz)
    return out


def _blocked_by(roots: set[str], dependents: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        for nxt in dependents.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _red_gates(conn: sqlite3.Connection, project: str | None = None) -> list[dict[str, Any]]:
    """Последний gate-событие каждого проекта, если оно красное."""
    sql = "SELECT id,ts,project,rc,payload FROM events WHERE kind='gate'"
    args: tuple[Any, ...] = ()
    if project:
        sql += " AND project=?"
        args = (project,)
    latest: dict[str, dict[str, Any]] = {}
    for row in conn.execute(sql + " ORDER BY id", args):
        latest[str(row["project"])] = dict(row)
    out = []
    for row in latest.values():
        code = _payload(row["payload"]).get("rc", row["rc"])
        try:
            if int(code) != 0:
                out.append(row)
        except (TypeError, ValueError):
            continue
    return out


_MENTION = re.compile(r"(?<![\w:])((?:fix:)?(?:request:[0-9a-fA-F]{8}|TZ-\d+))")


def _gate_sets(
    tasks: dict[str, dict[str, Any]], extra_edges: dict[str, set[str]], reasons: dict[str, str],
    held: set[str] | None = None,
) -> dict[str, frozenset[str]]:
    """Для каждой незавершённой задачи — набор «корневых ворот»: замороженных задач, которые надо снять,
    чтобы она пошла. Пустой набор у ready — её можно брать.

    Зачем (27.09.2026). «Разблокирует 37 задач» у каждого пункта очереди решений врало: снятие одного
    пункта открывало следующий слой. Телефон для TZ-78 дали — цепочка упёрлась в покупку второго входа
    (TZ-175), о которой пульт знал, но показывал отдельной строкой. Честный вопрос владельцу — какие ворота
    нужны ВСЕ ВМЕСТЕ, чтобы работа пошла. Рёбра: needs, «ждём X» из автоблока, упоминание замороженной
    задачи в причине блокировки («нужен owner-approved ingress (TZ-175/spend gate)») и fix:X → X.
    """
    blockers: dict[str, set[str]] = {tz: set() for tz, t in tasks.items() if t["state"] in UNFINISHED}
    for tz in blockers:
        for dep in tasks[tz]["needs"]:
            if tasks.get(dep, {}).get("state") != "accepted" and dep in tasks:
                blockers[tz].add(dep)
        for dep in _MENTION.findall(reasons.get(tz, "")):
            if dep != tz and (tasks.get(dep, {}).get("state") == "frozen" or dep in (held or ())):
                blockers[tz].add(dep)
    for dep, waiting in extra_edges.items():
        for tz in waiting:
            if tz in blockers and dep in tasks and tasks[dep]["state"] in UNFINISHED:
                blockers[tz].add(dep)
    for tz in blockers:
        base = _base_tz(tz)
        if tz.startswith("fix:") and base in blockers and base != tz:
            blockers[base].add(tz)

    memo: dict[str, frozenset[str]] = {}

    def gates(tz: str, stack: frozenset[str]) -> frozenset[str]:
        if tz in memo:
            return memo[tz]
        found: set[str] = set()
        for dep in blockers.get(tz, ()):
            if dep not in stack:
                found |= gates(dep, stack | {tz})
        if not found and (tasks[tz]["state"] == "frozen" or tz in (held or ())):
            found = {tz}
        result = frozenset(found)
        if not (stack & blockers.get(tz, set())):  # в цикле результат зависит от входа — не кешируем
            memo[tz] = result
        return result

    return {tz: gates(tz, frozenset()) for tz in blockers}


def stalls(conn: sqlite3.Connection, project: str | None = None) -> list[dict[str, Any]]:
    """Стоящая работа по проектам, сгруппированная по набору ворот, которые надо снять вместе."""
    _items, clusters = _decisions(conn, project)
    return clusters


def decisions(conn: sqlite3.Connection, project: str | None = None) -> list[dict[str, Any]]:
    """Очередь решений владельца: всё, что стоит и ждёт человека, по убыванию заблокированной работы."""
    items, _clusters = _decisions(conn, project)
    return items


def _decisions(
    conn: sqlite3.Connection, project: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = datetime.now(timezone.utc)
    task_sql = "SELECT project,tz,title,state,kind,needs,resources,updated_at FROM tasks"
    args: tuple[Any, ...] = ()
    if project:
        task_sql += " WHERE project=?"
        args = (project,)
    by_project: dict[str, dict[str, dict[str, Any]]] = {}
    for row in conn.execute(task_sql, args):
        try:
            needs = [str(x) for x in json.loads(row["needs"] or "[]")]
        except ValueError:
            needs = []
        by_project.setdefault(str(row["project"]), {})[str(row["tz"])] = {**dict(row), "needs": needs}
    # Ворота — только зависимости, которые правда держат: проверка мира не наследуется (world.py).
    # TZ-78 (телефон) больше не «ворота» юнит-экономики через шесть звеньев.
    # Уровень готовности (`TZ-12#contract`) объявлен — тоже не ворота; дальше рёбра — по именам задач.
    index = world.build_index(
        ({"project": p, "tz": tz, "state": t["state"], "needs": t["needs"], "resources": t["resources"]}
         for p, ts in by_project.items() for tz, t in ts.items()),
        world.provided_levels(conn, project), world.block_refs(conn, project))
    world_set = world.world_names()
    for p, ts in by_project.items():
        for tz, t in ts.items():
            if t["needs"]:
                # Настоящие держатели: задача-доказательство в чужом мире заменена своими входами.
                t["needs"] = world.effective_blockers(p, t["needs"], index[(p, tz)]["resources"],
                                                      index, world_set)

    # Последнее «жизненное» событие задачи и последний текст причины (block/escalate/freeze/reject).
    life: dict[tuple[str, str], dict[str, Any]] = {}
    reason: dict[tuple[str, str], dict[str, Any]] = {}
    said: dict[tuple[str, str], dict[str, Any]] = {}  # последнее событие с любым текстом — запасная причина
    kinds = [k for k in CHAIN_KINDS if k not in ("gate", "integrate")]
    ev_sql = ("SELECT id,ts,kind,worker_id,project,tz,rc,payload FROM events WHERE project IS NOT NULL "
              "AND tz IS NOT NULL AND kind IN (" + ",".join("?" for _ in kinds) + ")")
    ev_args: list[Any] = list(kinds)
    if project:
        ev_sql += " AND project=?"
        ev_args.append(project)
    for row in conn.execute(ev_sql + " ORDER BY id", ev_args):
        key = (str(row["project"]), str(row["tz"]))
        item = dict(row)
        life[key] = item
        payload = _payload(row["payload"])
        if row["kind"] in ("block", "escalate", "reject", "freeze") and any(payload.get(k) for k in ("note", "reason")):
            reason[key] = item
        if row["kind"] != "take" and not _event_text(row["kind"], payload).startswith(("было ", "{")):
            if _event_text(row["kind"], payload):
                said[key] = item

    active = {(str(r["project"]), str(r["tz"])) for r in conn.execute(
        "SELECT project,tz FROM claims WHERE state='active'")}

    # Рёбра из текста автоблокировки: «автоблок: ждём request:c2719966» — это тоже зависимость.
    extra: dict[str, dict[str, set[str]]] = {}
    for (proj, tz), item in reason.items():
        if item["kind"] == "block" and by_project.get(proj, {}).get(tz, {}).get("state") in UNFINISHED:
            for dep in _WAIT_FOR.findall(_event_text("block", _payload(item["payload"]))):
                extra.setdefault(proj, {}).setdefault(dep, set()).add(tz)

    items: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    for proj, tasks in by_project.items():
        dependents = _unfinished_dependents(tasks, extra.get(proj, {}))
        picked: set[str] = set()
        texts = {tz: _event_text(ev["kind"], _payload(ev["payload"]))
                 for (p, tz), ev in reason.items() if p == proj}
        held = {tz for (p, tz), ev in life.items() if p == proj and ev["kind"] in ("block", "escalate")
                and tasks.get(tz, {}).get("state") in UNFINISHED}
        gate_of = _gate_sets(tasks, extra.get(proj, {}), texts, held)
        stuck = {tz: g for tz, g in gate_of.items() if g and tasks[tz]["state"] in ("ready", "submitted")}
        groups: dict[frozenset[str], list[str]] = {}
        for tz, g in stuck.items():
            groups.setdefault(g, []).append(tz)
        for g, members in groups.items():
            clusters.append({"project": proj, "gates": sorted(g), "tasks": sorted(members)})

        def add(tz: str, why: str, since: Any) -> None:
            if tz in picked:
                return
            picked.add(tz)
            base = _base_tz(tz)
            hit = _blocked_by({tz, base}, dependents)
            if base != tz and tasks.get(base, {}).get("state") in UNFINISHED:
                hit.add(base)  # fix:TZ-62 держит и сам TZ-62
            hit.discard(tz)
            ev = reason.get((proj, tz))
            if ev:
                text = _one_line(_event_text(ev["kind"], _payload(ev["payload"])), 300)
            elif (proj, tz) in said:
                ev = said[(proj, tz)]
                text = _one_line(f"{ev['kind']}: " + _event_text(ev["kind"], _payload(ev["payload"])), 300)
            else:
                last = life.get((proj, tz))
                note = _event_text(last["kind"], _payload(last["payload"])) if last else ""
                text = f"{last['kind']} без объяснения{f' ({note})' if note else ''}" if last else "событий нет"
            items.append({
                "project": proj, "tz": tz, "title": tasks[tz]["title"], "state": tasks[tz]["state"],
                "why": why, "reason": text, "age_min": _age_min(since, now), "blocks": len(hit),
                "blocks_list": sorted(hit, key=lambda x: (tasks.get(x, {}).get("state") != "ready", x)),
                # frees — сколько задач пойдёт, если снять ТОЛЬКО эти ворота; needed_for — скольким они нужны
                "frees": sum(1 for g in stuck.values() if g == {tz}),
                "needed_for": sum(1 for g in stuck.values() if tz in g),
                "gate": tz in {x for g in stuck.values() for x in g},
                "leaf": gate_of.get(tz) == frozenset({tz}),
            })

        for tz, task in tasks.items():
            if task["state"] == "frozen":
                ev = reason.get((proj, tz)) or life.get((proj, tz))
                add(tz, "заморожена", ev["ts"] if ev else task["updated_at"])
        for tz, task in tasks.items():
            ev = life.get((proj, tz))
            if ev and ev["kind"] in ("block", "escalate") and task["state"] in UNFINISHED:
                add(tz, "эскалация" if ev["kind"] == "escalate" else "заблокирована", ev["ts"])
        for tz, task in tasks.items():
            if task["state"] != "submitted":
                continue
            if any(p == proj and c.startswith("review:") and _base_tz(c) == tz for p, c in active):
                continue
            if any(o.startswith("fix:") and _base_tz(o) == tz and t["state"] in UNFINISHED
                   for o, t in tasks.items()):
                continue  # отклонённая сдача ждёт своего fix (замороженный fix — отдельный пункт очереди)
            ev = life.get((proj, tz))
            since = ev["ts"] if ev else task["updated_at"]
            if _age_min(since, now) >= 60:
                add(tz, "сдана, приёмку никто не ведёт", since)

    for row in _red_gates(conn, project):
        proj = str(row["project"])
        pending = sorted(tz for tz, t in by_project.get(proj, {}).items() if t["state"] in ("ready", "taken", "submitted"))
        items.append({
            "project": proj, "tz": "(гейт)", "title": "гейт интеграции проекта красный", "state": "gate",
            "why": "гейт красный", "reason": _one_line(_event_text("gate", _payload(row["payload"]), row["rc"]), 300),
            "age_min": _age_min(row["ts"], now), "blocks": len(pending), "blocks_list": pending,
            "gate": True, "needed_for": 0, "frees": 0,  # решение владельца, но после ворот, держащих работу
        })
    # Сначала корневые ворота (их снятие двигает работу), по числу задач, которым они нужны;
    # промежуточные звенья цепочки — ниже: решать их напрямую бесполезно.
    items.sort(key=lambda it: (not it.get("gate", True), bool(it.get("leaf")) and not it.get("needed_for"),
                               -it.get("needed_for", 0), -it["blocks"],
                               -it["age_min"], it["project"], it["tz"]))
    clusters.sort(key=lambda c: (-len(c["tasks"]), c["project"]))
    return items, clusters


def _judgment_state(conn: sqlite3.Connection, project: str | None = None) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], str],
]:
    """Что о воротах знает fleet/judgment.py: (просьбы к владельцу, решённое системой за неделю,
    {(проект, TZ): triage, который их сейчас разбирает})."""
    where = " AND project=?" if project else ""
    args: tuple[Any, ...] = (project,) if project else ()
    last: dict[tuple[str, str], Any] = {}
    for row in conn.execute(
        "SELECT id,ts,kind,project,tz,payload FROM events WHERE kind IN "
        "('proposal','proposal_void','default_applied','default_undo')" + where + " ORDER BY id", args,
    ):
        last[(str(row["project"]), str(row["tz"]))] = row
    states, titles = {}, {}
    for r in conn.execute("SELECT project,tz,state,title FROM tasks"):
        states[(str(r["project"]), str(r["tz"]))] = str(r["state"])
        titles[(str(r["project"]), str(r["tz"]))] = str(r["title"] or "")
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    asks, done = [], []
    for (proj, tz), row in sorted(last.items()):
        p = _payload(row["payload"])
        item = {**p, "project": proj, "tz": tz, "title": p.get("title") or titles.get((proj, tz), "")}
        if row["kind"] == "proposal" and states.get((proj, tz)) not in ("accepted", "rejected") and (
                p.get("action") == "park" or not p.get("reversible")):
            asks.append(item)
        elif row["kind"] == "default_applied" and str(row["ts"]) >= week:
            done.append(item)
    handling: dict[tuple[str, str], str] = {}
    for row in conn.execute(
        "SELECT project,tz,body FROM tasks WHERE kind='triage' AND state IN ('ready','taken','submitted','draft')" + where, args,
    ):
        m = re.search(r"subject=(?:decide|gates):(\S+)", str(row["body"] or ""))
        for gate in (m.group(1).split(",") if m else []):
            handling[(str(row["project"]), gate)] = str(row["tz"])
    return asks, done, handling


def proposal_lines(conn: sqlite3.Connection, project: str | None = None) -> list[str]:
    """Владельцу — только то, что правда ждёт его, одной фразой; и что система решила сама (v2 §7)."""
    asks, done, _handling = _judgment_state(conn, project)
    lines = [f"НУЖНО ОТ ТЕБЯ: {a['project']}/{a['tz']} «{_one_line(a['title'], 70)}» — "
             f"{_one_line(a.get('owner_needs') or a.get('why') or '', 260)}" for a in asks if a.get("action") != "park"]
    lines += [f"ждёт условия, не тебя: {a['project']}/{a['tz']} «{_one_line(a['title'], 60)}» — "
              f"{_one_line(a.get('why') or '', 160)}; система перепроверит сама" for a in asks if a.get("action") == "park"]
    lines += [f"решено системой: {d['project']}/{d['tz']} «{_one_line(d['title'], 60)}» → {d.get('action')} "
              f"({_one_line(d.get('why') or '', 140)}); откат: {d.get('undo')}" for d in done]
    return lines


def _local_dm_hm(ts: Any) -> str:
    try:
        return datetime.fromisoformat(str(ts)).astimezone().strftime("%d.%m %H:%M")
    except ValueError:
        return str(ts)


def format_stalls(clusters: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """«Nova VPN: стоят 37 задач — нужны ВСЕ ворота: TZ-175, fix:TZ-62»."""
    lines = []
    for c in clusters[:limit]:
        gates = ", ".join(c["gates"])
        need = "нужны ВСЕ ворота" if len(c["gates"]) > 1 else "нужны ворота"
        lines.append(f"{c['project']}: стоят {_tasks_word(len(c['tasks'])).replace('задачу', 'задача')} — "
                     f"{need}: {gates}")
    return lines


def world_lines(conn: sqlite3.Connection, project: str | None = None) -> list[str]:
    """Ресурсы мира, которых сейчас нет, и сколько работы их ждёт (world.py).

    Зачем (07.10.2026): владелец подключал телефон, зная, что он нужен Nova, — но сам не видел,
    когда он нужен и сколько задач стоит без него. Здесь — одна строка на отсутствующий ресурс.
    """
    cfg = world.config()
    sensed = [name for name, info in cfg.items() if info.get("sensor")]
    if not sensed:
        return []
    where = " WHERE project=?" if project else ""
    args: tuple[Any, ...] = (project,) if project else ()
    rows = conn.execute("SELECT project,tz,state,needs,resources FROM tasks" + where, args).fetchall()
    index = world.build_index(rows, world.provided_levels(conn, project), world.block_refs(conn, project))
    world_set = world.world_names(cfg)
    open_states = ("ready", "frozen", "taken")
    dependents: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for (p, tz), info in index.items():
        if info["state"] in open_states and info["needs"]:
            for dep in world.unmet_needs(p, info["needs"], info["resources"], index, world_set):
                dependents.setdefault((p, world.dep_tz(dep)), set()).add((p, tz))
    out: list[str] = []
    for name in sensed:
        row = conn.execute(
            "SELECT ts,payload FROM events WHERE path_norm=? AND kind=? ORDER BY ts DESC LIMIT 1",
            (world.path_key(name), world.EVENT_KIND)).fetchone()
        if row is None or _payload(row["payload"]).get("state") != world.DOWN:
            continue
        direct = {k for k, info in index.items() if info["state"] in open_states and name in info["resources"]}
        if not direct:
            continue
        behind: set[tuple[str, str]] = set()
        stack = list(direct)
        while stack:
            for nxt in dependents.get(stack.pop(), ()):
                if nxt not in behind and nxt not in direct:
                    behind.add(nxt)
                    stack.append(nxt)
        need = ("маршрут Chrome→ChatGPT проверяется отдельно от egress сервера; "
                "VPN не требуется без подтверждённого отказа обоих"
                if name == "chatgpt-reachable" else str(cfg[name].get("need") or f"нужен ресурс «{name}»"))
        projects = ", ".join(sorted({p for p, _ in direct}))
        seen = str(_payload(row["payload"]).get("detail") or "")
        out.append(f"нет ресурса «{name}» с {_local_dm_hm(row['ts'])}: ждут {len(direct)} "
                   f"{_tasks_word(len(direct))} ({projects}), за ними ещё {len(behind)} — {need}"
                   + (f" [система видит: {seen}]" if seen else ""))
    return out


def stuck_lines(conn: sqlite3.Connection, project: str | None = None, limit: int = 12) -> list[str]:
    """Почему готовая задача не выдаётся — по каждой и сводкой (общий датчик заторов, 07.10.2026).

    Датчик телефона закрыл один вид затора; этот отвечает на вопрос «что держит работу» для всех:
    непринятая зависимость, ресурс мира отсутствует, ресурс занят, зона пересекается (с кем и
    каким файлом — горячие файлы видны сводкой), проект выключен. Те же правила, что у выдачи.
    """
    from dev_mcp.fleet import queue as fq
    from dev_mcp.fleet.model import paths_overlap
    where = " AND t.project=?" if project else ""
    args: tuple[Any, ...] = (project,) if project else ()
    all_rows = conn.execute("SELECT project,tz,state,needs,resources FROM tasks").fetchall()
    index = world.build_index(all_rows, world.provided_levels(conn), world.block_refs(conn))
    cfg = world.config()
    world_set, run_scoped = world.world_names(cfg), world.run_scoped(cfg)
    active = [(str(r["tz"]), str(r["path_norm"])) for r in conn.execute(
        "SELECT c.tz,cp.path_norm FROM claim_paths cp JOIN claims c ON c.id=cp.claim_id WHERE c.state='active'")]
    busy = {str(r["resource"]): str(r["tz"]) for r in conn.execute(
        "SELECT cr.resource,c.tz FROM claim_resources cr JOIN claims c ON c.id=cr.claim_id WHERE c.state='active'")}
    down = set()
    for name, info in cfg.items():
        if info.get("sensor"):
            row = conn.execute("SELECT payload FROM events WHERE path_norm=? AND kind=? ORDER BY ts DESC LIMIT 1",
                               (world.path_key(name), world.EVENT_KIND)).fetchone()
            if row is not None and _payload(row["payload"]).get("state") == world.DOWN:
                down.add(name)
    counts: dict[str, int] = {}
    holders: dict[str, set[str]] = {}
    lines: list[str] = []
    rows = conn.execute(
        "SELECT t.*,p.root AS project_root,p.active AS project_active FROM tasks t JOIN projects p "
        "ON p.name=t.project WHERE t.state='ready'" + where + " ORDER BY t.priority,t.created_at", args).fetchall()
    free = 0
    for r in rows:
        row = dict(r)
        key = (str(row["project"]), str(row["tz"]))
        # Maintenance can insert a ready task between the two SELECTs. Use
        # that row's current dependency fields instead of crashing the console.
        info = index.get(key)
        if info is None:
            info = world.build_index([row])[key]
        why: list[str] = []
        unmet = world.unmet_needs(str(row["project"]), info["needs"], info["resources"], index, world_set)
        if unmet:
            # 20.10: ждёт замороженную, отклонённую или несуществующую задачу — сама не дождётся
            # (Nova VPN TZ-249 ← TZ-243, которой нет; TZ-106 ← замороженные с сентября).
            dead = []
            for need in unmet:
                base = str(need).split("#", 1)[0].strip()
                if not re.fullmatch(r"[\w:.\-]+", base):
                    continue
                state = conn.execute("SELECT state FROM tasks WHERE project=? AND tz=?",
                                     (str(row["project"]), base)).fetchone()
                if state is None or state["state"] in ("frozen", "rejected"):
                    dead.append(f"{base} {'нет в реестре' if state is None else state['state']}")
            if dead:
                counts["ждёт мёртвую задачу"] = counts.get("ждёт мёртвую задачу", 0) + 1
                lines.append(f"  {row['project']}/{row['tz']}: ждёт мёртвую задачу — {', '.join(dead)}")
                continue
            counts["ждёт задачи"] = counts.get("ждёт задачи", 0) + 1
            continue  # цепочки показывает decisions; здесь — то, что готово, но стоит
        if not row["project_active"]:
            why.append("проект выключен")
        missing = [x for x in info["resources"] if x in down]
        if missing:
            why.append("нет ресурса " + ", ".join(missing))
        taken = [f"{x} у {busy[x]}" for x in info["resources"] if x in busy and x not in run_scoped]
        for x in info["resources"]:
            if x in busy and x not in run_scoped:
                holders.setdefault(busy[x], set()).add(str(row["tz"]))
        if taken:
            why.append("ресурс занят: " + ", ".join(taken))
        clash: dict[str, set[str]] = {}
        for owned in fq._claimed_paths(row):
            for holder, path in active:
                if holder != str(row["tz"]) and paths_overlap(owned, path):
                    clash.setdefault(holder, set()).add(path)
        if clash:
            parts = []
            for holder, paths in sorted(clash.items()):
                short = sorted(p.rsplit("/", 1)[-1] or p for p in paths)
                parts.append(f"{holder} ({', '.join(short[:2])}{' …' if len(short) > 2 else ''})")
                holders.setdefault(holder, set()).add(str(row["tz"]))
            why.append("зона у " + "; ".join(parts))
        if not why:
            free += 1
            continue
        kind = why[0].split(":")[0].split(" у ")[0]
        counts[kind] = counts.get(kind, 0) + 1
        if len(lines) < limit:
            lines.append(f"  {row['project']}/{row['tz']}: " + " · ".join(why))
    # Число «можно выдать» и причины — тем же кодом, что выдача (план консорциума 11.10, п.2):
    # 11.10 `why` писал «можно выдать 14», а выдача давала 0 — три часа ложной картины (урок 34).
    try:
        from dev_mcp.fleet.store import Store
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        skipped: list = []
        with Store.open(db_path).read() as tx:
            # A blank worker is deliberately ineligible for cross_review: its
            # author may be Gemini, while only a *real* independent ChatGPT
            # worker can take the reverse audit. Keep the usual unbound-worker
            # forecast for ordinary tasks; resolve identity-dependent audits
            # against actual registered, non-revoked workers with the exact
            # same _available predicate used by take_next.
            available = {
                (str(row["project"]), str(row["tz"]))
                for row, _owns, _resources in fq._available(tx, "", project, skipped=skipped)
            }
            audits = {
                (p, tz) for p, tz, reason in skipped
                if reason == "перекрёстное ревью: не этому агенту"
            }
            if audits:
                reviewers = tx.query(
                    "SELECT id FROM workers WHERE kind='chatgpt' AND revoked_at IS NULL "
                    "ORDER BY last_seen DESC"
                )
                for worker in reviewers:
                    for row, _owns, _resources in fq._available(tx, str(worker["id"]), project):
                        key = (str(row["project"]), str(row["tz"]))
                        if key in audits:
                            available.add(key)
                    if audits.issubset(available):
                        break
            free = len(available)
            skipped = [(p, tz, reason) for p, tz, reason in skipped
                       if (p, tz) not in available]
        counts = {}
        for _p, _tz, reason in skipped:
            counts[reason] = counts.get(reason, 0) + 1
    except Exception:  # noqa: BLE001 — диагностика не должна падать; старая оценка остаётся
        pass
    head = f"готовых {len(rows)}: можно выдать {free}; " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    dead_n = sum(1 for line in lines if ": ждёт мёртвую задачу — " in line)
    if dead_n:
        head += f" (из них ждут мёртвую задачу: {dead_n} — сами не сдвинутся)"
    out = [head] + lines
    if holders:
        top = sorted(holders.items(), key=lambda kv: -len(kv[1]))[:3]
        out.append("больше всех держит готовую работу: " + "; ".join(
            f"{holder} — {len(waiting)}" for holder, waiting in top))
    return out


def dev_mcp_asks(conn: sqlite3.Connection) -> list[str]:
    """Замороженные задачи, которые ждут правки dev-mcp: её делает только Claude Code (07.10.2026).

    Агенты флота закрыты на запись в dev-mcp и честно блокируются, а до Claude Code заявка не
    доходила: три заявки Beta App (bite строкой в note) висели с 04.10, хотя правка давно была
    сделана. Строка — в сводке SessionStart пульта dev-mcp.
    """
    out: list[str] = []
    for row in conn.execute(
        "SELECT t.project,t.tz,(SELECT payload FROM events e WHERE e.project=t.project AND e.tz=t.tz "
        "AND e.kind='block' ORDER BY e.id DESC LIMIT 1) AS reason FROM tasks t WHERE t.state='frozen'"
    ):
        text = str(_payload(row["reason"]).get("reason") or "")
        if re.search(r"(?i)dev-mcp|dev_mcp", text):
            out.append(f"{row['project']}/{row['tz']}: {_one_line(text, 140)}")
    return out


def queue_summary(conn: sqlite3.Connection, project: str | None = None) -> str:
    """Одна строка: сколько ready реально можно взять и сколько стоит за непринятыми needs."""
    where = " WHERE project=?" if project else ""
    args: tuple[Any, ...] = (project,) if project else ()
    rows = conn.execute("SELECT project,tz,state,needs,resources FROM tasks" + where, args).fetchall()
    index = world.build_index(rows, world.provided_levels(conn, project), world.block_refs(conn, project))
    world_set = world.world_names()
    ready = waiting = 0
    for r in rows:
        if r["state"] != "ready":
            continue
        ready += 1
        info = index[(str(r["project"]), str(r["tz"]))]
        if world.unmet_needs(str(r["project"]), info["needs"], info["resources"], index, world_set):
            waiting += 1
    claims = conn.execute(
        "SELECT COUNT(*) FROM claims WHERE state='active'" + (" AND project=?" if project else ""), args,
    ).fetchone()[0]
    return (f"очередь: ready {ready}, из них ждут непринятых needs {waiting}, "
            f"можно брать {ready - waiting}; активных захватов {claims}")


def format_decisions(items: list[dict[str, Any]], limit: int | None = None, *, compact: bool = False) -> list[str]:
    lines = []
    for n, it in enumerate(items[:limit] if limit else items, 1):
        if it.get("gate") and it.get("needed_for"):
            unlock = (f"одни эти ворота пустят {_tasks_word(it['frees'])}" if it["frees"]
                      else "одни эти ворота не пустят ничего")
            cnt = it["needed_for"]
            unlock += f", нужны для {cnt} {'задачи' if cnt % 10 == 1 and cnt % 100 != 11 else 'задач'}"
        elif it.get("leaf") and not it.get("needed_for"):
            unlock = "работу не держит — снять или закрыть"
        elif it["state"] in ("frozen", "submitted", "ready", "taken") and "needed_for" in it and it["tz"] != "(гейт)":
            unlock = f"звено цепочки, само ждёт ворот ниже (за ним {_tasks_word(it['blocks'])})"
        else:
            unlock = f"разблокирует {_tasks_word(it['blocks'])}"
        if compact:
            lines.append(f"{n}. {it['project']}/{it['tz']} [{it['why']}, {_age_label(it['age_min'])}] "
                         f"{unlock} — {_one_line(it['reason'] or it['title'], 110)}")
            continue
        lines.append(f"{n}. {it['project']}/{it['tz']} — {_one_line(it['title'], 90)}")
        sample = ", ".join(it["blocks_list"][:6]) + (" …" if len(it["blocks_list"]) > 6 else "")
        lines.append(f"   {it['why']} {_age_label(it['age_min'])} · {unlock}" + (f": {sample}" if sample else ""))
        if it["reason"]:
            lines.append(f"   причина: {it['reason']}")
    return lines


def _tasks_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} задачу"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} задачи"
    return f"{n} задач"


def split_alarms(
    items: list[tuple[str, str | None]], show_all: bool, hours: float = STALE_ALARM_H,
) -> tuple[list[str], int]:
    """Тревоги на событиях старше суток прячутся; тревоги про текущее состояние (ts=None) — никогда."""
    if show_all:
        return [text for text, _ in items], 0
    fresh, hidden = [], 0
    for text, ts in items:
        if ts is not None and _age_min(ts) > hours * 60:
            hidden += 1
        else:
            fresh.append(text)
    return fresh, hidden


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Пульт владельца fleet: по умолчанию только исключения.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=argparse.SUPPRESS)
    parser.add_argument("--selftest", action="store_true", help="самопроверка без побочных эффектов")
    parser.add_argument("--all", dest="show_all", action="store_true",
                        help="показать и тревоги старше суток (по умолчанию скрыты)")
    sub = parser.add_subparsers(dest="command")

    report = sub.add_parser("report", help="заявлено против сделано")
    report.add_argument("project", nargs="?")

    active = sub.add_parser("active", help="кто прямо сейчас держит какие пути")
    active.add_argument("project", nargs="?")

    result = sub.add_parser("result", help="итог задачи одним блоком: вердикты, приёмка, ход, файлы")
    result.add_argument("task", help="Проект/TZ-7 (или TZ-7, если имя однозначно)")

    watch = sub.add_parser("watch", help="значимые события строкой на событие (для монитора)")
    watch.add_argument("--stream", action="store_true", help="следить бесконечно, опрос раз в 2 с")
    watch.add_argument("--since", help="ISO-время начала без --stream (по умолчанию последние 2 ч)")
    watch.add_argument("--project")

    decide = sub.add_parser("decisions", help="очередь решений владельца по объёму заблокированной работы")
    decide.add_argument("--project")
    decide.add_argument("--limit", type=int, default=0)
    decide.add_argument("--json", action="store_true", help="машиночитаемо (для fleet/judgment.py)")

    undo = sub.add_parser("undo", help="откатить решение, применённое по умолчанию (v2 §7)")
    undo.add_argument("task", help="<Проект>/<TZ>")

    lessons_cmd = sub.add_parser(
        "lessons", help="уроки флота: каталог, эффект, пул отказов; add ФАЙЛ --project П / retire П/id")
    lessons_cmd.add_argument("action", nargs="?", choices=("add", "retire"))
    lessons_cmd.add_argument("target", nargs="?", help="add: файл урока; retire: <Проект>/<id>")
    lessons_cmd.add_argument("--project", help="add: проект урока")

    browser = sub.add_parser("browser", help="браузер флота: status | off (не запускать) | on | resume (снять аварийную остановку)")
    browser.add_argument("action", nargs="?", default="status", choices=("status", "on", "off", "resume"))

    why = sub.add_parser("why", help="почему готовые задачи не выдаются: зависимость, ресурс, зона, горячие файлы")
    why.add_argument("project", nargs="?")

    flow = sub.add_parser("flow", help="метрики потока v2: утечка приёмки, ходы, парковка, будки, гейты (E1–E5)")
    flow.add_argument("--since", help="ISO-время UTC или число часов назад (по умолчанию 24)")

    metric = sub.add_parser("metrics", help="метрики workflow")
    metric.add_argument("--since", required=True)

    revoke = sub.add_parser("revoke", help="kill switch воркера")
    revoke.add_argument("worker")
    revoke.add_argument("reason")

    resume = sub.add_parser("resume", help="снять kill switch")
    resume.add_argument("worker")

    recover = sub.add_parser("recover", help="вернуть длительно молчащий taken-claim в ready, сохранив файлы")
    recover.add_argument("project")
    recover.add_argument("tz")
    recover.add_argument("reason")
    recover.add_argument("--min-silence-min", type=int, default=120)

    over = sub.add_parser("overseer", help="надзиратель хода: add <url> --project X [--send] | mode <url> shadow|send|off | list | log [N]")
    over.add_argument("action", choices=["add", "mode", "list", "log"])
    over.add_argument("target", nargs="?", default="")
    over.add_argument("value", nargs="?", default="")
    over.add_argument("--project", default="")
    over.add_argument("--send", action="store_true", help="сразу отправлять в чат (по умолчанию тень)")
    over.add_argument("--engine", choices=["proxy", "semantic"], default="proxy",
                      help="proxy — работать за владельца (по умолчанию), semantic — прежний судья-модель")

    qk = sub.add_parser(
        "quick",
        help="быстрые агенты: new [--after q1,q2] ТЕКСТ|@файл | run | say | show | list | close | wait [--after N]",
        description=("ВНИМАНИЕ: cmd.exe обрезает многострочные аргументы на первом переводе строки. "
                     "Для многострочного сообщения используй quick say qN @файл "
                     "(UTF-8, путь с пробелами заключи в кавычки). "
                     "Для new/run также доступен @файл."),
    )
    qk.add_argument("action", choices=["run", "new", "say", "show", "list", "close", "wait", "adopt"])
    qk.add_argument("words", nargs="*")
    qk.add_argument("--project", default="")
    qk.add_argument("--scope", action="append", help="область записи (путь от корня проекта), можно несколько")
    qk.add_argument("--home", default="", help="адрес, где открыть новый чат (по умолчанию — проект флота в ChatGPT)")
    qk.add_argument("--timeout", type=float, default=3300, help="wait: потолок ожидания, с (≤55 мин)")
    qk.add_argument("--after", default=None,
                    help="new: ID зависимостей через запятую (q1,q2); wait: номер ответа N")
    qk.add_argument("--all", action="store_true", help="list: все владельцы; wait: ждать ответ каждого ID")
    qk.add_argument("--force", action="store_true", help="adopt: передать без ожидания 30 минут")

    say = sub.add_parser("say", help="broadcast от владельца")
    say.add_argument("body")

    tell = sub.add_parser("tell", help="адресное указание воркеру (W123) или держателю задачи (Проект/TZ-7)")
    tell.add_argument("to")
    tell.add_argument("body")

    instruct = sub.add_parser("instruct", help="поручение ведущему проекта: объект со статусом, закрывает ведущий")
    instruct.add_argument("project")
    instruct.add_argument("text", help="что сделать")
    instruct.add_argument("--done-when", dest="done_when", required=True, help="критерий «сделано»")
    instruct.add_argument("--want", default="", help="зачем человеку / требуемый результат")
    instruct.add_argument("--priority", type=int, default=5)
    instruct.add_argument("--source", default="", help="сессия или документ-источник")

    focus = sub.add_parser("focus", help="дать приоритет точной задаче открытого поручения владельца")
    focus.add_argument("project")
    focus.add_argument("instruction")
    focus.add_argument("tz")

    unblock = sub.add_parser("unblock", help="ответ владельца на карточку препятствия ведущего (ob-…)")
    unblock.add_argument("project")
    unblock.add_argument("obstacle", help="id препятствия ob-…")
    unblock.add_argument("answer", help="решение владельца")

    priority = sub.add_parser("priority", help="приоритет проекта")
    priority.add_argument("project")
    priority.add_argument("value", type=int)

    freeze = sub.add_parser("freeze", help="снять задачу с раздачи")
    freeze.add_argument("project")
    freeze.add_argument("tz")

    reactivate = sub.add_parser("reactivate", help="frozen/rejected → ready для точной задачи, включая generated fix:*")
    reactivate.add_argument("project")
    reactivate.add_argument("tz")
    reactivate.add_argument("reason")

    grant = sub.add_parser("grant", help="решение владельца в реестр: агенты видят его в памятке как формальное")
    grant.add_argument("project")
    grant.add_argument("text")
    grant.add_argument("--tz", dest="tzs", action="append", default=[],
                       help="задача (можно несколько); без --tz — на весь проект")

    set_acceptance = sub.add_parser(
        "set-acceptance",
        help="обновить acceptance существующей задачи без active claim; old/new пишутся в audit event",
    )
    set_acceptance.add_argument("project")
    set_acceptance.add_argument("tz")
    set_acceptance.add_argument(
        "--command",
        dest="commands",
        action="append",
        required=True,
        help="одна acceptance-команда; флаг повторяется для каждой команды",
    )

    budget = sub.add_parser("chat-budget", help="сколько задач подряд берёт один чат ChatGPT (без N — показать)")
    budget.add_argument("n", nargs="?", type=int)

    sub.add_parser(
        "owner-password",
        help="задать новый пароль владельца для вкладки ChatGPT (печатается один раз)",
    )

    grant = sub.add_parser(
        "owner-grant",
        help="выдать роль владельца агенту по номеру: owner-grant 123 [W124 …] (без номера — у кого роль)",
    )
    grant.add_argument("worker", nargs="*")
    # 48 ч (владелец 20.11.2026): за ночь 12-часовая роль истекала.
    grant.add_argument("--hours", type=float, default=48)
    grant.add_argument("--revoke", action="store_true", help="снять выдачу (без W — у всех)")
    grant.add_argument("--pick", action="store_true", help="спросить номер (owner-grant.cmd)")
    grant.add_argument("--list", action="store_true", help="у кого сейчас роль")

    bootstrap = sub.add_parser(
        "bootstrap",
        help="безопасно импортировать только явно выбранные TZ старого проекта",
    )
    bootstrap.add_argument("project")
    bootstrap.add_argument("root", type=Path)
    bootstrap.add_argument("tz", nargs="+")
    bootstrap.add_argument(
        "--reactivate",
        action="store_true",
        help="явно вернуть выбранную ранее frozen-задачу в ready",
    )
    retire = sub.add_parser(
        "retire",
        help="снять задачи выведенных генераторов (rudiments.py); владельца не спрашивать",
    )
    retire.add_argument("--dry", action="store_true", help="только счёт по классам")
    retire.add_argument("--class", dest="classes", action="append", default=None,
                        help="класс из rudiments.CLASSES (повторяется); по умолчанию — все")
    return parser


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE projects(name TEXT PRIMARY KEY,root TEXT NOT NULL,priority INTEGER NOT NULL DEFAULT 5,active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE tasks(project TEXT NOT NULL,tz TEXT NOT NULL,title TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 5,kind TEXT NOT NULL DEFAULT 'work',spec_path TEXT NOT NULL DEFAULT '',
      spec_sha TEXT NOT NULL DEFAULT '',needs TEXT NOT NULL DEFAULT '[]',owns TEXT NOT NULL DEFAULT '[]',
      resources TEXT NOT NULL DEFAULT '[]',acceptance TEXT NOT NULL DEFAULT '[]',author_worker TEXT DEFAULT NULL,
      body TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(project,tz));
    CREATE TABLE workers(id TEXT PRIMARY KEY,label TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'chatgpt',
      first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,revoked_at TEXT DEFAULT NULL,revoke_reason TEXT NOT NULL DEFAULT '');
    CREATE TABLE claims(id INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,tz TEXT NOT NULL,worker_id TEXT NOT NULL,
      state TEXT NOT NULL,started_at TEXT NOT NULL,released_at TEXT DEFAULT NULL);
    CREATE TABLE claim_paths(claim_id INTEGER NOT NULL,path_norm TEXT NOT NULL);
    CREATE TABLE claim_resources(claim_id INTEGER NOT NULL,resource TEXT NOT NULL);
    CREATE TABLE messages(id TEXT PRIMARY KEY,dedup_key TEXT NOT NULL DEFAULT '',to_worker TEXT DEFAULT NULL,to_task TEXT DEFAULT NULL,
      broadcast INTEGER NOT NULL DEFAULT 0,from_kind TEXT NOT NULL,from_id TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL,
      body TEXT NOT NULL,created_at TEXT NOT NULL,delivered_at TEXT DEFAULT NULL,acked_at TEXT DEFAULT NULL);
    CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,kind TEXT NOT NULL,worker_id TEXT DEFAULT NULL,
      project TEXT DEFAULT NULL,tz TEXT DEFAULT NULL,tool TEXT NOT NULL DEFAULT '',path_norm TEXT NOT NULL DEFAULT '',
      rc INTEGER DEFAULT NULL,payload TEXT NOT NULL DEFAULT '{}');
    """)


def selftest() -> int:
    """Exercise reports on a temporary registry only."""
    with tempfile.TemporaryDirectory(prefix="dev-mcp-fleet-selftest-") as tmp:
        db = Path(tmp) / "fleet.sqlite"
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _create_schema(conn)
        ts = _now()
        conn.execute("INSERT INTO projects(name,root) VALUES('P',?)", (tmp,))
        conn.execute(
            """INSERT INTO tasks(project,tz,state,created_at,updated_at)
               VALUES('P','TZ-1','taken',?,?)""",
            (ts, ts),
        )
        conn.execute(
            """INSERT INTO workers(id,first_seen,last_seen) VALUES('W1',?,?)""",
            (ts, ts),
        )
        conn.execute(
            """INSERT INTO claims(project,tz,worker_id,state,started_at)
               VALUES('P','TZ-1','W1','active',?)""",
            (ts,),
        )
        conn.execute(
            """INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload)
               VALUES(?,'run','W1','P','TZ-1','pytest','',0,?)""",
            (ts, json.dumps({"claimed_rc": 99})),
        )
        conn.commit()
        text = claimed_vs_done(conn)
        if "pytest rc=0" not in text or "rc=99" in text:
            raise AssertionError("report must use rc from the run event")
        if exceptions(conn):
            raise AssertionError(f"fresh healthy registry produced exceptions: {exceptions(conn)!r}")
        data = metrics(conn, "2000-01-01")
        for key in ("requeue_rate", "foreign_path_rate", "owner_interventions_per_accepted",
                    "protocol_declared_rate"):
            if "n" not in data[key] or "N" not in data[key]:
                raise AssertionError(f"{key} has no denominator")
        conn.close()
    print("selftest: OK")
    return 0


def _cmd_chat_budget(db_path: Path, n: int | None) -> int:
    """Показать или задать лимит задач на чат. Действует со следующего fleet_take, без рестарта."""
    from dev_mcp.fleet import queue

    class _Probe:
        path = str(db_path)

    current = queue.chat_budget(_Probe())
    if n is None:
        print(f"задач подряд в одном чате ChatGPT: {current} (по умолчанию {queue.CHAT_TASK_BUDGET})")
        return 0
    low, high = queue.CHAT_BUDGET_RANGE
    if not low <= n <= high:
        print(f"лимит {n} вне {low}–{high}: не меняю (сейчас {current})")
        return 2
    target = queue.write_setting(db_path, "chat_task_budget", n)
    print(f"задач подряд в одном чате ChatGPT: {current} → {n}. Действует со следующего fleet_take, "
          f"без перезапуска; открытые чаты считаются по новому лимиту. Файл: {target}")
    return 0


def _cmd_owner_password() -> int:
    """Новый пароль владельца: хеш — в data/owner-secret.json, сам пароль — только на экран.

    Старый пароль перестаёт действовать сразу для новых разблокировок; уже открытые сессии
    живут до перезапуска dev-mcp или своего срока.
    """
    import subprocess

    from dev_mcp.fleet.owner import generate_password, secret_path, set_password

    password = generate_password()
    target = secret_path(REPO)
    set_password(target, password)
    copied = False
    try:  # в буфер обмена — чтобы не перепечатывать руками; ASCII, clip справится
        subprocess.run(["clip"], input=password.encode("ascii"), check=True, timeout=5)
        copied = True
    except Exception:  # noqa: BLE001 — без буфера пароль всё равно на экране
        pass
    print(f"Пароль владельца: {password}")
    print(f"{'Скопирован в буфер обмена. ' if copied else ''}Хеш — в {target}; сам пароль нигде не хранится.")
    print("Дай его своему агенту во вкладке: fleet_join, затем fleet_owner_unlock(agent, password).")
    return 0


OWNER_GRANT_FRESH_H = 3  # список для двойного щелчка: только вкладки, звавшие сервер за эти часы


def _worker_ids(raw: list[str] | str | None) -> list[str]:
    """`70 W71, w72` → [W70, W71, W72]: владелец даёт номер так, как видит его в чате."""
    items = raw if isinstance(raw, list) else [raw or ""]
    out: list[str] = []
    for item in items:
        for token in str(item).replace(",", " ").split():
            token = token.strip().upper()
            if re.fullmatch(r"W?\d+", token):
                out.append(token if token.startswith("W") else f"W{token}")
            elif token:
                out.append(token)
    return list(dict.fromkeys(out))


def _grant_ids(db_path: Path, ids: list[str], hours: float) -> int:
    from dev_mcp.fleet.owner import grant, grants_path, secret_path

    target = grants_path(secret_path(REPO))
    code = 0
    with _open_existing(db_path) as conn:
        for w in ids:
            row = conn.execute("SELECT id,label,last_seen,revoked_at FROM workers WHERE id=?", (w,)).fetchone()
            if row is None:
                print(f"  {w}: такого номера в реестре нет — пусть агент один раз вызовет любой fleet_* инструмент")
                code = 2
                continue
            restored = ""
            if row["revoked_at"]:
                # 20.11.2026: автовосстановление молчащих захватов отзывало номер вкладки
                # владельца за ночь, и выдать роль снова было нельзя — агента приходилось
                # регистрировать заново. Явная выдача владельцем на своей машине и есть
                # доверие: снимаем отзыв тем же путём, что `fleet.cmd resume`.
                old = conn.execute("SELECT revoke_reason FROM workers WHERE id=?", (w,)).fetchone()
                _mutate(db_path, lambda c, w=w: _cmd_resume(c, w), intervention_command="resume")
                restored = f"; номер был отозван ({str(old[0] or '')[:80]}) — восстановлен"
            until = grant(target, w, int(hours * 3600))
            print(f"  {w}: роль владельца до {time.strftime('%d.%m %H:%M', time.localtime(until))} "
                  f"(метка {row['label'] or '—'}, последний вызов {str(row['last_seen'])[:16]}){restored}")
    return code


def _cmd_owner_grant_pick(db_path: Path, hours: float) -> int:
    """Двойной щелчок (owner-grant.cmd): номер из чата → роль. Заявки и метки не нужны (15.11)."""
    from dev_mcp.fleet.owner import grants_path, read_grants, secret_path

    now = time.time()
    granted = {w: u for w, u in read_grants(grants_path(secret_path(REPO))).items() if u > now}
    if granted:
        print("Роль владельца сейчас у: " + ", ".join(
            f"{w} (до {time.strftime('%d.%m %H:%M', time.localtime(u))})" for w, u in sorted(granted.items())))
    answer = input("\nНомер агента (из его чата: W123 или просто 123), можно несколько; Enter — выйти: ").strip()
    ids = _worker_ids(answer)
    if not ids:
        print("ничего не выдано")
        return 0
    return _grant_ids(db_path, ids, hours)


def _cmd_owner_grant(db_path: Path, workers: list[str] | None, hours: float, revoke: bool,
                     show: bool = False) -> int:
    """Роль владельца по номеру агента — любому воркеру, без заявки и без метки (владелец, 15.11).

    23.09.2026: пароль в аргументе fleet_owner_unlock ChatGPT не пропускал ни разу и после
    этого блокировал весь чат — доверие даёт эта машина. 15.11: требование метки pult/owner
    и «выдай роль» от агента сняты: выдача копила старые вкладки, а номер владелец и так видит.
    """
    from dev_mcp.fleet.owner import grants_path, read_grants, revoke_grant, secret_path

    target = grants_path(secret_path(REPO))
    ids = _worker_ids(workers)
    if revoke:
        gone = [g for w in (ids or [None]) for g in revoke_grant(target, w)]
        print(f"роль владельца снята: {', '.join(gone) or 'выдач не было'}")
        return 0
    if show or not ids:
        now = time.time()
        live = {w: u for w, u in read_grants(target).items() if u > now}
        print("роль владельца: " + (", ".join(
            f"{w} до {time.strftime('%d.%m %H:%M', time.localtime(u))}" for w, u in sorted(live.items()))
            or "ни у кого") + "\nвыдать: fleet.cmd owner-grant <номер> [<номер> …] [--hours N]")
        return 0
    return _grant_ids(db_path, ids, hours)


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    if ns.selftest:
        try:
            return selftest()
        except Exception as exc:
            print(f"selftest: FAIL — {exc}", file=sys.stderr)
            return 1

    if ns.command == "owner-password":
        return _cmd_owner_password()

    path = ns.db or fleet_db_path(REPO)
    if ns.command is None:
        # Print before platform/status output so a broken VPN is immediately visible.
        if alert := chatgpt_owner_alert(path):
            print("! " + alert)
    if ns.command == "lessons":
        from dev_mcp.fleet import lessons

        if ns.action is None:
            conn = _open_existing(path)
            try:
                for line in lessons.report_tx(lessons.ConnTx(conn)):
                    print(line)
            finally:
                conn.close()
            return 0
        store = Store.open(str(path))
        if ns.action == "add":
            if not (ns.target and ns.project):
                print("lessons add ФАЙЛ --project ПРОЕКТ", file=sys.stderr)
                return 1
            ids, errors = lessons.register_files(store, ns.project, [Path(ns.target)], "owner")
            print("зарегистрирован: " + ", ".join(ids) if ids else "не зарегистрирован")
            for err in errors:
                print(f"  {err}", file=sys.stderr)
            return 0 if ids else 1
        if ns.action == "retire":
            project, _, lesson_id = (ns.target or "").rpartition("/")
            print("снят" if lessons.retire(store, project, lesson_id) else "такого урока нет")
            return 0
    if ns.command == "undo":
        from dev_mcp.fleet.judgment import undo_default

        project, _, tz = ns.task.rpartition("/")
        try:
            if tz.startswith("lead-decision:"):
                from dev_mcp.fleet.lead_decision import undo as undo_lead_decision
                print(undo_lead_decision(Store.open(str(path)), project,
                                         tz.removeprefix("lead-decision:")))
            else:
                print(undo_default(Store.open(str(path)), project, tz))
        except ValueError as exc:
            print(f"undo: {exc}", file=sys.stderr)
            return 1
        return 0
    if ns.command == "browser":
        from dev_mcp.fleet import supervisor as fleet_supervisor
        if ns.action == "status":
            state = fleet_supervisor.browser_control(Path(path))
        else:
            state = fleet_supervisor.set_browser_control(
                Path(path), autostart={"on": True, "off": False}.get(ns.action),
                halt=None if ns.action in ("resume", "on") else False, by="fleet.cmd")
        halt = state["halt"]
        print("браузер флота: автозапуск " + ("вкл" if state["autostart"] else "ВЫКЛ") +
              " · " + (f"АВАРИЙНАЯ ОСТАНОВКА с {halt.get('at', '?')}: {halt.get('reason', '')}"
                       if halt else "работает"))
        return 0
    if ns.command == "chat-budget":
        return _cmd_chat_budget(path, ns.n)
    if ns.command == "owner-grant":
        if ns.pick:
            return _cmd_owner_grant_pick(path, ns.hours)
        return _cmd_owner_grant(path, ns.worker, ns.hours, ns.revoke, ns.list)
    try:
        if ns.command == "watch":
            return _cmd_watch(path, ns.stream, ns.since, ns.project)
        if ns.command in (None, "report", "metrics", "active", "result", "decisions", "flow", "why"):
            with _open_existing(path) as conn:
                if ns.command == "why":
                    for line in stuck_lines(conn, ns.project, limit=40):
                        print(line)
                elif ns.command == "flow":
                    raw = ns.since or ""
                    since = (_since_utc(None, float(raw)) if raw.replace(".", "", 1).isdigit()
                             else _since_utc(raw or None, 24.0))
                    data = flow_metrics.flow(conn, iter_entries(AUDIT_LOG, since), since)
                    for line in flow_metrics.format_flow(data):
                        print(line)
                elif ns.command == "result":
                    print(task_result(conn, ns.task))
                elif ns.command == "decisions" and ns.json:
                    items, clusters = _decisions(conn, ns.project)
                    print(json.dumps({"items": items, "clusters": clusters}, ensure_ascii=False, default=str))
                elif ns.command == "decisions":
                    items, clusters = _decisions(conn, ns.project)
                    print(queue_summary(conn, ns.project))
                    for line in world_lines(conn, ns.project):
                        print(line)
                    for line in proposal_lines(conn, ns.project):
                        print(line)
                    for line in format_stalls(clusters, 5):
                        print(line)
                    print(f"ждут владельца: {len(items)}" + ("" if items else " — решений не требуется"))
                    _asks, _done, handling = _judgment_state(conn, ns.project)
                    for it in items:
                        if (it["project"], it["tz"]) in handling:
                            it["why"] += f" · разбирает система ({handling[(it['project'], it['tz'])]})"
                    for line in format_decisions(items, ns.limit or None):
                        print(line)
                elif ns.command == "active":
                    print(active_claims(conn, ns.project))
                elif ns.command == "report":
                    print(platform_line())
                    print()
                    print(claimed_vs_done(conn, ns.project))
                    from dev_mcp.fleet.delivery_gap import summary_lines
                    try:  # воронка доставки — дополнение; пустой/старый реестр не роняет отчёт
                        for line in summary_lines(conn, ns.project):
                            print(line)
                    except sqlite3.Error as exc:
                        print(f"воронка доставки недоступна: {exc}")
                elif ns.command == "metrics":
                    _print_metrics(metrics(conn, ns.since))
                else:
                    print(platform_line())
                    # 20.11.2026: сразу под строкой транспорта — однозначный итог и смысл флагов:
                    # агенты с ролью владельца читали autostart=off/extension=stale как «флот не работает».
                    from dev_mcp.fleet.status_brief import safe_brief_lines
                    for line in safe_brief_lines(conn, Path(path), REPO / "logs"):
                        print(line)
                    print()
                    items, hidden = split_alarms(exception_items(conn), ns.show_all)
                    from dev_mcp.fleet import quick
                    book = quick.Book(path.parent)
                    actions = quick.first_actions(REPO / "logs", book.jobs().values())
                    print(quick.recent_summary(book, actions))
                    items.extend(quick.pause_alarms(book, actions=actions))
                    for item in items:
                        print("! " + item)
                    if not items:
                        print("исключений нет" + (" свежих" if hidden else ""))
                    if hidden:
                        print(f"скрыто {hidden} старых тревог (старше {int(STALE_ALARM_H)} ч; --all)")
            return 0

        if ns.command == "retire":
            # 08.11 (решение владельца): рудименты выведенных механизмов снимает платформа сама.
            from dev_mcp.fleet import rudiments
            result = rudiments.retire(Store.open(str(path)), names=tuple(ns.classes or ()) or None,
                                      dry=bool(ns.dry))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0

        if ns.command == "bootstrap":
            started = time.perf_counter()
            run_id = uuid.uuid4().hex[:12]
            events: list[dict[str, Any]] = []
            journal = REPO / "logs" / "bootstrap-timing.jsonl"

            def record(stage: str, seconds: float, details: dict[str, Any]) -> None:
                entry = {
                    "at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                    "project": ns.project, "tz": list(ns.tz), "stage": stage,
                    "seconds": round(seconds, 3), **details,
                }
                events.append(entry)
                extra = (" " + json.dumps(details, ensure_ascii=False, sort_keys=True)
                         if details else "")
                print(f"bootstrap: {stage} {seconds:.2f} с{extra}", file=sys.stderr,
                      flush=True)
                try:
                    journal.parent.mkdir(parents=True, exist_ok=True)
                    with journal.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                except OSError as exc:
                    print(f"bootstrap: журнал замеров недоступен: {exc}", file=sys.stderr)

            record("lock", _bootstrap_slot(), {})
            result = sync_selected(
                Store.open(str(path)),
                ns.project,
                str(ns.root),
                ns.tz,
                reactivate=bool(ns.reactivate),
                policy=Policy.load(str(REPO / "policy.toml")),
                timing=record,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if not result.get("errors"):
                # bootstrap owns its Store transaction; record its successful
                # manual CLI invocation separately instead of wrapping a
                # foreign transaction inside the console's sqlite connection.
                event_started = time.perf_counter()
                _mutate(path, lambda _conn: "bootstrap", intervention_command="bootstrap")
                record("intervention", time.perf_counter() - event_started, {})
            record("total", time.perf_counter() - started,
                   {"errors": bool(result.get("errors"))})
            def stage_total(stage: str) -> float:
                return sum(entry["seconds"] for entry in events if entry["stage"] == stage)
            runs = [entry for entry in events if entry["stage"] == "acceptance"]
            slow = max(runs, key=lambda entry: entry["seconds"]) if runs else None
            slow_label = (f"{slow['command'][:100]}: {slow['seconds']:.2f} с, "
                          f"rc={slow['rc']}" if slow else "нет")
            print(
                f"bootstrap {','.join(ns.tz)}: всего {stage_total('total'):.2f} с — "
                f"замок {stage_total('lock'):.2f}, разбор {stage_total('parse'):.2f}, "
                f"снимок {stage_total('snapshot'):.2f}, "
                f"копия {stage_total('workspace'):.2f}, "
                f"коллекция {stage_total('collection'):.2f}, "
                f"приёмка {stage_total('acceptance'):.2f} "
                f"(самая долгая команда {slow_label}), "
                f"запись {stage_total('registry') + stage_total('intervention'):.2f}",
                file=sys.stderr,  # stdout — машиночитаемый JSON bootstrap; замер времени — в stderr
                flush=True,
            )
            return 2 if result.get("errors") else 0

        if ns.command == "revoke":
            code, message = _mutate(path, lambda c: _cmd_revoke(c, ns.worker, ns.reason))
        elif ns.command == "resume":
            code, message = _mutate(path, lambda c: _cmd_resume(c, ns.worker))
        elif ns.command == "recover":
            code, message = _mutate(
                path,
                lambda c: _cmd_recover(c, ns.project, ns.tz, ns.reason, ns.min_silence_min),
                intervention_command="recover",
            )
        elif ns.command == "overseer":
            code, message = _cmd_overseer(path, ns)
        elif ns.command == "quick":
            code, message = _cmd_quick(path, ns)
        elif ns.command == "say":
            code, message = _mutate(path, lambda c: _cmd_say(c, ns.body))
        elif ns.command == "tell":
            code, message = _mutate(path, lambda c: _cmd_tell(c, ns.to, ns.body),
                                    intervention_command="tell")
        elif ns.command == "instruct":
            code, message = _mutate(
                path, lambda c: _cmd_instruct(c, ns.project, ns.text, ns.done_when, ns.want,
                                              ns.priority, ns.source),
                intervention_command="instruct")
        elif ns.command == "focus":
            code, message = _mutate(
                path, lambda c: _cmd_focus(c, ns.project, ns.instruction, ns.tz),
                intervention_command="focus")
        elif ns.command == "unblock":
            code, message = _mutate(
                path, lambda c: _cmd_unblock(c, ns.project, ns.obstacle, ns.answer),
                intervention_command="instruct")
        elif ns.command == "priority":
            code, message = _mutate(path, lambda c: _cmd_priority(c, ns.project, ns.value),
                                    intervention_command="priority")
        elif ns.command == "freeze":
            code, message = _mutate(path, lambda c: _cmd_freeze(c, ns.project, ns.tz),
                                    intervention_command="freeze")
        elif ns.command == "reactivate":
            code, message = _mutate(path, lambda c: _cmd_reactivate(c, ns.project, ns.tz, ns.reason),
                                    intervention_command="reactivate")
        elif ns.command == "grant":
            code, message = _mutate(path, lambda c: _cmd_grant(c, ns.project, ns.text, ns.tzs),
                                    intervention_command="reactivate")
        elif ns.command == "set-acceptance":
            code, message = _mutate(
                path,
                lambda c: _cmd_set_acceptance(c, ns.project, ns.tz, ns.commands),
            )
        else:
            raise ValueError(f"неизвестная команда: {ns.command}")
        print(message)
        return code
    except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
        print(f"реестр недоступен или команда не применена: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    _code = 1
    try:
        _code = main()
    finally:
        if os.environ.get(RC_FILE_ENV):
            try:
                Path(os.environ[RC_FILE_ENV]).write_text(str(_code), encoding="utf-8")
            except OSError:
                pass
    raise SystemExit(_code)


