"""TZ-46: исход флота через цепочку маршрутов — на локальных фиктивных апстримах."""
from __future__ import annotations

import base64
import json
import socket
import socketserver
import struct
import threading
from functools import partial

import pytest

from dev_mcp.fleet import egress

SECRET_USER, SECRET_PASS = "fleetuser", "s3cr3t-pass"


class _Srv(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start(handler, **attrs):
    srv = _Srv(("127.0.0.1", 0), handler)
    for k, v in attrs.items():
        setattr(srv, k, v)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class _Trace(socketserver.BaseRequestHandler):
    def handle(self):
        egress._recv_until(self.request, b"\r\n\r\n")
        body = f"fl=1\nloc={self.server.loc}\n".encode()
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)


class _HttpUpstream(socketserver.BaseRequestHandler):
    def handle(self):
        head = egress._recv_until(self.request, b"\r\n\r\n").decode("latin-1")
        want = "Basic " + base64.b64encode(f"{SECRET_USER}:{SECRET_PASS}".encode()).decode()
        if f"Proxy-Authorization: {want}" not in head:
            self.request.sendall(b"HTTP/1.1 407 Auth\r\n\r\n")
            return
        host, port = head.split(" ")[1].rsplit(":", 1)
        self.server.hits.append(host)
        up = socket.create_connection((host, int(port)))
        self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        egress._pipe(self.request, up)
        up.close()


class _Socks(socketserver.BaseRequestHandler):
    def handle(self):
        s = self.request
        _, n = egress._recv_exact(s, 2)
        egress._recv_exact(s, n)
        s.sendall(b"\x05\x02")
        egress._recv_exact(s, 1)
        u = egress._recv_exact(s, egress._recv_exact(s, 1)[0]).decode()
        p = egress._recv_exact(s, egress._recv_exact(s, 1)[0]).decode()
        if (u, p) != (SECRET_USER, SECRET_PASS):
            s.sendall(b"\x01\x01")
            return
        s.sendall(b"\x01\x00")
        egress._recv_exact(s, 4)
        host = egress._recv_exact(s, egress._recv_exact(s, 1)[0]).decode()
        port = struct.unpack(">H", egress._recv_exact(s, 2))[0]
        self.server.hits.append(host)
        up = socket.create_connection((host, port))
        s.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        egress._pipe(s, up)
        up.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def world():
    trace = _start(_Trace, loc="NL")
    http_up = _start(_HttpUpstream, hits=[])
    socks_up = _start(_Socks, hits=[])
    yield trace, http_up, socks_up
    for srv in (trace, http_up, socks_up):
        srv.shutdown()
        srv.server_close()


def _routes_text(*lines):
    return "\n".join(lines)


def _prober(trace_port):
    return partial(egress.probe, host="127.0.0.1", port=trace_port, use_tls=False, timeout=3)


