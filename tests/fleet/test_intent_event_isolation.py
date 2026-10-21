import json

from dev_mcp.fleet.controller import Controller
from dev_mcp.fleet.store import Store


def test_failed_intent_does_not_hold_other_projects_and_retries_durably(tmp_path, monkeypatch):
    from dev_mcp.intents import spawn
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    controller = Controller(store, maintenance=lambda: None)
    controller._schema()
    with store.tx() as tx:
        tx.record_event(kind='outcome.closed', payload={'id': 'broken'})
        tx.record_event(kind='outcome.closed', payload={'id': 'healthy'})
    monkeypatch.setattr(controller, '_intent_tree', lambda identifier: identifier)
    calls = []
    def route(_store, tree):
        calls.append(tree)
        if tree == 'broken':
            raise ValueError('target conflict')
        return tree
    monkeypatch.setattr(spawn, 'route_proven_understood', route)
    monkeypatch.setattr(spawn, 'reconcile_tree', lambda _store, tree: ('advanced',))
    errors = []
    assert controller._reconcile_intent_events(errors) == 1
    assert calls == ['broken', 'healthy']
    assert len(errors) == 1
    with store.read() as tx:
        pending = tx.one('SELECT event_id,attempts FROM controller_intent_retries')
        assert pending['attempts'] == 1
    calls.clear()
    controller._reconcile_intent_events([])
    assert calls == []  # backoff, not a tight loop
    with store.tx() as tx:
        tx.change('UPDATE controller_intent_retries SET retry_at=?', ('2000-01-01',), event='test')
    monkeypatch.setattr(spawn, 'route_proven_understood', lambda _store, tree: tree)
    restarted = Controller(store, maintenance=lambda: None)
    restarted._schema()
    monkeypatch.setattr(restarted, '_intent_tree', lambda identifier: identifier)
    assert restarted._reconcile_intent_events([]) == 1
    with store.read() as tx:
        assert tx.scalar('SELECT COUNT(*) FROM controller_intent_retries') == 0
