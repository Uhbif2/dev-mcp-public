"""A blocked reactivation scan must leave unrelated SQLite writers available."""
import threading
from datetime import datetime,timezone

from dev_mcp.fleet import queue,deploy_events
from dev_mcp.fleet.store import Store


def test_read_planning_does_not_hold_the_registry_writer(tmp_path,monkeypatch):
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    stamp=datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)",(str(tmp_path),),event='test.project')
        tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('P','TZ-1','frozen',?,?)",
                  (stamp,stamp),event='test.task')
        tx.record_event(kind='block',project='P',tz='TZ-1',payload={
            'origin':'fleet_auto','gate':'operational','reactivate_policy':{'kind':'registered_probe'}})
    monkeypatch.setattr(deploy_events,'tick',lambda _: {})
    planning=threading.Event();written=threading.Event();observed=[]
    original=queue._verified_reactivation
    def slow_reader(tx,*args):
        planning.set()
        observed.append(written.wait(1))
        return original(tx,*args)
    monkeypatch.setattr(queue,'_verified_reactivation',slow_reader)
    def independent_writer():
        assert planning.wait(3)
        with store.tx() as tx:
            tx.record_event(kind='independent_work_progress')
        written.set()
    writer=threading.Thread(target=independent_writer)
    writer.start()
    try:
        assert queue.thaw_frozen(store,after_min=0)==[]
        writer.join(timeout=3)
        assert observed==[True]
        assert not writer.is_alive()
    finally:
        planning.set()
        writer.join(timeout=3)
        store.close()


def test_dependency_reopen_between_read_plan_and_commit_cancels_transition(tmp_path):
    from dev_mcp.fleet.reactivation_plan import ThawPlanner
    store=Store.open(str(tmp_path/'fleet.sqlite'))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)",(str(tmp_path),),event='test.project')
        for name,state in [('consumer','frozen'),('supplier','accepted')]:
            tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('P',?,?, 't','t')",
                      (name,state),event='test.task')
    with store.read() as reader:
        plan=ThawPlanner(reader)
        plan.begin_subject('P','consumer')
        plan.one("SELECT state FROM tasks WHERE project='P' AND tz='supplier'")
        plan.change("UPDATE tasks SET state='ready' WHERE project='P' AND tz='consumer'",event={
            'kind':'thaw','project':'P','tz':'consumer'})
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE tz='supplier'",event='supplier.reopened')
    assert plan.apply(store,lambda *_:True)==set()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='consumer'")=='frozen'
    store.close()


def test_multiple_legacy_blocks_reuse_one_project_state_index(tmp_path, monkeypatch):
    """A project with thousands of tasks must not be reindexed per frozen block."""
    from dev_mcp.fleet.reactivation_plan import ThawPlanner

    store = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)",
                  (str(tmp_path),), event="test.project")
        for tz, state in (("TZ-100", "accepted"), ("TZ-101", "frozen"), ("TZ-102", "frozen")):
            tx.change(
                "INSERT INTO tasks(project,tz,state,needs,created_at,updated_at) "
                "VALUES('P',?,?,'[\"TZ-100\"]',?,?)",
                (tz, state, now, now), event="test.task",
            )
            if state == "frozen":
                tx.record_event(kind="block", project="P", tz=tz,
                                payload={"reason": "waiting for TZ-100"})
    monkeypatch.setattr(deploy_events, "tick", lambda *_args: {})
    seen = []
    original = ThawPlanner.project_tasks

    def observe(self, project):
        seen.append(project)
        return original(self, project)

    monkeypatch.setattr(ThawPlanner, "project_tasks", observe)
    try:
        assert queue.thaw_frozen(store, after_min=0) == []
        assert seen == ["P"]
    finally:
        store.close()