def _get_via_proxy(proxy_port, target_port):
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    s.sendall(f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n\r\n".encode())
    status = egress._recv_until(s, b"\r\n\r\n").split(b"\r\n", 1)[0]
    if b"200" not in status:
        s.close()
        return status
    s.sendall(b"GET /cdn-cgi/trace HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    data = b""
    while chunk := s.recv(4096):
        data += chunk
    s.close()
    return data


def test_first_dead_goes_second_then_returns_when_first_revives(world):
    trace, http_up, socks_up = world
    dead = _free_port()
    routes = egress.parse_routes(_routes_text(
        f"http://{SECRET_USER}:{SECRET_PASS}@127.0.0.1:{dead}  # первый — мёртв",
        f"socks5://{SECRET_USER}:{SECRET_PASS}@127.0.0.1:{socks_up.server_address[1]}"))
    switches = []
    sel = egress.Selector(routes, prober=_prober(trace.server_address[1]),
                          on_switch=lambda a, b, r: switches.append((a, b, r)))
    assert sel.refresh() is routes[1]
    proxy = egress.EgressProxy(sel, port=0)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        out = _get_via_proxy(proxy.server_address[1], trace.server_address[1])
        assert b"loc=NL" in out and socks_up.hits
        # первый ожил: на его порту поднимаем настоящий HTTP-апстрим
        revived = _Srv(("127.0.0.1", dead), _HttpUpstream)
        revived.hits = []
        threading.Thread(target=revived.serve_forever, daemon=True).start()
        try:
            assert sel.refresh() is routes[0]
            _get_via_proxy(proxy.server_address[1], trace.server_address[1])
            assert revived.hits
        finally:
            revived.shutdown()
            revived.server_close()
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert [(a, b) for a, b, _ in switches] == [
        (None, routes[1].label), (routes[1].label, routes[0].label)]


def test_all_dead_refuses_clearly_and_records_switch(world):
    trace, *_ = world
    routes = egress.parse_routes(_routes_text(f"http://127.0.0.1:{_free_port()}", f"socks5://127.0.0.1:{_free_port()}"))
    switches = []
    sel = egress.Selector(routes, prober=_prober(trace.server_address[1]),
                          on_switch=lambda a, b, r: switches.append((a, b, r)))
    sel.current = routes[0]
    assert sel.refresh() is None
    assert switches == [(routes[0].label, None, "все маршруты мертвы")]
    proxy = egress.EgressProxy(sel, port=0)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        assert b"503" in _get_via_proxy(proxy.server_address[1], trace.server_address[1])
    finally:
        proxy.shutdown()
        proxy.server_close()


def test_blocked_country_is_dead(world):
    trace, *_ = world
    trace.loc = "RU"
    sel = egress.Selector([egress.Route("direct")], prober=_prober(trace.server_address[1]))
    assert sel.refresh() is None and sel.last_loc["direct"] == "RU"


def test_credentials_never_leak(world, tmp_path):
    trace, http_up, _ = world
    routes = egress.parse_routes(f"http://{SECRET_USER}:{SECRET_PASS}@127.0.0.1:{http_up.server_address[1]}")
    events = []
    sel = egress.Selector(routes, prober=_prober(trace.server_address[1]),
                          on_switch=lambda *a: events.append(a))
    sel.refresh()
    state = tmp_path / "state.json"
    egress.write_state(state, sel, 8899)
    blob = " ".join([state.read_text(encoding="utf-8"), repr(routes), repr(events),
                     json.dumps(egress.iter_labels(routes)), str(routes[0].label)])
    assert SECRET_USER not in blob and SECRET_PASS not in blob
    assert json.loads(state.read_text(encoding="utf-8"))["route"] == f"http://127.0.0.1:{http_up.server_address[1]}"


def test_registry_event_written(tmp_path, world):
    from dev_mcp.fleet.store import Store
    db = tmp_path / "fleet.sqlite"
    Store.open(str(db))
    egress.registry_switch_writer(db)("direct", "socks5://h:1", "loc=NL")
    with Store.open(str(db)).read() as tx:
        row = tx._conn.execute("select kind,payload from events where kind='egress_switch'").fetchone()
    assert row and json.loads(row[1]) == {"from": "direct", "to": "socks5://h:1", "reason": "loc=NL"}


def test_listens_only_on_loopback():
    sel = egress.Selector([egress.Route("direct")], prober=lambda r: "NL")
    with pytest.raises(egress.RouteError):
        egress.EgressProxy(sel, port=0, host="0.0.0.0")
    proxy = egress.EgressProxy(sel, port=0)
    try:
        assert proxy.server_address[0] == "127.0.0.1"
    finally:
        proxy.server_close()


def test_parse_routes_and_missing_file(tmp_path):
    assert egress.load_routes(tmp_path / "nope.txt") is None
    assert "fleet-egress" in egress.missing_routes_action(tmp_path / "fleet-egress.txt")
    assert [r.label for r in egress.parse_routes("# c\ndirect\n\nsocks5://u:p@h:1080\n")] == ["direct", "socks5://h:1080"]
    with pytest.raises(egress.RouteError):
        egress.parse_routes("ftp://h:1")
    with pytest.raises(egress.RouteError):
        egress.Selector([])


def test_configured_proxy_does_not_bypass_all_dead_routes():
    assert "DIRECT" not in egress.pac_for(8899) and "127.0.0.1:8899" in egress.pac_for(8899)
    assert egress.pac_data_url(8899).startswith("data:application/x-ns-proxy-autoconfig;base64,")
