"""Guard integration tests: exclusively temporary Git worktrees and SQLite."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
from datetime import datetime, timezone
import pytest

from guard.core import DeployGuard, GuardConfig
from guard import metrics
from guard.install import install, schtasks_args, task_xml


def cmd(*args, cwd=None):
    completed = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    assert completed.returncode == 0, (args, completed.stdout, completed.stderr)
    return completed.stdout.strip()


def commit(repo, filename="module.py", value="new\n"):
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    cmd("git", "-C", repo, "add", filename)
    cmd("git", "-C", repo, "commit", "-m", "test change")
    return cmd("git", "-C", repo, "rev-parse", "HEAD")


def db_init(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, "
                "project TEXT, tz TEXT)")
    con.commit()
    con.close()


def events(path, kinds, project="Fleet Platform", tz="TZ-01"):
    stamp = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO events(ts,kind,project,tz) VALUES (?,?,?,?)",
                     [(stamp, kind, project, tz) for kind in kinds])
    conn.commit()
    conn.close()


class Clock:
    def __init__(self):
        self.seconds = 1000.0
        self.on_sleep = None

    def now(self):
        return self.seconds

    def sleep(self, duration):
        self.seconds += duration
        if self.on_sleep:
            callback, self.on_sleep = self.on_sleep, None
            callback()


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "Тестовый проект с пробелами"
    root.mkdir()
    live = root / "live"
    work = root / "work"
    cmd("git", "init", "-b", "master", live)
    for setting, value in (("user.name", "Guard Tester"), ("user.email", "test@example.org")):
        cmd("git", "-C", live, "config", setting, value)
    original = commit(live, ".gitignore", "logs/\ndata/\n")
    cmd("git", "-C", live, "worktree", "add", "-b", "platform/work", work)
    candidate = commit(work, "module.py", "candidate\n")
    database = live / "data/fleet/fleet.sqlite"
    db_init(database)
    clock = Clock()
    config = GuardConfig(live, work, state_root=root / "guard state")
    guard = DeployGuard(review_check=lambda *_a: [], config=config, validator=lambda _: True,
                        restarter=lambda _: True, prober=lambda _: True,
                        sleeper=clock.sleep, now=clock.now,
                        canary_seconds=2, sample_seconds=1)
    guard._health = lambda: True
    cmd("git", "-C", live, "tag", "platform/known-good", original)
    guard._save_state(deployed=original)
    guard.event("known_good", candidate=original, stage="after")
    return SimpleNamespace(live=live, work=work, root=root, original=original,
                           candidate=candidate, guard=guard, db=database, clock=clock)


def head(path):
    return cmd("git", "-C", path, "rev-parse", "HEAD")


def log(tree):
    path = tree.live / "logs/platform-guard.jsonl"
    return [json.loads(s)["code"] for s in path.read_text(encoding="utf-8").splitlines()]


def test_success_candidate_validated_in_disposable_worktree_and_tagged(tree):
    validated = []
    tree.guard.validator = lambda folder: validated.append(folder) or folder.joinpath("module.py").read_text() == "candidate\n"
    assert tree.guard.deploy() == "deployed"
    assert head(tree.live) == tree.candidate
    assert cmd("git", "-C", tree.live, "rev-parse", "platform/known-good") == tree.candidate
    assert len(validated) == 1 and not validated[0].exists()
    assert "deployed" in log(tree)


def test_interrupted_merge_same_head_fast_recovers_verified_good(tree):
    """An FF that lost its guard process cannot be called deployed by HEAD alone."""
    cmd("git", "-C", tree.live, "tag", "-f", "platform/known-good", tree.original)
    cmd("git", "-C", tree.live, "merge", "--ff-only", tree.candidate)
    assert head(tree.live) == tree.candidate
    assert tree.guard._state().get("deployed") != tree.candidate
    actions = []
    tree.guard.validator = lambda path: actions.append("validate") or True
    tree.guard.restarter = lambda path: actions.append("restart") or True
    tree.guard.prober = lambda path: actions.append("probe") or True

    assert tree.guard.deploy() == "release_recovered"
    assert actions == ["restart", "probe"]
    assert tree.guard._state()["deployed"] == tree.original
    assert head(tree.live) == tree.original
    assert "no_change" not in log(tree)


def test_interrupted_merge_failed_probe_rolls_back_to_known_good(tree):
    cmd("git", "-C", tree.live, "tag", "-f", "platform/known-good", tree.original)
    cmd("git", "-C", tree.live, "merge", "--ff-only", tree.candidate)
    tree.guard.prober = lambda _: False

    assert tree.guard.deploy() == "rollback_failed"
    assert head(tree.live) == tree.original
    assert "rollback_ok" not in log(tree)  # probe still fails after rollback


def test_dirty_live_refuses_without_touching_owner_file(tree):
    owner_file = tree.live / "personal-note.txt"
    owner_file.write_text("owner private edits\n", encoding="utf-8")
    assert tree.guard.deploy() == "dirty_live"
    assert owner_file.read_text(encoding="utf-8") == "owner private edits\n"
    assert head(tree.live) == tree.original


def test_protected_path_and_red_tests_are_fail_closed(tree):
    commit(tree.work, "policy.toml", "blocked = true\n")
    assert tree.guard.deploy() == "protected_path"
    assert head(tree.live) == tree.original
    cmd("git", "-C", tree.work, "reset", "--soft", tree.candidate)
    cmd("git", "-C", tree.work, "restore", "--staged", "policy.toml")
    tree.guard.validator = lambda _: False
    assert tree.guard.deploy() == "tests_red"
    assert head(tree.live) == tree.original


@pytest.mark.parametrize("protected,renamed", [
    ("guard/core.py", "old_core.py"),
    ("policy.toml", "old-policy.toml"),
])
def test_rename_out_of_protected_path_refused_before_validation(tmp_path, protected, renamed):
    """Git rename detection must not hide a protected deletion's old path."""
    live = tmp_path / "live"
    work = tmp_path / "work"
    cmd("git", "init", "-b", "master", live)
    for setting, value in (("user.name", "Guard Tester"), ("user.email", "test@example.org")):
        cmd("git", "-C", live, "config", setting, value)
    old = commit(live, protected, "unchanged content\n")
    cmd("git", "-C", live, "worktree", "add", "-b", "platform/work", work)
    cmd("git", "-C", work, "mv", protected, renamed)
    cmd("git", "-C", work, "commit", "-m", "move protected file outside guard")
    candidate = head(work)
    guard = DeployGuard(review_check=lambda *_a: [], config=
        GuardConfig(live, work, state_root=tmp_path / "guard-state"),
        validator=lambda _: pytest.fail("protected rename must block before tests"),
    )
    cmd("git", "-C", live, "tag", "platform/known-good", old)
    guard._save_state(deployed=old)
    guard.event("known_good", candidate=old, stage="after")
    # Both the path detector and deploy itself must see the original protected path.
    assert guard._protected(guard._paths(old, candidate))
    assert guard.deploy() == "protected_path"
    assert head(live) == old
    assert (live / protected).is_file() and not (live / renamed).exists()


