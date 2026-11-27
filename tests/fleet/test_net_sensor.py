"""TZ-45: WinINET routing, Cloudflare evidence, deduplicated owner action and world gate."""
from __future__ import annotations

import json
import socket
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import net_sensor, world
from dev_mcp.fleet.queue import take_next
from dev_mcp.fleet.store import Store


@pytest.fixture(autouse=True)
def enable_sensors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEV_MCP_FLEET_SENSORS", "1")


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store.open(str(tmp_path / "fleet.sqlite"))


class Connection:
    def close(self) -> None:
        return None


TRACE = "fl=7f\nip=203.0.113.7\nloc=NL\nhttp=http/2\n"


def test_proxy_from_windows_registry_and_disabled_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {"ProxyEnable": 1, "ProxyServer": "http=127.0.0.1:10809;https=127.0.0.1:10808"}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    fake = types.SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=lambda *_: Key(),
                                 QueryValueEx=lambda _, name: (values[name], 1))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    route = net_sensor.windows_route()
    assert route == net_sensor.Route(True, "http://127.0.0.1:10808", "127.0.0.1", 10808)
    values["ProxyEnable"] = 0
    assert net_sensor.windows_route() == net_sensor.Route(False)
    values["ProxyEnable"] = 1
    values["ProxyServer"] = "127.0.0.1:10808"
    assert net_sensor.windows_route().port == 10808


def test_proxy_is_used_but_unrelated_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated.invalid:4444")
    configured = []

    def opener(handler):
        configured.append(handler.proxies)
        return object()

    monkeypatch.setattr(net_sensor.urllib.request, "build_opener", opener)
    with pytest.raises(AttributeError):
        net_sensor._request(net_sensor.Route(False), 0.1)
    with pytest.raises(AttributeError):
        net_sensor._request(net_sensor.Route(True, "http://127.0.0.1:10808"), 0.1)
    assert configured == [{}, {"https": "http://127.0.0.1:10808"}]


def test_trace_proxy_and_network_results() -> None:
    route = net_sensor.Route(True, "http://127.0.0.1:10808", "127.0.0.1", 10808)
    calls = []

    def connect(address, timeout):
        calls.append((address, timeout))
        return Connection()

    good = net_sensor.probe(route=route, connect=connect, fetch=lambda *_: (200, TRACE))
    assert (good.state, good.exit_ip, good.loc, good.proxy_listening) == (world.UP, "203.0.113.7", "NL", True)
    assert calls[0][0] == ("127.0.0.1", 10808)
    bad = net_sensor.probe(route=route, connect=connect, fetch=lambda *_: (403, "Unable to load site. VPN"))
    assert (bad.state, bad.cause) == (world.DOWN, "cloudflare_block")
    unavailable = net_sensor.probe(route=route, connect=lambda *_1, **_2: (_ for _ in ()).throw(OSError()),
                                   fetch=lambda *_: pytest.fail("must not bypass stopped proxy"))
    assert unavailable.cause == "proxy_not_listening"


def test_direct_block_does_not_silently_use_env_proxy() -> None:
    result = net_sensor.probe(route=net_sensor.Route(False),
                              fetch=lambda *_: (403, "Unable to load site; VPN required"))
    assert result.state == world.DOWN and result.cause == "proxy_disabled"
    assert result.proxy_enabled is False


def test_socks5_trace_uses_proxy_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    observed = []

    def server() -> None:
        with listener:
            conn, _ = listener.accept()
            with conn:
                assert conn.recv(3) == b"\x05\x01\x00"
                conn.sendall(b"\x05\x00")
                request = conn.recv(128)
                observed.append(request)
                conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x01\xbb")
                observed.append(conn.recv(512))
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(TRACE)).encode() + b"\r\n\r\n" + TRACE.encode())

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    # The fake endpoint tests the SOCKS5 protocol, not a real TLS handshake.
    monkeypatch.setattr(net_sensor.ssl, "create_default_context",
                        lambda: types.SimpleNamespace(wrap_socket=lambda sock, server_hostname: sock))
    route = net_sensor.Route(True, f"socks5://127.0.0.1:{port}", "127.0.0.1", port)
    assert net_sensor._request(route, 2) == (200, TRACE)
    thread.join(timeout=2)
    assert b"chatgpt.com" in observed[0]
    assert b"GET /cdn-cgi/trace" in observed[1]


