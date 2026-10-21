"""TZ-69: policy must not turn owner/security freeze into automatic authority."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from dev_mcp.fleet.reactivate_evidence import (
    BlockingEvent,
    DecisionStatus,
    ResolutionPredicate,
    TaskSnapshot,
    VerifiedEvidence,
    evaluate_reactivation,
)


NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
BLOCKED = "2026-10-12T08:55:00+00:00"
RESOLVED = "2026-10-12T08:58:00+00:00"
CHECKED = "2026-10-12T08:59:00+00:00"
SHA = "a" * 40
PROBE = "probe:" + "b" * 64


def scenario(kind="until_probe", *, target=None):
    task = TaskSnapshot("Fleet Platform", "TZ-69", "frozen", 52)
    event = BlockingEvent(52, task.project, task.tz, "block", "fleet_auto",
                          "operational", BLOCKED)
    targets = {
        "until_probe": PROBE,
        "accepted_work": "Fleet Platform/TZ-63",
        "platform_deploy_commit": SHA,
        "platform_deploy_after": "2026-10-12T08:57:00+00:00",
    }
    authorities = {
        "until_probe": ("probe_registry", "probe_pass", "fleet_probe"),
        "accepted_work": ("fleet_task_events", "work_accepted", "fleet_acceptance"),
        "platform_deploy_commit": ("platform_guard", "platform_deployed", "platform_guard"),
        "platform_deploy_after": ("platform_guard", "platform_deployed", "platform_guard"),
    }
    authority, fact_kind, issuer = authorities[kind]
    pred = ResolutionPredicate(52, kind, target or targets[kind], authority)
    fact = VerifiedEvidence(
        52, fact_kind, SHA if kind == "platform_deploy_after" else pred.target,
        "fleet-event:55", issuer, True, 55, RESOLVED, CHECKED,
    )
    return task, event, pred, fact


def assess(inputs, **kwargs):
    return evaluate_reactivation(*inputs, now=NOW, **kwargs)


@pytest.mark.parametrize("kind", [
    "until_probe", "accepted_work", "platform_deploy_commit", "platform_deploy_after",
])
def test_verified_independent_block_can_be_candidate(kind):
    task, event, pred, fact = scenario(kind)
    result = assess((task, event, pred, fact))
    assert result.status is DecisionStatus.ELIGIBLE
    assert result.reason == "verified_block_resolved"
    assert result.block_event_id == 52
    assert result.evidence_refs == ("fleet-event:55",)


def test_repeated_evaluation_is_identical_and_does_not_mutate_inputs():
    inputs = scenario()
    first = assess(inputs)
    assert first == assess(inputs)
    assert inputs == scenario()
    with pytest.raises(FrozenInstanceError):
        inputs[0].state = "ready"
    assert first.status is DecisionStatus.ELIGIBLE


@pytest.mark.parametrize("changed,reason", [
    ({"satisfied": False}, "predicate_not_satisfied"),
    ({"checked_at": "2026-10-12T08:50:00+00:00"}, "stale_or_invalid_evidence_time"),
    ({"checked_at": "2026-10-12T09:01:00+00:00"}, "stale_or_invalid_evidence_time"),
    ({"occurred_at": "2026-10-12T08:54:00+00:00"}, "stale_or_invalid_evidence_time"),
    ({"event_id": 51}, "untrusted_or_unbound_evidence"),
    ({"block_event_id": 51}, "untrusted_or_unbound_evidence"),
    ({"issuer": "owner"}, "untrusted_or_unbound_evidence"),
    ({"subject": "probe:" + "0" * 64}, "evidence_subject_mismatch"),
    ({"reference": ""}, "untrusted_or_unbound_evidence"),
])
def test_false_stale_foreign_or_untrusted_probe_fails_closed(changed, reason):
    task, event, pred, fact = scenario()
    result = assess((task, event, pred, replace(fact, **changed)))
    assert result.status is not DecisionStatus.ELIGIBLE
    assert result.reason == reason


def test_no_evidence_or_predicate_is_unknown():
    task, event, pred, fact = scenario()
    assert assess((task, event, pred, None)).status is DecisionStatus.UNKNOWN
    assert assess((task, event, None, fact)).status is DecisionStatus.UNKNOWN


@pytest.mark.parametrize("changed,expected", [
    ({"kind": "freeze", "origin": "owner"}, "explicit_freeze"),
    ({"kind": "freeze", "origin": "guard"}, "explicit_freeze"),
    ({"kind": "block", "origin": "owner"}, "nonautomatic_block"),
    ({"kind": "block", "origin": "manual"}, "nonautomatic_block"),
    ({"kind": "block", "origin": "fleet_auto", "gate": "security"}, "protected_gate"),
    ({"kind": "block", "origin": "fleet_auto", "gate": "manual"}, "protected_gate"),
])
def test_owner_guard_and_security_never_auto_reactivate(changed, expected):
    task, event, pred, fact = scenario()
    verdict = assess((task, replace(event, **changed), pred, fact))
    assert verdict.status is DecisionStatus.BLOCKED
    assert verdict.reason == expected


def test_later_transition_invalidates_original_block_even_with_positive_proof():
    task, event, pred, fact = scenario()
    result = assess((replace(task, latest_transition_id=60), event, pred, fact))
    assert result.status is DecisionStatus.UNKNOWN
    assert result.reason == "transition_changed"


@pytest.mark.parametrize("task_delta,event_delta", [
    ({"state": "ready"}, {}),
    ({"project": "Different"}, {}),
    ({}, {"kind": "reactivate"}),
    ({}, {"origin": "unspecified"}),
    ({}, {"gate": "unknown"}),
])
def test_unknown_task_event_state_or_origin_never_eligible(task_delta, event_delta):
    task, event, pred, fact = scenario()
    result = assess((replace(task, **task_delta), replace(event, **event_delta),
                     pred, fact))
    assert result.status is DecisionStatus.UNKNOWN


@pytest.mark.parametrize("predicate_delta", [
    {"block_event_id": 51},
    {"authority": "manual"},
    {"kind": "unknown"},
    {"target": "unregistered command string"},
])
def test_predicate_must_be_recorded_for_exact_block(predicate_delta):
    task, event, pred, fact = scenario()
    verdict = assess((task, event, replace(pred, **predicate_delta), fact))
    assert verdict.status is DecisionStatus.UNKNOWN


def test_acceptance_of_own_work_is_not_independent_evidence():
    task, event, pred, fact = scenario("accepted_work", target="Fleet Platform/TZ-69")
    result = assess((task, event, pred, fact))
    assert result.status is DecisionStatus.BLOCKED
    assert result.reason == "nonindependent_work"


def test_deploy_must_match_exact_sha_and_later_than_boundaries():
    inputs = scenario("platform_deploy_commit")
    assert assess((inputs[0], inputs[1], inputs[2],
                   replace(inputs[3], subject="c" * 40))).status is DecisionStatus.UNKNOWN
    inputs = scenario("platform_deploy_after", target="2026-10-12T08:59:00+00:00")
    assert assess(inputs).status is DecisionStatus.BLOCKED


def test_malformed_clock_and_old_evidence_are_not_valid():
    task, event, pred, fact = scenario()
    assert assess((task, replace(event, occurred_at=BLOCKED[:-6]), pred, fact)).status is DecisionStatus.UNKNOWN
    assert evaluate_reactivation(task, event, pred, fact,
                                 now=NOW + timedelta(minutes=6)).status is DecisionStatus.UNKNOWN


def test_general_manual_reactivate_ledger_class_does_not_justify_approval():
    # TZ-48 aggregates manual-reactivate without a task, blocking event or
    # proof; this *historical* class alone must never be auto-authorized.
    task = TaskSnapshot("Fleet Platform", "TZ-69", "frozen", None)
    verdict = assess((task, None, None, None))
    assert verdict.status is DecisionStatus.UNKNOWN
    assert verdict.evidence_refs == ()
