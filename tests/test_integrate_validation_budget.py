"""Candidate validation is selective while collection and the trunk gate stay full."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dev_mcp.fleet.integrate import Integrator  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402


def git(root: Path, *args: str):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True, encoding="utf-8")


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "source"
    (root / "src" / "widget").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "widget" / "__init__.py").write_text("", encoding="utf-8")
    module = root / "src" / "widget" / "core.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_business.py").write_text(
        "from widget.core import VALUE\n\ndef test_value(): assert VALUE == 1\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_unrelated.py").write_text(
        "def test_other(): assert True\n", encoding="utf-8",
    )
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.test")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    module.write_text("VALUE = 2\n", encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    yield store, root, tmp_path
    store.close()


def fake_runner(calls, *, timeout=False):
    def run(argv, **kwargs):
        argv = list(argv)
        if Path(argv[0]).name.lower() in {"git", "git.exe"}:
            return subprocess.run(argv, **kwargs)
        calls.append(argv)
        if (timeout and "-m" in argv and "pytest" in argv
                and "--collect-only" not in argv):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def test_candidate_runs_collection_selected_importer_and_task_acceptance(candidate):
    store, root, tmp = candidate
    calls = []
    integ = Integrator(
        store, tmp / "cursor.json", runner=fake_runner(calls),
        project_info=lambda _: {"gate": "python -m pytest -q"},
    )
    ok, reason = integ._candidate(
        str(root), ["src/widget/core.py"], "Project",
        ["python -m pytest -q tests/test_unrelated.py"],
    )
    assert (ok, reason) == (True, "")
    full_collect = [argv for argv in calls if "--collect-only" in argv]
    assert full_collect, "unrestricted collection must still run"
    scoped = [argv for argv in calls if "-m" in argv and "pytest" in argv
              and "--collect-only" not in argv]
    assert scoped
    assert any("tests/test_business.py" in argv for argv in scoped), (
        "AST-importing test must be included even though its name differs")
    assert all(any(part.startswith("tests/") for part in argv) for argv in scoped), (
        "no full pytest run allowed in candidate validation")
    assert any("tests/test_unrelated.py" in argv for argv in scoped), (
        "task acceptance must also execute, not just affected tests")


def test_validation_timeout_is_not_gate_launch_failure(candidate):
    store, root, tmp = candidate
    calls = []
    integ = Integrator(
        store, tmp / "cursor.json", runner=fake_runner(calls, timeout=True),
        project_info=lambda _: {"gate": "python -m pytest -q"},
    )
    ok, reason = integ._candidate(str(root), ["src/widget/core.py"], "Project")
    assert not ok
    assert reason == "validation_timeout"
    assert all("gate_launch_failed" not in str(c) for c in calls)


def test_changed_test_and_conftest_selection(candidate):
    _, root, _ = candidate
    assert Integrator._candidate_tests(root, ["tests/test_unrelated.py"]) == [
        "tests/test_unrelated.py"
    ]
    assert Integrator._candidate_tests(root, ["tests/conftest.py"]) == [
        "tests/test_business.py", "tests/test_unrelated.py",
    ]


def test_candidate_selects_suffix_pytest_importer(candidate):
    _, root, _ = candidate
    (root / "tests" / "business_test.py").write_text(
        "from widget.core import VALUE\n\ndef test_value(): assert VALUE == 1\n",
        encoding="utf-8",
    )
    assert Integrator._candidate_tests(root, ["src/widget/core.py"]) == [
        "tests/business_test.py", "tests/test_business.py",
    ]


def test_suffix_pytest_fixture_without_inline_import_is_selected(candidate):
    _, root, _ = candidate
    (root / "tests" / "conftest.py").write_text(
        "import pytest\nfrom widget.core import VALUE\n"
        "@pytest.fixture\ndef widget_fixture():\n    return VALUE\n",
        encoding="utf-8",
    )
    (root / "tests" / "core_test.py").write_text(
        "def test_value(widget_fixture):\n    assert widget_fixture == 1\n",
        encoding="utf-8",
    )
    assert Integrator._candidate_tests(root, ["src/widget/core.py"]) == [
        "tests/core_test.py", "tests/test_business.py",
    ]