def test_unavailable_socks5_does_not_fallback_direct() -> None:
    route = net_sensor.Route(True, "socks5://127.0.0.1:10808", "127.0.0.1", 10808)
    result = net_sensor.probe(route=route, connect=lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert result.state == world.DOWN and result.cause == "proxy_not_listening"


def test_one_event_and_owner_alert_per_cause_then_recovery(store: Store, tmp_path: Path) -> None:
    alerts = tmp_path / "owner-alerts.jsonl"
    down = net_sensor.Result(world.DOWN, "proxy_not_listening", True, False)
    up = net_sensor.Result(world.UP, "ok", True, True, "203.0.113.7", "NL")
    assert net_sensor.publish(store, down, alerts_path=alerts)
    assert not net_sensor.publish(store, down, alerts_path=alerts)
    assert net_sensor.publish(store, net_sensor.Result(world.DOWN, "cloudflare_block", True, True), alerts_path=alerts)
    assert net_sensor.publish(store, up, alerts_path=alerts)
    assert not net_sensor.publish(store, up, alerts_path=alerts)
    notices = [json.loads(line) for line in alerts.read_text(encoding="utf-8").splitlines()]
    assert [row["state"] for row in notices] == ["active", "active", "resolved"]
    assert "Chrome→ChatGPT неизвестен" in notices[0]["message"]
    assert "Включи VPN" not in notices[0]["message"]
    assert notices[0]["browser_route"] == "unknown"
    assert notices[0]["proxy_enabled"] is True
    with store.read() as tx:
        rows = tx.query("SELECT kind,payload FROM events WHERE kind LIKE 'chatgpt_%' ORDER BY id")
        assert [r["kind"] for r in rows] == ["chatgpt_unreachable", "chatgpt_unreachable", "chatgpt_reachable"]
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.UNKNOWN


def test_first_up_is_baseline_not_recovery(store: Store, tmp_path: Path) -> None:
    target = tmp_path / "notice.jsonl"
    assert not net_sensor.publish(store, net_sensor.Result(world.UP, "ok", False, False), alerts_path=target)
    assert not net_sensor.publish(store, net_sensor.Result(world.UP, "ok", False, False), alerts_path=target)
    assert not target.exists()
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.UNKNOWN


def test_extension_three_consecutive_cloudflare_failures_block_even_with_trace_up(store: Store,
                                                                                   tmp_path: Path,
                                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = tmp_path / "alerts.jsonl"
    monkeypatch.setattr(net_sensor, "windows_route", lambda: net_sensor.Route(False))
    up = lambda: net_sensor.Result(world.UP, "ok", False, False, "203.0.113.7", "NL")
    assert not net_sensor.tick(store, sync=True, sensor=up, alerts_path=alerts)
    for count in (1, 2, 3):
        assert net_sensor.report_extension(store, "не найдено поле ввода", "Unable to load site... VPN", alerts_path=alerts) == count
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.DOWN
    # Server trace UP refines the browser-only alert without opening the gate.
    assert net_sensor.tick(store, sync=True, sensor=up, alerts_path=alerts)
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.DOWN
    assert net_sensor.report_extension(store, "composer появился", "нормальная страница", alerts_path=alerts) == 0
    assert net_sensor.tick(store, sync=True, sensor=up, alerts_path=alerts)
    assert [json.loads(x)["state"] for x in alerts.read_text(encoding="utf-8").splitlines()] == ["active", "active", "resolved"]


def test_bad_composer_without_cloudflare_page_does_not_block(store: Store) -> None:
    for _ in range(5):
        assert net_sensor.report_extension(store, "не найдено поле ввода", "Login required") == 0
    assert not net_sensor._extension_blocked(store)


def test_down_resource_blocks_queue_task(store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "fleet-projects.toml"
    cfg.write_text('[resources."chatgpt-reachable"]\nworld=true\nsensor="chatgpt"\n', encoding="utf-8")
    monkeypatch.setenv("DEV_MCP_FLEET_PROJECTS", str(cfg))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)",
                  (str(tmp_path),), event="setup")
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
                  "VALUES('W1','w','chatgpt','2026-10-11','2026-10-11','')", event="setup")
        tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                  "resources,acceptance,body,created_at,updated_at) "
                  "VALUES('P','T1','chat','ready',1,'work','','','[]','[]',?,'[]','','2026','2026')",
                  (json.dumps([net_sensor.RESOURCE]),), event="setup")
    # Egress-only failure cannot prove the Chrome resource is DOWN.
    assert net_sensor.publish(store, net_sensor.Result(world.DOWN, "proxy_disabled", False, False))
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.UNKNOWN
    for _ in range(3):
        net_sensor.report_extension(store, "не найдено поле ввода", "Unable to load site")
    # Browser failures alone block the launch gate; VPN advice is independent.
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.DOWN
    net_sensor.tick(store, sync=True,
                    sensor=lambda: net_sensor.Result(world.DOWN, "network_error", True, True))
    with store.read() as tx:
        assert world.down_resources(tx, [net_sensor.RESOURCE]) == [net_sensor.RESOURCE]
    assert take_next(store, "W1") is None
    assert net_sensor.report_extension(store, "composer ok", "") == 0
    assert net_sensor.publish(store, net_sensor.Result(world.UP, "ok", True, True))
    assert take_next(store, "W1").tz == "T1"


