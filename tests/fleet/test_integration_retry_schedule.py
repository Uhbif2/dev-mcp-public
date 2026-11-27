"""Fair batches, restarts and unrelated events must not defeat retry deadlines."""

import json

import pytest

from dev_mcp.fleet.integrate import Integrator
from dev_mcp.fleet.store import Store


@pytest.fixture
def lane(tmp_path):
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        for project in ('P','Q'):
            tx.change('INSERT INTO projects(name,root) VALUES(?,?)',
                      (project,str(tmp_path/project)),event='test.seed')
            (tmp_path/project).mkdir()
            for i in range(12):
                tz=f'TZ-{i}'
                tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) "
                          "VALUES(?,?,'accepted',?,?)",(project,tz,'2026-10-23','2026-10-23'),event='test.seed')
                tx.record_event(kind='accept',project=project,tz=tz)
                aid=tx.scalar('SELECT MAX(id) FROM events')
                tx.record_event(kind='integrate',project=project,tz=tz,
                                payload={'accept_id':aid,'deferred':['file.txt'],'waiting_for':['DEP']})
    yield store,tmp_path
    store.close()


def integrator(store,path,calls):
    engine=Integrator(store,path/'cursor.json',project_info=lambda _: {'commit':True})
    engine._retry.head=lambda *_:'same-head'
    engine._validate_train=lambda _:{}
    def defer(batch,accept,**_):
        calls.append((batch.project,accept['id']))
        return engine._record(kind='integrate',project=batch.project,tz=accept['tz'],payload={
            'accept_id':accept['id'],'deferred':['file.txt'],'waiting_for':['DEP']})
    engine._commit=defer
    with store.read() as tx:
        engine._save_cursor(tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'"))
    return engine


def test_rotation_and_restart_do_not_retry_the_same_accept(lane):
    store,path=lane
    calls=[]
    engine=integrator(store,path,calls)
    for _ in range(4):
        engine.tick()
    assert len(calls)==24 and len(set(calls))==24
    assert {p for p,_ in calls[:10]}=={'P','Q'}
    integrator(store,path,calls).tick()
    assert len(calls)==24
    # A different task in the same project is not a changed blocker.
    with store.tx() as tx:
        tx.record_event(kind='submit',project='P',tz='UNRELATED')
    engine.tick()
    assert len(calls)==24


def test_resource_busy_error_survives_cursor_and_restart(tmp_path):
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    cursor = tmp_path / 'cursor.json'
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)", (str(tmp_path),),
                  event='test.seed')
        tx.record_event(kind='accept', project='P', tz='TZ-1')
        accept_id = tx.scalar("SELECT MAX(id) FROM events")
        tx.record_event(kind='integrate', project='P', tz='TZ-1', payload={
            'accept_id': accept_id, 'error': 'resource busy: build is running', 'paths': 2,
        })
    engine = Integrator(store, cursor, project_info=lambda _: {'commit': True})
    engine._save_cursor(accept_id)
    engine._validate_train = lambda _: {}

    def still_busy(batch, accept, **_):
        return engine._record(kind='integrate', project=batch.project, tz=accept['tz'], payload={
            'accept_id': accept['id'], 'error': 'resource busy: build is running', 'paths': 2,
        })

    engine._commit = still_busy
    attempt, = engine.tick()
    assert attempt['payload']['reason_code'] == 'resource_busy'
    assert attempt['payload']['retry']['attempt'] == 1
    restarted = Integrator(store, cursor, project_info=lambda _: {'commit': True})
    assert restarted._pending_accepts() == []
    restarted._retry.clock = lambda: attempt['payload']['retry']['next_at'] + 1
    pending, = restarted._pending_accepts()
    assert pending['id'] == accept_id
    store.close()


