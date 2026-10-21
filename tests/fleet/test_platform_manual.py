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
