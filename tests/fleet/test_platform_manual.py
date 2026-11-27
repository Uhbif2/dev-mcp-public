from datetime import datetime, timezone
import json
from dev_mcp.fleet import platform_manual, platform_route, watchdog
from dev_mcp.fleet.store import Store

def test_platform_repair_does_not_recreate_self_editing_project(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_manual,'enabled',lambda:True)
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    now=datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)',('P',str(tmp_path)),event='seed')
        tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('P','TZ-1','ready',?,?)",
                  (now,now),event='seed')
    routed=platform_route.route_request(store,source_project='P',source_tz='TZ-1',
                 summary='fleet_take server defect',path='src/dev_mcp/fleet/queue.py')
    assert routed.task.startswith('manual-platform:')
    platform_manual.export(store)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM projects WHERE name='Fleet Platform'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Fleet Platform'") == 0
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-1'") == 'frozen'
    assert json.loads((tmp_path/'manual-platform-backlog.json').read_text(encoding='utf-8'))['fronts']
    assert platform_route.thaw_deployed(store) == []
    with __import__('pytest').raises(ValueError):
        platform_manual.resolve(store,routed.task,candidate='abc',evidence='')
    platform_manual.resolve(store,routed.task,candidate='a'*40,evidence='production regression check passed')
    assert platform_route.thaw_deployed(store) == ['P/TZ-1']
    with store.read() as tx:
        assert platform_route.pending_platform(tx,'P','TZ-1') is None
    assert platform_route.thaw_deployed(store) == []

def test_repeated_watchdog_cause_has_one_manual_repair_and_no_fleet_task(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_manual,'enabled',lambda:True)
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    finding=watchdog._issue('intent_stall','Fleet Platform','P/TZ-1','no progress','observability')
    with store.tx() as tx:
        watchdog._ticket(tx,finding,0,datetime.now(timezone.utc))
        watchdog._ticket(tx,finding,0,datetime.now(timezone.utc))
    with store.read() as tx:
        assert tx.scalar('SELECT COUNT(*) FROM tasks') == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1


def _git(root, *args):
    import subprocess
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)


def test_recheck_retires_only_fronts_whose_cause_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_manual, 'enabled', lambda: True)
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 't@t')
    _git(repo, 'config', 'user.name', 't')
    (repo / 'clean.md').write_text('a', encoding='utf-8')
    (repo / 'dirty.md').write_text('a', encoding='utf-8')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'init')
    (repo / 'dirty.md').write_text('changed', encoding='utf-8')
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)', ('P', str(repo)), event='seed')
        for tz, state in (('TZ-done', 'accepted'), ('TZ-live', 'frozen')):
            tx.change('INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES(?,?,?,?,?)',
                      ('P', tz, state, now, now), event='seed')
        title = 'Принятая P/TZ-x не коммитится: файлы изменены вне флота'
        stale = platform_manual.enqueue(tx, 'f1', title=title, detail='Файлы: clean.md',
                                        source_project='P', source_tz='TZ-a')
        legacy = platform_manual.enqueue(tx, 'f2', title=title, detail='x. Файлы: clean.md, dirty.md',
                                         source_project='P', source_tz='TZ-b')
        owned = platform_manual.enqueue(tx, 'f3', title=title, detail='', owns=['clean.md'],
                                        source_project='P', source_tz='TZ-c')
        done = platform_manual.enqueue(tx, 'f4', title='Watchdog: loop_task', detail='',
                                       source_project='Fleet Platform', source_tz='P/TZ-done')
        live = platform_manual.enqueue(tx, 'f5', title='Watchdog: loop_task', detail='',
                                       source_project='Fleet Platform', source_tz='P/TZ-live')
        other = platform_manual.enqueue(tx, 'f6', title='Repair intent stage prover exception',
                                        detail='', source_project='P', source_tz='TZ-done')
    result = platform_manual.recheck(store)
    assert result == {'open': 6, 'retired': 3}
    with store.read() as tx:
        rows = [json.loads(r['payload']) for r in
                tx.query("SELECT payload FROM events WHERE kind='manual_platform_resolved'")]
    assert {r['id'] for r in rows} == {stale, owned, done}
    assert all(r['producer'] == 'auto-recheck' for r in rows)
    assert {legacy, live, other}.isdisjoint(r['id'] for r in rows)
    # Throttled: an immediate second pass does no git work and retires nothing.
    assert platform_manual.recheck(store) == {'skipped': 1}
    # Auto-retirement is not a deployment proof: no platform waiter thaws on it.
    assert platform_route.thaw_deployed(store) == []


def test_export_skips_idle_rebuild_but_refreshes_on_new_front(tmp_path, monkeypatch):
    from dev_mcp.fleet import progress_watch

    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    writes = []
    original_write = progress_watch.write_front_report

    def counted_write(*args, **kwargs):
        writes.append(1)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(progress_watch, 'write_front_report', counted_write)
    with store.tx() as tx:
        platform_manual.enqueue(tx, 'one', title='First', detail='x')
    platform_manual.export(store)
    platform_manual.export(store)
    assert len(writes) == 1

    with store.tx() as tx:
        platform_manual.enqueue(tx, 'two', title='Second', detail='y')
    platform_manual.export(store)
    assert len(writes) == 2
    report = json.loads((tmp_path / 'manual-platform-backlog.json').read_text(encoding='utf-8'))
    assert len(report['fronts']) == 2
    store.close()
