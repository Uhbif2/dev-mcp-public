"""Regression coverage for idempotent, low-cost fleet maintenance."""
from datetime import datetime, timedelta, timezone

from dev_mcp.fleet.store import Store
from dev_mcp.fleet.tools import FleetContext, _repair_dedupe_heartbeats


def test_dedupe_heartbeat_repair_only_touches_workers_with_dedupe_events(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc)
    before = (now - timedelta(hours=2)).isoformat()
    current = now.isoformat()
    try:
        with store.tx() as tx:
            for wid, label in (("W1", "f1115-1900abc"), ("W2", "f1115-1901def")):
                tx.change(
                    "INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                    "VALUES(?,?,'chatgpt',?,?)",
                    (wid, label, before, current),
                    event="test.worker",
                )
            tx.record_event(kind="worker_dedupe", worker_id="W1",
                            payload={"label": "f1115-1900abc"})
        repaired = _repair_dedupe_heartbeats(FleetContext(store=store))
        assert repaired == ["f1115-1900abc/W1"]
        with store.read() as tx:
            assert tx.one("SELECT last_seen FROM workers WHERE id='W1'")["last_seen"] == before
            assert tx.one("SELECT last_seen FROM workers WHERE id='W2'")["last_seen"] == current
        assert _repair_dedupe_heartbeats(FleetContext(store=store)) == []
    finally:
        store.close()
