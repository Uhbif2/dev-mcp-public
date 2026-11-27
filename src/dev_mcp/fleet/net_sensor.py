"""Independent server egress and FleetChrome→ChatGPT route observations.

``tick(store)`` is a nonblocking once-per-minute maintenance hook. Its trace
probes the SERVER route, not the Chrome UI route. Browser sends/answers and
page-failure reports are separate evidence for the fleet resource and owner
alert. dev/core MCP availability and the Oracle gateway are separate monitors.
No Windows proxy settings are changed by this module.
"""
from __future__ import annotations

import json
import http.client
import os
import sqlite3
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import world

RESOURCE = "chatgpt-reachable"
TRACE_URL = "https://chatgpt.com/cdn-cgi/trace"
EVERY_S = 60.0
EXTENSION_EVIDENCE_S = 120.0
BROWSER_ROUTE_WINDOW_S = 600.0  # A recent browser send or answer, not a server trace.
_lock = threading.Lock()
_last_started = 0.0
_running = False
_pending: "Result | None" = None
_services: set[str] = set()


@dataclass(frozen=True)
class Route:
    enabled: bool
    proxy: str = ""
    host: str = ""
    port: int = 0


@dataclass(frozen=True)
class Result:
    state: str
    cause: str
    proxy_enabled: bool
    proxy_listening: bool
    exit_ip: str = ""
    loc: str = ""
    server_egress_state: str = ""  # Original trace result when a browser failure overrides state.

    @property
    def detail(self) -> str:
        return f"{self.cause}; ip={self.exit_ip}; loc={self.loc}; proxy={int(self.proxy_enabled)}; listening={int(self.proxy_listening)}"

    def payload(self) -> dict[str, Any]:
        return {"cause": self.cause, "exit_ip": self.exit_ip, "loc": self.loc,
                "proxy_enabled": self.proxy_enabled, "proxy_listening": self.proxy_listening}


@dataclass(frozen=True)
class BrowserRoute:
    state: str  # working | failed | unknown; independent of server egress
    sends: int = 0
    answers: int = 0
    failed_attempts: int = 0


def _browser_route(store: Any, *, now: datetime | None = None) -> BrowserRoute:
    """Observe the *Chrome* route using extension receipts, never the server's proxy.

    Quick-agent messages are acknowledged by FleetChrome after an actual send
    or finished answer. For regular agents, use a fresh *tab generating* probe
    from worker_activity; MCP activity or extension heartbeat alone is not
    evidence that Chrome received a ChatGPT response.
    Three matching Cloudflare page failures are failed page attempts. No
    observations, rate limits and transient failures remain UNKNOWN.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - BROWSER_ROUTE_WINDOW_S
    sends = answers = 0
    last_success = float("-inf")
    db_path = Path(store.path) if hasattr(store, "path") else Path(store)
    path = db_path.parent / "quick" / "jobs.json"
    try:
        jobs = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(jobs, dict):
            jobs = {}
    except (OSError, ValueError, TypeError):
        jobs = {}
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for message in job.get("messages", []):
            if not isinstance(message, dict):
                continue
            try:
                stamp = datetime.fromisoformat(str(message.get("ts", "")).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    continue
                age = stamp.timestamp()
            except (ValueError, TypeError):
                continue
            if not cutoff <= age <= now.timestamp():
                continue
            if message.get("role") == "owner":
                sends += 1
                last_success = max(last_success, age)
            elif message.get("role") == "agent" and job.get("answer_source") != "mcp":
                answers += 1
                last_success = max(last_success, age)
    # The activity table is optional and created only after actual Chrome
    # observation; stale/idle states do not prove successful browser transport.
    activity_sql = ("SELECT seen_at FROM worker_activity WHERE state='generating' "
                    "ORDER BY seen_at DESC LIMIT 1")
    if hasattr(store, "read"):
        with store.read() as tx:
            row = tx.one("SELECT ts,payload FROM events WHERE kind='chatgpt_extension_signal' ORDER BY id DESC LIMIT 1")
            activity = (tx.one(activity_sql) if tx.one(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_activity'") else None)
    else:
        # CLI status must be strictly read-only; Store.open() runs migrations.
        row = None
        activity = None
        try:
            connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
            try:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT ts,payload FROM events WHERE kind='chatgpt_extension_signal' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_activity'"
                ).fetchone():
                    activity = connection.execute(activity_sql).fetchone()
            finally:
                connection.close()
        except (sqlite3.Error, OSError, ValueError):
            pass
    if activity:
        try:
            stamp = datetime.fromisoformat(str(activity["seen_at"]).replace("Z", "+00:00"))
            if stamp.tzinfo is not None and cutoff <= stamp.timestamp() <= now.timestamp():
                last_success = max(last_success, stamp.timestamp())
        except (ValueError, KeyError, TypeError):
            pass
    if row:
        try:
            stamp = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
            failure_cutoff = max(cutoff, now.timestamp() - EXTENSION_EVIDENCE_S)
            if stamp.tzinfo is not None and failure_cutoff <= stamp.timestamp() <= now.timestamp():
                evidence = json.loads(row["payload"] or "{}")
                if (evidence.get("matching") is True and int(evidence.get("count", 0)) >= 3
                        and stamp.timestamp() >= last_success):
                    return BrowserRoute("failed", failed_attempts=int(evidence["count"]))
        except (TypeError, ValueError):
            pass
    if last_success > float("-inf"):
        return BrowserRoute("working", sends, answers)
    return BrowserRoute("unknown")


def fleet_route() -> Route:
    """Match Fleet Chrome's configured egress, falling back to its normal route."""
    data = Path(__file__).resolve().parents[3] / 'data'
    if not (data/'secrets/fleet-egress.txt').is_file():
        return windows_route()
    port = 8899
    try:
        state = json.loads((data/'fleet/egress-state.json').read_text(encoding='utf-8'))
        value = state.get('port')
        if type(value) is int and 1 <= value <= 65535: port = value
    except (OSError,ValueError,TypeError):
        pass
    return Route(True,f'http://127.0.0.1:{port}','127.0.0.1',port)


