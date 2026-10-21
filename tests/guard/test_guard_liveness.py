"""Guard liveness tests never touch the live checkout or start services."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from guard.core import DeployGuard, GuardConfig


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return proc.stdout.strip()


@pytest.fixture
def guarded(tmp_path):
    live, work = tmp_path / "live", tmp_path / "work"
    live.mkdir()
    _git(live, "init", "-b", "master")
    _git(live, "config", "user.name", "Guard Test")
    _git(live, "config", "user.email", "test@example.org")
    (live / ".gitignore").write_text("data/\nlogs/\n")
    _git(live, "add", ".gitignore")
    _git(live, "commit", "-m", "original")
    original = _git(live, "rev-parse", "HEAD")
    _git(live, "worktree", "add", "-b", "platform/work", str(work))
    (work / "module.py").write_text("candidate\n")
    _git(work, "add", "module.py")
    _git(work, "commit", "-m", "candidate")
    candidate = _git(work, "rev-parse", "HEAD")
    database = live / "data/fleet/fleet.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, project TEXT, tz TEXT)")
    class Clock:
        value = 100.0
        def now(self):
            return self.value
        def sleep(self, seconds):
            self.value += seconds
    clock = Clock()
    guard = DeployGuard(
        GuardConfig(live, work, state_root=tmp_path / "state"),
        validator=lambda _: True, restarter=lambda _: True,
        prober=lambda _: True, review_check=lambda *_: [],
        sleeper=clock.sleep, now=clock.now, canary_seconds=130,
        sample_seconds=130,
    )
    guard._health = lambda: True
    _git(live, "tag", "platform/known-good", original)
    guard._save_state(deployed=original)
    guard.event("known_good", candidate=original, stage="after")
    return guard, live, original, candidate


def _events(guard):
    return [json.loads(line) for line in guard.log.read_text(encoding="utf-8").splitlines()]


def test_journal_fsync_and_phase_order(guarded, monkeypatch):
    guard, _, _, candidate = guarded
    observed = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: observed.append(fd) or real_fsync(fd))
    assert guard.deploy() == "deployed"
    entries = _events(guard)
    for name in ("start", "tests_ok", "merge", "restart", "probe", "canary_tick",
                 "known_good"):
        phases = [e["stage"] for e in entries if e["code"] == name
                  and (name != "known_good" or e.get("candidate") == candidate)]
        assert phases == ["before", "after"], name
    assert [e["code"] for e in entries][-1] == "deployed"
    assert observed
    assert guard._known_good_logged(candidate)
    pulse = json.loads((guard.config.live / "data/guard-heartbeat.json").read_text())
    assert pulse["pid"] == os.getpid() and pulse["candidate"] == candidate


def test_journal_io_error_stops_before_validation(guarded, monkeypatch):
    guard, live, original, _ = guarded
    validated = []
    guard.validator = lambda _: validated.append(True) or True
    monkeypatch.setattr(guard, "event", lambda *_args, **_kw: (_ for _ in ()).throw(OSError("journal read-only")))
    assert guard.deploy() == "journal_error"
    assert not validated
    assert _git(live, "rev-parse", "HEAD") == original


def test_journal_failure_after_merge_blocks_restart_and_canary(guarded, monkeypatch):
    guard, live, original, candidate = guarded
    calls = []
    guard.restarter = lambda _: calls.append("restart") or True
    actual = guard.phase_event

    def fail_merge_receipt(code, stage, **fields):
        if code == "merge" and stage == "after":
            raise OSError("disk flush failed after fast-forward")
        return actual(code, stage, **fields)

    monkeypatch.setattr(guard, "phase_event", fail_merge_receipt)
    assert guard.deploy() == "journal_error"
    assert _git(live, "rev-parse", "HEAD") == candidate
    assert not calls
    assert guard._state().get("deployed") == original
    assert not guard._known_good_logged(candidate)


def test_heartbeat_each_minute_during_long_canary(guarded, monkeypatch):
    guard, _, _, _ = guarded
    seen = []
    original = guard.heartbeat
    monkeypatch.setattr(guard, "heartbeat", lambda phase, candidate=None:
                        (seen.append((phase, guard.now())), original(phase, candidate)))
    assert guard.deploy() == "deployed"
    canary = [stamp for phase, stamp in seen if phase == "canary"]
    assert len(canary) >= 3
    assert max(b - a for a, b in zip(canary, canary[1:])) <= 60


def test_same_head_without_receipt_recovers_prior_verified_good(guarded):
    guard, live, original, candidate = guarded
    _git(live, "tag", "-f", "platform/known-good", original)
    _git(live, "merge", "--ff-only", candidate)
    actions = []
    guard.validator = lambda _: actions.append("tests") or True
    guard.restarter = lambda _: actions.append("restart") or True
    guard.prober = lambda _: actions.append("probe") or True
    assert guard.deploy() == "release_recovered"
    assert actions == ["restart", "probe"]
    assert _git(live, "rev-parse", "HEAD") == original
    assert "no_change" not in [x["code"] for x in _events(guard)]


def test_completed_receipt_allows_no_change(guarded):
    guard, _, _, _ = guarded
    assert guard.deploy() == "deployed"
    assert guard.deploy() == "no_change"


def _completed_legacy_deploy(guard, live, candidate):
    """Old guard: tag, state, then SHA-less known_good/deployed pair."""
    _git(live, "merge", "--ff-only", candidate)
    _git(live, "tag", "-f", "platform/known-good", candidate)
    guard._save_state(rollbacks=0, deployed=candidate)
    guard.event("known_good")
    guard.event("deployed")


def test_legacy_completed_receipt_upgrades_without_false_missing(guarded):
    guard, live, _, candidate = guarded
    _completed_legacy_deploy(guard, live, candidate)
    assert guard._known_good_logged(candidate), (guard._state(), _events(guard), (guard.state_dir / 'state.json').stat().st_mtime)
    actions = []
    guard.validator = lambda _: actions.append("validate") or True
    guard.restarter = lambda _: actions.append("restart") or True
    guard.prober = lambda _: actions.append("probe") or True
    assert guard.deploy() == "no_change"
    assert actions == []
    assert [e["code"] for e in _events(guard)][-1] == "no_change"


def test_legacy_missing_final_receipt_is_not_known_good(guarded):
    guard, live, _, candidate = guarded
    _completed_legacy_deploy(guard, live, candidate)
    entries = _events(guard)
    entries.pop()  # old known_good without deployed is an interrupted write
    guard.log.write_text("".join(json.dumps(e) + "\n" for e in entries),
                         encoding="utf-8")
    assert guard.deploy() == "known_good_missing"


def test_legacy_stale_state_is_not_borrowed_for_fresh_receipt(guarded):
    guard, live, _, candidate = guarded
    _completed_legacy_deploy(guard, live, candidate)
    state = guard.state_dir / "state.json"
    stamp = state.stat().st_mtime - 3600
    os.utime(state, (stamp, stamp))
    assert guard.deploy() == "known_good_missing"


@pytest.mark.parametrize("mismatch", ["tag", "state"])
def test_legacy_receipt_requires_matching_sha_pointers(guarded, mismatch):
    guard, live, original, candidate = guarded
    _completed_legacy_deploy(guard, live, candidate)
    if mismatch == "tag":
        _git(live, "tag", "-f", "platform/known-good", original)
        # A mismatched state/tag pair does not authorize either good or rollback.
        assert guard.deploy() == "known_good_missing"
    else:
        guard._save_state(deployed=original)
        assert guard.deploy() == "known_good_missing"
    assert [e["code"] for e in _events(guard)][-1] != "no_change"


def test_legacy_receipt_invalidated_by_later_interrupted_restart(guarded):
    guard, live, _, candidate = guarded
    _completed_legacy_deploy(guard, live, candidate)
    guard.event("restart", stage="before", candidate=candidate)
    assert guard.deploy() == "known_good_missing"


def test_clean_ignores_own_runtime_files_only(guarded):
    guard, live, _, _ = guarded
    # Remove the project's convenience ignore before the guard writes its own
    # journal and heartbeat. They must not turn a clean deploy into dirty_live.
    (live / ".gitignore").write_text("logs/\ndata/fleet/\n")
    _git(live, "add", ".gitignore")
    _git(live, "commit", "-m", "do not ignore data")
    guard.heartbeat("watch")
    guard.event("start", stage="before")
    assert guard._clean()
    (live / "data" / "owner-note.txt").write_text("not guard metadata")
    assert not guard._clean()
