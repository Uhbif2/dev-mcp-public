from types import SimpleNamespace
from pathlib import Path
import json

import pytest

from dev_mcp.fleet import owner_notices
from dev_mcp.fleet.store import Store
from dev_mcp.owner import overview


def notices(store):
    return [line for line in owner_notices.lines(store) if line.startswith('owner-notice: ')]


def test_durable_notice_receipt_is_transport_only_and_resolves_with_actual_action(tmp_path, monkeypatch):
    monkeypatch.setattr("dev_mcp.fleet.windows_notify.gui_session_available",lambda:True)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    data = {"status":"ready","human":{"actions":[{"project":"P","action_id":"a"*20,
            "default":"wait","reason":"private reason","action":"PRIVATE OWNER TEXT"}]}}
    monkeypatch.setattr(overview,"snapshot",lambda *a,**k:data)
    assert owner_notices.reconcile(store) == 1
    line, = notices(store)
    assert "PRIVATE" not in line and "private reason" not in line
    notice = json.loads(line.partition(": ")[2])
    owner_notices.record_transport(store,{"id":notice["id"],"state":"api_accepted"})
    assert notices(store) == []
    owner_notices.reconcile(store)
    assert notices(store) == []  # restart/reprojection cannot repeat acceptance
    with store.read() as tx:
        assert not tx.scalar("SELECT COUNT(*) FROM events WHERE kind IN ('owner.done','intent.ops_approved','thaw')")
    data["human"]["actions"] = []
    owner_notices.reconcile(store)
    assert owner_notices.record_transport(store,{"id":notice["id"],"state":"clicked"}) is False
    data['human']['actions'] = [{'project':'P','action_id':'a'*20,'default':'wait','reason':'private reason'}]
    owner_notices.reconcile(store)
    new = json.loads(notices(store)[0].partition(': ')[2])
    assert new['id'] != notice['id']  # a new episode must survive the browser's old dedup cache