def test_one_transient_trace_failure_does_not_stop_fleet_but_second_does(store, tmp_path):
    alerts = tmp_path/'alerts.jsonl'
    up = net_sensor.Result(world.UP, 'ok', False, False)
    down = net_sensor.Result(world.DOWN, 'proxy_disabled', False, False)
    net_sensor.tick(store,sync=True,sensor=lambda:up,alerts_path=alerts)
    net_sensor.tick(store,sync=True,sensor=lambda:down,alerts_path=alerts)
    with store.read() as tx:
        assert world.last_state(tx,net_sensor.RESOURCE)[0] == world.UNKNOWN
        observed = json.loads(tx.one("SELECT payload FROM events WHERE kind='chatgpt_probe' ORDER BY id DESC LIMIT 1")['payload'])
        assert observed['observed_state'] == world.DOWN and observed['awaiting_confirmation']
    net_sensor.tick(store,sync=True,sensor=lambda:down,alerts_path=alerts)
    with store.read() as tx:
        assert world.last_state(tx,net_sensor.RESOURCE)[0] == world.UNKNOWN
    assert net_sensor.tick(store,sync=True,sensor=lambda:up,alerts_path=alerts)


def test_unknown_browser_route_is_not_blocked_by_server_proxy_failure(store, tmp_path):
    alerts = tmp_path/'alerts.jsonl'
    down = net_sensor.Result(world.DOWN,'proxy_disabled',False,False)
    net_sensor.tick(store,sync=True,sensor=lambda:down,alerts_path=alerts)
    with store.read() as tx:
        assert world.last_state(tx,net_sensor.RESOURCE)[0] == world.UNKNOWN
    net_sensor.tick(store,sync=True,sensor=lambda:net_sensor.Result(world.UP,'ok',True,True),alerts_path=alerts)
    net_sensor.tick(store,sync=True,sensor=lambda:net_sensor.Result(world.DOWN,'proxy_not_listening',True,False),alerts_path=alerts)
    with store.read() as tx:
        assert world.last_state(tx,net_sensor.RESOURCE)[0] == world.UNKNOWN


