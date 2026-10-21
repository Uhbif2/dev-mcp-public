"""Свой исход флота в сеть (TZ-46): локальный прокси с цепочкой маршрутов.

Флотский Chrome ходит в ChatGPT через `127.0.0.1:<порт>` (PAC с запасным DIRECT). Прокси держит
упорядоченный список маршрутов из `data/secrets/fleet-egress.txt` и пускает трафик по первому
живому. Живой — тот, через который `https://chatgpt.com/cdn-cgi/trace` отвечает с `loc` не из
блокируемых стран. Первый ожил — возврат на него. Каждая смена — событие реестра `egress_switch`.

Формат файла маршрутов — по строке: `direct` | `http://[user:pass@]host:port` |
`socks5://[user:pass@]host:port`; `#` — комментарий. Логин и пароль читает только этот модуль;
наружу (события, состояние, логи, ответы) маршрут выходит только как `label` без учётных данных.
Только stdlib.
"""
from __future__ import annotations

import base64
import json
import select
import socket
import socketserver
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

BLOCKED_LOC = frozenset({"RU", "BY", "CN", "IR", "KP", "SY", "CU"})
PROBE_HOST = "chatgpt.com"
PROBE_PATH = "/cdn-cgi/trace"
DEFAULT_PORT = 8899


class RouteError(ValueError):
    pass


@dataclass(frozen=True)
class Route:
    scheme: str            # direct | http | socks5
    host: str = ""
    port: int = 0
    user: str = field(default="", repr=False)
    password: str = field(default="", repr=False)

    @property
    def label(self) -> str:
        return "direct" if self.scheme == "direct" else f"{self.scheme}://{self.host}:{self.port}"

    def __repr__(self) -> str:  # учётные данные не попадают даже в repr
        return f"Route({self.label})"


def parse_routes(text: str) -> list[Route]:
    routes: list[Route] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.lower() == "direct":
            routes.append(Route("direct"))
            continue
        parts = urlsplit(line)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "socks5") or not parts.hostname or not parts.port:
            raise RouteError(f"строка {n}: ожидается direct | http://host:port | socks5://host:port")
        routes.append(Route(scheme, parts.hostname, parts.port,
                            unquote(parts.username or ""), unquote(parts.password or "")))
    return routes


def load_routes(path: Path) -> list[Route] | None:
    """None — файла нет (флот работает как раньше, владельцу одно действие)."""
    if not path.is_file():
        return None
    return parse_routes(path.read_text(encoding="utf-8"))


# ── открытие туннеля через маршрут ──────────────────────────────────────────────────────────────

def _recv_until(sock: socket.socket, marker: bytes, limit: int = 65536) -> bytes:
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("upstream closed")
        data += chunk
        if len(data) > limit:
            raise OSError("upstream header too large")
    return data


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise OSError("upstream closed")
        data += chunk
    return data


def open_tunnel(route: Route, host: str, port: int, timeout: float = 10.0) -> socket.socket:
    """TCP-сокет до host:port через маршрут. Ошибки — OSError без учётных данных в тексте."""
    if route.scheme == "direct":
        return socket.create_connection((host, port), timeout=timeout)
    sock = socket.create_connection((route.host, route.port), timeout=timeout)
    try:
        if route.scheme == "http":
            head = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if route.user:
                token = base64.b64encode(f"{route.user}:{route.password}".encode()).decode()
                head += f"Proxy-Authorization: Basic {token}\r\n"
            sock.sendall((head + "\r\n").encode())
            status = _recv_until(sock, b"\r\n\r\n").split(b"\r\n", 1)[0]
            if b" 200" not in status:
                raise OSError(f"{route.label}: CONNECT отклонён ({status[:40].decode('latin-1')})")
            return sock
        # socks5 (RFC 1928/1929)
        methods = b"\x00\x02" if route.user else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, method = _recv_exact(sock, 2)
        if ver != 5 or method == 0xFF:
            raise OSError(f"{route.label}: SOCKS5 метод не принят")
        if method == 2:
            u, p = route.user.encode(), route.password.encode()
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if _recv_exact(sock, 2)[1] != 0:
                raise OSError(f"{route.label}: SOCKS5 авторизация не прошла")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
        rep = _recv_exact(sock, 4)
        if rep[1] != 0:
            raise OSError(f"{route.label}: SOCKS5 CONNECT код {rep[1]}")
        atyp = rep[3]
        skip = {1: 4, 4: 16}.get(atyp)
        if skip is None:
            skip = _recv_exact(sock, 1)[0]
        _recv_exact(sock, skip + 2)
        return sock
    except BaseException:
        sock.close()
        raise


# ── проба маршрута ──────────────────────────────────────────────────────────────────────────────