def windows_route() -> Route:
    """Read Chrome's WinINET manual proxy, not HTTP_PROXY environment variables."""
    try:
        import winreg  # Windows only
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled = bool(int(winreg.QueryValueEx(key, "ProxyEnable")[0]))
            try:
                raw = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
            except OSError:
                raw = ""
    except (ImportError, OSError, ValueError):
        return Route(False)
    if not enabled:
        return Route(False)

    # WinINET: a single host:port, or ``http=...;https=...;socks=...``.
    entries = dict(part.split("=", 1) for part in raw.split(";") if "=" in part)
    selected = entries.get("https") or entries.get("http") or (raw if not entries else "")
    if not selected and entries.get("socks"):
        # urllib cannot speak SOCKS; fail closed, NEVER silently bypass Chrome's proxy.
        selected = "socks5://" + entries["socks"].removeprefix("socks5://")
    if not selected:
        return Route(True)
    if "://" not in selected:
        selected = "http://" + selected
    parsed = urlsplit(selected)
    try:
        host, port = parsed.hostname or "", parsed.port or 0
    except ValueError:
        host, port = "", 0
    return Route(True, selected, host, port)


def _request(route: Route, timeout: float) -> tuple[int, str]:
    if route.proxy.startswith("socks5:"):
        # urllib.ProxyHandler does not support SOCKS; a direct fallback would
        # probe another route than Chrome. Use DNS-over-proxy SOCKS5 instead.
        with _socks5_connect(route, timeout) as channel:
            with ssl.create_default_context().wrap_socket(channel, server_hostname="chatgpt.com") as secured:
                secured.sendall(b"GET /cdn-cgi/trace HTTP/1.1\r\nHost: chatgpt.com\r\n"
                                b"Accept-Encoding: identity\r\nConnection: close\r\n\r\n")
                response = http.client.HTTPResponse(secured)
                response.begin()
                return response.status, response.read(8192).decode("utf-8", "replace")
    if route.proxy.startswith("socks4:"):
        raise ValueError("unsupported SOCKS4 proxy: do not bypass system proxy")
    # This explicit ProxyHandler prevents leaking to unrelated HTTP(S)_PROXY env vars.
    proxies = {"https": route.proxy} if route.enabled and route.proxy else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    try:
        with opener.open(TRACE_URL, timeout=timeout) as response:
            return int(response.status), response.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        with error:
            return int(error.code), error.read(8192).decode("utf-8", "replace")


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    data = b""
    while len(data) < length:
        part = sock.recv(length - len(data))
        if not part:
            raise OSError("SOCKS proxy disconnected")
        data += part
    return data


