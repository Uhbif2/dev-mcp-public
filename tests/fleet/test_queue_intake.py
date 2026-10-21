from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_mcp.fleet import queue_intake
from dev_mcp.fleet.store import Store


@pytest.fixture
def env(tmp_path):
    root = tmp_path/'product'
    root.mkdir()
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)', ('Product', str(root)), event='seed')
    policy = {'roots': [str(root)], 'write_roots': [str(root)], 'allow_write': True}
    try:
        yield store, root, policy
    finally:
        store.close()


def spec(root, tz='TZ-1', suffix='work', body='# Work\nImplement new behavior'):
    path = root/'ToDo'/'tz'/f'{tz}-{suffix}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'```yaml\ntz: {tz}\nowns: [src/{tz}.py]\nneeds: []\n'
                    f'acceptance: [py -3 -m pytest tests/test_{tz}.py -q]\n```\n\n{body}\n', encoding='utf-8')
    return path


def tick(env, **kwargs):
    store, _, policy = env
    return queue_intake.tick(store, policy, lambda *_: 4, **kwargs)


def test_new_spec_is_registered_after_stable_observation_without_operator(env):
    store, root, _ = env
    assert tick(env)['baselined'] == ['Product']
    source = spec(root)
    assert tick(env)['deferred'] == ['Product/TZ-1']
    assert tick(env)['registered'] == ['Product/TZ-1']
    assert tick(env)['registered'] == []
    with store.read() as tx:
        task = tx.one('SELECT * FROM tasks WHERE project=? AND tz=?', ('Product', 'TZ-1'))
        assert task['state'] == 'ready'
        assert json.loads(task['acceptance']) == ['py -3 -m pytest tests/test_TZ-1.py -q']
    assert source.is_file()


def test_initial_history_and_renames_are_not_replayed_after_restart(env):
    store, root, policy = env
    path = spec(root, 'TZ-OLD')
    tick(env)
    path.rename(path.with_name('TZ-OLD-renamed.md'))
    spec(root, 'TZ-NEW')
    tick(env)
    # A new Store/process has no in-memory cursor; the persisted fence is enough.
    other = Store(str(store.path), available=True)
    try:
        result = queue_intake.tick(other, policy, lambda *_: 4)
        assert result['registered'] == ['Product/TZ-NEW']
        with other.read() as tx:
            assert tx.one("SELECT 1 FROM tasks WHERE tz='TZ-OLD'") is None
    finally:
        other.close()


def test_duplicate_gets_one_repair_then_real_registration_after_resolution(env):
    store, root, _ = env
    tick(env)
    original = spec(root)
    duplicate = spec(root, suffix='different', body='# Different\nRetain both requirements')
    tick(env)
    assert tick(env)['conflicts'] == ['Product/TZ-1']
    tick(env)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'fix:queue-source:%'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='queue_source_conflict'") == 1
        assert not tx.one("SELECT 1 FROM tasks WHERE tz='TZ-1'")
    duplicate.write_text('# Merged\nSee '+original.name, encoding='utf-8')
    tick(env)
    assert tick(env)['registered'] == ['Product/TZ-1']
    with store.read() as tx:
        assert tx.one('SELECT state FROM queue_source_issues')['state'] == 'resolved'


def test_partial_file_is_rechecked_and_never_registered_early(env):
    store, root, _ = env
    tick(env)
    path = spec(root)
    path.write_text('```yaml\ntz: TZ-1\n', encoding='utf-8')
    tick(env)
    spec(root)
    assert tick(env)['registered'] == []
    assert tick(env)['registered'] == ['Product/TZ-1']


def test_registry_race_is_deferred_and_other_project_keeps_progress(env, monkeypatch):
    store, root, policy = env
    other = root/'other'
    other.mkdir()
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)', ('Other', str(other)), event='seed')
    tick(env)
    spec(root)
    spec(other, 'TZ-2')
    tick(env)
    real = queue_intake.sync_selected
    def raced(*args, **kwargs):
        if args[1] == 'Product':
            raise RuntimeError('one source unavailable')
        return real(*args, **kwargs)
    monkeypatch.setattr(queue_intake, 'sync_selected', raced)
    result = tick(env)
    assert result['registered'] == ['Other/TZ-2']
    assert result['deferred'] == ['Product/TZ-1']