def test_not_fast_forward_cannot_replace_master(tree):
    commit(tree.live, "different.py", "owner work committed\n")
    owner_head = head(tree.live)
    assert tree.guard.deploy() == "not_ff"
    assert head(tree.live) == owner_head


def test_probe_failure_rolls_back_and_opens_incident(tree):
    tree.guard.prober = lambda _: False
    assert tree.guard.deploy() == "probe_red"
    assert head(tree.live) == tree.original
    assert "rollback_ok" not in log(tree)
    assert list((tree.work / "ToDo/incidents").glob("*-deploy.md"))
    repairs = list((tree.work / "ToDo/tz").glob("TZ-R*-deploy-repair.md"))
    assert repairs
    from dev_mcp.fleet.spec import parse_spec
    assert parse_spec(str(repairs[0])).tz.startswith("TZ-R")


def test_probe_project_is_bootstrapped_without_touching_live_source(tree):
    from dev_mcp.fleet.spec import parse_spec
    calls = []
    cli = tree.live / "fleet.cmd"
    cli.write_text("@echo off\n", encoding="utf-8")
    tree.guard._exec = lambda argv, **kwargs: (calls.append(argv)
                                                or SimpleNamespace(returncode=0, stdout=""))
    assert tree.guard._prepare_probe(tree.live)
    specs = list((tree.live / "data/probe-project/ToDo/tz").glob("TZ-*.md"))
    assert len(specs) == 1
    task = parse_spec(str(specs[0]))
    assert task.tz.startswith("TZ-P") and not task.owns and not task.acceptance
    assert calls[0][-3:-1] == ["Fleet Probe", str(tree.live / "data/probe-project")]
    # The runtime probe only creates ignored data; never edits the live checkout.
    assert head(tree.live) == tree.original


