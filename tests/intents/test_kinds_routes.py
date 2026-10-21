"""TZ-311: route trees, trusted owner gates and independent preproduction work."""
from __future__ import annotations

from pathlib import Path

import pytest

from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store
from dev_mcp.intents import stage_check, stages
from dev_mcp.intents.sources.model import IntentEnvelope
from dev_mcp.intents.spawn import (
    SpawnError, reconcile_tree, route_proven_understood, spawn_project)
from dev_mcp.intents.template import KIND_STAGES, build_template, selected_stages


def source(parent: Path) -> IntentEnvelope:
    return IntentEnvelope("files", "case-311", "v1", "Explore the target safely",
                          {"create_project": True, "project_roots": [str(parent)]},
                          "2026-10-13T00:00:00+00:00")


def _proved_understood(tmp_path: Path, kind: str, *, registered: bool = False,
                       target_name: str = "existing", size: str = ""):
    parent = tmp_path / "allowed"
    parent.mkdir()
    target = parent / target_name
    target.mkdir()
    (target / "preserve.txt").write_text("do not replace", encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    if registered:
        with store.tx() as tx:
            tx.change("INSERT INTO projects(name,root,priority,active) "
                      "VALUES('ExistingFleet',?,0,1)", (str(target),),
                      event="test.project")
    first = spawn_project(store, source(parent), parent=parent, project_name="Study",
                          size="large", allowed_roots=[parent], triple=True)
    doc = first.directory / "intent" / "understood.md"
    doc.parent.mkdir(exist_ok=True)
    doc.write_text(
        f"kind: {kind}\ntarget: {'new' if kind == 'build' else target}\n"
        + (f"size: {size}\n" if size else "")
        + "\n".join(f"## {h}\n" + "details " * 28 for h in
                    ("Интенция", "Готовые решения", "Что уже есть",
                     "Ограничения", "Неясности", "Нужно от владельца")),
        encoding="utf-8")
    node = first.tree.nodes[1]
    outcome.add_evidence(
        store, writer=outcome.server_evidence_writer(store), outcome_id=node.identifier,
        kind=node.evidence_spec["kind"], ref="proof:server-triple",
        subject_version=first.tree.revision, env_version="test",
        verdict="pass", producer="test-server")
    outcome.close_outcome(store, node.identifier)
    return store, first, target


@pytest.mark.parametrize("kind", ("build", "change", "investigate", "ops"))
def test_all_kinds_have_valid_ordered_dependency_trees(tmp_path: Path, kind: str):
    root = tmp_path / "target"
    root.mkdir()
    target = "new" if kind == "build" else str(root)
    tree = build_template(source(tmp_path), project="Example", size="large",
                          kind=kind, target=target, triple=True)
    expected = selected_stages(kind, "large")
    assert expected == (KIND_STAGES[kind] if kind != "build" else expected)
    assert tuple(node.stage for node in tree.nodes[1:]) == expected
    assert all(n.spec["kind"] == kind and n.spec["target"] == target for n in tree.nodes)
    tree.validate()
    if kind in ("ops", "investigate"):
        assert "executable_goal" not in expected


@pytest.mark.parametrize("kind", ("change", "investigate", "ops"))
def test_proved_understood_rebases_into_existing_fleet_project(tmp_path: Path, kind: str):
    store, first, target = _proved_understood(tmp_path, kind, registered=True)
    old_id = first.tree.root_id
    routed = route_proven_understood(store, first.tree)
    assert routed.kind == kind and routed.project == "ExistingFleet"
    assert routed.root_id != old_id
    assert routed.target == str(target)
    assert (target / "preserve.txt").read_text() == "do not replace"
    assert not (target / "Study").exists()
    assert (target / "intent" / "understood.md").is_file()
    assert outcome.get_outcome(store, routed.nodes[1].identifier).state == "done"
    assert outcome.get_outcome(store, first.tree.nodes[1].identifier).state == "done"
    assert outcome.get_outcome(store, old_id).state == "dropped"
    assert route_proven_understood(store, first.tree).root_id == routed.root_id
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM evidence WHERE outcome_id=?",
                         (routed.nodes[1].identifier,)) == 1
    store.close()


