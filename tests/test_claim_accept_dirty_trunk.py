"""Урок 55: доверенная интеграция не требует полностью чистого общего ствола."""
from __future__ import annotations

import subprocess
from pathlib import Path

from dev_mcp.fleet.claim_accept_flow import _dirty_overlap


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_only_dirt_in_accepted_paths_blocks(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "ToDo").mkdir()
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")          # чужая правка
    (tmp_path / "ToDo" / "draft.md").write_text("x", encoding="utf-8")   # движение контроллера
    assert _dirty_overlap(tmp_path, ("a.py", "tests/test_a.py")) == []
    (tmp_path / "a.py").write_text("a = 2\n", encoding="utf-8")
    assert _dirty_overlap(tmp_path, ("a.py",)) == ["a.py"]