def test_independent_sensor_publishes_without_controller_and_legacy_tick_does_not_duplicate(store,tmp_path,monkeypatch):
    stop, published, release = threading.Event(), threading.Event(), threading.Event()
    real_publish = net_sensor.publish
    def observe(*args,**kwargs):
        result = real_publish(*args,**kwargs)
        published.set()
        assert release.wait(5)
        stop.set()
        return result
    monkeypatch.setattr(net_sensor,'publish',observe)
    worker = threading.Thread(target=net_sensor.run_worker,args=(store,stop),kwargs={
        'sensor':lambda:net_sensor.Result(world.UP,'ok',False,False),
        'alerts_path':tmp_path/'alerts.jsonl','interval_s':0.01})
    worker.start()
    try:
        assert published.wait(3)
        with store.read() as tx:
            assert world.last_state(tx,net_sensor.RESOURCE)[0] == world.UNKNOWN
            assert not tx.one("SELECT 1 FROM events WHERE kind='controller_tick'")
        assert not net_sensor.tick(store,sensor=lambda:pytest.fail('duplicate sensor'))
    finally:
        release.set()
        stop.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


@pytest.mark.parametrize(
    ("egress", "browser", "vpn"),
    [
        (world.UP, "working", False),
        (world.UP, "failed", False),
        (world.DOWN, "working", False),
        (world.DOWN, "failed", True),
        (world.DOWN, "unknown", False),
    ],
)
def test_vpn_action_requires_both_failed_routes_and_browser_attempts(
    egress: str, browser: str, vpn: bool,
) -> None:
    result = net_sensor.Result(egress, "network_error", True, True)
    text = net_sensor._message(result, net_sensor.BrowserRoute(browser, failed_attempts=3 if browser == "failed" else 0))
    assert ("Включи VPN" in text) is vpn
    assert "Egress сервера" in text or "egress сервера" in text
    assert "Chrome→ChatGPT" in text


def test_chrome_quick_send_receipt_overrides_down_server_egress(
    store: Store, tmp_path: Path,
) -> None:
    quick = tmp_path / "quick" / "jobs.json"
    quick.parent.mkdir()
    recent = datetime.now(timezone.utc).isoformat()
    quick.write_text(json.dumps({"q": {
        "messages": [{"role": "owner", "text": "sent by Chrome", "ts": recent}],
    }}), encoding="utf-8")
    assert net_sensor._browser_route(store).state == "working"
    alerts = tmp_path / "owner-alerts.jsonl"
    assert net_sensor.publish(store, net_sensor.Result(world.DOWN, "network_error", False, False), alerts_path=alerts)
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.UP
    event = json.loads(alerts.read_text(encoding="utf-8").splitlines()[-1])
    assert event["server_egress"] == world.DOWN
    assert event["browser_route"] == "working"
    assert "действий не нужно" in event["message"]
    assert "Включи VPN" not in event["message"]
    old = (datetime.now(timezone.utc) - timedelta(seconds=net_sensor.BROWSER_ROUTE_WINDOW_S + 5)).isoformat()
    quick.write_text(json.dumps({"q": {
        "messages": [{"role": "owner", "text": "old", "ts": old}],
    }}), encoding="utf-8")
    assert net_sensor._browser_route(store).state == "unknown"
    quick.write_text(json.dumps({"q": {
        "answer_source": "browser",
        "messages": [{"role": "agent", "text": "finished Chrome answer", "ts": recent}],
    }}), encoding="utf-8")
    assert net_sensor._browser_route(store).answers == 1
    quick.write_text(json.dumps({"q": {
        "answer_source": "mcp",
        "messages": [{"role": "agent", "text": "MCP answer, not Chrome proof", "ts": recent}],
    }}), encoding="utf-8")
    assert net_sensor._browser_route(store).state == "unknown"