def test_untrusted_target_does_not_receive_files(tmp_path: Path):
    store, first, allowed = _proved_understood(tmp_path, "change")
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = first.directory / "intent/understood.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace(str(allowed), str(outside)),
                   encoding="utf-8")
    with pytest.raises(SpawnError, match="trusted roots"):
        route_proven_understood(store, first.tree)
    assert not (outside / "intent").exists()
    store.close()


def test_unregistered_existing_folder_is_target_without_child_project(tmp_path: Path):
    store, provisional, target = _proved_understood(tmp_path, "change")
    tree = route_proven_understood(store, provisional.tree)
    # 17.10.2026: the folder's own name when free, no "-intent-<hash>" suffix.
    assert tree.project == "existing"
    with store.read() as tx:
        staged = tx.query("SELECT state FROM tasks WHERE project=?", (provisional.tree.project,))
    assert all(row["state"] not in ("ready", "submitted") for row in staged)
    with store.read() as tx:
        assert tx.one("SELECT root FROM projects WHERE name=?", (tree.project,))["root"] == str(target)
    assert set(path.name for path in target.iterdir()) == {
        "preserve.txt", "intent", ".intent", "component.json"}
    store.close()


@pytest.mark.parametrize('target_name', ['Portfolio Export', 'Patterns Lab', 'component.v2'])
def test_existing_target_with_spaces_or_punctuation_gets_stable_safe_registry_name(tmp_path, target_name):
    store, provisional, target = _proved_understood(tmp_path, 'change', target_name=target_name)
    routed = route_proven_understood(store, provisional.tree)
    assert ' ' not in routed.project and '.' not in routed.project
    assert routed.target == str(target)
    assert route_proven_understood(store, provisional.tree).project == routed.project
    assert (target/'preserve.txt').read_text() == 'do not replace'
    store.close()


def test_route_conflict_is_reported_before_copying_any_other_files(tmp_path):
    store, provisional, target = _proved_understood(tmp_path, 'change')
    (target/'intent').mkdir()
    (target/'intent/z-conflict.md').write_text('independent target data')
    (provisional.directory/'intent/z-conflict.md').write_text('different source')
    (provisional.directory/'intent/a-new.md').write_text('must not be partly copied')
    with pytest.raises(SpawnError, match='z-conflict.md'):
        route_proven_understood(store, provisional.tree)
    assert not (target/'intent/a-new.md').exists()
    assert (target/'intent/z-conflict.md').read_text() == 'independent target data'
    assert reconcile_tree(store, provisional.tree) == ()
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project=? AND tz=?",
                      (provisional.project, provisional.tree.root_id+'-executable_goal'))['state'] == 'frozen'
    store.close()


def test_proved_routed_placeholder_retires_only_after_claim_release(tmp_path):
    from dev_mcp.intents.progress_recovery import retire_routed_placeholders
    store, provisional, target = _proved_understood(tmp_path, 'change')
    routed = route_proven_understood(store, provisional.tree)
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES('w','w','chatgpt','now','now')",event='test')
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES(?,?,'w','active','now')",
                  (provisional.project,provisional.tree.root_id+'-understood'),event='test')
    assert retire_routed_placeholders(store) == []
    with store.tx() as tx:
        tx.change("UPDATE claims SET state='released'",event='test')
    assert retire_routed_placeholders(store) == [provisional.project]
    assert retire_routed_placeholders(store) == []
    with store.read() as tx:
        assert tx.scalar('SELECT active FROM projects WHERE name=?',(routed.project,)) == 1
    store.close()


