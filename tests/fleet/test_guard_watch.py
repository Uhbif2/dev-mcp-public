"""Synthetic controller guard watchdog: no pythonw or live Fleet registry."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

from dev_mcp.fleet.guard_watch import guard_watch_tick
from dev_mcp.fleet.store import Store


def _setup(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES (?,?,?,1)",
                  ("Fleet Platform", str(root), 1), event={"kind": "project_seed"})
    return store, live


def _event_rows(store, kind):
    with store.read() as tx:
        return list(tx.query("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)))


def test_live_recent_heartbeat_does_not_spawn(tmp_path):
    store, live = _setup(tmp_path)
    now = datetime.now(timezone.utc)
    path = live / "data/guard-heartbeat.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": os.getpid(), "phase": "canary",
                                "candidate": "abc", "time": now.isoformat()}))
    called = []
    assert guard_watch_tick(store, live, now=now, alive=lambda pid: pid == os.getpid(),
                            start=lambda script: called.append(script) or 444) == "healthy"
    assert called == [] and not _event_rows(store, "guard_restarted")


def test_dead_or_stale_guard_restarts_once_and_preserves_grace(tmp_path):
    store, live = _setup(tmp_path)
    now = datetime.now(timezone.utc)
    path = live / "data/guard-heartbeat.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 9001, "phase": "watch", "candidate": "",
                                "time": (now - timedelta(minutes=10)).isoformat()}))
    starts = []
    def spawn(script):
        starts.append(script)
        return 12345
    assert guard_watch_tick(store, live, now=now, start=spawn,
                            alive=lambda pid: pid == 12345) == "restarted"
    assert guard_watch_tick(store, live, now=now + timedelta(seconds=20),
                            start=spawn, alive=lambda pid: pid == 12345) == "starting"
    assert len(starts) == 1
    assert starts[0] == live / "data/guard-runtime/run_guard.pyw"
    assert len(_event_rows(store, "guard_restarted")) == 1


def test_three_restarts_in_hour_create_single_fix_then_block(tmp_path):
    store, live = _setup(tmp_path)
    now = datetime.now(timezone.utc)
    path = live / "data/guard-heartbeat.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 9001, "phase": "watch", "candidate": "",
                                "time": (now - timedelta(minutes=10)).isoformat()}))
    started = []
    for i in range(3):
        answer = guard_watch_tick(
            store, live, now=now + timedelta(seconds=80 * i),
            alive=lambda _pid: False,
            start=lambda script: started.append(script) or 100 + i,
        )
        assert answer == ("blocked" if i == 2 else "restarted")
    assert len(started) == 3
    assert guard_watch_tick(store, live, now=now + timedelta(seconds=250),
                            alive=lambda _pid: False,
                            start=lambda script: started.append(script) or 999) == "blocked"
    assert len(started) == 3
    assert len(_event_rows(store, "guard_restarted")) == 3
    assert len(_event_rows(store, "guard_watch_blocked")) == 1
    with store.read() as tx:
        tasks = list(tx.query("SELECT tz,kind,state FROM tasks WHERE project='Fleet Platform'"))
    assert len(tasks) == 1 and tasks[0]["kind"] == "fix" and tasks[0]["state"] == "ready"


def test_failed_spawn_records_event_and_does_not_claim_restart(tmp_path):
    store, live = _setup(tmp_path)
    path = live / "data/guard-heartbeat.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 9001, "phase": "watch", "candidate": "",
                                "time": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}))
    def refuse(_script):
        raise FileNotFoundError("missing")
    assert guard_watch_tick(store, live, now=datetime.now(timezone.utc),
                            start=refuse) == "launch_error"
    assert len(_event_rows(store, "guard_watch_error")) == 1
    assert not _event_rows(store, "guard_restarted")


def test_stale_heartbeat_from_still_alive_process_never_spawns_duplicate(tmp_path):
    store, live = _setup(tmp_path)
    now = datetime.now(timezone.utc)
    path = live / "data/guard-heartbeat.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 9911, "phase": "canary",
                                "candidate": "xyz",
                                "time": (now - timedelta(minutes=9)).isoformat()}))
    started = []
    result = guard_watch_tick(store, live, now=now,
                              alive=lambda pid: pid == 9911,
                              start=lambda script: started.append(script) or 9912)
    assert result == "stale_alive"
    assert not started
    assert len(_event_rows(store, "guard_watch_stale_alive")) == 1
    assert len(_event_rows(store, "guard_watch_blocked")) == 1


def test_missing_heartbeat_without_known_pid_never_bootstraps_second_guard(tmp_path):
    store, live = _setup(tmp_path)
    started = []
    result = guard_watch_tick(store, live, now=datetime.now(timezone.utc),
                              alive=lambda _pid: False,
                              start=lambda script: started.append(script) or 9002)
    assert result == "unknown"
    assert not started
    assert len(_event_rows(store, "guard_watch_blocked")) == 1


def test_missing_pid_obstacle_is_stable_across_hours_and_rearms_after_recovery(tmp_path):
    store, live = _setup(tmp_path)
    at = datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc)
    spawns = []
    for hour in range(3):
        assert guard_watch_tick(store, live, now=at + timedelta(hours=hour),
                                alive=lambda _pid: False,
                                start=lambda script: spawns.append(script) or 3000) == "unknown"
    assert not spawns
    with store.read() as tx:
        tasks = list(tx.query("SELECT tz,body FROM tasks WHERE author_worker='guard_watch'"))
    assert len(tasks) == 1
    assert "reason=guard_pid_unproven" in tasks[0]["body"]
    assert len(_event_rows(store, "guard_watch_blocked")) == 1

    # One healthy, confirmed watcher is a new fact; a subsequent loss may
    # open one NEW incident, but never a new task on every clock-hour change.
    pulse = live / "data/guard-heartbeat.json"
    pulse.parent.mkdir(parents=True, exist_ok=True)
    recovered = at + timedelta(hours=3)
    pulse.write_text(json.dumps({"pid": 321, "phase": "watch", "candidate": "abc",
                                 "time": recovered.isoformat()}))
    assert guard_watch_tick(store, live, now=recovered,
                            alive=lambda pid: pid == 321) == "healthy"
    assert guard_watch_tick(store, live, now=recovered + timedelta(minutes=1),
                            alive=lambda pid: pid == 321) == "healthy"
    assert len(_event_rows(store, "guard_watch_recovered")) == 1

    pulse.unlink()
    for hour in (4, 5):
        assert guard_watch_tick(store, live, now=at + timedelta(hours=hour),
                                alive=lambda _pid: False) == "unknown"
    with store.read() as tx:
        after = list(tx.query("SELECT tz FROM tasks WHERE author_worker='guard_watch'"))
    assert len(after) == 2
    assert after[0]["tz"] != after[1]["tz"]
    assert len(_event_rows(store, "guard_watch_blocked")) == 2


def test_controller_runtime_wires_watchdog_only_for_installed_database(tmp_path, monkeypatch):
    from dev_mcp.fleet import controller as module

    monkeypatch.setattr(module, "GUARD_DISABLED", False)  # проверяется проводка, а не решение 0025
    live = tmp_path / "live"
    live.mkdir()
    database = live / "data/fleet/fleet.sqlite"
    database.parent.mkdir(parents=True)
    store = Store.open(str(database))
    seen = []
    monkeypatch.setattr(module, "_GUARD_LIVE_ROOT", live)
    monkeypatch.setattr(module, "guard_watch_tick",
                        lambda current, root: seen.append((current, root)) or "healthy")
    assert module._controller_guard_watch_step(store, tmp_path / "foreign.sqlite") == "disabled"
    assert seen == []

    stopper = threading.Event()
    class OneTick:
        def __init__(self, *_args, **_kwargs):
            pass
        def route_status(self):
            return {"delivery": "unprovisioned"}
        def tick(self):
            stopper.set()
            return 0
    class NoThread:
        def __init__(self, *_args, **_kwargs):
            pass
        def start(self):
            pass
    monkeypatch.setattr(module, "Controller", OneTick)
    monkeypatch.setattr(module.threading, "Thread", NoThread)
    module.run_controller(database, stopper, interval_s=0)
    assert len(seen) == 1 and seen[0][1] == live


def test_guard_watch_is_off_by_owner_decision(tmp_path):
    from dev_mcp.fleet import controller as module
    assert module.GUARD_DISABLED is True
    assert module._controller_guard_watch_step(object(), tmp_path / "x.sqlite") == "disabled"