def test_no_policy_or_runner_has_no_effect(env):
    store, root, policy = env
    spec(root)
    assert not queue_intake.tick(store, policy, None)['registered']
    with store.read() as tx:
        assert not tx.one("SELECT 1 FROM sqlite_master WHERE name='queue_sources'")


def test_source_cannot_reactivate_a_manually_disabled_project(env):
    store, root, _ = env
    tick(env)
    spec(root)
    tick(env)
    with store.tx() as tx:
        tx.change("UPDATE projects SET active=0 WHERE name='Product'", event='freeze')
    assert tick(env)['registered'] == []
    with store.read() as tx:
        assert not tx.one('SELECT 1 FROM tasks')


def test_removed_historical_file_is_not_new_when_restored_later(env):
    store, root, _ = env
    path = spec(root, 'TZ-OLD')
    content = path.read_text(encoding='utf-8')
    tick(env)
    path.unlink()
    tick(env)
    path.write_text(content, encoding='utf-8')
    tick(env)
    assert tick(env)['registered'] == []
    with store.read() as tx:
        assert not tx.one('SELECT 1 FROM tasks')


def test_fresh_intent_workspace_has_fence_before_planner_writes(env):
    from dev_mcp.intents.spawn import spawn_project
    from dev_mcp.intents.sources.model import IntentEnvelope
    store, root, policy = env
    envelope = IntentEnvelope('files','new-project','v1','Build an offline tool',
                              {'create_project':True,'project_roots':[str(root)]},
                              '2026-10-15T09:00:00+00:00')
    spawned = spawn_project(store,envelope,parent=root,project_name='New',size='small',allowed_roots=[root])
    spec(spawned.directory)
    tick(env)
    assert 'New/TZ-1' in tick(env)['registered']


def test_obsolete_repair_does_not_keep_its_own_valid_spec_waiting_forever(env):
    store,root,_ = env
    tick(env)
    path = spec(root)
    path.write_text(path.read_text(encoding='utf-8').replace(
        'owns: [src/TZ-1.py]', 'owns: [src/TZ-1.py, ToDo/tz/TZ-1-work.md]'), encoding='utf-8')
    duplicate = spec(root,suffix='duplicate')
    tick(env)
    tick(env)
    duplicate.write_text('# Superseded\nSee TZ-1-work.md',encoding='utf-8')
    tick(env)
    assert tick(env)['registered'] == []  # repair owns the spec; retire it first
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz LIKE 'fix:queue-source:%'")['state'] == 'rejected'
    assert tick(env)['registered'] == ['Product/TZ-1']


def test_manual_freeze_of_source_repair_is_not_overridden(env):
    import hashlib
    store,root,_ = env
    tick(env)
    path = spec(root)
    queue_intake._issue(store,'Product',root,'TZ-1','previous invalid syntax',[path.relative_to(root).as_posix()])
    fix = 'fix:queue-source:'+hashlib.sha256(b'TZ-1').hexdigest()[:16]
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='frozen' WHERE tz=?",(fix,),
                  event={'kind':'freeze','project':'Product','tz':fix})
    queue_intake._retire_obsolete_repair(store,'Product','TZ-1',[])
    with store.read() as tx:
        assert tx.one('SELECT state FROM tasks WHERE tz=?',(fix,))['state'] == 'frozen'


def test_historical_spec_demanded_by_live_task_is_registered(env):
    """17.10: Beta App TZ-526 needed fenced-off TZ-507 and waited forever."""
    store, root, _ = env
    spec(root, 'TZ-1')
    assert tick(env)['baselined'] == ['Product']
    tick(env)
    with store.read() as tx:
        assert not tx.one("SELECT 1 FROM tasks WHERE tz='TZ-1'")
    with store.tx() as tx:
        tx.change("INSERT INTO tasks(project,tz,state,owns,needs,acceptance,created_at,updated_at) VALUES(?,?,?,?,?,?,'2026-10-17','2026-10-17')",
                  ('Product', 'TZ-2', 'ready', '["b/**"]', '["TZ-1"]', '["py -3 -c pass"]'), event='seed')
    assert tick(env)['registered'] == ['Product/TZ-1']