def test_triple_stage_waits_for_three_different_claimed_workers(tmp_path: Path):
    parent = tmp_path / "allowed"
    parent.mkdir()
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    first = spawn_project(store, source(parent), parent=parent, project_name="Study",
                          size="large", allowed_roots=[parent], triple=True)
    stage = "understood"
    tz = f"{first.tree.root_id}-{stage}"
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='Study' AND tz=?",
                      (tz,))["state"] == "frozen"
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Study' "
                         "AND tz LIKE '%-understood-candidate-%' AND state='ready'") == 3
    with store.tx() as tx:
        for i in (1, 2, 3):
            worker = "same-worker" if i <= 2 else "another-worker"
            tx.change("INSERT OR IGNORE INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES(?,?,'chatgpt','now','now')", (worker, worker),
                      event="test.worker")
            candidate = stages.candidate_tz(first.tree.root_id, stage, i)
            tx.change("UPDATE tasks SET state='accepted' WHERE project='Study' AND tz=?",
                      (candidate,), event="test.accept")
            tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
                      "VALUES('Study',?,?,'released','now','now')",
                      (candidate, worker), event="test.claim")
    # 14.10: the duplicate variant is reopened for another worker, consensus still waits
    assert stages.advance_triples(store) == 1
    duplicate = stages.candidate_tz(first.tree.root_id, stage, 2)
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='Study' AND tz=?",
                      (duplicate,))["state"] == "ready"
        assert tx.one("SELECT state FROM tasks WHERE project='Study' AND tz=?",
                      (tz,))["state"] == "frozen"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Study' AND tz=?",
                  (duplicate,), event="test.reaccept")
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES('independent','independent','chatgpt','now','now')",
                  event="test.worker")
        tx.change("UPDATE claims SET worker_id='independent' WHERE project='Study' "
                  "AND tz=?", (stages.candidate_tz(first.tree.root_id, stage, 2),),
                  event="test.worker_changed")
    assert stages.advance_triples(store) == 1
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='Study' AND tz=?",
                      (tz,))["state"] == "ready"
    store.close()


def test_investigation_verification_requires_independent_agent(tmp_path: Path):
    store, original, target = _proved_understood(tmp_path, "investigate",
                                                  registered=True)
    tree = route_proven_understood(store, original.tree)
    (target / "intent" / "verification.md").write_text(
        "\n".join(f"## {title}\n" + "repeat observations " * 22
                  for title in ("Повторы", "Независимый агент", "Критика Gemini",
                                "Расхождения")), encoding="utf-8")
    stage = f"{tree.root_id}-verified"
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES('agent','agent','chatgpt','now','now')", event="test.worker")
        for item in ("experiments", "verified"):
            tz = f"{tree.root_id}-{item}"
            tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
                      "VALUES(?,?,'agent','released','now','now')", (tree.project, tz),
                      event="test.claim")
        tx.change("UPDATE tasks SET state='accepted' WHERE project=? AND tz=?",
                  (tree.project, stage), event="test.accept")
    assert stages.prove_once(store) == 0
    with store.read() as tx:
        row = tx.one("SELECT body FROM tasks WHERE project=? AND tz=?",
                     (tree.project, stage))
    assert "другой агент" in row["body"]
    store.close()


def test_ops_cannot_be_proved_by_agent_written_owner_receipt(tmp_path: Path):
    store, provisional, target = _proved_understood(tmp_path, "ops", registered=True)
    tree = route_proven_understood(store, provisional.tree)
    intent = target / "intent"
    (intent / "rollback-plan.md").write_text("safe reversible plan", encoding="utf-8")
    (intent / "owner-approval.md").write_text("result: pass\napproved_by: owner",
                                               encoding="utf-8")
    task = f"{tree.root_id}-owner_approved"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project=? AND tz=?",
                  (tree.project, task), event="test.accept")
    assert stages.prove_once(store) == 0
    assert outcome.get_outcome(store, next(n.identifier for n in tree.nodes
                                           if n.stage == "owner_approved")).state == "open"
    with store.read() as tx:
        row = tx.one("SELECT state,body FROM tasks WHERE project=? AND tz=?",
                     (tree.project, task))
    assert row["state"] == "ready" and "доверенного одобрения владельца" in row["body"]
    store.close()


