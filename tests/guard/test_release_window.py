"""TZ-59: one pinned release per canary, recovered from durable evidence.

All tests use private Git repositories, fake time, and stubbed IO.  No live
service, real restart, or wall-clock canary is involved.
"""
from __future__ import annotations

import json

from test_guard_platform_core import git, lane, make_guard


def _events(guard):
    if not guard.log.exists():
        return []
    return [json.loads(line) for line in guard.log.read_text(encoding="utf-8").splitlines()]


def _commit(work, value):
    (work / "payload.txt").write_text(value, encoding="utf-8")
    git(work, "add", "payload.txt")
    git(work, "commit", "-m", value)
    return git(work, "rev-parse", "HEAD")


def _good(guard, live):
    old = git(live, "rev-parse", "HEAD")
    git(live, "tag", "-f", "platform/known-good", old)
    guard._save_state(deployed=old)
    guard.event("known_good", candidate=old, stage="after")
    return old


def test_pre_window_accepts_share_one_validation_restart_and_canary(lane):
    live, work, _, _ = lane
    _commit(work, "accepted second")
    candidate = _commit(work, "accepted third")
    calls = []
    guard, _ = make_guard(
        lane, validator=lambda _: calls.append("validate") or True,
        restarter=lambda _: calls.append("restart") or True,
    )
    _good(guard, live)
    assert guard.deploy() == "deployed"
    assert git(live, "rev-parse", "HEAD") == candidate
    assert calls == ["validate", "restart"]
    assert sum(e["code"] == "canary_tick" and e["stage"] == "before"
               for e in _events(guard)) >= 1


def test_later_commits_do_not_change_in_flight_sha_or_fake_clock(lane):
    live, work, _, _ = lane
    initial = git(work, "rev-parse", "HEAD")
    guard, clock = make_guard(lane, validator=lambda _: True)
    _good(guard, live)
    later = []

    def accept_mid_canary():
        later.append(_commit(work, "accepted during window"))
        later.append(_commit(work, "latest accepted"))

    clock.tick_hook = accept_mid_canary
    assert guard.deploy() == "deployed"
    assert git(live, "rev-parse", "HEAD") == initial
    assert guard._state()["deployed"] == initial
    assert clock.now() == 10
    assert guard.deploy() == "deployed"
    assert git(live, "rev-parse", "HEAD") == later[-1]
    assert guard.deploy() == "no_change"
    assert len([e for e in _events(guard) if e["code"] == "deployed"]) == 2
    for record in _events(guard):
        if record["code"] in {
            "release_window_open", "release_window_pending",
            "release_window_success", "release_window_rollback",
        }:
            assert {"window_id", "candidate", "restore_head", "pending_head"} <= set(record)
            assert record["window_id"] and record["candidate"]


def test_parallel_release_uses_one_kernel_lock(lane):
    live, _, _, _ = lane
    guard, _ = make_guard(lane)
    before = git(live, "rev-parse", "HEAD")
    with guard.lock():
        assert guard.watch_once() == "lock_busy"
    assert git(live, "rev-parse", "HEAD") == before


def test_preflight_red_does_not_switch_live_or_known_good(lane):
    live, _, _, _ = lane
    guard, _ = make_guard(lane, validator=lambda _: False)
    known = _good(guard, live)
    assert guard.deploy() == "tests_red"
    assert git(live, "rev-parse", "HEAD") == known
    assert git(live, "rev-parse", "platform/known-good") == known


def test_failed_sha_blocked_but_subsequent_fixed_sha_allowed(lane):
    live, work, _, _ = lane
    guard, _ = make_guard(lane, validator=lambda _: False)
    _good(guard, live)
    assert guard.deploy() == "tests_red"
    calls = []
    guard.validator = lambda _: calls.append("validated") or True
    assert guard.deploy() == "release_blocked"
    assert calls == []
    _commit(work, "corrected commit")
    assert guard.deploy() == "deployed"
    assert calls == ["validated"]


def test_failed_canary_rolls_back_without_tests_or_second_canary(lane):
    live, work, _, _ = lane
    guard, clock = make_guard(lane)
    good = _good(guard, live)
    original = guard._canary
    calls = []
    guard._canary = lambda base: calls.append("canary") or "canary_unhealthy"
    assert guard.deploy() == "canary_unhealthy"
    assert git(live, "rev-parse", "HEAD") == good
    assert calls == ["canary"]
    guard._canary = original
    assert guard.deploy() == "release_blocked"
    assert git(live, "rev-parse", "HEAD") == good


def test_crash_after_fast_forward_recovers_verified_good_without_revalidation(lane):
    live, work, _, _ = lane
    guard, _ = make_guard(lane)
    good = _good(guard, live)
    candidate = git(work, "rev-parse", "HEAD")
    guard._save_state(release_window={
        "window_id": "test-crash", "candidate_sha": candidate,
        "restore_sha": good, "phase": "probing", "started_at": 1,
    })
    git(live, "merge", "--ff-only", candidate)
    called = []
    guard.validator = lambda _: called.append("validate") or True
    guard.restarter = lambda _: called.append("restart") or True
    guard.prober = lambda _: called.append("probe") or True
    assert guard.deploy() == "release_recovered"
    assert called == ["restart", "probe"]
    assert git(live, "rev-parse", "HEAD") == good
    assert guard.deploy() == "release_blocked"