def test_actual_dependency_and_candidate_change_wake_only_their_project(lane):
    store,path=lane
    calls=[]
    engine=integrator(store,path,calls)
    for _ in range(3): engine.tick()
    calls.clear()
    with store.tx() as tx:
        tx.record_event(kind='accept',project='P',tz='DEP')
    # Do not consume this synthetic dependency as new work; isolate retry selection.
    with store.read() as tx:
        engine._save_cursor(tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'"))
    engine.tick()
    assert calls and {p for p,_ in calls}=={'P'}
    calls.clear()
    (path/'Q'/'file.txt').write_text('new candidate bytes',encoding='utf-8')
    engine.tick()
    # 05.11: early-wake fingerprints are checked at most every EARLY_WAKE_S per accept.
    assert not any(p=='Q' for p,_ in calls)
    engine._retry._checked.clear()          # EARLY_WAKE_S later
    engine.tick()
    assert any(p=='Q' for p,_ in calls)


def test_project_can_pause_deferred_retries_and_still_integrate_new_accepts(lane):
    store, path = lane
    calls = []
    engine = integrator(store, path, calls)
    engine.project_info = lambda project: {'commit': True, 'retry_deferred': project != 'P'}
    with store.tx() as tx:
        tx.record_event(kind='accept', project='P', tz='FRESH')
        fresh = tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'")

    engine.tick()
    assert ('P', fresh) in calls
    assert [accept_id for project, accept_id in calls if project == 'P'] == [fresh]
    assert any(project == 'Q' for project, _ in calls)

    restarted = integrator(store, path, [])
    restarted.project_info = engine.project_info
    assert all(row['project'] == 'Q' for row in restarted._pending_accepts())


def test_acceptance_output_tail_does_not_reset_backoff(tmp_path):
    from dev_mcp.fleet.integration_retry import RetryPolicy
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    try:
        with store.tx() as tx:
            tx.change('INSERT INTO projects(name,root) VALUES(?,?)',('P',str(tmp_path)),event='test.seed')
        now=[1000.0]
        policy=RetryPolicy(store,lambda *_:'head',clock=lambda:now[0])
        previous={}
        delays=[]
        for i in range(4):
            payload={'reason':f'acceptance: ...pp\android BUILD FAILED in {30+i}s\n2 failed in {80+i}.4s'}
            previous={'retry':policy.schedule('P','TZ-1',payload,previous)}
            delays.append(previous['retry']['delay_s'])
        assert delays==[600,1200,2400,3600]
    finally:
        store.close()


def test_earlier_accepts_of_same_task_are_superseded_once(tmp_path):
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    try:
        (tmp_path/'P').mkdir()
        with store.tx() as tx:
            tx.change('INSERT INTO projects(name,root) VALUES(?,?)',('P',str(tmp_path/'P')),event='test.seed')
            tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('P','fix:A:3','accepted','t','t')",
                      event='test.seed')
            ids=[]
            for _ in range(3):
                tx.record_event(kind='accept',project='P',tz='fix:A:3')
                aid=tx.scalar('SELECT MAX(id) FROM events'); ids.append(aid)
                tx.record_event(kind='integrate',project='P',tz='fix:A:3',
                                payload={'accept_id':aid,'deferred':['f.txt'],'waiting_for':[]})
        calls=[]
        engine=integrator(store,tmp_path,calls)
        engine.tick()
        assert calls==[('P',ids[-1])]
        with store.read() as tx:
            sup=[json.loads(r['payload'])['accept_id'] for r in tx.query(
                "SELECT payload FROM events WHERE kind='integrate' AND json_extract(payload,'$.superseded')=1")]
        assert sorted(sup)==ids[:2]
        calls.clear(); engine.tick()
        assert ('P',ids[0]) not in calls and ('P',ids[1]) not in calls
    finally:
        store.close()


def test_dependency_wait_wakes_on_dependency_state_not_on_head_or_activity(lane):
    # 08.11: ТЗ Nova, ждущие зависимостей, будил каждый коммит ведущего (HEAD) и любой take/submit
    # ожидаемых задач — 1340 полных строгих проверок с 26.10.
    from dev_mcp.fleet.integration_retry import RetryPolicy
    store, _ = lane
    heads = iter(['h1', 'h2', 'h3'])
    policy = RetryPolicy(store, lambda *_: next(heads))
    payload = {'reason': 'source_dependencies', 'waiting_for': ['P/TZ-1']}
    first = policy.fingerprint('P', 'TZ-0', payload, files=False)
    policy._heads.clear()  # новый ход — новый HEAD
    with store.tx() as tx:
        tx.record_event(kind='take', project='P', tz='TZ-1', worker_id='W')
    assert policy.fingerprint('P', 'TZ-0', payload, files=False) == first
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE project='P' AND tz='TZ-1'", (), event='test.seed')
    assert policy.fingerprint('P', 'TZ-0', payload, files=False) != first