def test_regular_agent_generating_tab_probe_is_browser_route_evidence(store: Store) -> None:
    now = datetime.now(timezone.utc)
    earlier = (now - timedelta(minutes=3)).isoformat()
    with store.tx() as tx:
        tx.change("CREATE TABLE worker_activity(worker_id TEXT PRIMARY KEY, claim_id INTEGER NOT NULL, "
                  "state TEXT NOT NULL, stall_s INTEGER NOT NULL, seen_at TEXT NOT NULL)",
                  event="fixture")
        tx.change("INSERT INTO worker_activity VALUES(?,?,?,?,?)",
                  ("W1", 1, "generating", 0, earlier), event="fixture")
    assert net_sensor._browser_route(store, now=now).state == "working"
    old = (now - timedelta(seconds=net_sensor.BROWSER_ROUTE_WINDOW_S + 5)).isoformat()
    with store.tx() as tx:
        tx.change("UPDATE worker_activity SET seen_at=?", (old,), event="fixture")
    assert net_sensor._browser_route(store, now=now).state == "unknown"
    with store.tx() as tx:
        tx.change("UPDATE worker_activity SET seen_at=?,state='idle'", (earlier,), event="fixture")
    assert net_sensor._browser_route(store, now=now).state == "unknown"


def test_extension_page_failures_need_three_attempts_and_no_success(
    store: Store, tmp_path: Path,
) -> None:
    alerts = tmp_path / "alerts.jsonl"
    down = net_sensor.Result(world.DOWN, "network_error", True, True)
    assert net_sensor._browser_route(store).state == "unknown"
    assert "Включи VPN" not in net_sensor._message(down, net_sensor._browser_route(store))
    for count in (1, 2):
        assert net_sensor.report_extension(store, "не найдено поле ввода", "Unable to load site", alerts_path=alerts) == count
        assert net_sensor._browser_route(store).state == "unknown"
    assert net_sensor.report_extension(store, "не найдено поле ввода", "Unable to load site", alerts_path=alerts) == 3
    assert net_sensor._browser_route(store).state == "failed"
    assert "Включи VPN" in net_sensor._message(down, net_sensor._browser_route(store))
    # Browser failure alone is not evidence that server egress also failed.
    assert "Включи VPN" not in net_sensor._message(
        net_sensor.Result(world.DOWN, "extension_cloudflare", True, True), net_sensor._browser_route(store))
    assert net_sensor.report_extension(store, "composer ok", "", alerts_path=alerts) == 0
    assert net_sensor._browser_route(store).state == "unknown"


def test_extension_outage_with_successful_server_trace_never_demands_vpn(
    store: Store, tmp_path: Path,
) -> None:
    alerts = tmp_path / "alerts.jsonl"
    for _ in range(3):
        net_sensor.report_extension(store, "не найдено поле ввода", "Unable to load site", alerts_path=alerts)
    net_sensor.tick(store, sync=True, sensor=lambda: net_sensor.Result(world.UP, "ok", True, True), alerts_path=alerts)
    notice = json.loads(alerts.read_text(encoding="utf-8").splitlines()[-1])
    assert notice["server_egress"] == world.UP
    assert notice["browser_route"] == "failed"
    assert "Включи VPN" not in notice["message"]
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.DOWN


def test_new_confirmed_browser_failures_override_older_success_and_stop_launches(
    store: Store, tmp_path: Path,
) -> None:
    quick = tmp_path / "quick" / "jobs.json"
    quick.parent.mkdir()
    quick.write_text(json.dumps({"q": {"messages": [{
        "role": "owner", "ts": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }]}}), encoding="utf-8")
    assert net_sensor._browser_route(store).state == "working"
    for n in (1, 2, 3):
        assert net_sensor.report_extension(
            store, "не найдено поле ввода", "Unable to load site",
        ) == n
    assert net_sensor._browser_route(store).state == "failed"
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.DOWN
    assert net_sensor.tick(store, sync=True, sensor=lambda: net_sensor.Result(
        world.UP, "ok", True, True))
    with store.read() as tx:
        assert world.last_state(tx, net_sensor.RESOURCE)[0] == world.DOWN
    notice = json.loads(net_sensor._alert_path(store).read_text(encoding="utf-8").splitlines()[-1])
    assert notice["server_egress"] == world.UP
    assert notice["browser_route"] == "failed"
    assert "Включи VPN" not in notice["message"]