def _socks5_connect(route: Route, timeout: float) -> socket.socket:
    channel = socket.create_connection((route.host, route.port), timeout=timeout)
    try:
        channel.settimeout(timeout)
        channel.sendall(b"\x05\x01\x00")  # SOCKS5, no auth
        if _recv_exact(channel, 2) != b"\x05\x00":
            raise OSError("SOCKS5 negotiation failed")
        domain = b"chatgpt.com"
        channel.sendall(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + (443).to_bytes(2, "big"))
        head = _recv_exact(channel, 4)
        if head[:2] != b"\x05\x00":
            raise OSError("SOCKS5 connect failed")
        atyp = head[3]
        addr_size = 4 if atyp == 1 else 16 if atyp == 4 else _recv_exact(channel, 1)[0] if atyp == 3 else 0
        if not addr_size:
            raise OSError("invalid SOCKS5 address type")
        _recv_exact(channel, addr_size + 2)
        return channel
    except BaseException:
        channel.close()
        raise


def probe(*, route: Route | None = None, fetch: Callable[[Route, float], tuple[int, str]] = _request,
          connect: Callable[..., Any] = socket.create_connection, timeout: float = 6.0) -> Result:
    route = route if route is not None else fleet_route()
    listening = False
    if route.enabled:
        if not route.host or not route.port:
            return Result(world.DOWN, "proxy_not_configured", True, False)
        try:
            sock = connect((route.host, route.port), timeout=min(timeout, 1.0))
            sock.close()
            listening = True
        except OSError:
            return Result(world.DOWN, "proxy_not_listening", True, False)
    try:
        status, body = fetch(route, timeout)
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return Result(world.DOWN, "network_error" if route.enabled else "proxy_disabled", route.enabled, listening)

    parsed = dict(line.split("=", 1) for line in body.splitlines()
                  if "=" in line and not line.startswith("<"))
    ip, loc = parsed.get("ip", ""), parsed.get("loc", "")
    if "unable to load site" in body.casefold():
        cause = "cloudflare_block"
    elif not 200 <= status < 300:
        cause = f"http_{status}"
    elif not ip or not loc:
        cause = "unexpected_trace"
    else:
        return Result(world.UP, "ok", route.enabled, listening, ip, loc)
    return Result(world.DOWN, cause if route.enabled else "proxy_disabled", route.enabled, listening, ip, loc)


def _alert_path(store: Any) -> Path:
    parent = Path(store.path).parent
    return (parent.parent if parent.name.lower() == "fleet" else parent) / "owner-alerts.jsonl"


def _message(result: Result, browser: BrowserRoute | None = None) -> str:
    """VPN is an owner action only for two independently observed route failures."""
    browser = browser or BrowserRoute("unknown")
    egress = result.server_egress_state or (
        "unknown" if result.cause == "extension_cloudflare" else result.state)
    if egress == world.UP:
        return ("Egress сервера достигает ChatGPT; маршрут Chrome→ChatGPT не работает — "
                "проверь вкладки и ошибки браузера." if browser.state == "failed"
                else "Egress сервера достигает ChatGPT; маршрут Chrome→ChatGPT: " + browser.state + ".")
    if egress != world.DOWN:
        return "Egress сервера: unknown; маршрут Chrome→ChatGPT: " + browser.state + " — проверь браузер, VPN пока не требуется."
    if browser.state == "working":
        return "Egress сервера не достаёт ChatGPT; Chrome→ChatGPT работает — действий не нужно."
    if browser.state != "failed" or browser.failed_attempts < 3:
        return "Egress сервера не достаёт ChatGPT; маршрут Chrome→ChatGPT неизвестен — не требуется включать VPN без проверки браузера."
    return ("Включи VPN: egress сервера не достигает ChatGPT и Chrome→ChatGPT "
            "не смог открыть страницу после повторных попыток.")


