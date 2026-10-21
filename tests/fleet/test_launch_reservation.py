from datetime import datetime, timezone, timedelta
import json
import threading
from dev_mcp.fleet import launch, queue
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.tools import FleetContext, register

class MCP:
    def tool(self, **kwargs):
        return lambda fn: fn

def setup(tmp_path):
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)',('P',str(tmp_path)),event='seed')
        tx.change("INSERT INTO tasks(project,tz,state,kind,created_at,updated_at) VALUES('P','TZ-1','ready','work',?,?)",
                  (stamp,stamp),event='seed')
    return store, register(MCP(),FleetContext(store))

def test_one_real_task_cannot_create_two_launch_reservations(tmp_path):
    store, api = setup(tmp_path)
    results = []
    def take(label):
        results.append(json.loads(api['fleet_launch_reserve'](label,'P')))
    threads = [threading.Thread(target=take,args=(f'f1014-130{n}abc',)) for n in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(5)
    assert sorted(r['status'] for r in results) == ['empty','reserved']
    assert queue.peek_next(store,project='P') == (None,0)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE state='active'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='launch_reserved'") == 1

def test_lost_reservation_response_replays_same_claim_without_empty_queue(tmp_path):
    store, api = setup(tmp_path)
    args = ('f1014-1300abc','P')
    first = json.loads(api['fleet_launch_reserve'](*args))
    assert json.loads(api['fleet_launch_reserve'](*args)) == first
    joined = api['fleet_join'](args[0])
    assert first['worker_id'] in joined
    resumed = api['fleet_resume'](first['worker_id'])
    assert 'TZ-1' in resumed
    assert json.loads(api['fleet_launch_release'](args[0],first['claim_id']))['status'] == 'confirmed'


def test_project_paused_after_reservation_never_starts_a_model(tmp_path):
    store,api = setup(tmp_path)
    args = ('f1014-1300abc','P')
    first = json.loads(api['fleet_launch_reserve'](*args))
    with store.tx() as tx:
        tx.change("UPDATE projects SET active=0 WHERE name='P'",event='owner_pause')
    assert json.loads(api['fleet_launch_reserve'](*args)) == {'status':'empty'}
    assert json.loads(api['fleet_launch_release'](args[0],first['claim_id'])) == {'status':'released'}

def test_failed_start_restores_task_but_never_releases_other_label(tmp_path):
    store, api = setup(tmp_path)
    first = json.loads(api['fleet_launch_reserve']('f1014-1300abc','P'))
    import pytest
    with pytest.raises(ValueError):
        api['fleet_launch_release']('f1014-1301abc',first['claim_id'])
    assert json.loads(api['fleet_launch_release']('f1014-1300abc',first['claim_id']))['status'] == 'released'
    assert queue.peek_next(store,project='P')[1] == 1

def test_crashed_browser_start_expires_without_a_waiting_agent(tmp_path):
    store, api = setup(tmp_path)
    result = json.loads(api['fleet_launch_reserve']('f1014-1300abc','P'))
    old = (datetime.now(timezone.utc)-timedelta(seconds=launch.LEASE_S+10)).isoformat()
    with store.tx() as tx:
        tx.change('UPDATE claims SET started_at=? WHERE id=?',(old,result['claim_id']),event='backdate')
    launch.release_expired(store)
    launch.release_expired(store)
    assert queue.peek_next(store,project='P')[1] == 1

def test_empty_queue_does_not_register_a_synthetic_worker(tmp_path):
    store, api = setup(tmp_path)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted'",event='finish')
    assert json.loads(api['fleet_launch_reserve']('f1014-1300abc','P')) == {'status':'empty'}
    with store.read() as tx:
        assert tx.scalar('SELECT COUNT(*) FROM workers') == 0