def test_hidden_native_acceptance_migrates_to_browser_attention_once(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    monkeypatch.setattr(overview,"snapshot",lambda *a,**k:{"status":"ready","human":{"actions":[
        {"project":"P","action_id":"a"*20}]}})
    monkeypatch.setattr("dev_mcp.fleet.windows_notify.gui_session_available",lambda:True)
    owner_notices.reconcile(store)
    notice = json.loads(notices(store)[0].partition(": ")[2])
    owner_notices.record_transport(store,{"id":notice["id"],"state":"api_accepted"})
    monkeypatch.setattr("dev_mcp.fleet.windows_notify.gui_session_available",lambda:False)
    owner_notices.reconcile(store)
    assert len(notices(store)) == 1
    owner_notices.record_transport(store,{"id":notice["id"],"state":"browser_attention"})
    owner_notices.reconcile(store)
    assert notices(store) == []


def test_delivery_failure_remains_pending_and_never_accepts_approval(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    monkeypatch.setattr(overview,"snapshot",lambda *a,**k:{"status":"ready","human":{"actions":[
        {"project":"P","action_id":"a"*20}]}})
    owner_notices.reconcile(store)
    notice = json.loads(notices(store)[0].partition(": ")[2])
    owner_notices.record_transport(store,{"id":notice["id"],"state":"denied"})
    assert notices(store)
    with pytest.raises(ValueError):
        owner_notices.record_transport(store,{"id":notice["id"],"state":"approved"})
    monkeypatch.setattr(overview,"snapshot",lambda *a,**k:{"status":"error"})
    owner_notices.reconcile(store)
    assert notices(store)  # a broken read cannot clear an owner request


def test_owner_notice_keeps_supervisor_needed_without_runnable_work(tmp_path):
    from dev_mcp.fleet.supervisor import fleet_demand
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    owner_notices.install(store)
    with store.tx() as tx:
        tx.change("INSERT INTO owner_notices VALUES(?,'needs','source','active','pending','now')",
                  ('on-'+'a'*24,),event='seed')
    demand = fleet_demand(store)
    assert demand.runnable == 0 and demand.active_claims == 0 and demand.unfinished == 0
    assert demand.owner_notices == 1 and demand.needed


def test_delivered_completion_does_not_inflate_required_decision_badge(tmp_path):
    from dev_mcp.fleet.supervisor import fleet_demand
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    owner_notices.install(store)
    with store.tx() as tx:
        tx.change("INSERT INTO owner_notices VALUES(?,'done','source','active','browser_attention','now')",
                  ('on-'+'a'*24,),event='seed')
    head = owner_notices.lines(store)[0]
    assert 'browser_signal=1;' in head
    assert 'attention=0;' in head
    assert fleet_demand(store).owner_notices == 0
    with store.tx() as tx:
        tx.change("UPDATE owner_notices SET kind='needs',delivery_state='clicked'",event='seed')
    assert fleet_demand(store).owner_notices == 1


def test_nonconcrete_proved_ops_plan_queues_one_refinement_without_any_approval(tmp_path):
    from dev_mcp.fleet import outcome
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    (tmp_path/'intent').mkdir()
    (tmp_path/'intent/rollback-plan.md').write_text(
        '## Нужно от владельца\n**Для текущего документа — ничего.** Позже отдельный пилот.\n',encoding='utf-8')
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('P',?,1)",(str(tmp_path),),event='seed')
    plan = outcome.create_outcome(store,project='P',intent_id='i',title='rollback_plan',spec={'stage':'rollback_plan'},
        evidence_spec={'kind':'review','subject_version':'v1'},authority={'test':True})
    approval = outcome.create_outcome(store,project='P',intent_id='i',title='owner_approved',
        spec={'depends_on':[plan.id]},evidence_spec={'subject_version':'v1'},authority={'test':True})
    outcome.open_obstacle(store,outcome_id=approval.id,type='owner_action',cause_code='owner_money',
        predicate={},owner_action='Одобрить операционный план с откатом')
    assert owner_notices.prepare_ops_requests(store) == 0
    outcome.add_evidence(store,writer=outcome.server_evidence_writer(store),outcome_id=plan.id,
        kind='review',subject_version='v1',env_version='test',verdict='pass',ref='test',producer='test')
    outcome.close_outcome(store,plan.id)
    assert owner_notices.prepare_ops_requests(store) == 1
    assert owner_notices.prepare_ops_requests(store) == 0
    with store.tx() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='P'") == 1
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P'",event='seed')
    assert owner_notices.prepare_ops_requests(store) == 0
    with store.read() as tx:
        assert not tx.scalar("SELECT COUNT(*) FROM events WHERE kind IN ('owner.done','intent.ops_approved')")


def test_attention_projection_does_not_starve_ninth_current_exception(tmp_path):
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    from datetime import datetime,timezone
    from dev_mcp.fleet.watchdog import watchdog_tick,Signals
    watchdog_tick(store,now=datetime.now(timezone.utc),signals=Signals())
    with store.tx() as tx:
        for i in range(10):
            tx.change("INSERT INTO watchdog_attention VALUES(?,0,?,'active')",(str(i),"issue "+str(i)),event="seed")
    assert owner_notices.reconcile(store) == 10
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM owner_notices WHERE state='active'") == 10


def test_unreviewed_ops_refinement_cannot_publish_owner_approval_request(tmp_path):
    from dev_mcp.fleet import outcome
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    (tmp_path/'intent').mkdir()
    (tmp_path/'intent/rollback-plan.md').write_text('## Нужно от владельца\nОдобрить конкретный пилот.\n',encoding='utf-8')
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('P',?,1)",(str(tmp_path),),event='seed')
    obj = outcome.create_outcome(store,project='P',intent_id='i',title='owner_approved',
        spec={'stage':'owner_approved','kind':'ops','source_id':'source'},
        evidence_spec={'subject_version':'v1'},authority={'test':True})
    outcome.open_obstacle(store,outcome_id=obj.id,type='owner_action',cause_code='owner_money',
        predicate={},owner_action='Одобрить операционный план с откатом')
    assert len(overview.snapshot(store)['human']['actions']) == 1
    with store.tx() as tx:
        tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('P',?,'submitted','now','now')",
                  (owner_notices.ops_refinement_id(obj.id),),event='seed')
    assert overview.snapshot(store)['human']['actions'] == []
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted'",event='seed')
    assert len(overview.snapshot(store)['human']['actions']) == 1


def test_watchdog_exhaustion_reaches_browser_outbox_and_clears_on_recovery(tmp_path):
    from datetime import datetime,timezone,timedelta
    from dev_mcp.fleet.watchdog import watchdog_tick,Signals
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    now = datetime.now(timezone.utc)
    watchdog_tick(store,now=now,signals=Signals(runnable=1,extension_enabled=False))
    owner_notices.reconcile(store)
    packet = json.loads(notices(store)[0].partition(': ')[2])
    assert packet['kind'] == 'attention'
    watchdog_tick(store,now=now+timedelta(minutes=1),signals=Signals(runnable=0,extension_enabled=True))
    owner_notices.reconcile(store)
    assert notices(store) == []
    with store.read() as tx:
        assert not tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.done'")


def test_needs_notice_becomes_one_ticktick_delivery_task_and_freezes_when_resolved(tmp_path, monkeypatch):
    # Owner 19.10.2026: TickTick only through the official connector of the ChatGPT fleet agent.
    monkeypatch.setattr("dev_mcp.fleet.windows_notify.gui_session_available", lambda: True)
    root = tmp_path / 'hub'
    root.mkdir()
    monkeypatch.setattr(owner_notices, 'TICKTICK_RECEIPTS', root / 'owner-ticktick')
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES(?,?,1)",
                  (owner_notices.TICKTICK_PROJECT, str(root)), event='seed')
    data = {"status": "ready", "human": {"actions": [{"project": "P", "title": "Проект П",
            "action_id": "b" * 20, "default": "ждать", "reason": "нужно решение",
            "action": "Одобрить выпуск"}]}}
    monkeypatch.setattr(overview, "snapshot", lambda *a, **k: data)
    owner_notices.reconcile(store)
    owner_notices.reconcile(store)  # idempotent: one task per notice
    with store.read() as tx:
        rows = tx.query("SELECT tz,state,acceptance,body FROM tasks WHERE project=?",
                        (owner_notices.TICKTICK_PROJECT,))
    assert len(rows) == 1
    task = rows[0]
    assert task['state'] == 'ready'
    assert 'Одобрить выпуск' in task['body'] and 'create_task' in task['body']
    assert 'ticktick_receipt.py' in json.loads(task['acceptance'])[0]
    data["human"]["actions"] = []
    owner_notices.reconcile(store)
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project=? AND tz=?",
                         (owner_notices.TICKTICK_PROJECT, task['tz'])) == 'frozen'


def test_ticktick_receipt_checker_rejects_invented_ids(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('ttr',
        Path(__file__).resolve().parents[2] / 'src/dev_mcp/owner/ticktick_receipt.py')
    ttr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ttr)
    ident = 'on-' + 'a' * 24
    path = tmp_path / f'{ident}.json'
    path.write_text(json.dumps({'notice': ident, 'ticktick_task_id': 'fake-1',
                                'ticktick_project_id': 'inbox123456', 'title': 't'}), encoding='utf-8')
    assert ttr.check(path)
    path.write_text(json.dumps({'notice': ident, 'ticktick_task_id': '6713a1b2c3d4e5f6a7b8c9d0',
                                'ticktick_project_id': 'inbox123456', 'title': 't'}), encoding='utf-8')
    assert ttr.check(path) == []
