"""Regression tests for autonomous red-trunk recheck (TZ-33)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from dev_mcp.fleet.integrate import Batch, Integrator, STYK_TITLE
from dev_mcp.fleet import queue
from dev_mcp.fleet.regate import Regater, project_gate_lock
from dev_mcp.fleet.store import Store


def stamp(offset_s=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


@pytest.fixture()
def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "tests").mkdir()
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("P", str(root)), event={"kind":"setup"})
    return store, root, tmp_path


def task(store, tz, kind, state, paths, title="task"):
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P",tz,title,state,5,kind,"","","[]",json.dumps(paths),
             "[]","[]",None,"test",stamp(),stamp()), event={"kind":"setup"},
        )


def red(setup, *, errors=1, age_s=100):
    store, root, tmp = setup
    paths = []
    for i in range(errors):
        path = root / "tests" / f"test_{i}.py"
        path.write_text(f"value={i}\n", encoding="utf-8")
        os.utime(path, (time.time()-400, time.time()-400))
        paths.append(f"tests/test_{i}.py")
    old = stamp(-age_s)
    with store.tx() as tx:
        tx.record_event(kind="gate",project="P",tz="TZ-OLD",rc=1,
                        ts=old,payload={"failed":paths,"summary":"collection errors"})
    rootn = str(root).replace("\\","/").casefold()
    owns = [f"{rootn}/{p}" for p in paths]
    task(store,"styk:1","work","ready",owns,STYK_TITLE+": last")
    with store.tx() as tx:
        tx.record_event(kind="styk_open",project="P",tz="styk:1",ts=old)
    task(store,"review:TZ-NEW","review","ready",owns)
    return paths


def runner_factory(text, rc=1):
    calls = []
    def runner(argv,**kw):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv,rc,text,"")
    return runner,calls


def regater(setup, stdout, rc=1):
    store,root,tmp = setup
    runner,calls = runner_factory(stdout,rc)
    integ=Integrator(store,tmp/"integrate-cursor.json",runner=runner,
                     project_info=lambda _p:{"gate":"fleet-test-gate"})
    return Regater(store,integ),calls


def test_red_changed_files_triggers_regate_and_replaces_obsolete_zone(setup):
    paths = red(setup,errors=18)
    store,root,_=setup
    changed=root/paths[0]
    os.utime(changed,(time.time()+3,time.time()+3))
    retry,calls=regater(setup,"FAILED tests/test_0.py::test_x - assertion",1)
    outcome=retry.tick()
    assert len(outcome)==1 and outcome[0]["rc"]==1
    assert calls==[("fleet-test-gate",)]
    with store.read() as tx:
        zone,_=queue.red_zone(tx,"P")
        attempts=tx.scalar("SELECT COUNT(*) FROM events WHERE kind='regate_attempt'")
        narrow=json.loads(tx.scalar("SELECT owns FROM tasks WHERE tz='styk:1'"))
    assert attempts==1
    assert narrow==[str(changed).replace("\\","/").casefold()]
    assert len(set(zone))==1,zone
    with store.read() as tx:
        assert queue.red_overlap(tx, "P", "TZ-NEW", [str(root/paths[1])]) == ""
        assert queue.red_overlap(tx, "P", "TZ-NEW", [str(changed)]) != ""
    # Still red, but only the truly failing test remains blocked.
    assert retry.tick()==[] and calls==[("fleet-test-gate",)]


def test_red_unchanged_and_recent_does_not_trigger(setup):
    red(setup,errors=2,age_s=60)
    retry,calls=regater(setup,"FAILED tests/test_0.py::x",1)
    assert retry.tick()==[] and calls==[]


def test_red_age_20_minutes_triggers_even_when_unchanged(setup):
    red(setup,age_s=1201)
    retry,calls=regater(setup,"FAILED tests/test_0.py::x",1)
    assert len(retry.tick())==1
    assert calls==[("fleet-test-gate",)]


def test_no_pending_review_or_submit_never_rechecks(setup):
    red(setup,age_s=2000)
    store,root,_=setup
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='review:TZ-NEW'",event={"kind":"setup"})
    retry,calls=regater(setup,"FAILED tests/test_0.py::x",1)
    assert retry.tick()==[] and calls==[]


def test_green_recheck_lifts_styk_and_clears_red_zone(setup):
    red(setup,errors=18,age_s=1201)
    store,root,_=setup
    retry,calls=regater(setup,"18 passed",0)
    assert len(retry.tick())==1
    with store.read() as tx:
        zone,_=queue.red_zone(tx,"P")
        state=tx.scalar("SELECT state FROM tasks WHERE tz='styk:1'")
    assert zone==[] and state=="accepted"
    assert calls==[("fleet-test-gate",)]


def test_existing_integrator_gate_lock_blocks_concurrent_regate(setup):
    red(setup,age_s=1201)
    store,root,_=setup
    retry,calls=regater(setup,"FAILED tests/test_0.py::x",1)
    holding=threading.Event()
    release=threading.Event()
    def owner():
        with project_gate_lock("P") as acquired:
            assert acquired
            holding.set()
            assert release.wait(3)
    thread=threading.Thread(target=owner)
    thread.start()
    assert holding.wait(2)
    try:
        assert retry.tick()==[] and calls==[]
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(retry.tick())==1


def test_real_integrator_gate_excludes_concurrent_regate(setup):
    red(setup, age_s=1201)
    store, root, tmp = setup
    running = threading.Event()
    release = threading.Event()
    failures = []
    called = []

    def slow_runner(argv, **kwargs):
        called.append(tuple(argv))
        running.set()
        assert release.wait(3), "gate runner was not released"
        return subprocess.CompletedProcess(argv, 1, "FAILED tests/test_0.py::x", "")

    integrator = Integrator(
        store, tmp / "integrate-cursor.json", runner=slow_runner,
        project_info=lambda _p: {"gate": "fleet-test-gate"},
    )

    def normal_gate():
        try:
            integrator._gate(Batch("P", str(root)))
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=normal_gate)
    thread.start()
    assert running.wait(2)
    try:
        assert Regater(store, integrator).tick() == []
        assert called == [("fleet-test-gate",)]
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert not failures


def test_production_controller_invokes_regater_tick(setup, monkeypatch):
    from dev_mcp.fleet import controller as controller_module

    _, _, tmp = setup
    stop = threading.Event()
    fired = threading.Event()
    monkeypatch.setattr(Regater, "tick", lambda self: fired.set() or [])
    monkeypatch.setattr(controller_module, "run_daily_drill_scheduler", lambda _stop: None)
    monkeypatch.setattr(controller_module, "run_cross_review_worker",
                        lambda _store, _stop: None)

    def finish_tick(_self):
        found = fired.wait(2)
        stop.set()
        assert found, "production controller never started regate maintenance"

    monkeypatch.setattr(controller_module.Controller, "tick", finish_tick)
    controller_module.run_controller(tmp / "fleet.sqlite", stop, interval_s=0)
    assert fired.is_set()


def test_collection_five_files_classified_and_zone_narrowed(setup):
    red(setup,errors=18,age_s=1201)
    store,root,_=setup
    output="\n".join(f"ERROR collecting tests/test_{i}.py" for i in range(5))
    retry,calls=regater(setup,output,1)
    assert len(retry.tick())==1
    with store.read() as tx:
        row=tx.one("SELECT payload FROM events WHERE kind='gate_collect_error'")
        narrow=json.loads(tx.scalar("SELECT owns FROM tasks WHERE tz='styk:1'"))
    assert row is not None
    assert len(json.loads(row["payload"])["failed"])==5
    assert len(narrow)==5
    assert len(calls)==1


def test_per_project_lock_nonblocking_and_reentrant():
    with project_gate_lock("P") as acquired:
        assert acquired
        with project_gate_lock("P",blocking=False) as nested:
            assert nested
    with pytest.raises(ValueError):
        with project_gate_lock(""):
            pass
