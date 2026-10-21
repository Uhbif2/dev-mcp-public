"""Regression of pinned guard handoff across Git ancestry races (TZ-80).

All Git repositories, guard runtime, event journal and clock live in tmp_path.
No production install, scheduler, restart, or network is touched.
"""
from __future__ import annotations

import json

import pytest

import test_guard_self_update as handoff
import test_platform_guard as platform
from test_platform_guard import cmd, commit, head


# These fixtures initialize disposable live + platform/work worktrees and
# an immutable staged guard runtime, never C:/hub/dev-mcp.
tree = platform.tree
guard_candidate = handoff.guard_candidate


def _journal(t):
    journal = t.live / "logs" / "platform-guard.jsonl"
    return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]


def _known_good(t):
    return cmd("git", "-C", t.live, "rev-parse", "refs/tags/platform/known-good")


def _start_pinned_handoff(t):
    """Old guard stages and hands off a candidate without FF on live."""
    child = handoff.FakeChild()

    def heartbeat(version, token):
        (t.live / "data" / "guard-heartbeat.json").write_text(
            json.dumps({"pid": child.pid, "version": version, "handoff_token": token}),
            encoding="utf-8",
        )
        return child

    t.guard._spawn_handoff = heartbeat
    assert t.guard.deploy() == "self_update"
    pending = t.guard._state()["self_update"]
    assert pending["candidate"] == t.candidate
    assert pending["version"] != pending["previous"]
    assert head(t.live) == t.original
    assert _known_good(t) == t.original
    return pending.copy()


def _successor(t, version, *, validator=None):
    new = type(t.guard)(
        t.guard.config,
        review_check=lambda *_: [],
        validator=validator or (lambda _: True),
        restarter=lambda _: True,
        prober=lambda _: True,
        sleeper=t.clock.sleep,
        now=t.clock.now,
        canary_seconds=2,
        sample_seconds=1,
    )
    new._health = lambda: True
    new._running_guard_version = lambda: version
    return new


def test_pinned_candidate_published_before_descendant_in_next_cycle(guard_candidate):
    t = guard_candidate
    pending = _start_pinned_handoff(t)
    pinned = pending["candidate"]

    # Racing commit arrives after the old guard has fixed the candidate SHA.
    descendant = commit(t.work, "later.py", "newer, independent work\n")
    assert pinned != descendant
    cmd("git", "-C", t.work, "merge-base", "--is-ancestor", pinned, descendant)
    assert head(t.work) == descendant

    validations = []

    def validate(snapshot):
        validations.append((head(snapshot), (snapshot / "later.py").is_file()))
        return True

    successor = _successor(t, pending["version"], validator=validate)
    assert successor.deploy() == "deployed"
    assert validations == [(pinned, False)]
    assert head(t.live) == pinned != descendant
    assert _known_good(t) == pinned
    state = successor._state()
    assert state["deployed"] == pinned
    assert state["guard_known_good"] == pending["version"]
    assert state["self_update"] is None
    assert state["release_window"] is None

    first_receipt = [
        event for event in _journal(t)
        if event["code"] == "release_window_success"
        and event.get("candidate") == pinned
        and event.get("pending_head") == descendant
    ]
    assert len(first_receipt) == 1, "pinned SHA must have its own successful receipt"
    assert not [
        event for event in _journal(t)
        if event["code"] == "release_window_success"
        and event.get("candidate") == descendant
    ]

    # The later commit is eligible only after handoff is fully closed.
    assert successor.deploy() == "deployed"
    assert validations == [(pinned, False), (descendant, True)]
    assert head(t.live) == descendant
    assert _known_good(t) == descendant
    assert successor._state()["deployed"] == descendant
    assert successor._state()["guard_known_good"] == pending["version"]
    receipts = [
        event for event in _journal(t)
        if event["code"] == "release_window_success"
    ]
    assert [event["candidate"] for event in receipts[-2:]] == [pinned, descendant]


def test_diverged_trunk_rejects_unrelated_sha_without_false_receipt(guard_candidate):
    t = guard_candidate
    pending = _start_pinned_handoff(t)
    baseline_events = _journal(t)
    old_launcher = (t.live / "data" / "guard-runtime" / "run_guard.pyw").read_bytes()

    # Rewrite only the disposable worktree branch to a sibling of pinned SHA.
    cmd("git", "-C", t.work, "reset", "--hard", t.original)
    unrelated = commit(t.work, "sibling.py", "divergent\n")
    assert unrelated != pending["candidate"]
    assert cmd(
        "git", "-C", t.work, "merge-base", pending["candidate"], unrelated,
    ) == t.original

    successor = _successor(
        t, pending["version"],
        validator=lambda _: pytest.fail("unrelated SHA must never be validated"),
    )
    assert successor.deploy() == "self_update_reverted"
    assert head(t.live) == t.original
    assert _known_good(t) == t.original

    state = successor._state()
    assert state["deployed"] == t.original
    assert state["guard_known_good"] == pending["previous"]
    assert state["self_update"]["candidate"] == pending["candidate"]
    assert state["self_update"]["version"] == pending["version"]
    assert state["self_update"]["candidate"] != unrelated
    assert (t.live / "data" / "guard-runtime" / "run_guard.pyw").read_bytes() == old_launcher

    new_events = _journal(t)[len(baseline_events):]
    assert any(e["code"] == "self_update_reverted" for e in new_events)
    assert not any(
        e["code"] in ("deployed", "release_window_success", "known_good")
        for e in new_events
    )


def test_head_only_legacy_selector_is_killed_by_pinned_assertion(guard_candidate, monkeypatch):
    """A HEAD-only successor would reject the legitimate pinned handoff."""
    t = guard_candidate
    pending = _start_pinned_handoff(t)
    pinned = pending["candidate"]
    descendant = commit(t.work, "later.py", "fresh content\n")
    cmd("git", "-C", t.work, "merge-base", "--is-ancestor", pinned, descendant)

    successor = _successor(t, pending["version"])
    true_git_rc = successor.git_rc

    def legacy_head_only(repo, *args, **kwargs):
        # Mutate the pinned-selection boundary to keep platform/work HEAD.
        # Every other real Git operation and actual history is left intact.
        if args == ("merge-base", "--is-ancestor", pinned, descendant):
            return 1
        return true_git_rc(repo, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(successor, "git_rc", legacy_head_only)
        with pytest.raises(AssertionError, match="HEAD-only regression"):
            observed = successor.deploy()
            assert observed == "deployed", (
                f"HEAD-only regression: expected pinned success, got {observed}"
            )
        assert head(t.live) == t.original
        assert _known_good(t) == t.original

    # Identical real Git history succeeds with the production selector.
    assert successor.deploy() == "deployed"
    assert head(t.live) == pinned
    assert _known_good(t) == pinned
    assert successor._state()["deployed"] == pinned
