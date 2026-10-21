"""request:a7b66d4d: real maintenance path, temporary SQLite and durable receipts."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dev_mcp.fleet import queue, deploy_events
from test_queue import StubStore, add_project, add_task, add_worker


@pytest.fixture
def lane(tmp_path, monkeypatch):
    store = StubStore.open(tmp_path / "reactivation.sqlite")
    monkeypatch.setattr(deploy_events, "tick", lambda _: {})
    add_project(store, "P")
    for worker in ("W-author", "W-review", "W-block"):
        add_worker(store, worker)
    add_task(store, "TZ-DEP", state="ready", owns=("/repo/dependency.txt",))
    add_task(store, "TZ-BLOCK", state="taken", needs=("TZ-DEP",),
             owns=("/repo/blocked.txt",))
    with store.tx() as tx:
        tx.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P','TZ-BLOCK','W-block','active',?)",
            (datetime.now(timezone.utc).isoformat(),))
    return store


def state(store):
    with store.read() as tx:
        return str(tx.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-BLOCK'")["state"])


def last_block(store):
    with store.read() as tx:
        row = tx.one("SELECT id,payload FROM events WHERE kind='block' "
                     "AND project='P' AND tz='TZ-BLOCK' ORDER BY id DESC LIMIT 1")
        return int(row["id"]), json.loads(row["payload"])


def test_dependency_acceptance_unblocks_once_via_real_thaw(lane):
    queue.block(lane, "W-block", "blocked: dependency still pending")
    block_id, payload = last_block(lane)
    assert payload["origin"] == "fleet_auto"
    assert payload["gate"] == "dependency"
    assert payload["reactivate_policy"]["target"] == "P/TZ-DEP"
    assert queue.thaw_frozen(lane, after_min=0) == []
    with lane.tx() as tx:
        tx.execute("UPDATE tasks SET state='accepted',author_worker='W-author' "
                   "WHERE project='P' AND tz='TZ-DEP'")
        tx.record_event(kind="accept", project="P", tz="TZ-DEP", worker_id="W-review")
    resumed = queue.thaw_frozen(lane, after_min=999)
    assert resumed == ["P/TZ-BLOCK: verified independent evidence"]
    assert state(lane) == "ready"
    assert queue.thaw_frozen(lane, after_min=0) == []
    with lane.read() as tx:
        records = tx.query("SELECT payload FROM events WHERE kind='auto_reactivate' "
                           "AND project='P' AND tz='TZ-BLOCK'")
        assert len(records) == 1
        receipt = json.loads(records[0]["payload"])
        assert receipt["blocked_event_id"] == block_id
        assert receipt["predicate"]["kind"] == "accepted_work"
        assert len(receipt["evidence_refs"]) == 1


def test_nonindependent_or_obsolete_evidence_fails_closed(lane):
    queue.block(lane, "W-block", "blocked: dependency still pending")
    with lane.tx() as tx:
        tx.execute("UPDATE tasks SET state='accepted',author_worker='W-author' "
                   "WHERE project='P' AND tz='TZ-DEP'")
        tx.record_event(kind="accept", project="P", tz="TZ-DEP", worker_id="W-author")
    assert queue.thaw_frozen(lane, after_min=0) == []
    assert state(lane) == "frozen"
    with lane.tx() as tx:
        tx.record_event(kind="freeze", project="P", tz="TZ-BLOCK",
                        payload={"origin": "owner", "gate": "manual"})
        tx.record_event(kind="accept", project="P", tz="TZ-DEP", worker_id="W-review")
    assert queue.thaw_frozen(lane, after_min=0) == []
    assert state(lane) == "frozen"


def test_probe_receipt_verified_and_old_receipt_not_accepted(tmp_path, monkeypatch):
    store = StubStore.open(tmp_path / "probe.sqlite")
    monkeypatch.setattr(deploy_events, "tick", lambda _: {})
    add_project(store, "P")
    add_worker(store, "W-block")
    add_task(store, "TZ-BLOCK", state="taken", owns=("/repo/blocked.txt",))
    with store.tx() as tx:
        tx.execute("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                   "VALUES('P','TZ-BLOCK','W-block','active',?)",
                   (datetime.now(timezone.utc).isoformat(),))
    queue.block(store, "W-block", "blocked: unavailable. until: python -V")
    block_id, payload = last_block(store)
    assert payload["reactivate_policy"]["kind"] == "registered_probe"
    with store.tx() as tx:
        tx.record_event(kind="block_probe", project="P", tz="TZ-BLOCK",
                        payload={"argv": ["python", "-V"]})
        tx.record_event(kind="probe_run", project="P", tz="TZ-BLOCK",
                        payload={"rc": 1})
    assert queue.thaw_frozen(store, after_min=0) == []
    with store.tx() as tx:
        tx.record_event(kind="probe_run", project="P", tz="TZ-BLOCK",
                        payload={"rc": 0})
    assert queue.thaw_frozen(store, after_min=999) == [
        "P/TZ-BLOCK: verified independent evidence"]
    assert state(store) == "ready"
    with store.read() as tx:
        receipt = tx.one("SELECT payload FROM events WHERE kind='auto_reactivate'")
        assert json.loads(receipt["payload"])["blocked_event_id"] == block_id


def test_manual_freeze_remains_frozen_without_trusted_block(tmp_path, monkeypatch):
    store = StubStore.open(tmp_path / "manual.sqlite")
    monkeypatch.setattr(deploy_events, "tick", lambda _: {})
    add_project(store, "P")
    add_task(store, "TZ-BLOCK", state="frozen")
    with store.tx() as tx:
        tx.record_event(kind="freeze", project="P", tz="TZ-BLOCK",
                        payload={"origin": "owner", "gate": "manual"})
    assert queue.thaw_frozen(store, after_min=0) == []
    assert state(store) == "frozen"


def test_guard_deployment_receipt_uses_policy_not_legacy_thaw(tmp_path, monkeypatch):
    from pathlib import Path

    candidate = "a" * 40
    store = StubStore.open(tmp_path / "deployment.sqlite")
    add_project(store, "P")
    add_worker(store, "W-block")
    add_task(store, "TZ-BLOCK", state="taken")
    with store.tx() as tx:
        tx.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P','TZ-BLOCK','W-block','active',?)",
            (datetime.now(timezone.utc).isoformat(),))
    queue.block(store, "W-block",
                f"blocked: change_source=platform_deploy commit={candidate}")
    block_id, payload = last_block(store)
    assert payload["reactivate_policy"]["kind"] == "platform_deploy_commit"
    base = datetime.now(timezone.utc) - timedelta(minutes=3)
    # The block really predates the deployment in this disposable registry.
    with store.tx() as tx:
        tx.execute("UPDATE events SET ts=? WHERE id=?", (base.isoformat(), block_id))
    journal = tmp_path / "guard" / "platform-guard.jsonl"
    journal.parent.mkdir(parents=True)
    deployed = base + timedelta(minutes=1)
    journal.write_text(
        json.dumps({"code": "known_good", "stage": "after",
                    "candidate": candidate, "ts": deployed.isoformat()}) + "\n"
        + json.dumps({"code": "deployed", "ts": (deployed + timedelta(seconds=1)).isoformat()})
        + "\n", encoding="utf-8")
    original_tick = deploy_events.tick
    monkeypatch.setattr(
        deploy_events, "tick", lambda s: original_tick(s, journal))
    assert queue.thaw_frozen(store, after_min=999) == [
        "P/TZ-BLOCK: verified independent evidence"]
    assert state(store) == "ready"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='platform_deployed'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='thaw'") == 0
        records = tx.query("SELECT payload FROM events WHERE kind='auto_reactivate'")
        assert len(records) == 1
        assert json.loads(records[0]["payload"])["evidence_refs"]


def test_latest_rollback_invalidates_previous_guard_evidence(tmp_path, monkeypatch):
    store = StubStore.open(tmp_path / "rollback.sqlite")
    monkeypatch.setattr(deploy_events, "tick", lambda _: {})
    add_project(store, "P")
    add_task(store, "TZ-BLOCK", state="frozen")
    candidate = "b" * 40
    with store.tx() as tx:
        tx.record_event(kind="block", project="P", tz="TZ-BLOCK",
                        payload={"origin": "fleet_auto", "gate": "operational",
                                 "reactivate_policy": {
                                     "kind": "platform_deploy_commit", "target": candidate,
                                     "authority": "platform_guard"}})
        tx.record_event(kind="platform_deployed",
                        payload={"candidate": candidate, "known_good": candidate,
                                 "ts": datetime.now(timezone.utc).isoformat()})
        tx.record_event(kind="platform_rolled_back", payload={"success": True})
    assert queue.thaw_frozen(store, after_min=0) == []
    assert state(store) == "frozen"


def test_proof_stale_after_five_minutes_cannot_thaw(tmp_path, monkeypatch):
    store = StubStore.open(tmp_path / "stale.sqlite")
    monkeypatch.setattr(deploy_events, "tick", lambda _: {})
    add_project(store, "P")
    add_task(store, "TZ-BLOCK", state="frozen")
    earlier = datetime.now(timezone.utc) - timedelta(minutes=30)
    with store.tx() as tx:
        tx.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            (earlier.isoformat(), "block", "P", "TZ-BLOCK", json.dumps({
                "origin": "fleet_auto", "gate": "operational",
                "reactivate_policy": {"kind": "registered_probe"}})))
        tx.record_event(kind="block_probe", project="P", tz="TZ-BLOCK",
                        payload={"argv": ["python", "-V"]})
        tx.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            ((earlier + timedelta(minutes=10)).isoformat(), "probe_run",
             "P", "TZ-BLOCK", '{"rc":0}'))
    assert queue.thaw_frozen(store, after_min=0) == []
    assert state(store) == "frozen"
    with store.tx() as tx:
        tx.record_event(kind="probe_run", project="P", tz="TZ-BLOCK",
                        payload={"rc": 0})
    assert queue.thaw_frozen(store, after_min=0) == [
        "P/TZ-BLOCK: verified independent evidence"]


def test_newer_manual_transition_between_policy_and_commit_stops_thaw(lane, monkeypatch):
    queue.block(lane, "W-block", "blocked: dependency still pending")
    with lane.tx() as tx:
        tx.execute("UPDATE tasks SET state='accepted',author_worker='W-author' "
                   "WHERE project='P' AND tz='TZ-DEP'")
        tx.record_event(kind="accept", project="P", tz="TZ-DEP", worker_id="W-review")
    original = queue._verified_reactivation
    injected = []

    def transition_during_read(tx, *args):
        result = original(tx, *args)
        if result is not None and not injected:
            tx.record_event(kind="freeze", project="P", tz="TZ-BLOCK",
                            payload={"origin": "owner", "gate": "manual"})
            injected.append(True)
        return result

    monkeypatch.setattr(queue, "_verified_reactivation", transition_during_read)
    assert queue.thaw_frozen(lane, after_min=0) == []
    assert injected == [True]
    assert state(lane) == "frozen"