def _last_alert(tx: Any) -> tuple[str, str]:
    row = tx.one("SELECT kind,payload FROM events WHERE kind IN ('chatgpt_unreachable','chatgpt_reachable') "
                 "ORDER BY id DESC LIMIT 1")
    if row is None:
        return "", ""
    try:
        payload = json.loads(row["payload"] or "{}")
        cause = "|".join(str(payload.get(key) or "") for key in
                         ("cause", "server_egress", "browser_route"))
    except ValueError:
        cause = ""
    return str(row["kind"]), cause


def _extension_blocked(store: Any, *, now: datetime | None = None) -> bool:
    with store.read() as tx:
        row = tx.one("SELECT ts,payload FROM events WHERE kind='chatgpt_extension_signal' ORDER BY id DESC LIMIT 1")
    if row is None:
        return False
    try:
        payload = json.loads(row["payload"] or "{}")
        ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - ts).total_seconds()
        return bool(payload.get("count", 0) >= 3 and 0 <= age <= EXTENSION_EVIDENCE_S)
    except (TypeError, ValueError):
        return False


def publish(store: Any, result: Result, *, alerts_path: Path | None = None) -> bool:
    """Persist browser resource separately from egress and deduplicate owner advice."""
    if result.state not in (world.UP, world.DOWN):
        return False
    browser = _browser_route(store)
    egress = result.server_egress_state or (
        "unknown" if result.cause == "extension_cloudflare" else result.state)
    evidence = {**result.payload(), "server_egress": egress, "browser_route": browser.state}
    # Resource DOWN is solely a verified Chrome outage: stop launching tabs
    # even when server egress is UP or UNKNOWN. Owner VPN advice has a separate
    # two-failure condition in _message().
    effective_state = {
        "working": world.UP, "failed": world.DOWN, "unknown": world.UNKNOWN,
    }[browser.state]
    alerts_path = alerts_path or _alert_path(store)
    # A browser outage remains an active alert even if the *server* trace is UP.
    # An egress-only outage is likewise visible but must not block Chrome work.
    active = egress == world.DOWN or browser.state == "failed"
    kind = "chatgpt_unreachable" if active else "chatgpt_reachable"
    changed = False
    with store.tx() as tx:
        old, _ = world.last_state(tx, RESOURCE)
        previous_kind, previous_cause = _last_alert(tx)
        if old != effective_state:
            tx.record_event(kind=world.EVENT_KIND, path_norm=world.path_key(RESOURCE),
                            payload={"state": effective_state, "was": old, "detail": result.detail[:300],
                                     "server_egress": egress, "browser_route": browser.state})
        # Healthy startup (including subsequent ticks) is not a recovery.
        # Recover only after an actual outage; a new failure cause is a new action.
        key = "|".join(str(evidence.get(field) or "") for field in ("cause", "server_egress", "browser_route"))
        notify = ((previous_kind != kind or previous_cause != key)
                  if active else previous_kind == "chatgpt_unreachable")
        if notify:
            tx.record_event(kind=kind, path_norm=world.path_key(RESOURCE), payload=evidence)
            changed = True
    if changed:
        alert = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "kind": kind, "state": "active" if active else "resolved",
                 **result.payload(), "server_egress": egress,
                 "browser_route": browser.state, "browser_sends": browser.sends,
                 "browser_answers": browser.answers,
                 "message": _message(result, browser) if active else "ChatGPT: тревога снята."}
        alerts_path.parent.mkdir(parents=True, exist_ok=True)
        with alerts_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(alert, ensure_ascii=False) + "\n")
    return changed


def report_extension(store: Any, detail: str, page_text: str, *, alerts_path: Path | None = None) -> int:
    """Extension reports composer failure PLUS actual Cloudflare page text.

    Three consecutive matching reports cause DOWN even if /cdn-cgi/trace is UP.
    A successful composer or a different page resets the streak. Evidence expires.
    """
    matching = "не найдено поле ввода" in detail.casefold() and "unable to load site" in page_text.casefold()
    with store.tx() as tx:
        row = tx.one("SELECT ts,payload FROM events WHERE kind='chatgpt_extension_signal' ORDER BY id DESC LIMIT 1")
        try:
            stamp = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00")) if row else None
            if stamp is not None and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            recent = stamp is not None and 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= EXTENSION_EVIDENCE_S
            old_count = int(json.loads(row["payload"] or "{}").get("count", 0)) if recent else 0
        except (ValueError, TypeError):
            old_count = 0
        count = min(old_count + 1, 3) if matching else 0
        tx.record_event(kind="chatgpt_extension_signal", payload={"count": count, "matching": matching})
    if count >= 3:
        route = windows_route()
        publish(store, Result(world.DOWN, "extension_cloudflare", route.enabled, False), alerts_path=alerts_path)
    return count


