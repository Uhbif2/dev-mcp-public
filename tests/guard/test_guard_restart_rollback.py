"""TZ-62 reproductions for the failed guard restart and rollback.

All Git operations use disposable repositories; no live restarts.
The regressions must now pass normally, not as expected failures.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from test_guard_platform_core import git, lane, make_guard  # noqa: F401


@pytest.mark.skipif(os.name != "nt", reason="Windows job flags")
def test_job_denies_breakaway_and_current_restart_fails_closed(lane, monkeypatch):
    """A denied breakaway is caught as false, not a successful launch."""
    live, *_ = lane
    venv = live / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_bytes(b"")
    guard, _ = make_guard(lane)
    observed = []

    class Child:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def communicate(self, timeout=None):
            return ("restart ok", None)

    def refuse_job_breakaway(_argv, **kwargs):
        flags = kwargs.get("creationflags", 0)
        observed.append(flags)
        if flags & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "breakaway denied by inherited Job")
        return Child()

    monkeypatch.setattr("guard.core.subprocess.Popen", refuse_job_breakaway)
    assert guard._restart(live) is True
    assert len(observed) == 2
    assert observed[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert not observed[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB


def test_restart_launch_failure_must_not_fast_forward_or_rewrite_live(lane):
    """If the child could not launch, no live HEAD mutation is permitted."""
    live, *_ = lane
    guard, _ = make_guard(lane, restarter=lambda _: False)
    original = git(live, "rev-parse", "HEAD")
    touched = []
    real_git_rc = guard.git_rc

    def spy(repo, *args):
        if args and args[0] in {"merge", "checkout"}:
            touched.append(tuple(args))
        return real_git_rc(repo, *args)

    guard.git_rc = spy
    assert guard.deploy() == "restart_error"
    assert git(live, "rev-parse", "HEAD") == original
    assert touched == [], "cannot merge and rollback when restart launch was impossible"


def test_rollback_does_not_repeat_a_known_broken_restart_launch(lane):
    """The identical launcher failure must not strand a rollback."""
    calls = []

    def launch(_path):
        calls.append("spawn")
        return False

    guard, _ = make_guard(lane, restarter=launch)
    guard.deploy()
    assert len(calls) == 1, "restart failed; retrying identical launcher is ineffective"


def _live_ahead_of_stale_tag(lane):
    """Advance live with manual FF, leave verified tag at old SHA."""
    live, work, *_ = lane
    old = git(live, "rev-parse", "HEAD")
    (work / "manual.txt").write_text("manual accepted owner integration", encoding="utf-8")
    git(work, "add", "manual.txt")
    git(work, "commit", "-m", "manual owner integration")
    git(live, "merge", "--ff-only", "platform/work")
    prior = git(live, "rev-parse", "HEAD")
    git(work, "commit", "--allow-empty", "-m", "subsequent fleet candidate")
    assert prior != old
    return live, work, old, prior


def test_stale_good_tag_refuses_release_without_destroying_manual_head(lane):
    """Current fail-closed behavior: stale tag must not authorize rollback."""
    live, _work, old, prior = _live_ahead_of_stale_tag(lane)
    guard, _ = make_guard(lane)
    assert guard.deploy() == "known_good_missing"
    assert git(live, "rev-parse", "HEAD") == prior
    assert git(live, "rev-parse", "platform/known-good") == old


def test_verified_live_receipt_updates_stale_good_pointer_before_release(lane):
    """An independently proven current live should reconcile the stale tag.

    A synthetic receipt is injected; actual guard fix must authenticate it.
    """
    live, _work, old, prior = _live_ahead_of_stale_tag(lane)
    guard, _ = make_guard(lane)
    guard._save_state(deployed=prior)
    guard.event("known_good", candidate=prior, stage="after")
    assert guard.deploy() == "deployed"
    assert git(live, "rev-parse", "platform/known-good") != old


@pytest.mark.parametrize("rejected", ["validation", "probe"])
def test_synthetic_receipt_alone_cannot_promote_live(lane, rejected):
    """A forged/stale positive receipt must not replace a real health check."""
    live, _work, old, prior = _live_ahead_of_stale_tag(lane)
    guard, _ = make_guard(
        lane,
        validator=(lambda _path: rejected != "validation"),
        prober=(lambda _path: rejected != "probe"),
    )
    guard._save_state(deployed=prior)
    guard.event("known_good", candidate=prior, stage="after")

    assert guard.deploy() == "known_good_missing"
    assert git(live, "rev-parse", "HEAD") == prior
    assert git(live, "rev-parse", "platform/known-good") == old
    assert not guard._verified_good(prior)


def test_interrupted_reattest_receipt_never_authorizes_unverified_sha(lane, monkeypatch):
    """If logging fails after tag update, an unfinished attestation is unsafe."""
    live, _work, _old, prior = _live_ahead_of_stale_tag(lane)
    guard, _ = make_guard(lane)
    guard._save_state(deployed=prior)
    guard.event("known_good", candidate=prior, stage="after")
    original = guard.event

    def break_attestation(code, **fields):
        if code == "known_good" and fields.get("attested") == "independent":
            raise OSError("interrupted receipt fsync")
        return original(code, **fields)

    monkeypatch.setattr(guard, "event", break_attestation)
    assert guard.deploy() == "journal_error"
    assert guard._state().get("reattest_pending") == prior
    assert not guard._verified_good(prior)
    assert git(live, "rev-parse", "HEAD") == prior
    monkeypatch.setattr(guard, "event", original)
    assert guard.deploy() == "deployed"
