from __future__ import annotations

import dataclasses
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store
from dev_mcp.intents.sources.model import IntentEnvelope
from dev_mcp.intents.sources.registry import IntentChange
from dev_mcp.intents.spawn import SpawnError, reconcile_tree, spawn_project
from dev_mcp.intents.template import build_template


def source(parent: Path, *, revision="r1", text="Produce a verifiable tool", grant=True):
    return IntentEnvelope(
        "files", "case-1", revision, text,
        {"create_project": grant, "project_roots": [str(parent)]},
        "2026-10-09T09:00:00+00:00",
    )


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    db = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        yield root, db
    finally:
        db.close()


def spawn(root, db, e=None, name="Example", size="large"):
    return spawn_project(db, e or source(root), parent=root,
                         project_name=name, size=size, allowed_roots=[root])


def test_spawn_is_atomic_versioned_and_connected_to_outcome(env):
    root, store = env
    result = spawn(root, store)
    assert result.created and result.actionable_stage == "understood"
    component = json.loads((result.directory / "component.json").read_text(encoding="utf-8"))
    assert component["origin"] == {"source_id": "files", "external_id": "case-1"}
    assert component["kind"] == "intent-outcome-tree"
    assert component["name"] == "Example"
    assert outcome.get_outcome(store, result.tree.root_id).state == "open"
    assert not outcome.check_invariant(store)
    assert outcome.next_step(store, result.tree.root_id).kind == "work"
    with store.read() as tx:
        assert tx.scalar("SELECT count(*) FROM outcomes") == len(result.tree.nodes)
        assert tx.scalar("SELECT count(*) FROM outcome_work") == len(result.tree.nodes) - 1
        assert tx.scalar("SELECT count(*) FROM tasks WHERE state='ready'") == 1
        assert tx.scalar("SELECT count(*) FROM tasks WHERE state='frozen'") == len(result.tree.nodes) - 2
        assert tx.scalar("SELECT count(*) FROM obstacles WHERE state='open'") >= len(result.tree.nodes) - 2


def test_repeat_same_revision_and_update_create_only_new_tree(env):
    root, store = env
    first = spawn(root, store, size="small")
    again = spawn(root, store, size="small")
    assert not again.created and again.tree == first.tree
    updated = spawn(root, store, source(root, revision="r2"), size="small")
    assert updated.created and updated.tree.root_id != first.tree.root_id
    assert json.loads((updated.directory / "component.json").read_text())["component_id"]
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM outcomes") == len(first.tree.nodes) * 2


def test_two_concurrent_spawns_share_one_tree(env):
    root, store = env
    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = list(executor.map(lambda _: spawn(root, store, size="small"), range(2)))
    assert a.tree == b.tree
    assert sorted([a.created, b.created]) == [False, True]
    assert not list(root.glob(".spawn-*"))
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM outcomes") == len(a.tree.nodes)


def test_state_does_not_advance_on_only_acceptance_or_unproven_closure(env):
    root, store = env
    result = spawn(root, store, size="large")
    understood = result.tree.nodes[1]
    assert reconcile_tree(store, result.tree) == ()
    # Task accepted does not mean evidence exists.
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Example' "
                  "AND tz=?", (f"{result.tree.root_id}-understood",), event="test")
    assert reconcile_tree(store, result.tree) == ()
    outcome.close_outcome(store, understood.identifier)
    assert reconcile_tree(store, result.tree) == ()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE state='ready'") == 0
    writer = outcome.server_evidence_writer(store)
    outcome.add_evidence(
        store, writer=writer, outcome_id=understood.identifier,
        kind=understood.evidence_spec["kind"], ref="proof:server-check",
        subject_version=understood.evidence_spec["subject_version"],
        env_version="test-env-1", verdict="pass", producer="test-server",
    )
    assert reconcile_tree(store, result.tree) == ("executable_goal",)
    assert reconcile_tree(store, result.tree) == ()
    assert not outcome.check_invariant(store)