def test_canary_detects_five_block_thaw_cycles_and_rolls_back(tree):
    tree.clock.on_sleep = lambda: events(tree.db, ["block", "thaw"] * 5)
    assert tree.guard.deploy() == "canary_cycle"
    assert head(tree.live) == tree.original
    assert "rollback_ok" in log(tree)


def test_canary_maintenance_and_take_share_on_readonly_sqlite(tree):
    stamp = datetime.now(timezone.utc)
    before = metrics.snapshot(tree.db, now=stamp)
    events(tree.db, ["maintenance_error"] * 5)
    after = metrics.snapshot(tree.db, since_id=before.last_id, now=stamp)
    assert metrics.regression(before, after) == "canary_maintenance"
    events(tree.db, ["take"] * 11)
    after = metrics.snapshot(tree.db, since_id=before.last_id, now=stamp)
    assert metrics.regression(before, after) == "canary_maintenance"
    events(tree.db, ["take"] * 11)
    fresh = metrics.snapshot(tree.db, since_id=before.last_id, now=stamp)
    assert "Fleet Platform/TZ-01" in fresh.takes


def test_unpaired_bursts_do_not_masquerade_as_five_oscillations(tree):
    initial = metrics.snapshot(tree.db)
    events(tree.db, ["block"] * 5 + ["thaw"] * 5)
    observed = metrics.snapshot(tree.db, since_id=initial.last_id)
    assert metrics.regression(initial, observed) is None
    events(tree.db, ["block", "thaw"] * 5)
    observed = metrics.snapshot(tree.db, since_id=initial.last_id)
    assert metrics.regression(initial, observed) == "canary_cycle"


def test_two_rollbacks_trigger_safe_mode_from_known_good(tree):
    cmd("git", "-C", tree.live, "tag", "-f", "platform/known-good", tree.original)
    tree.guard._save_state(rollbacks=1)
    tree.guard.prober = lambda _: False
    assert tree.guard.deploy() == "probe_red"
    assert head(tree.live) == tree.original
    assert tree.guard._state()["rollbacks"] == 2
    assert "rollback_failed" in log(tree)


def test_watch_lock_prevents_parallel_deploy(tree):
    with tree.guard.lock():
        assert tree.guard.watch_once() == "lock_busy"
        assert head(tree.live) == tree.original


def test_missing_db_corrupt_tag_and_empty_log_fail_closed(tree):
    assert tree.guard._state()["deployed"] == tree.original
    tree.db.unlink()
    assert tree.guard.deploy() == "canary_unavailable"
    assert head(tree.live) == tree.original
    cmd("git", "-C", tree.live, "tag", "-d", "platform/known-good")
    assert tree.guard._safe_mode() is False
    assert "known_good_missing" in log(tree)


def test_installer_arguments_and_logon_restart_xml_never_spawn_schtasks(tmp_path):
    source = Path(__file__).resolve().parents[2] / "guard"
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    # Stage only. register=True must prove live heartbeat and cannot succeed
    # with this fake runner, which has no process or heartbeat.
    installed = install(source, base=tmp_path / "local appdata", runner=fake_run, register=False)
    assert (installed / "guard/__main__.py").is_file()
    assert calls == []
    xml = (installed / "guard-task.xml").read_text(encoding="utf-16")
    assert "LogonTrigger" in xml and "RestartOnFailure" in xml
    assert "PT1M" in xml and "python" in xml.lower()
    assert "WorkingDirectory" not in xml
