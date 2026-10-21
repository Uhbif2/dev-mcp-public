"""TZ-01. Real ephemeral git/worktree/SQLite; never touch the live dev-mcp."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from guard.core import DeployGuard, GuardConfig, GuardFailure
from guard.install import TASK_NAME, schtasks_args, task_xml
from guard.metrics import Metrics, regression, snapshot


def git(path: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(path), *args], text=True,
                          capture_output=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done.stdout.strip()


class Clock:
    def __init__(self):
        self.value = 0.0
        self.tick_hook = None

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds
        if self.tick_hook:
            self.tick_hook()
            self.tick_hook = None


def event(db: Path, kind: str, project: str = "P", tz: str = "TZ-X") -> None:
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO events(ts,kind,project,tz) VALUES(?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(), kind, project, tz))
        conn.commit()


@pytest.fixture
def lane(tmp_path: Path):
    parent = tmp_path / "полоса с пробелами"
    parent.mkdir()
    live, work = parent / "live dev", parent / "work dev"
    live.mkdir()
    git(live, "init", "-b", "master")
    git(live, "config", "user.name", "Test Guard")
    git(live, "config", "user.email", "guard@example.invalid")
    (live / ".gitignore").write_text("logs/\ndata/\n", encoding="utf-8")
    (live / "payload.txt").write_text("before", encoding="utf-8")
    git(live, "add", ".")
    git(live, "commit", "-m", "baseline")
    git(live, "worktree", "add", "-b", "platform/work", str(work))
    (work / "payload.txt").write_text("after", encoding="utf-8")
    git(work, "add", "payload.txt")
    git(work, "commit", "-m", "candidate")
    db = live / "data" / "fleet" / "fleet.sqlite"
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "ts TEXT NOT NULL, kind TEXT NOT NULL, project TEXT, tz TEXT)")
        conn.commit()
    # TZ-59: a clean Git HEAD without a completed canary receipt is not a
    # rollback target. Seed a *verified* pre-existing deployment explicitly.
    prior = git(live, "rev-parse", "HEAD")
    git(live, "tag", "platform/known-good", prior)
    bootstrap = DeployGuard(GuardConfig(live, work, state_root=tmp_path / "guard-state"))
    bootstrap._save_state(deployed=prior)
    bootstrap.event("known_good", candidate=prior, stage="after")
    return live, work, db, tmp_path / "guard-state"


def make_guard(lane, *, validator=None, restarter=None, prober=None):
    live, work, db, state = lane
    clock = Clock()
    guard = DeployGuard(review_check=lambda *_a: [], config=
        GuardConfig(live, work, state_root=state),
        validator=validator or (lambda p: (p / "payload.txt").read_text(encoding="utf-8") == "after"),
        restarter=restarter or (lambda _p: True),
        prober=prober or (lambda _p: True),
        sleeper=clock.sleep, now=clock.now,
        canary_seconds=10, sample_seconds=2,
    )
    guard._health = lambda: True
    return guard, clock


def test_successful_temporary_worktree_deployment(lane):
    live, work, db, state = lane
    old = git(live, "rev-parse", "HEAD")
    guard, _ = make_guard(lane)
    assert guard.deploy() == "deployed"
    assert (live / "payload.txt").read_text(encoding="utf-8") == "after"
    assert git(live, "rev-parse", "HEAD") != old
    assert git(live, "rev-parse", "platform/known-good") == git(live, "rev-parse", "HEAD")
    assert guard.deploy() == "no_change"
    assert (state / "deploy.lock").exists()
    with guard.lock():
        pass
    assert "deployed" in [json.loads(x)["code"] for x in guard.log.read_text(encoding="utf-8").splitlines()]


def test_dirty_live_does_not_mutate_owner_work(lane):
    live, _, _, _ = lane
    document = live / "Заметки владельца.txt"
    document.write_text("owner uncommitted", encoding="utf-8")
    head = git(live, "rev-parse", "HEAD")
    guard, _ = make_guard(lane)
    assert guard.deploy() == "dirty_live"
    assert document.read_text(encoding="utf-8") == "owner uncommitted"
    assert git(live, "rev-parse", "HEAD") == head
    assert "??" in git(live, "status", "--porcelain", "--untracked-files=all")


def test_protected_diff_fails_before_validation(lane):
    live, work, _, _ = lane
    (work / "guard").mkdir()
    (work / "guard" / "new.py").write_text("x=1\n", encoding="utf-8")
    git(work, "add", "guard/new.py")
    git(work, "commit", "-m", "protected guard change")
    old = git(live, "rev-parse", "HEAD")
    guard, _ = make_guard(lane, validator=lambda _: pytest.fail("must not validate"))
    assert guard.deploy() == "protected_path"
    assert git(live, "rev-parse", "HEAD") == old


def test_failed_isolated_validation_keeps_head(lane):
    live, _, _, _ = lane
    old = git(live, "rev-parse", "HEAD")
    guard, _ = make_guard(lane, validator=lambda _: False)
    assert guard.deploy() == "tests_red"
    assert git(live, "rev-parse", "HEAD") == old


def test_non_fast_forward_rejected_without_mutation(lane):
    live, _, _, _ = lane
    (live / "owner.txt").write_text("private accepted code", encoding="utf-8")
    git(live, "add", "owner.txt")
    git(live, "commit", "-m", "hotfix outside lane")
    old = git(live, "rev-parse", "HEAD")
    guard, _ = make_guard(lane)
    assert guard.deploy() == "not_ff"
    assert git(live, "rev-parse", "HEAD") == old


def test_probe_failure_rolls_back_and_writes_incident(lane):
    live, work, db, _ = lane
    old = git(live, "rev-parse", "HEAD")
    calls = []
    def prober(_):
        calls.append("probe")
        return len(calls) > 1  # bad new version, then healthy rollback
    guard, _ = make_guard(lane, prober=prober)
    assert guard.deploy() == "probe_red"
    assert git(live, "rev-parse", "HEAD") == old
    assert calls == ["probe", "probe"]
    assert len(list((work / "ToDo" / "incidents").glob("*-deploy.md"))) == 1
    assert len(list((work / "ToDo" / "tz").glob("TZ-R*-deploy-repair.md"))) == 1


def test_canary_block_thaw_loop_rolls_back(lane):
    live, _, db, _ = lane
    old = git(live, "rev-parse", "HEAD")
    guard, clock = make_guard(lane)
    def burst():
        for _ in range(5):
            event(db, "block")
            event(db, "thaw")
    clock.tick_hook = burst
    assert guard.deploy() == "canary_cycle"
    assert git(live, "rev-parse", "HEAD") == old


def test_safe_mode_after_two_rollbacks_uses_known_good(lane):
    live, work, db, state = lane
    guard, _ = make_guard(lane)
    assert guard.deploy() == "deployed"
    good = git(live, "rev-parse", "HEAD")
    for attempt in range(2):
        (work / "payload.txt").write_text("after " + str(attempt), encoding="utf-8")
        git(work, "add", "payload.txt")
        git(work, "commit", "-m", "faulty candidate " + str(attempt))
        failing, _ = make_guard(lane, validator=lambda _: True, prober=lambda _: True)
        # Probe false on new candidate, true during rollback.
        calls = []
        def fail_then_recover(_):
            calls.append(1)
            return len(calls) > 1
        failing.prober = fail_then_recover
        assert failing.deploy() == "probe_red"
        assert git(live, "rev-parse", "HEAD") == good
    assert failing._state()["rollbacks"] == 2
    codes = [json.loads(line)["code"] for line in failing.log.read_text(encoding="utf-8").splitlines()]
    assert "safe_mode" in codes


def test_missing_database_is_fail_closed(lane):
    live, _, db, _ = lane
    db.unlink()
    old = git(live, "rev-parse", "HEAD")
    guard, _ = make_guard(lane)
    assert guard.deploy() == "canary_unavailable"
    assert git(live, "rev-parse", "HEAD") == old


def test_malformed_known_good_tag_no_destructive_fallback(lane):
    live, _, _, _ = lane
    guard, _ = make_guard(lane)
    old = git(live, "rev-parse", "HEAD")
    git(live, "tag", "-d", "platform/known-good")
    assert guard._safe_mode() is False
    assert git(live, "rev-parse", "HEAD") == old
    git(live, "tag", "platform/known-good", old)
    assert guard._safe_mode() is True


def test_lock_blocks_concurrent_watches(lane):
    live, _, _, _ = lane
    old = git(live, "rev-parse", "HEAD")
    guard, _ = make_guard(lane)
    with guard.lock():
        assert guard.watch_once() == "lock_busy"
    assert git(live, "rev-parse", "HEAD") == old
    assert guard.watch_once() == "deployed"


def test_sqlite_ro_empty_log_and_regression_thresholds(lane):
    _, _, db, _ = lane
    assert snapshot(db).last_id == 0
    event(db, "maintenance_error")
    previous = snapshot(db)
    for _ in range(5):
        event(db, "maintenance_error")
    since = snapshot(db, since_id=previous.last_id)
    assert regression(previous, since) == "canary_maintenance"
    takes = Metrics(0, 0, tuple("P/TZ-X" for _ in range(10)), (), ())
    assert regression(previous, takes) == "canary_skew"
    one = Metrics(0, 0, ("P/TZ-X",), (), ())
    assert regression(previous, one) is None


def test_schtasks_xml_is_logon_trigger_and_restartable_without_install(tmp_path):
    source = tmp_path / "имя с пробелами" / "python.exe"
    folder = tmp_path / "имя с пробелами"
    content = task_xml(source, folder)
    root = ET.fromstring(content)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:LogonTrigger", ns) is not None
    assert root.find(".//t:TimeTrigger", ns) is not None
    assert root.find(".//t:Settings/t:Hidden", ns).text == "true"
    assert root.find(".//t:RestartOnFailure/t:Count", ns).text == "3"
    assert root.find(".//t:Exec/t:WorkingDirectory", ns) is None
    args = root.find(".//t:Exec/t:Arguments", ns).text
    assert str(folder) in args and "'watch'" in args and "run_module('guard'" in args
    args = schtasks_args(tmp_path / "task.xml")
    assert args == ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(tmp_path / "task.xml"), "/F"]
