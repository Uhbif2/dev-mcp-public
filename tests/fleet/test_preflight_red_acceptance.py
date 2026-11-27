"""Admission: RED pytest baselines are visible, launch/infrastructure failures refuse."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_mcp.fleet import exec_preflight as pre, spec
from dev_mcp.fleet.store import Store


@pytest.mark.parametrize(("rc", "output", "accepted"), [
    (1, "FAILED tests/test_future.py::test_new", True),
    (4, "ERROR: file or directory not found: tests/test_future.py", True),
    (5, "no tests ran", True),
    (1, "ImportError: cannot import system_package", False),
    (1, "ModuleNotFoundError: No module named system_package", False),
    (1, "SyntaxError: invalid syntax", False),
    (4, "pytest: error: unrecognized arguments: --bad-switch", False),
    (2, "pytest internal error", False),
])
def test_red_probe_only_allows_test_failures(tmp_path: Path, rc: int, output: str,
                                              accepted: bool):
    work = tmp_path / "work"
    work.mkdir()
    def runner(argv, **kwargs):
        if "--collect-only" in argv:
            return SimpleNamespace(returncode=0, stdout="collected", stderr="")
        return SimpleNamespace(returncode=rc, stdout=output, stderr="")
    result = pre._probe("python -m pytest tests/test_future.py", work, work, None,
                        runner, sys.executable, 5, None, "a" * 64, False,
                        acceptance_red=True, collection_targets=())
    assert result.ok is accepted
    assert (result.reason == "baseline_red") is accepted


def test_non_pytest_uses_no_red_bypass(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="error", stderr="")
    result = pre._probe("python run_custom.py", work, work, None, runner,
                        sys.executable, 5, None, "a" * 64, False,
                        acceptance_red=True, collection_targets=())
    assert not result.ok and result.reason == "gate_launch_failed"


def test_missing_future_test_must_be_owned(tmp_path: Path):
    owns = ("tests/test_new.py",)
    assert spec._red_acceptance_owned(
        "python -m pytest tests/test_new.py -q", tmp_path, owns)
    assert not spec._red_acceptance_owned(
        "python -m pytest tests/test_other.py -q", tmp_path, owns)
    assert not spec._red_acceptance_owned(
        "python -m pytest tests/test_new.py -q", tmp_path, ("src/feature.py",))
    assert not spec._red_acceptance_owned(
        "python -m compileall src", tmp_path, owns)


def _backlog(tmp_path: Path, command: str):
    root = tmp_path / "project"
    spec_file = root / "ToDo" / "tz" / "TZ-1-future.md"
    spec_file.parent.mkdir(parents=True)
    spec_file.write_text(
        "```yaml\n"
        "tz: TZ-1\n"
        "owns: [tests/test_future.py]\n"
        "acceptance: [" + json.dumps(command) + "]\n"
        "```\n\n# Future feature\n", encoding="utf-8")
    return root


def test_registration_records_red_and_keeps_task(tmp_path: Path, monkeypatch):
    root = _backlog(tmp_path, "python -m pytest tests/test_future.py -q")
    def red_probe(*args, **kwargs):
        return pre.PreflightResult(
            True, "baseline_red",
            pre.ExecutionResult(("python", "-m", "pytest"), 4,
                                "ERROR: file or directory not found: tests/test_future.py", ""))
    monkeypatch.setattr(pre, "preflight_gate", red_probe)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('P',?,1)",
                  (str(root),), event="seed")
    try:
        result = spec.sync_backlog(store, "P", str(root))
        assert not result["errors"], result
        assert any("acceptance RED" in note
                   for notices in result["warnings"].values() for note in notices)
        with store.read() as tx:
            assert tx.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'") is not None
            events = tx.query("SELECT payload FROM events WHERE kind='exec_preflight_red'")
            assert len(events) == 1
            assert json.loads(events[0]["payload"])["rc"] == 4
    finally:
        store.close()


@pytest.mark.parametrize("bad_output", [
    "ImportError: broken external dependency",
    "SyntaxError: invalid infrastructure command",
])
def test_registration_rejects_infrastructure_red(tmp_path: Path, monkeypatch,
                                                 bad_output: str):
    root = _backlog(tmp_path, "python -m pytest tests/test_future.py -q")
    def rejected_probe(*args, **kwargs):
        return pre.PreflightResult(
            False, "gate_launch_failed",
            pre.ExecutionResult(("python", "-m", "pytest"), 1, "", bad_output))
    monkeypatch.setattr(pre, "preflight_gate", rejected_probe)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('P',?,1)",
                  (str(root),), event="seed")
    try:
        result = spec.sync_backlog(store, "P", str(root))
        assert result["errors"], result
        with store.read() as tx:
            assert tx.one("SELECT tz FROM tasks WHERE project='P' AND tz='TZ-1'") is None
            assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='exec_preflight_red'") == 0
    finally:
        store.close()
