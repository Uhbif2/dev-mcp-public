import json
import threading
from datetime import datetime, timezone

from dev_mcp.fleet import queue
from dev_mcp.fleet.store import Store


def test_advisory_routing_after_take_leaves_writer_available(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.changes([
            ('INSERT INTO projects(name,root) VALUES(?,?)', ('Unit', str(tmp_path))),
            ("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES('W1','test','chatgpt',?,?)", (stamp, stamp)),
            ("INSERT INTO tasks(project,tz,state,owns,created_at,updated_at) VALUES('Unit','TZ-1','ready',?,?,?)",
             (json.dumps([str(tmp_path/'src/new.py')]), stamp, stamp)),
        ], event='seed')
    entered, release = threading.Event(), threading.Event()
    results = []
    def slow(*args):
        assert getattr(store._local, 'active_tx', None) is None
        entered.set()
        assert release.wait(5)
        return None
    monkeypatch.setattr(queue, '_routing_shadow_event', slow)
    worker = threading.Thread(target=lambda: results.append(queue.take_next(store, 'W1', 'Unit')))
    worker.start()
    try:
        assert entered.wait(3)
        with store.tx() as tx:
            assert tx.one("SELECT state FROM tasks WHERE tz='TZ-1'")['state'] == 'taken'
            tx.record_event(kind='concurrent_writer_progress')
    finally:
        release.set()
        worker.join(timeout=5)
    assert results[0].tz == 'TZ-1'
    store.close()


def test_large_history_skips_comparison_instead_of_truncating_a_false_graph(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)', ('Unit', str(tmp_path)), event='seed')
        tx.changes([
            ("INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('Unit',?,'accepted',?,?)",
             (f'TZ-{i}', stamp, stamp)) for i in range(257)
        ], event='seed')
    monkeypatch.setattr('dev_mcp.fleet.routing.lanes', lambda *_: (_ for _ in ()).throw(AssertionError('unbounded graph')))
    with store.read() as tx:
        event = queue._routing_shadow_event(tx, 'W1', {'project':'Unit','tz':'TZ-1','kind':'work'}, [])
    assert event['kind'] == 'routing_shadow_skipped'
    assert json.loads(event['payload'])['reason'] == 'history_budget'
    store.close()
