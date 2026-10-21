"""Revalidating an accepted RED-phase stage must not demand red scenarios again."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from dev_mcp.fleet.integrate import revalidation_acceptance
from dev_mcp.intents import stage_check

CHECKER = Path(stage_check.__file__)


def test_executable_goal_acceptance_is_mapped_for_revalidation():
    raw = json.dumps(["py -3 .intent/check.py executable_goal", "py -3 -m pytest -q"])
    assert json.loads(revalidation_acceptance(raw)) == [
        "py -3 .intent/check.py executable_goal_published", "py -3 -m pytest -q"]
    other = json.dumps(["py -3 .intent/check.py implemented"])
    assert revalidation_acceptance(other) == other
    assert revalidation_acceptance("not json") == "not json"


def _project(tmp_path: Path, scenarios: str) -> Path:
    (tmp_path / ".intent").mkdir()
    (tmp_path / ".intent" / "check.py").write_bytes(CHECKER.read_bytes())
    (tmp_path / "intent").mkdir()
    (tmp_path / "intent" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "intent" / "goal.md").write_text(
        "# Цель\n## Результат\n" + "результат " * 30 + "\n## Сценарии\n" + "сценарий " * 20
        + "\n## Вне рамок\nz\n", encoding="utf-8")
    (tmp_path / "intent" / "scenarios.py").write_text(scenarios, encoding="utf-8")
    return tmp_path


def _check(root: Path, stage: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(root / ".intent" / "check.py"), stage],
                          capture_output=True, cwd=root, timeout=120)


def test_green_scenarios_fail_red_stage_but_pass_published(tmp_path):
    root = _project(tmp_path, "raise SystemExit(0)\n")
    assert _check(root, "executable_goal").returncode == 1
    assert _check(root, "executable_goal_published").returncode == 0


def test_published_still_rejects_broken_scenarios(tmp_path):
    root = _project(tmp_path, "def broken(:\n")
    done = _check(root, "executable_goal_published")
    assert done.returncode == 1
    assert b"invalid syntax (scenarios.py" in done.stdout