def _apply(store: Any, result: Result, alerts_path: Path | None) -> bool:
    if _extension_blocked(store) and _browser_route(store).state != "working":
        result = Result(world.DOWN, "extension_cloudflare", result.proxy_enabled,
                        result.proxy_listening, result.exit_ip, result.loc,
                        server_egress_state=result.state)
    now = datetime.now(timezone.utc)
    with store.read() as tx:
        previous = tx.one("SELECT payload FROM events WHERE kind='chatgpt_probe' ORDER BY id DESC LIMIT 1")
    try:
        prior = json.loads(previous['payload']) if previous else {}
        good = datetime.fromisoformat(prior['last_good_at']) if prior.get('last_good_at') else None
    except (TypeError, ValueError, KeyError):
        prior, good = {}, None
    failures = int(prior.get('failures', 0)) + 1 if result.state == world.DOWN else 0
    if result.state == world.UP:
        good = now
    # A single trace timeout is not a fleet outage. Retain only a RECENT
    # verified UP, for at most 120s, while awaiting the second independent
    # probe. Missing proxy / three browser Cloudflare failures remain immediate.
    pending = (result.state == world.DOWN and result.cause in {'network_error','proxy_disabled','unexpected_trace'}
               and prior.get('observed_state') == world.UP
               and failures == 1 and good is not None and good.tzinfo is not None
               and 0 <= (now-good).total_seconds() <= 120)
    with store.tx() as tx:
        tx.record_event(kind='chatgpt_probe', payload={
            'observed_state': result.state, 'cause': result.cause,
            'last_good_at': good.isoformat() if good else None,
            'failures': failures, 'awaiting_confirmation': pending})
    if pending:
        return False
    return publish(store, result, alerts_path=alerts_path)


def tick(store: Any, *, every_s: float = EVERY_S, sync: bool = False,
         alerts_path: Path | None = None, sensor: Callable[[], Result] = probe) -> bool:
    """Service loop: async in production; ``sync=True`` runs deterministically in tests."""
    global _last_started, _running, _pending
    if os.environ.get("DEV_MCP_FLEET_SENSORS", "1") == "0":
        return False
    if sync:
        return _apply(store, sensor(), alerts_path)
    with _lock:
        if str(store.path) in _services:
            return False  # independently supervised publisher owns this source
        ready = _pending
        _pending = None
        start = not _running and (_last_started == 0 or time.monotonic() - _last_started >= every_s)
        if start:
            _running = True
            _last_started = time.monotonic()
    if start:
        def work() -> None:
            global _running, _pending
            try:
                result = sensor()
                with _lock:
                    _pending = result
            finally:
                with _lock:
                    _running = False
        threading.Thread(target=work, name="fleet-chatgpt-net", daemon=True).start()
    return _apply(store, ready, alerts_path) if ready is not None else False


def run_worker(store: Any, stop_event: Any, *, sensor: Callable[[], Result] = probe,
               interval_s: float = EVERY_S, alerts_path: Path | None = None) -> None:
    """Publish observations immediately; unrelated maintenance cannot delay recovery.

    The production controller starts this only for its installed registry. Tests
    inject a synthetic sensor. Legacy tick remains available without the service.
    """
    import logging
    if os.environ.get('DEV_MCP_FLEET_SENSORS', '1') == '0':
        return
    key = str(store.path)
    with _lock:
        if key in _services:
            return
        _services.add(key)
    try:
        while not stop_event.is_set():
            try:
                _apply(store, sensor(), alerts_path)
            except Exception as exc:
                logging.getLogger(__name__).exception('independent network observation deferred')
                try:
                    with store.tx() as tx:
                        tx.record_event(kind='chatgpt_probe_error', payload={'error': type(exc).__name__})
                except Exception:
                    logging.getLogger(__name__).exception('network sensor heartbeat unavailable')
            stop_event.wait(interval_s)
    finally:
        with _lock:
            _services.discard(key)
