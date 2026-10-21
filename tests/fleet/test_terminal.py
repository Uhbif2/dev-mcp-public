from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.terminal import (  # noqa: E402
    AcceptedFacts,
    apply_failure_decision,
    check_invariant,
    reopen_decision,
    terminal_event,
)


def test_every_closed_claim_needs_typed_outcome_and_reason():
    claims = [
        {"id": 1, "state": "active", "released_at": None},
        {"id": 2, "state": "released", "released_at": "2026-10-11T12:00:00Z"},
        {"id": 3, "state": "requeued", "released_at": "2026-10-11T12:00:00Z"},
    ]
    recorded = [terminal_event(3, "expired", "lease_timeout")]
    issues = check_invariant(claims, recorded)
    assert [(x.code, x.claim_id) for x in issues] == [("missing_terminal", 2)]
    assert not check_invariant(claims, [terminal_event(2, "released", "blocked_external"), *recorded])


def test_terminal_validation_and_corruption_detection():
    for outcome, reason in [
        ("submitted", "acceptance_passed"), ("released", "handoff"),
        ("expired", "worker_lost"), ("failed", "guard_revoked"),
    ]:
        assert terminal_event(1, outcome, reason)["reason"] == reason
    with pytest.raises(ValueError, match="invalid"):
        terminal_event(1, "released", "anything")
    with pytest.raises(ValueError, match="invalid"):
        terminal_event(1, "completed", "work_finished")
    with pytest.raises(ValueError, match="claim_id"):
        terminal_event(0, "released", "handoff")
    issues = check_invariant(
        [{"id": 7, "state": "released", "released_at": ""}],
        [{"kind": "claim_terminal", "payload": json.dumps({"claim_id": 7, "outcome": "failed", "reason": "not_listed"})}],
    )
    assert {v.code for v in issues} == {"missing_release_time", "invalid_terminal"}


def test_accepted_reopens_only_for_new_verified_fact():
    baseline = AcceptedFacts("acceptance-a", 42, "spec-a")
    assert reopen_decision("accepted", baseline, baseline).event == "reopen_refused"
    assert not reopen_decision("accepted", baseline, AcceptedFacts("", 40, "")).allowed
    for current, name in [
        (AcceptedFacts("acceptance-b", 42, "spec-a"), "acceptance_output_sha"),
        (AcceptedFacts("acceptance-a", 43, "spec-a"), "review_reject_id"),
        (AcceptedFacts("acceptance-a", 42, "spec-b"), "spec_sha"),
    ]:
        result = reopen_decision("accepted", baseline, current)
        assert result.allowed and result.event == "reopened" and result.changes == (name,)


def test_failed_triage_application_creates_one_repair_not_a_new_triage():
    accepted_state = "accepted"
    kwargs = dict(
        state=accepted_state, project="P", tz="triage:31", phase="triage_apply",
        accepted_version="spec:v7", errors=["sync: PermissionError", "missing registry record"],
    )
    first = apply_failure_decision(**kwargs)
    assert first.state == "accepted"
    assert first.event == "apply_failure_repair"
    assert first.repair is not None and first.repair.project == "Fleet Platform"
    assert first.repair.evidence["source_tz"] == "triage:31"
    assert "PermissionError" in first.repair.evidence["errors"][0]
    second = apply_failure_decision(**kwargs, existing_keys={first.repair.dedup_key})
    assert second.state == "accepted"
    assert second.event == "apply_failure_duplicate" and second.repair is None
    changed = apply_failure_decision(
        **{**kwargs, "accepted_version": "spec:v8"},
        existing_keys={first.repair.dedup_key},
    )
    assert changed.repair is not None and changed.repair.dedup_key != first.repair.dedup_key


def test_failed_apply_does_not_count_as_new_fact():
    baseline = AcceptedFacts("acceptance-a", 20, "spec-a")
    fail = apply_failure_decision(
        state="accepted", project="P", tz="triage:31", errors=["could not apply"],
    )
    assert fail.state == "accepted" and fail.repair is not None
    assert reopen_decision(fail.state, baseline, baseline).event == "reopen_refused"
    assert reopen_decision(fail.state, baseline, AcceptedFacts("acceptance-b", 20, "spec-a")).allowed


def test_apply_failure_requires_evidence_and_accepted_state():
    with pytest.raises(ValueError, match="accepted"):
        apply_failure_decision(state="ready", project="P", tz="triage:1", errors=["failed"])
    with pytest.raises(ValueError, match="evidence"):
        apply_failure_decision(state="accepted", project="P", tz="triage:1", errors=["  "])
