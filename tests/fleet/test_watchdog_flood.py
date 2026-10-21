"""Урок 38: сторож потока не плодит тикеты на каждом тике."""
from __future__ import annotations

from dev_mcp.fleet import watchdog
from dev_mcp.fleet.watchdog import Finding


def test_fingerprint_ignores_volatile_detail():
    a = Finding("guard_denial", "P", "TZ-1", "last events: 16:07", "reopen_styk")
    b = Finding("guard_denial", "P", "TZ-1", "last events: 16:08", "reopen_styk")
    assert a.fingerprint == b.fingerprint


class Tx:
    def __init__(self, open_count):
        self.open_count, self.inserted = open_count, 0

    def one(self, sql, params=()):
        return {"root": "C:/x"} if "FROM projects" in sql else None

    def scalar(self, sql, params=()):
        return self.open_count

    def query(self, sql, params=()):
        return []

    def change(self, *a, **k):
        self.inserted += 1


def test_backpressure_stops_new_tickets():
    from datetime import datetime, timezone
    f = Finding("metrics_mismatch", "P", "runnable", "why=1", "observability")
    now = datetime.now(timezone.utc)
    full = Tx(watchdog.TICKET_BACKPRESSURE)
    watchdog._ticket(full, f, 0, now)
    assert full.inserted == 0
    room = Tx(0)
    watchdog._ticket(room, f, 0, now)
    assert room.inserted == 1


def _store(tmp_path):
    from dev_mcp.fleet.store import Store
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("P", str(tmp_path)), event={"kind": "test_setup"})
    return store


def _tickets(store):
    with store.tx() as tx:
        return [r["tz"] for r in tx.query("SELECT tz FROM tasks WHERE tz LIKE 'watch:%'")]


def test_taken_ticket_still_counts_after_author_changes(tmp_path, monkeypatch):
    """Урок 43: при выдаче author_worker меняется — счёт по имени тикета."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(watchdog, "TICKET_BACKPRESSURE", 1)
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    with store.tx() as tx:
        watchdog._ticket(tx, Finding("a", "P", "s1", "d", "observability"), 0, now)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='taken', author_worker='W1'", (), event={"kind": "test_setup"})
    with store.tx() as tx:
        watchdog._ticket(tx, Finding("b", "P", "s2", "d", "observability"), 0, now)
    assert len(_tickets(store)) == 1


def test_flapping_finding_gets_one_ticket_per_cooldown(tmp_path):
    from datetime import datetime, timedelta, timezone
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    f = Finding("metrics_mismatch", "P", "runnable", "why=1", "observability")
    for episode, shift in ((0, 0), (11, 10), (22, 20)):  # эпизоды мигают каждые 10 мин
        with store.tx() as tx:
            watchdog._ticket(tx, f, episode, now + timedelta(minutes=shift))
        with store.tx() as tx:
            tx.change("UPDATE tasks SET state='accepted'", (), event={"kind": "test_setup"})
    assert len(_tickets(store)) == 1
    with store.tx() as tx:
        watchdog._ticket(tx, f, 33, now + timedelta(hours=7))
    assert len(_tickets(store)) == 2


def test_frozen_tickets_do_not_hold_backpressure(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(watchdog, "TICKET_BACKPRESSURE", 1)
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    with store.tx() as tx:
        watchdog._ticket(tx, Finding("a", "P", "s1", "d", "observability"), 0, now)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='frozen'", (), event={"kind": "test_setup"})
    with store.tx() as tx:
        watchdog._ticket(tx, Finding("b", "P", "s2", "d", "observability"), 0, now)
    assert len(_tickets(store)) == 2
