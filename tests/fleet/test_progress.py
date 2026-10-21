"""Adversarial regressions for finite progress and event-only unfreezing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from dev_mcp.fleet import outcome, progress
from dev_mcp.fleet.store import Store


@pytest.fixture
def world(tmp_path):
    store = Store.open(str(tmp_path / "progress.sqlite"))
    created = outcome.create_outcome(
        store, project="demo", title="Result", intent_id="intent",
        spec={"requirements_version": "v1", "alternatives": ["B"], "method_id": "A"},
        evidence_spec={"required": "pass"}, authority={"verifier": "server"},
    )
    yield store, created.id
    store.close()


def _obstacle(store, oid, *, code="procedure_unknown", kind="unknown_method",
              source=None, predicate=None):
    return outcome.open_obstacle(
        store, outcome_id=oid, type=kind, cause_code=code,
        predicate=predicate if predicate is not None else {},
        change_source=source,
    )


def test_fingerprint_ignores_reports_but_respects_proof_subject_and_verdict(world):
    store, oid = world
    initial = progress.fingerprint(store, oid)
    # Timeline prose is not an input to structural progress.
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET title='Report: completed' WHERE id=?",
                  (oid,), event="test.report")
    assert progress.fingerprint(store, oid) == initial
    writer = outcome.server_evidence_writer(store)
    outcome.add_evidence(store, writer=writer, outcome_id=oid, kind="run",
                         ref="same-run", subject_version="commit-A", env_version="env",
                         verdict="pass", producer="gate")
    a = progress.fingerprint(store, oid)
    assert a != initial
    # Same ref and verdict, different code version MUST NOT collide.
    outcome.add_evidence(store, writer=writer, outcome_id=oid, kind="run",
                         ref="same-run", subject_version="commit-B", env_version="env",
                         verdict="pass", producer="gate")
    b = progress.fingerprint(store, oid)
    assert b != a
    outcome.add_evidence(store, writer=writer, outcome_id=oid, kind="run",
                         ref="same-run", subject_version="commit-B", env_version="env",
                         verdict="fail", producer="gate")
    assert progress.fingerprint(store, oid) != b


def test_one_transient_retry_then_research(world):
    store, oid = world
    _obstacle(store, oid, code="apply_failed_transient", kind="defect")
    step = progress.decide(store, oid)
    assert step.kind == "retry"
    progress.record_attempt(store, oid, step)
    assert progress.decide(store, oid).kind == "change_method"
    with pytest.raises(Exception):
        progress.record_attempt(store, oid, step)


def test_success_report_without_new_fingerprint_is_not_progress(world):
    store, oid = world
    _obstacle(store, oid, code="apply_failed_transient", kind="defect")
    step = progress.decide(store, oid)
    progress.record_attempt(store, oid, step, successful=True)
    with store.read() as tx:
        assert tx.scalar(
            "SELECT successful FROM progress_attempts WHERE outcome_id=? AND step='retry'",
            (oid,),
        ) == 0
    assert progress.decide(store, oid).kind == "change_method"


def test_change_method_prose_is_rejected_and_real_structure_accepted(world):
    store, oid = world
    step = progress.decide(store, oid)
    assert step.kind == "change_method"
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET title='A detailed research report' WHERE id=?",
                  (oid,), event="test.report")
    with pytest.raises(ValueError, match="new method_id"):
        progress.record_attempt(store, oid, step, successful=True)
    assert progress.decide(store, oid).kind == "change_method"
    progress.record_attempt(store, oid, step, successful=True, method_id="procedure-v2")
    assert progress.decide(store, oid).kind == "alternative"


def test_method_id_churn_cannot_reset_the_finite_ladder(world):
    store, oid = world
    used = []
    for n in range(100):
        step = progress.decide(store, oid, gemini_available=True)
        if step.kind == "owner" and dict(step.params).get("platform_outcome_id"):
            continue
        used.append(step.kind)
        if step.kind == "change_method":
            progress.record_attempt(store, oid, step, successful=True, method_id=f"method-{n}")
        else:
            progress.record_attempt(store, oid, step)
    assert used == ["change_method", "alternative", "cross_model", "owner"]


def test_structural_subtree_and_obstacle_type_also_count(world):
    store, oid = world
    step = progress.decide(store, oid)
    outcome.create_outcome(
        store, project="demo", title="Subproblem", intent_id="intent",
        parent_id=oid, spec={"requirements_version": "v1"},
        evidence_spec={"required": "pass"}, authority={"verifier": "server"},
    )
    assert progress.accepts_change_method(step.baseline,
                                          progress.decide(store, oid).baseline)
    progress.record_attempt(store, oid, step, successful=True)
    assert progress.decide(store, oid).kind in {"alternative", "wait"}


def test_hundred_failures_are_bounded_and_owner_gap_is_unique(world):
    store, oid = world
    kinds = []
    for _ in range(100):
        step = progress.decide(store, oid, gemini_available=True)
        if step.kind == "owner" and dict(step.params).get("platform_outcome_id"):
            continue
        kinds.append(step.kind)
        progress.record_attempt(store, oid, step)
    assert kinds == ["change_method", "alternative", "cross_model", "owner"]
    assert progress.decide(store, oid).kind == "owner"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM progress_attempts WHERE outcome_id=?", (oid,)) == 4
        assert tx.scalar("SELECT COUNT(*) FROM outcomes WHERE project='Fleet Platform'") == 1


def test_triage_prose_never_unlocks_obstacle(world):
    store, oid = world
    obstacle = _obstacle(
        store, oid, kind="external_fact", code="resource_down",
        source="device.ready", predicate={"device_online": True},
    )
    assert progress.decide(store, oid).kind == "wait"
    # An accepted unrelated task mentioned in prose is not a source event.
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET title='fix:request accepted 08.10' WHERE id=?",
                  (oid,), event="test.triage")
    assert progress.decide(store, oid).kind == "wait"
    assert not progress.signal_change(store, obstacle_id=obstacle, source="unrelated.task",
                                      event_id="a1", predicate_true=True)
    assert not progress.signal_change(store, obstacle_id=obstacle, source="device.ready",
                                      event_id="a2", predicate_true=False)
    assert progress.decide(store, oid).kind == "wait"
    assert progress.signal_change(store, obstacle_id=obstacle, source="device.ready",
                                  event_id="a3", predicate_true=True)
    assert progress.decide(store, oid).kind == "retry"
    progress.record_attempt(store, oid, progress.decide(store, oid))
    assert progress.decide(store, oid).kind == "change_method"


def test_self_wait_tz186_breaks_into_method_research(world):
    store, oid = world
    _obstacle(store, oid, kind="needs_result", code="procedure_unknown",
              source=oid, predicate={"requires_outcome": oid})
    assert progress.decide(store, oid).kind == "change_method"
    step = progress.decide(store, oid)
    progress.record_attempt(store, oid, step, successful=True, method_id="procedure")
    # Method change is real structural acceptance, not a free retry loop.
    assert progress.decide(store, oid).kind == "alternative"


def test_tz186_self_wait_hidden_by_unrelated_change_source(world):
    store, oid = world
    _obstacle(store, oid, kind="needs_result", code="procedure_unknown",
              source="external.task", predicate={"requires_outcome": oid})
    # The semantic dependency, not an unrelated nonempty change source,
    # determines whether we are waiting on our own result.
    assert "self_wait" in {v.code for v in outcome.check_invariant(store)}
    assert progress.decide(store, oid).kind == "change_method"


def test_tz186_indirect_dependency_cycle_ignores_decoy_event_sources(world):
    store, left = world
    right = outcome.create_outcome(
        store, project="demo", title="Dependency", intent_id="intent",
        spec={"requirements_version": "v1"},
        evidence_spec={"required": "pass"}, authority={"verifier": "server"},
    ).id
    _obstacle(store, left, kind="needs_result", source="external.task.a",
              predicate={"requires_outcome": right})
    _obstacle(store, right, kind="needs_result", source="external.task.b",
              predicate={"requires_outcome": left})
    violations = {(v.code, v.outcome_id) for v in outcome.check_invariant(store)}
    assert ("wait_cycle", left) in violations
    assert ("wait_cycle", right) in violations
    assert progress.decide(store, left).kind == "change_method"
    assert progress.decide(store, right).kind == "change_method"


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_tz186_research_procedure_real_run_and_terminal_or_repair(world, verdict):
    store, oid = world
    self_block = _obstacle(
        store, oid, kind="needs_result", code="procedure_unknown",
        source="external.task", predicate={"requires_outcome": oid},
    )
    research = progress.decide(store, oid)
    assert research.kind == "change_method"
    progress.record_attempt(store, oid, research, successful=True, method_id="procedure-v2")
    outcome.resolve_obstacle(store, self_block)

    # A result needs a real server-produced run proof, not a textual success report.
    writer = outcome.server_evidence_writer(store)
    outcome.add_evidence(store, writer=writer, outcome_id=oid,
                         kind="run", ref="tz186-procedure-v2",
                         subject_version="impl-v2", env_version="test-station",
                         verdict=verdict, producer="verifier")
    if verdict == "pass":
        outcome.close_outcome(store, oid, state="done")
        assert progress.decide(store, oid).kind == "done"
    else:
        _obstacle(store, oid, kind="defect", code="apply_failed_transient")
        retry = progress.decide(store, oid)
        assert retry.kind == "retry"
        progress.record_attempt(store, oid, retry)
        assert progress.decide(store, oid).kind != "retry"


def test_owner_latch_requires_actual_owner_event(world):
    store, oid = world
    for _ in range(3):
        step = progress.decide(store, oid)
        if step.kind == "owner":
            break
        progress.record_attempt(store, oid, step)
    progress.record_attempt(store, oid, step)
    blocked = progress.decide(store, oid)
    assert blocked.kind == "owner"
    obstacle_id = dict(blocked.params)["obstacle_id"]
    assert not progress.signal_change(store, obstacle_id=obstacle_id, source="timer",
                                      event_id="time", predicate_true=True)
    assert progress.signal_change(store, obstacle_id=obstacle_id, source="owner",
                                  event_id="owner:changed-requirement", predicate_true=True)
    assert progress.decide(store, oid).kind == "change_method"


def test_existing_owner_action_never_creates_triage_of_triage(world):
    store, oid = world
    obstacle_id = outcome.open_obstacle(
        store, outcome_id=oid, type="owner_action", cause_code="owner_physical",
        predicate={"owner_event": True}, change_source="owner",
        owner_action="Подключить устройство",
    )
    for _ in range(30):
        step = progress.decide(store, oid)
        assert step.kind == "owner"
        assert dict(step.params)["obstacle_id"] == obstacle_id
        with pytest.raises(ValueError, match="already waiting"):
            progress.record_attempt(store, oid, step)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM outcomes WHERE project='Fleet Platform'") == 0


def test_concurrent_attempts_one_winner(world):
    store, oid = world
    step = progress.decide(store, oid)
    def attempt(_):
        try:
            progress.record_attempt(store, oid, step)
            return "ok"
        except Exception as exc:
            return type(exc).__name__
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, range(2)))
    assert result.count("ok") == 1
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM progress_attempts WHERE outcome_id=?", (oid,)) == 1
