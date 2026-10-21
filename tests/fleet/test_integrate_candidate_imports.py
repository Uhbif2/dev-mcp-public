"""TZ-61: a candidate test must not import modules absent from HEAD + its paths."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dev_mcp.fleet.integrate import Integrator
from dev_mcp.fleet.store import Store


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def test_candidate_test_cannot_import_module_from_dirty_checkout(
    tmp_path: Path, monkeypatch,
) -> None:
    """Run actual pytest collection against HEAD+test, not editable checkout.

    The new test belongs to the commit, but its imported module is untracked.
    PYTHONPATH deliberately points at the dirty checkout, mimicking editable
    installs and inherited Python environment seen by the integration worker.
    """
    root = tmp_path / "project"
    package = root / "src" / "candidate_import_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fleet Test")
    _git(root, "config", "user.email", "fleet@example.invalid")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")

    tests = root / "tests"
    tests.mkdir()
    candidate_test = tests / "test_candidate_import.py"
    candidate_test.write_text(
        "from candidate_import_demo.missing_module import VALUE\n"
        "\n"
        "def test_import_is_present():\n"
        "    assert VALUE == 42\n",
        encoding="utf-8",
    )
    missing = package / "missing_module.py"
    missing.write_text("VALUE = 42\n", encoding="utf-8")
    assert "missing_module.py" not in _git(root, "ls-tree", "-r", "--name-only", "HEAD")
    assert "missing_module.py" in _git(root, "status", "--porcelain", "-uall")

    # This inherited path lets a faulty preflight import modules from the live
    # checkout even though the materialized candidate contains no such module.
    monkeypatch.setenv(
        "PYTHONPATH",
        str(root / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        integ = Integrator(
            store, tmp_path / "cursor.json",
            project_info=lambda _: {
                "commit": True,
                "gate": f'"{sys.executable}" -c "pass"',
            },
        )
        ready, reason = integ._candidate(
            str(root), ["tests/test_candidate_import.py"], "Fleet Platform",
        )
        assert not ready, (
            "unsafe: candidate accepted a test whose module is only in dirty "
            f"checkout (gate reported {reason!r})"
        )
        assert "pytest" in reason.lower() or "import" in reason.lower()
        assert _git(root, "rev-parse", "HEAD") == baseline
        assert missing.read_text(encoding="utf-8") == "VALUE = 42\n"
    finally:
        store.close()
