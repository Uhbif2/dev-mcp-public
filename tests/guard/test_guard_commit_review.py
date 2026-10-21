"""Regression: quotePath must not turn a ToDo-only commit into unreviewed code."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from guard.core import DeployGuard, GuardConfig


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[DeployGuard, Path, str]:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.name", "Guard Regression")
    _git(work, "config", "user.email", "guard-regression@example.invalid")
    (work / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-qm", "baseline")
    prior = _git(work, "rev-parse", "HEAD")
    return DeployGuard(GuardConfig(live=tmp_path / "live", work=work)), work, prior


def _commit(work: Path) -> str:
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "manually registered draft")
    return _git(work, "rev-parse", "HEAD")


def test_unicode_todo_only_is_exempt_from_review(tmp_path: Path) -> None:
    guard, work, prior = _repo(tmp_path)
    todo = work / "ToDo" / "tz"
    todo.mkdir(parents=True)
    (todo / "TZ-67-сторож-живости.md").write_text("draft\n", encoding="utf-8")
    candidate = _commit(work)

    # Git normally octal-quotes Cyrillic paths. -z must keep the real ToDo/ prefix.
    quoted = _git(work, "show", "--name-only", "--format=", candidate)
    assert quoted.startswith('"ToDo/')
    assert guard._unreviewed(prior, candidate) == []


def test_mixed_todo_and_source_commit_still_requires_review(tmp_path: Path) -> None:
    guard, work, prior = _repo(tmp_path)
    todo = work / "ToDo"
    todo.mkdir()
    (todo / "план.md").write_text("plan\n", encoding="utf-8")
    source = work / "src"
    source.mkdir()
    (source / "module.py").write_text("answer = 42\n", encoding="utf-8")
    candidate = _commit(work)

    pending = guard._unreviewed(prior, candidate)
    assert len(pending) == 1
    assert pending[0].startswith(candidate[:9])


def test_unreviewed_source_only_stays_blocked(tmp_path: Path) -> None:
    guard, work, prior = _repo(tmp_path)
    source = work / "src"
    source.mkdir()
    (source / "unreviewed.py").write_text("enabled = True\n", encoding="utf-8")
    candidate = _commit(work)

    pending = guard._unreviewed(prior, candidate)
    assert len(pending) == 1
    assert "src/unreviewed.py" in pending[0]


@pytest.mark.parametrize(
    ("git_path", "requires_review"),
    [
        ("ToDo\\code.py", True),
        ("ToDo/tz/документ.md", False),
    ],
)
def test_nul_git_paths_do_not_treat_literal_backslash_as_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_path: str,
    requires_review: bool,
) -> None:
    guard, work, prior = _repo(tmp_path)
    (work / "README.md").write_text("changed\n", encoding="utf-8")
    candidate = _commit(work)
    original_git = guard.git

    def fake_git(repo: Path, *args: str, ok: bool = True) -> str:
        if args and args[0] == "show":
            assert "--name-only" in args
            assert "-z" in args
            return git_path + "\0"
        return original_git(repo, *args, ok=ok)

    monkeypatch.setattr(guard, "git", fake_git)
    pending = guard._unreviewed(prior, candidate)
    assert bool(pending) is requires_review
    if requires_review:
        assert pending[0].startswith(candidate[:9])


def test_rename_from_todo_to_source_is_not_exempt(tmp_path: Path) -> None:
    guard, work, _ = _repo(tmp_path)
    todo = work / "ToDo"
    todo.mkdir()
    (todo / "переезд.md").write_text("draft\n", encoding="utf-8")
    _commit(work)
    prior = _git(work, "rev-parse", "HEAD")
    source = work / "src"
    source.mkdir()
    _git(work, "mv", "ToDo/переезд.md", "src/переезд.md")
    candidate = _commit(work)

    pending = guard._unreviewed(prior, candidate)
    assert len(pending) == 1
    assert "src/переезд.md" in pending[0]