def test_ops_owner_action_default_is_no_and_trusted_consent_unlocks(tmp_path: Path):
    from hashlib import sha256
    store, provisional, target = _proved_understood(tmp_path, "ops", registered=True)
    tree = route_proven_understood(store, provisional.tree)
    plan = target / "intent" / "rollback-plan.md"
    plan.write_text("Plan with a verifiable rollback", encoding="utf-8")
    prior = next(n for n in tree.nodes if n.stage == "rollback_plan")
    outcome.add_evidence(
        store, writer=outcome.server_evidence_writer(store),
        outcome_id=prior.identifier, kind=prior.evidence_spec["kind"],
        subject_version=tree.revision, ref="server:ops_plan",
        env_version="test", verdict="pass", producer="test-server")
    outcome.close_outcome(store, prior.identifier)
    reconcile_tree(store, tree)
    approval = next(n for n in tree.nodes if n.stage == "owner_approved")
    assert outcome.next_step(store, approval.identifier).kind == "owner"
    assert stages.advance_ops_approvals(store) == 0
    with store.tx() as tx:
        tx.record_event(kind="intent.ops_approved", project=tree.project,
                        payload={"approved_by": "owner",
                                 "plan_sha256": sha256(plan.read_bytes()).hexdigest()})
    assert stages.advance_ops_approvals(store) == 1
    assert stages.prove_once(store) == 1
    assert outcome.get_outcome(store, approval.identifier).state == "done"
    store.close()


def test_experiment_checker_requires_real_reproduction_artifacts(tmp_path: Path):
    assert stage_check.check(tmp_path, "experiments")
    folder = tmp_path / "experiments" / "baseline"
    folder.mkdir(parents=True)
    (folder / "result.txt").write_text("measured 17 ms over three repeated runs")
    folder.joinpath("README.md").write_text(
        "\n".join(f"## {h}\n" + "measure " * 15 +
                  ("\ncommand: python reproduce.py" if h == "Команда" else "") +
                  ("\nartifact: result.txt" if h == "Результат" else "")
                  for h in ("Гипотеза", "Команда", "Результат", "Среда")),
        encoding="utf-8")
    assert not stage_check.check(tmp_path, "experiments")


def test_small_change_routes_into_three_stages_without_triple(tmp_path: Path):
    store, first, _ = _proved_understood(tmp_path, "change", registered=True, size="small")
    routed = route_proven_understood(store, first.tree)
    assert routed.size == "small" and not routed.triple
    assert tuple(n.stage for n in routed.nodes[1:]) == ("understood", "implemented", "handed_over")
    store.close()


def test_small_build_is_resized_in_place_and_old_stages_leave_queue(tmp_path: Path):
    store, first, _ = _proved_understood(tmp_path, "build", size="small")
    resized = route_proven_understood(store, first.tree)
    assert resized.project == first.tree.project and resized.root_id != first.tree.root_id
    assert resized.size == "small" and not resized.triple
    assert outcome.get_outcome(store, resized.nodes[1].identifier).state == "done"
    assert outcome.get_outcome(store, first.tree.root_id).state == "dropped"
    with store.read() as tx:
        stale = tx.scalar("SELECT COUNT(*) FROM tasks WHERE project=? AND tz LIKE ? "
                          "AND state IN ('ready','frozen','submitted')",
                          (first.tree.project, f"%{first.tree.root_id}%"))
        fresh = tx.scalar("SELECT COUNT(*) FROM tasks WHERE project=? AND tz LIKE ? "
                          "AND state IN ('ready','frozen')",
                          (first.tree.project, f"{resized.root_id}-%"))
    assert stale == 0 and fresh == len(resized.nodes) - 2
    assert route_proven_understood(store, first.tree).root_id == resized.root_id
    store.close()


def test_build_without_size_line_keeps_tree(tmp_path: Path):
    store, first, _ = _proved_understood(tmp_path, "build")
    assert route_proven_understood(store, first.tree).root_id == first.tree.root_id
    store.close()
