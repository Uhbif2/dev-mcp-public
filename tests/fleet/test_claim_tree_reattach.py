"""Ветка захвата без папки дерева переподключается, а не отказывает (14.10.2026)."""
import shutil
import subprocess

from dev_mcp.fleet.claim_tree import ClaimWorktrees


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_branch_without_worktree_is_reattached(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    tree = ClaimWorktrees(repo)
    path = tree.open("7")
    (path / "b.txt").write_text("b\n", encoding="utf-8")
    _git(path, "add", "b.txt")
    _git(path, "commit", "-qm", "work")
    shutil.rmtree(path)
    again = tree.open("7")
    assert (again / "b.txt").read_text(encoding="utf-8") == "b\n"
