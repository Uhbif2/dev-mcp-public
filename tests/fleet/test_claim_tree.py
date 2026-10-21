"""End-to-end Git tests in temporary repositories only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import subprocess

import pytest

from dev_mcp.fleet.claim_tree import (
    Claim, ClaimTreeError, ClaimWorktrees, close, integrate, open as open_claim,
    python_environment,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True,
        encoding="utf-8", errors="replace", text=True,
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "Проект с пробелом"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fleet Test")
    git(root, "config", "user.email", "fleet-test@example.invalid")
    (root / "shared.txt").write_text("line one\nline two\n", encoding="utf-8")
    git(root, "add", "shared.txt")
    git(root, "commit", "-m", "base")
    return root


def commit(tree: Path, name: str = "changed") -> None:
    git(tree, "add", "-A")
    git(tree, "commit", "-m", name)


def test_two_claims_isolated_until_accept_conflict_and_close(repo: Path):
    manager = ClaimWorktrees(repo)
    base = git(repo, "rev-parse", "HEAD")
    one = manager.open("TZ-A", base)
    two = manager.open("TZ-B", base)
    assert one == repo.parent / ".fleet-trees" / repo.name / "TZ-A"
    assert one != two and one.is_dir() and two.is_dir()
    assert open_claim(repo, "TZ-A", base) == one  # idempotent retry
    assert (one / "shared.txt").read_text() == "line one\nline two\n"
    assert (two / "shared.txt").read_text() == "line one\nline two\n"

    (one / "shared.txt").write_text("first task\nline two\n", encoding="utf-8")
    (two / "shared.txt").write_text("second task\nline two\n", encoding="utf-8")
    commit(one, "first task accepted")
    with pytest.raises(ClaimTreeError, match="acceptance"):
        manager.integrate("TZ-A")
    assert git(repo, "rev-parse", "HEAD") == base
    first_gate = []
    result = integrate(Claim(repo, "TZ-A", accepted=True),
                       gate=lambda trunk: first_gate.append(
                           (trunk / "shared.txt").read_text(encoding="utf-8")
                       ) or 0)
    assert result.status == "integrated" and result.gate_rc == 0
    assert first_gate == ["first task\nline two\n"]
    assert (repo / "shared.txt").read_text(encoding="utf-8") == first_gate[0]
    assert (two / "shared.txt").read_text(encoding="utf-8") == "second task\nline two\n"
    assert manager.integrate("TZ-A", accepted=True).status == "already_integrated"

    with pytest.raises(ClaimTreeError, match="uncommitted"):
        manager.integrate("TZ-B", accepted=True)
    commit(two, "second task accepted")
    conflicted = manager.integrate("TZ-B", accepted=True)
    assert conflicted.status == "needs_result"
    assert conflicted.conflict_paths == ("shared.txt",)
    assert not (two / ".git" / "rebase-merge").exists()  # no in-progress rebase
    assert git(two, "status", "--porcelain") == ""
    assert (repo / "shared.txt").read_text(encoding="utf-8") == "first task\nline two\n"

    close(Claim(repo, "TZ-A"))
    assert not one.exists()
    with pytest.raises(ClaimTreeError, match="unmerged"):
        manager.close("TZ-B")
    manager.close("TZ-B", abandon=True)
    assert not two.exists()


def test_non_conflicting_rebase_then_gate_on_trunk(repo: Path):
    manager = ClaimWorktrees(repo)
    first = manager.open("first")
    second = manager.open("second")
    (first / "alpha.txt").write_text("from first", encoding="utf-8")
    (second / "beta.txt").write_text("from second", encoding="utf-8")
    commit(first)
    commit(second)
    initial = git(repo, "rev-parse", "HEAD")
    assert manager.integrate("first", accepted=True).status == "integrated"
    after_first = git(repo, "rev-parse", "HEAD")
    assert initial != after_first
    seen = []
    merged = manager.integrate(
        "second", accepted=True,
        gate=lambda root: seen.append(sorted(p.name for p in root.glob("*.txt"))) or 0,
    )
    assert merged.status == "integrated" and merged.gate_rc == 0
    assert seen == [["alpha.txt", "beta.txt", "shared.txt"]]
    assert git(repo, "merge-base", "--is-ancestor", after_first, merged.commit) == ""
    assert (repo / "alpha.txt").read_text() == "from first"
    assert (repo / "beta.txt").read_text() == "from second"
    manager.close("first")
    manager.close("second")


def test_dirty_trunk_not_merged_and_gate_failure_reported(repo: Path):
    manager = ClaimWorktrees(repo)
    tree = manager.open("dirty-check")
    (tree / "new.txt").write_text("reviewed", encoding="utf-8")
    commit(tree)
    (repo / "unreviewed.txt").write_text("not yet reviewed", encoding="utf-8")
    with pytest.raises(ClaimTreeError, match="trunk worktree is dirty"):
        manager.integrate("dirty-check", accepted=True)
    assert not (repo / "new.txt").exists()
    (repo / "unreviewed.txt").unlink()
    result = manager.integrate("dirty-check", accepted=True, gate=lambda _: 17)
    assert result.status == "gate_failed" and result.gate_rc == 17
    assert (repo / "new.txt").read_text() == "reviewed"
    manager.close("dirty-check")


def test_invalid_base_and_hostile_claim_ids(repo: Path):
    manager = ClaimWorktrees(repo)
    old_head = git(repo, "rev-parse", "HEAD")
    (repo / "new-base.txt").write_text("new trunk", encoding="utf-8")
    commit(repo, "trunk advanced")
    with pytest.raises(ClaimTreeError, match="current trunk HEAD"):
        manager.open("stale", base=old_head)
    first = manager.open("fix:TZ-12/..")
    second = manager.open("fix:TZ-12\\..")
    assert first != second and first.parent == second.parent
    assert ".." not in first.name and ":" not in first.name
    assert first.is_dir() and second.is_dir()
    manager.close("fix:TZ-12/..")
    manager.close("fix:TZ-12\\..")


def test_terminal_age_cleanup_and_shared_python_env(repo: Path):
    manager = ClaimWorktrees(repo)
    merged = manager.open("done")
    orphan = manager.open("abandoned")
    active = manager.open("active")
    (merged / "merged.txt").write_text("accepted", encoding="utf-8")
    commit(merged)
    assert manager.integrate("done", accepted=True).status == "integrated"
    (orphan / "scratch.md").write_text("unfinished", encoding="utf-8")
    yesterday = datetime.now(timezone.utc) - timedelta(days=2)
    now = datetime.now(timezone.utc)
    removed = manager.cleanup(
        [("done", "accepted", yesterday), ("abandoned", "abandoned", yesterday),
         ("active", "taken", yesterday), ("missing", "accepted", yesterday),
         ("active", "abandoned", now)],
        older_than=now - timedelta(days=1),
    )
    assert removed == ("done", "abandoned")
    assert not merged.exists() and not orphan.exists() and active.exists()
    env = python_environment(repo, active, {"PYTHONPATH": "existing"})
    assert env["PYTHONPATH"] == os.pathsep.join((str(active / "src"), "existing"))
    assert env["VIRTUAL_ENV"] == str(repo / ".venv")
    manager.close("active")
