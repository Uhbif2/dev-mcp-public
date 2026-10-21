"""TZ-333: cancellation and retarget are bound to the exact intent revision."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store
from dev_mcp.intents import stages
from dev_mcp.intents.sources.model import IntentEnvelope
from dev_mcp.intents.sources.registry import IntentChange, IntentRegistry
from dev_mcp.intents.spawn import (
    SpawnError, reconcile_tree, route_proven_understood, spawn_project,
)


def source(parent: Path, external: str = "old.md") -> IntentEnvelope:
    return IntentEnvelope(
        "file:owner", external, "revision-1", "Same owner intent text",
        {"create_project": True, "project_roots": [str(parent)]},
        "2026-10-14T00:00:00+00:00",
    )


def fixture(tmp_path):
    parent = tmp_path / "allowed"
    parent.mkdir()
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    IntentRegistry(store._connection())
    return parent, store


def cancelled_event(store, envelope):
    with store.tx() as tx:
        tx.change(
            "INSERT INTO intent_source_events "
            "(kind,source_id,external_id,revision,raw_intent,authority,timestamp) "
            "VALUES('cancelled',?,?,?,?,?,?)",
            (envelope.source_id, envelope.external_id, envelope.revision,
             envelope.raw_intent, json.dumps(envelope.authority), envelope.timestamp),
            event="test.cancelled")
        tx.change(
            "INSERT OR REPLACE INTO intent_source_revisions "
            "(source_id,external_id,revision,raw_intent,authority,timestamp,state) "
            "VALUES(?,?,?,?,?,?,'cancelled')",
            (envelope.source_id, envelope.external_id, envelope.revision,
             envelope.raw_intent, json.dumps(envelope.authority), envelope.timestamp),
            event="test.source_cancelled")


def test_cancel_stops_exact_tree_and_acknowledges(tmp_path):
    parent, store = fixture(tmp_path)
    old = source(parent)
    current = spawn_project(store, old, parent=parent, project_name="Old",
                            size="small", allowed_roots=[parent], triple=True)
    sibling = spawn_project(store, source(parent, "new.md"), parent=parent,
                            project_name="New", size="small",
                            allowed_roots=[parent], triple=True)
    claimed = f"{current.tree.root_id}-understood-candidate-1"
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES('W-cancel','W-cancel','chatgpt','now','now')",
                  event="test.worker")
        tx.change("UPDATE tasks SET state='taken' WHERE project='Old' AND tz=?",
                  (claimed,), event="test.take")
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('Old',?,'W-cancel','active','now')",
                  (claimed,), event="test.claim")
    cancelled_event(store, old)
    assert stages.cancel_pending_intents(store) > 0
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM outcomes WHERE id=?", (
            current.tree.root_id,)) == "dropped"
        assert tx.scalar("SELECT state FROM outcomes WHERE id=?", (
            sibling.tree.root_id,)) == "open"
        assert tx.scalar("SELECT count(*) FROM tasks WHERE project='Old' "
                         "AND state != 'frozen'") == 0
        assert tx.scalar("SELECT count(*) FROM tasks WHERE project='New' "
                         "AND state='ready'") >= 1
        assert tx.scalar("SELECT acknowledged FROM intent_source_events "
                         "WHERE kind='cancelled'") == 1
        assert tx.scalar("SELECT state FROM claims WHERE worker_id='W-cancel'") == "released"
    assert stages.cancel_pending_intents(store) == 0
    try:
        spawn_project(store, IntentChange("created", old), parent=parent,
                      project_name="Old", size="small", allowed_roots=[parent])
    except SpawnError as exc:
        assert "cancelled" in str(exc)
    else:
        raise AssertionError("cancelled intent was spawned")
    store.close()


def test_inactive_project_does_not_advance(tmp_path):
    parent, store = fixture(tmp_path)
    project = spawn_project(store, source(parent), parent=parent,
                            project_name="Paused", size="small",
                            allowed_roots=[parent], triple=True)
    with store.tx() as tx:
        tx.change("UPDATE projects SET active=0 WHERE name='Paused'",
                  event="test.pause")
    stage = project.tree.nodes[1]
    outcome.add_evidence(
        store, writer=outcome.server_evidence_writer(store), outcome_id=stage.identifier,
        kind=stage.evidence_spec["kind"], ref="test:proof",
        subject_version=project.tree.revision, env_version="test",
        verdict="pass", producer="test")
    outcome.close_outcome(store, stage.identifier)
    assert reconcile_tree(store, project.tree) == ()
    assert stages.advance_triples(store) == 0
    assert stages.prove_once(store) == 0
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Paused' "
                         "AND state='ready'") == 3  # candidates originally ready
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Paused' "
                         "AND state='frozen'") > 0
    store.close()


@pytest.mark.parametrize("old_active", [False, True])
def test_retarget_reuses_cancelled_disabled_root_for_live_source(tmp_path, old_active):
    parent, store = fixture(tmp_path)
    destination = parent / "old-target"
    destination.mkdir()
    old = source(parent)
    (destination / "component.json").write_text(json.dumps({
        "name": "OldCancelled", "kind": "intent-outcome-tree",
        "origin": {"source_id": old.source_id, "external_id": old.external_id},
    }), encoding="utf-8")
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) "
                  "VALUES('OldCancelled',?,0,?)", (str(destination), int(old_active)),
                  event="test.disabled_project")
    cancelled_event(store, old)
    new = source(parent, "new.md")
    provisional = spawn_project(store, new, parent=parent,
                                project_name="Provisional", size="small",
                                allowed_roots=[parent], triple=True)
    understood = provisional.directory / "intent/understood.md"
    understood.parent.mkdir(exist_ok=True)
    understood.write_text(f"kind: change\ntarget: {destination}\n", encoding="utf-8")
    stage = provisional.tree.nodes[1]
    outcome.add_evidence(
        store, writer=outcome.server_evidence_writer(store), outcome_id=stage.identifier,
        kind=stage.evidence_spec["kind"], ref="test:proof",
        subject_version=provisional.tree.revision, env_version="test",
        verdict="pass", producer="test")
    outcome.close_outcome(store, stage.identifier)
    if old_active:
        with pytest.raises(SpawnError, match="another active intent"):
            route_proven_understood(store, provisional.tree)
        store.close()
        return
    routed = route_proven_understood(store, provisional.tree)
    assert routed.project != "OldCancelled"
    assert "new" in routed.project
    assert routed.target == str(destination)
    with store.read() as tx:
        assert tx.scalar("SELECT active FROM projects WHERE name='OldCancelled'") == 0
        assert tx.scalar("SELECT active FROM projects WHERE name=?", (routed.project,)) == 1
        assert tx.scalar("SELECT COUNT(*) FROM events "
                         "WHERE kind='intent.target_reused' AND project=?",
                         (routed.project,)) == 1
    data = json.loads((destination / "component.json").read_text(encoding="utf-8"))
    assert data["origin"]["external_id"] == "new.md"
    store.close()