def test_end_to_end_all_required_proofs_close_root(env):
    root, store = env
    result = spawn(root, store, size="large")
    writer = outcome.server_evidence_writer(store)
    for stage in result.tree.nodes[1:]:
        with store.read() as tx:
            row = tx.one("SELECT state FROM tasks WHERE project='Example' AND tz=?",
                         (f"{result.tree.root_id}-{stage.stage}",))
        assert row["state"] == "ready", stage.stage
        with store.tx() as tx:
            tx.change("UPDATE tasks SET state='accepted' WHERE project='Example' AND tz=?",
                      (f"{result.tree.root_id}-{stage.stage}",), event="test")
        outcome.add_evidence(
            store, writer=writer, outcome_id=stage.identifier,
            kind=stage.evidence_spec["kind"], ref=f"server-proof://{stage.stage}",
            subject_version=result.tree.revision, env_version="test-env",
            verdict="pass", producer="test-server",
        )
        outcome.close_outcome(store, stage.identifier)
        reconcile_tree(store, result.tree)
    assert outcome.get_outcome(store, result.tree.root_id).state == "done"
    assert outcome.next_step(store, result.tree.root_id).kind == "done"
    with store.read() as tx:
        root_proof = tx.one("SELECT kind,subject_version,producer FROM evidence WHERE outcome_id=?",
                            (result.tree.root_id,))
    assert root_proof["kind"] == "review"
    assert root_proof["subject_version"] == result.tree.revision
    assert root_proof["producer"] == "trusted_tree_verifier"


def test_reconcile_cannot_close_large_root_using_small_tree(env):
    # 17.10.2026: размер входит в id дерева (кроме large) — маленькое дерево больше не
    # совпадает с большим по id и физически не может закрыть его корень.
    root, store = env
    large = spawn(root, store, size="large").tree
    small = build_template(source(root), project="Example", size="small")
    assert small.root_id != large.root_id
    assert len(small.nodes) < len(large.nodes)
    assert reconcile_tree(store, small) == ()
    assert outcome.get_outcome(store, large.root_id).state == "open"

def test_spawn_rejects_foreign_roots_and_untrusted_claims(env, tmp_path):
    root, store = env
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    with pytest.raises(SpawnError, match="trusted roots"):
        spawn_project(store, source(foreign), parent=foreign,
                      project_name="External", size="small", allowed_roots=[root])
    with pytest.raises(SpawnError, match="creation authority"):
        spawn(root, store, source(root, grant=False))
    e = dataclasses.replace(source(root), authority={"create_project": True,
                                                      "project_roots": [str(foreign)]})
    with pytest.raises(SpawnError, match="source authority"):
        spawn(root, store, e)
    assert not (foreign / "External").exists()
    assert not (root / "Example").exists()


@pytest.mark.parametrize("name", ["", ".", "..", "A/B", "C:\\root", "COM1",
                                   "NUL", "bad.", "bad ", "name:ADS", "trailing/"])
def test_invalid_names_never_touch_files(env, name):
    root, store = env
    with pytest.raises(SpawnError, match="invalid project"):
        spawn(root, store, name=name)
    assert not list(root.iterdir())


def test_project_collision_and_modified_manifest_cannot_be_overwritten(env):
    root, store = env
    existing = root / "Example"
    existing.mkdir()
    (existing / "foreign-data").write_text("keep")
    with pytest.raises(SpawnError, match="component.json"):
        spawn(root, store)
    assert (existing / "foreign-data").read_text() == "keep"
    existing.rmdir() if not list(existing.iterdir()) else None
    (existing / "component.json").write_text("{}")
    with pytest.raises(SpawnError, match="different intent"):
        spawn(root, store)
    assert json.loads((existing / "component.json").read_text()) == {}


def test_existing_symlink_project_and_alias_root_rejected(env, tmp_path):
    root, store = env
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "Example"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(SpawnError, match="alias|ordinary|symlink"):
        spawn(root, store)
    link.unlink()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(SpawnError, match="symlink|alias"):
        spawn_project(store, source(root), parent=alias, project_name="Test",
                      size="small", allowed_roots=[alias])


def test_cancelled_event_never_spawns(env):
    root, store = env
    cancelled = IntentChange("cancelled", source(root), previous_revision="old")
    with pytest.raises(SpawnError, match="only created or updated"):
        spawn(root, store, cancelled)


def test_corrupt_or_collision_does_not_duplicate_existing_state(env):
    root, store = env
    first = spawn(root, store, size="small")
    (first.directory / "component.json").write_text('{"bad":1}')
    with pytest.raises(SpawnError, match="different intent"):
        spawn(root, store, size="small")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM outcomes") == len(first.tree.nodes)
