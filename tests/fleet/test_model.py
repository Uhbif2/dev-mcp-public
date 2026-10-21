from __future__ import annotations

import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.model import Decision, Message, Task, Worker, norm_path, paths_overlap  # noqa: E402


def _canonical(path: str) -> str:
    value = os.path.realpath(path).replace("\\", "/").rstrip("/")
    return value.casefold()


def test_public_dataclasses_are_frozen() -> None:
    task = Task(
        project="dev-mcp",
        tz="TZ-01",
        state="ready",
        priority=1,
        kind="work",
        spec_path="ToDo/fleet/TZ-01-store-and-model.md",
        spec_sha="abc",
        needs=(),
        owns=("src/dev_mcp/fleet/model.py",),
        resources=(),
        acceptance=("pytest",),
        title="Store",
        body="",
    )
    worker = Worker("W1", "tab", "chatgpt", False, "")
    message = Message("m-1", "note", "W2", "hello", 3)
    decision = Decision(True, "", "", "W1", False)

    with pytest.raises(FrozenInstanceError):
        task.state = "taken"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        worker.revoked = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        message.age_min = 4  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.allow = False  # type: ignore[misc]


def test_norm_path_normalizes_case_separators_and_trailing_slash(tmp_path) -> None:
    project = tmp_path / "Project"
    project.mkdir()
    raw = str(project / "OPS" / "Probe")
    if os.name == "nt":
        raw = raw.replace("/", "\\")
    raw += os.sep

    assert norm_path(raw) == _canonical(str(project / "OPS" / "Probe"))


def test_norm_path_resolves_dotdot(tmp_path) -> None:
    project = tmp_path / "Project"
    project.mkdir()
    raw = os.path.join(str(project), "ops", "tmp", "..", "probe")

    assert norm_path(raw) == _canonical(str(project / "ops" / "probe"))


def test_norm_path_relative_requires_root(tmp_path) -> None:
    with pytest.raises(ValueError, match="project_root"):
        norm_path("ops/probe")

    assert norm_path("OPS/probe", project_root=str(tmp_path)) == _canonical(
        str(tmp_path / "OPS" / "probe")
    )


def test_norm_path_preserves_supported_subtree_marker(tmp_path) -> None:
    expected = _canonical(str(tmp_path / "tests")) + "/**"
    assert norm_path("tests/**", project_root=str(tmp_path)) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "tests/*",
        "tests/?",
        "tests/[ab]",
        "tests/**/unit",
        "te*sts/file.py",
    ],
)
def test_norm_path_rejects_other_globs(tmp_path, raw: str) -> None:
    with pytest.raises(ValueError, match="trailing '/\*\*'"):
        norm_path(raw, project_root=str(tmp_path))


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("ops/probe", "ops/probe/runner.py", True),
        ("ops/probe/runner.py", "ops/probe", True),
        ("ops/probe", "ops/probe2", False),
        ("ops/probe", "ops/selfheal", False),
        ("client/win/app.py", "client/win/app.py", True),
        ("OPS/Probe", "ops/probe/runner.py", True),
        ("ops\\probe", "OPS/probe/runner.py", True),
        ("tests/**", "tests/fleet/test_store.py", True),
    ],
)
def test_paths_overlap_segmentwise(a: str, b: str, expected: bool) -> None:
    assert paths_overlap(a, b) is expected
    assert paths_overlap(b, a) is expected