def test_promotion_crash_after_tag_move_restores_previous_good_pointer(lane):
    live, work, _, _ = lane
    guard, _ = make_guard(lane)
    good = _good(guard, live)
    candidate = git(work, "rev-parse", "HEAD")
    git(live, "merge", "--ff-only", candidate)
    guard._save_state(release_window={
        "window_id": "partial-good", "candidate_sha": candidate,
        "restore_sha": good, "phase": "finalizing", "started_at": 1,
    })
    # Crash after Git tag update but before state/journal promotion.
    git(live, "tag", "-f", "platform/known-good", candidate)
    calls = []
    guard.validator = lambda _: calls.append("validator") or True
    guard.restarter = lambda _: calls.append("restart") or True
    guard.prober = lambda _: calls.append("probe") or True
    assert guard.deploy() == "release_recovered"
    assert calls == ["restart", "probe"]
    assert git(live, "rev-parse", "HEAD") == good
    assert git(live, "rev-parse", "platform/known-good") == good
    assert guard._state()["deployed"] == good


def test_promotion_crash_after_verified_receipt_only_finishes_journal(lane):
    live, work, _, _ = lane
    guard, _ = make_guard(lane)
    old = _good(guard, live)
    candidate = git(work, "rev-parse", "HEAD")
    git(live, "merge", "--ff-only", candidate)
    guard._save_state(release_window={
        "window_id": "complete-good", "candidate_sha": candidate,
        "restore_sha": old, "phase": "finalizing", "started_at": 1,
    }, deployed=candidate)
    git(live, "tag", "-f", "platform/known-good", candidate)
    guard.event("known_good", candidate=candidate, stage="after")
    guard.validator = lambda _: (_ for _ in ()).throw(AssertionError("no extra test"))
    guard.restarter = lambda _: (_ for _ in ()).throw(AssertionError("no restart"))
    assert guard.deploy() == "no_change"
    assert guard._state()["release_window"] is None
    assert git(live, "rev-parse", "HEAD") == candidate
    assert git(live, "rev-parse", "platform/known-good") == candidate


def test_absent_verified_good_refuses_first_switch(lane):
    live, _, _, _ = lane
    guard, _ = make_guard(lane)
    before = git(live, "rev-parse", "HEAD")
    git(live, "tag", "-d", "platform/known-good")
    assert guard.deploy() == "known_good_missing"
    assert git(live, "rev-parse", "HEAD") == before


def test_safe_mode_never_uses_unverified_candidate_tag(lane):
    live, work, _, _ = lane
    guard, _ = make_guard(lane)
    verified = _good(guard, live)
    unverified = git(work, "rev-parse", "HEAD")
    git(live, "tag", "-f", "platform/known-good", unverified)
    assert not guard._safe_mode()
    assert git(live, "rev-parse", "HEAD") == verified


def test_review_and_policy_cover_whole_batch_and_guard_update_needs_handoff(lane, monkeypatch):
    live, work, _, _ = lane
    _commit(work, "whole batch")
    guard, _ = make_guard(lane)
    _good(guard, live)
    before = git(live, "rev-parse", "HEAD")
    guard.review_check = lambda old, new: ["unreviewed second commit"]
    assert guard.deploy() == "unreviewed_paths"
    assert git(live, "rev-parse", "HEAD") == before
    guard.review_check = lambda *_: []
    (work / "policy.toml").write_text("unsafe", encoding="utf-8")
    git(work, "add", "policy.toml")
    git(work, "commit", "-m", "protected policy")
    assert guard.deploy() == "protected_path"
    assert git(live, "rev-parse", "HEAD") == before


def test_stale_window_superseded_by_verified_live_is_closed(lane):
    """Урок 45: окно прежней версии стража не держит live, ушедший вперёд."""
    live, work, _, _ = lane
    guard, _ = make_guard(lane)
    restore = git(live, "rev-parse", "HEAD")
    old_candidate = git(work, "rev-parse", "HEAD")
    git(live, "merge", "--ff-only", old_candidate)
    newer = _commit(work, "later accepted")
    git(live, "merge", "--ff-only", newer)
    _good(guard, live)
    guard._save_state(release_window={
        "window_id": "stale", "candidate_sha": old_candidate,
        "restore_sha": restore, "phase": "validating", "started_at": 1,
    })
    result = guard.deploy()
    assert result != "known_good_missing"
    assert any(e["code"] == "release_window_stale" for e in _events(guard))
    assert not guard._state().get("release_window")
    assert git(live, "rev-parse", "HEAD") == newer


def test_window_with_unverified_live_still_fails_closed(lane):
    live, work, _, _ = lane
    guard, _ = make_guard(lane)
    restore = git(live, "rev-parse", "HEAD")
    old_candidate = git(work, "rev-parse", "HEAD")
    git(live, "merge", "--ff-only", old_candidate)
    newer = _commit(work, "unverified")
    git(live, "merge", "--ff-only", newer)
    guard._save_state(release_window={
        "window_id": "stale", "candidate_sha": old_candidate,
        "restore_sha": restore, "phase": "validating", "started_at": 1,
    })
    assert guard.deploy() == "known_good_missing"
