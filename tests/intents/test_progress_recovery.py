"""Legacy self-wait recovery consumes new independent plan facts, never timers."""
import json

import pytest

from dev_mcp.fleet.store import Store
from dev_mcp.intents.progress_recovery import resume_implementation_plans


def fixture(tmp_path, *, newer=True, manual=False, accepted=True, independent=True):
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    from dev_mcp.fleet.outcome import install_schema
    install_schema(store)
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('P',?,1)", (str(tmp_path),), event='seed')
        tx.change("INSERT INTO outcomes(id,project,title,intent_id,spec,evidence_spec,authority,state,rev,created_ts) "
                  "VALUES('intent-x:implemented','P','implemented','intent-x','{}','{}','{}','open',1,'2026-10-15')", event='seed')
        tx.change("INSERT INTO tasks(project,tz,state,body,acceptance,created_at,updated_at) "
                  "VALUES('P','intent-x-implemented','frozen',?,?,?,?)",
                  ('[intent-template] outcome=intent-x:implemented\nStage',
                   json.dumps(['py -3 .intent/check.py implemented']), '2026-10-15', '2026-10-15'), event='seed')
        tx.change("INSERT INTO tasks(project,tz,kind,state,spec_path,author_worker,created_at,updated_at) "
                  "VALUES('P','TZ-1','work',?,'ToDo/tz/TZ-1.md','author','2026-10-15','2026-10-15')",
                  ('accepted' if accepted else 'submitted',), event='seed')
        def proof():
            tx.record_event(kind='accept', project='P', tz='TZ-1', worker_id='reviewer' if independent else 'author',
                            payload={'review_task': 'review:TZ-1'})
        if not newer:
            proof()
        tx.record_event(kind='freeze', project='P', tz='intent-x-implemented',
                        worker_id=None if manual else 'stage-author',
                        payload={'reason': 'blocked: evidence missing\nuntil: py -3 .intent/check.py implemented'})
        if newer:
            proof()
    return store


def test_new_independent_plan_accept_retries_stage_once_without_accepting_it(tmp_path):
    store = fixture(tmp_path)
    assert resume_implementation_plans(store) == ['P/intent-x-implemented']
    assert resume_implementation_plans(store) == []
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='intent-x-implemented'") == 'ready'
        assert tx.scalar("SELECT state FROM outcomes WHERE id='intent-x:implemented'") == 'open'
        assert tx.scalar("SELECT count(*) FROM events WHERE kind='intent.implementation_dependencies_ready'") == 1
        assert tx.scalar("SELECT count(*) FROM events WHERE kind='accept' AND tz='intent-x-implemented'") == 0


@pytest.mark.parametrize('condition', ['manual', 'unchanged', 'not_accepted', 'self_review', 'cancelled'])
def test_ineligible_wait_is_preserved(tmp_path, condition):
    store = fixture(tmp_path, manual=condition == 'manual', newer=condition != 'unchanged',
                    accepted=condition != 'not_accepted', independent=condition != 'self_review')
    if condition == 'cancelled':
        with store.tx() as tx:
            tx.change("UPDATE projects SET active=0", event='cancel')
    assert resume_implementation_plans(store) == []
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='intent-x-implemented'") == 'frozen'
