from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from dev_mcp.fleet import goals, queue, claim_take_routing
from dev_mcp.fleet.claim_context import ClaimRoutingError
from dev_mcp.fleet.store import Store


def seed(tmp_path):
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)',
                  ('P', str(tmp_path)), event='seed')
        for worker in ('W1', 'W2'):
            tx.change('INSERT INTO workers(id,first_seen,last_seen) VALUES(?,?,?)',
                      (worker, stamp, stamp), event='seed')
        tx.change('INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES(?,?,?,?,?)',
                  ('P', 'TZ-1', 'ready', stamp, stamp), event='seed')
    return store


def test_slow_git_preparation_does_not_hold_registry_writer(tmp_path, monkeypatch):
    store = seed(tmp_path)
    preparing = threading.Event()
    release = threading.Event()
    wrote = threading.Event()
    outcomes = []
    monkeypatch.setattr(goals, 'project_info',
                        lambda _: {'commit': True, 'claim_trees': True})

    def slow_prepare(**kwargs):
        preparing.set()
        release.wait(4)
        raise ClaimRoutingError('deliberate checkout failure')

    monkeypatch.setattr(claim_take_routing, 'prepare_claim_context', slow_prepare)
    take = threading.Thread(target=lambda: outcomes.append(queue.take_next(store, 'W1')))
    take.start()
    assert preparing.wait(2)

    def other_write():
        with store.tx() as tx:
            tx.record_event(kind='unrelated_submit', worker_id='W2')
        wrote.set()

    writer = threading.Thread(target=other_write)
    writer.start()
    try:
        assert wrote.wait(1), 'checkout holds the registry writer'
        with store.read() as tx:
            assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='take'") == 0
    finally:
        release.set()
        take.join(5)
        writer.join(5)
    assert outcomes == [None]
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-1'") == 'ready'
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE state IN ('active','preparing')") == 0


def test_stale_preparation_is_recovered_and_cannot_publish_a_take(tmp_path):
    store = seed(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('P','TZ-1','W1','preparing',?)", (old,), event='seed')
        tx.change("UPDATE tasks SET state='taken',updated_at=? WHERE tz='TZ-1'",
                  (old,), event='seed')
    assert queue.recover_preparing(store) == 1
    assert queue.recover_preparing(store) == 0
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-1'") == 'ready'
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE state='preparing'") == 0
