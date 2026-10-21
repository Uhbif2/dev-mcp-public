"""TZ-337: external outages are not author defects; internal faults belong to platform."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_mcp.fleet import gemini, outcome
from dev_mcp.fleet.store import Store
from dev_mcp.intents import stages
from dev_mcp.intents.sources.model import IntentEnvelope
from dev_mcp.intents.spawn import spawn_project


@pytest.fixture
def case(tmp_path: Path, monkeypatch):
    parent = tmp_path / "intent-projects"
    parent.mkdir()
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    source = IntentEnvelope(
        "files", "tz337", "v1", "Make a tested artifact",
        {"create_project": True, "project_roots": [str(parent)]},
        "2026-10-14T00:00:00+00:00",
    )
    spawned = spawn_project(store, source, parent=parent, project_name="StageCase",
                            size="large", allowed_roots=[parent], triple=True)
    node = next(node for node in spawned.tree.nodes if node.stage == "understood")
    tz = f"{spawned.tree.root_id}-understood"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='StageCase' AND tz=?",
                  (tz,), event="test.accept")
    folder = spawned.directory / "intent"
    (folder / "candidates" / "understood").mkdir(parents=True)
    (folder / "consensus").mkdir(parents=True)
    for index in (1, 2, 3):
        (folder / "candidates" / "understood" / f"{index}.md").write_text(
            f"candidate: {index}", encoding="utf-8")
    (folder / "consensus" / "understood.md").write_text(
        "consensus: good", encoding="utf-8")
    monkeypatch.setattr(stages.stage_check, "check", lambda *args, **kwargs: [])
    monkeypatch.setattr(stages.stage_check, "routing", lambda *args, **kwargs: {})
    monkeypatch.setattr(stages, "_triple_workers", lambda *args, **kwargs: (
        ["worker1", "worker2", "worker3"], []))
    monkeypatch.setattr(stages, "advance_triples", lambda *args, **kwargs: 0)
    yield store, node, tz, spawned.directory
    store.close()


def _task(store, tz):
    with store.read() as tx:
        return tx.one("SELECT state,body FROM tasks WHERE project='StageCase' AND tz=?",
                      (tz,))


def _events(store, kind):
    with store.read() as tx:
        return [json.loads(row["payload"]) for row in tx.query(
            "SELECT payload FROM events WHERE kind=? AND project='StageCase'",
            (kind,))]


def test_external_outage_keeps_accepted_and_recovers_same_submission(case, monkeypatch):
    store, node, tz, root = case
    online = {"up": False}
    calls = []
    monkeypatch.setattr(gemini, "_listening", lambda: online["up"])
    monkeypatch.setattr(gemini, "last_error", lambda: {"code": "network_error"})
    def ask(*args, **kwargs):
        calls.append(1)
        return {"verdict": "approve", "critique": "All good"} if online["up"] else None
    monkeypatch.setattr(gemini, "ask", ask)

    assert stages.prove_once(store) == 0
    assert _task(store, tz)["state"] == "accepted"
    assert len(calls) == 1
    waiting = _events(store, "intent.stage_waiting_external")
    assert len(waiting) == 1 and waiting[0]["kind"] == "external_pending"
    assert waiting[0]["digest"]
    assert not _events(store, "intent.stage_reopened")

    # No blind 60-second poll that calls Gemini again for an unchanged artifact/service.
    assert stages.prove_once(store) == 0
    assert len(calls) == 1
    online["up"] = True
    assert stages.prove_once(store) == 1
    assert len(calls) == 2
    assert outcome.get_outcome(store, node.identifier).state == "done"
    assert _task(store, tz)["state"] == "accepted"
    assert _events(store, "intent.stage_external_resumed")


def test_changed_artifact_bypasses_external_backoff(case, monkeypatch):
    store, node, tz, root = case
    monkeypatch.setattr(gemini, "_listening", lambda: False)
    monkeypatch.setattr(gemini, "last_error", lambda: {"code": "relay_unavailable"})
    calls = []
    monkeypatch.setattr(gemini, "ask", lambda *args, **kwargs: calls.append(1) or None)
    assert stages.prove_once(store) == 0
    (root / "intent" / "consensus" / "understood.md").write_text(
        "a new submitted artifact revision", encoding="utf-8")
    assert stages.prove_once(store) == 0
    assert len(calls) == 2
    assert _task(store, tz)["state"] == "accepted"
    assert len(_events(store, "intent.stage_waiting_external")) == 2


def test_real_revise_reopens_author_with_critique(case, monkeypatch):
    store, _node, tz, _root = case
    monkeypatch.setattr(gemini, "_listening", lambda: True)
    monkeypatch.setattr(gemini, "ask", lambda *args, **kwargs: {
        "verdict": "revise", "critique": "Missing independent evidence"})
    assert stages.prove_once(store) == 0
    task = _task(store, tz)
    assert task["state"] == "ready"
    assert "Missing independent evidence" in task["body"]
    assert _events(store, "intent.stage_reopened")
    assert not _events(store, "intent.stage_waiting_external")


def test_platform_exception_creates_single_repair_without_reopening(case, monkeypatch,
                                                                    tmp_path):
    from dev_mcp.fleet import platform_manual
    monkeypatch.setattr(platform_manual,'enabled',lambda:True)
    store, _node, tz, _root = case
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,0,1)",
                  ("Fleet Platform", str(tmp_path / "platform")), event="test.platform")
    def broken(*args, **kwargs):
        raise RuntimeError("stage checker exploded")
    monkeypatch.setattr(stages.stage_check, "check", broken)
    assert stages.prove_once(store) == 0
    assert stages.prove_once(store) == 0
    assert _task(store, tz)["state"] == "accepted"
    assert not _events(store, "intent.stage_reopened")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Fleet Platform' AND tz LIKE 'repair:%'") == 0
        rows = tx.query("SELECT payload AS body FROM events WHERE kind='manual_platform_required'")
    assert len(rows) == 1
    assert "RuntimeError" in rows[0]["body"]
    assert "stage checker exploded" in rows[0]["body"]
    assert _events(store, "intent.stage_platform_error")