def probe(route: Route, *, host: str = PROBE_HOST, port: int = 443, use_tls: bool = True,
          timeout: float = 10.0, ssl_context: ssl.SSLContext | None = None) -> str | None:
    """`loc` из trace через маршрут или None, если маршрут не довёз ответ."""
    try:
        raw = open_tunnel(route, host, port, timeout)
        try:
            raw.settimeout(timeout)
            conn = (ssl_context or ssl.create_default_context()).wrap_socket(raw, server_hostname=host) \
                if use_tls else raw
            conn.sendall(f"GET {PROBE_PATH} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            body = b""
            while len(body) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                body += chunk
        finally:
            raw.close()
    except (OSError, ssl.SSLError, ValueError):
        return None
    for line in body.decode("latin-1").splitlines():
        if line.startswith("loc="):
            return line[4:].strip().upper() or None
    return None


def route_alive(loc: str | None) -> bool:
    return bool(loc) and loc not in BLOCKED_LOC


# ── выбор маршрута ──────────────────────────────────────────────────────────────────────────────

class Selector:
    """Текущий маршрут: первый живой по порядку. Смена → on_switch(from, to, reason)."""

    def __init__(self, routes: list[Route], prober: Callable[[Route], str | None] = probe,
                 on_switch: Callable[[str | None, str | None, str], None] | None = None) -> None:
        if not routes:
            raise RouteError("пустой список маршрутов")
        self.routes = routes
        self.prober = prober
        self.on_switch = on_switch or (lambda *_: None)
        self.current: Route | None = None
        self.last_loc: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def refresh(self) -> Route | None:
        chosen: Route | None = None
        reason = "все маршруты мертвы"
        for route in self.routes:
            loc = self.prober(route)
            self.last_loc[route.label] = loc
            if route_alive(loc):
                chosen = route
                reason = f"loc={loc}"
                break
        with self._lock:
            before = self.current
            self.current = chosen
        if (before and before.label) != (chosen and chosen.label):
            self.on_switch(before.label if before else None, chosen.label if chosen else None, reason)
        return chosen

    def route(self) -> Route | None:
        with self._lock:
            return self.current


# ── прокси-сервер ───────────────────────────────────────────────────────────────────────────────

def _pipe(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    try:
        while True:
            ready, _, err = select.select(socks, [], socks, 120)
            if err or not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: C901 — линейный разбор одного запроса
        client: socket.socket = self.request
        client.settimeout(30)
        try:
            head = _recv_until(client, b"\r\n\r\n")
        except OSError:
            return
        line, _, rest = head.partition(b"\r\n")
        try:
            method, target, _version = line.decode("latin-1").split(" ", 2)
        except ValueError:
            return
        route = self.server.selector.route()  # type: ignore[attr-defined]
        if route is None:
            client.sendall(b"HTTP/1.1 503 No Live Egress\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        try:
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream = open_tunnel(route, host.strip("[]"), int(port or 443))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                    return
                upstream = open_tunnel(route, parts.hostname, parts.port or 80)
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                upstream.sendall(f"{method} {path} {_version}\r\n".encode("latin-1") + rest)
        except (OSError, ValueError):
            client.sendall(b"HTTP/1.1 502 Egress Failed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        try:
            upstream.settimeout(None)
            client.settimeout(None)
            _pipe(client, upstream)
        finally:
            upstream.close()


class EgressProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, selector: Selector, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RouteError("прокси слушает только loopback")
        self.selector = selector
        super().__init__((host, port), _Handler)


# ── реестр и состояние ──────────────────────────────────────────────────────────────────────────

def registry_switch_writer(db_path: Path) -> Callable[[str | None, str | None, str], None]:
    def write(src: str | None, dst: str | None, reason: str) -> None:
        try:
            from dev_mcp.fleet.store import Store
            with Store.open(str(db_path)).tx() as tx:
                tx.record_event(kind="egress_switch", project="Fleet Platform",
                                payload={"from": src, "to": dst, "reason": reason})
        except Exception:  # noqa: BLE001 — реестр недоступен: прокси продолжает работать
            pass
    return write


def write_state(path: Path, selector: Selector, port: int) -> None:
    cur = selector.route()
    state = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "port": port,
             "route": cur.label if cur else None,
             "routes": [{"label": r.label, "loc": selector.last_loc.get(r.label)} for r in selector.routes]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def pac_for(port: int) -> str:
    return f'function FindProxyForURL(u,h){{return "PROXY 127.0.0.1:{port}";}}'


def pac_data_url(port: int) -> str:
    return "data:application/x-ns-proxy-autoconfig;base64," + base64.b64encode(pac_for(port).encode()).decode()


def missing_routes_action(path: Path) -> str:
    return (f"Положи маршруты исхода флота в {path}: по строке, в порядке предпочтения — "
            "`direct`, `http://логин:пароль@хост:порт` или `socks5://логин:пароль@хост:порт`. "
            "Пока файла нет, флотский Chrome сохраняет прежний системный маршрут.")


def iter_labels(routes: Iterable[Route]) -> list[str]:
    return [r.label for r in routes]
