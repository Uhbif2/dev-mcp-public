"""TZ-63: explicit accepted-only commit contract; real temporary Git repositories."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dev_mcp.fleet.manual_commit import CommitRefused, commit_accepted  # noqa: E402


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                            text=True, encoding="utf-8", check=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    for name in ("accepted.txt", "staged.txt", "unstaged.txt"):
        (root / name).write_text("base\n", encoding="utf-8")
    git(root, "add", "--", "accepted.txt", "staged.txt", "unstaged.txt")
    git(root, "commit", "-q", "-m", "base")
    return root


def receipt(root: Path, *paths: str, state: str = "accepted",
            base: str | None = None, rid: str = "server-101") -> dict:
    files = {p: {"sha256": hashlib.sha256((root / p).read_bytes()).hexdigest(),
                 "mode": "100644"} for p in paths}
    return {"receipt_id": rid, "task_id": "TZ-A", "review_state": state,
            "review_event_id": 101, "base_sha": base or git(root, "rev-parse", "HEAD"),
            "files": files}


def call(root: Path, data: dict | None, *paths: str):
    return commit_accepted(root, task_id="TZ-A", accepted_paths=paths,
                           receipt_loader=lambda _task: data)


def test_only_accepted_path_committed_and_original_index_untouched(repo: Path):
    (repo / "accepted.txt").write_text("approved\n", encoding="utf-8")
    (repo / "staged.txt").write_text("private stage\n", encoding="utf-8")
    git(repo, "add", "--", "staged.txt")
    (repo / "unstaged.txt").write_text("unapproved worktree\n", encoding="utf-8")
    before = git(repo, "rev-parse", "HEAD")
    stage_before = git(repo, "ls-files", "--stage")
    index_before = git(repo, "write-tree")
    proof = receipt(repo, "accepted.txt", base=before)

    result = call(repo, proof, "accepted.txt")
    assert result.commit_sha == git(repo, "rev-parse", "HEAD")
    assert result.repeated is False
    assert git(repo, "rev-parse", "HEAD^") == before
    assert git(repo, "show", "--pretty=format:", "--name-only", "HEAD") == "accepted.txt"
    assert git(repo, "show", "HEAD:accepted.txt") == "approved"
    assert git(repo, "show", "HEAD:staged.txt") == "base"
    assert git(repo, "show", "HEAD:unstaged.txt") == "base"
    assert git(repo, "ls-files", "--stage") == stage_before
    assert git(repo, "write-tree") == index_before
    assert (repo / "staged.txt").read_text() == "private stage\n"
    assert (repo / "unstaged.txt").read_text() == "unapproved worktree\n"

    repeated = call(repo, proof, "accepted.txt")
    assert repeated.repeated is True
    assert repeated.commit_sha == result.commit_sha
    assert git(repo, "rev-list", "--count", "--all") == "2"
    assert git(repo, "ls-files", "--stage") == stage_before


@pytest.mark.parametrize("bad_state", ["pending", "submitted", "rejected", "in_review", ""])
def test_only_terminal_independent_accept_can_commit(repo: Path, bad_state: str):
    (repo / "accepted.txt").write_text("modified\n", encoding="utf-8")
    proof = receipt(repo, "accepted.txt", state=bad_state)
    head, stage = git(repo, "rev-parse", "HEAD"), git(repo, "ls-files", "--stage")
    with pytest.raises(CommitRefused):
        call(repo, proof, "accepted.txt")
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "ls-files", "--stage") == stage


@pytest.mark.parametrize("bad_path", [
    "../outside.txt", "/etc/passwd", ".git/config", "sub/../accepted.txt",
    "secret.txt", ".env", "config/password.json", r"sub\accepted.txt",
    "accepted*.txt", "sub//accepted.txt",
])
def test_unsafe_paths_fail_closed(repo: Path, bad_path: str):
    head, stage = git(repo, "rev-parse", "HEAD"), git(repo, "ls-files", "--stage")
    proof = receipt(repo, "accepted.txt")
    with pytest.raises(CommitRefused):
        call(repo, proof, bad_path)
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "ls-files", "--stage") == stage


def test_no_receipt_empty_set_and_path_mismatch(repo: Path):
    (repo / "accepted.txt").write_text("approved\n", encoding="utf-8")
    head = git(repo, "rev-parse", "HEAD")
    for data, paths in [
        (None, ("accepted.txt",)), (receipt(repo, "accepted.txt"), ()),
        (receipt(repo, "accepted.txt"), ("staged.txt",)),
        ({**receipt(repo, "accepted.txt"), "review_event_id": 0}, ("accepted.txt",)),
        ({**receipt(repo, "accepted.txt"), "task_id": "OTHER"}, ("accepted.txt",)),
    ]:
        with pytest.raises(CommitRefused):
            call(repo, data, *paths)
        assert git(repo, "rev-parse", "HEAD") == head


def test_drift_and_modified_after_review_do_not_commit(repo: Path):
    (repo / "accepted.txt").write_text("approved\n", encoding="utf-8")
    proof = receipt(repo, "accepted.txt")
    initial_head = git(repo, "rev-parse", "HEAD")
    original_index = git(repo, "ls-files", "--stage")
    (repo / "accepted.txt").write_text("changed after acceptance\n", encoding="utf-8")
    with pytest.raises(CommitRefused):
        call(repo, proof, "accepted.txt")
    assert git(repo, "rev-parse", "HEAD") == initial_head
    assert git(repo, "ls-files", "--stage") == original_index

    (repo / "accepted.txt").write_text("approved\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("other\n", encoding="utf-8")
    git(repo, "add", "--", "unrelated.txt")
    git(repo, "commit", "-q", "-m", "unrelated")
    moved = git(repo, "rev-parse", "HEAD")
    with pytest.raises(CommitRefused, match="base"):
        call(repo, proof, "accepted.txt")
    assert git(repo, "rev-parse", "HEAD") == moved


def test_symlink_escape_and_unreviewed_content(repo: Path, tmp_path: Path):
    (repo / "accepted.txt").write_text("approved\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    link = repo / "symlink.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    data = receipt(repo, "symlink.txt")
    with pytest.raises(CommitRefused):
        call(repo, data, "symlink.txt")
    assert not git(repo, "status", "--porcelain", "-uno").startswith("A ")


def test_reject_secret_file_content_even_when_listed(repo: Path):
    (repo / "accepted.txt").write_bytes(b"-----BEGIN PRIVATE KEY-----\nkey\n")
    data = receipt(repo, "accepted.txt")
    with pytest.raises(CommitRefused):
        call(repo, data, "accepted.txt")
