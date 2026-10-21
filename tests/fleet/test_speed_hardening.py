from datetime import datetime, timezone
from types import SimpleNamespace
import threading
import time

from dev_mcp.fleet import watchdog
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.tools import FleetContext


def test_advisory_never_blocks_response_and_is_bounded(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    def slow(*args):
        calls.append(1)
        started.set()
        release.wait(3)
    ctx = FleetContext(None, modules={
        'hazard': SimpleNamespace(stale_reads=slow),
        'inbox': SimpleNamespace(pending_for=lambda *args: [], render_envelope=lambda *args: '')})
    ctx.steering = lambda worker: ''
    before = time.monotonic()
    try:
        assert ctx.with_envelope('W1', 'actual result') == 'actual result'
        assert time.monotonic() - before < 1
        assert started.wait(1)
        ctx.with_envelope('W1', 'next result')
        assert len(calls) == 1
    finally:
        release.set()


def test_concurrent_idle_cursor_cannot_duplicate_first_incident(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(watchdog, 'detect', lambda *args, **kw: [])
    watchdog.watchdog_tick(store, now=now)
    snapshot = watchdog._watchdog_history(store)
    # Simulate two completed reads against the same idle cursor, then writes.
    monkeypatch.setattr(watchdog, '_watchdog_history', lambda _: snapshot)
    finding = watchdog._issue('extension_off', 'Fleet Platform', 'extension', 'off', 'owner')
    monkeypatch.setattr(watchdog, 'detect', lambda *args, **kw: [finding])
    watchdog.watchdog_tick(store, now=now)
    watchdog.watchdog_tick(store, now=now)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='watchdog_open'") == 1


def test_watchdog_repair_has_code_zone_and_executable_acceptance(tmp_path, monkeypatch):
    from dev_mcp.fleet import platform_manual
    monkeypatch.setattr(platform_manual,'enabled',lambda:True)
    import json
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)',
                  ('Fleet Platform', str(tmp_path)), event='seed')
        finding = watchdog._issue('intent_stall', 'Fleet Platform', 'intent/stage',
                                  'stage does not advance', 'observability')
        watchdog._ticket(tx, finding, 0, datetime.now(timezone.utc))
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'watch:%'") == 0
        task = json.loads(tx.one("SELECT payload FROM events WHERE kind='manual_platform_required'")['payload'])
    assert any('stages.py' in p for p in task['owns'])
    assert task['acceptance']
