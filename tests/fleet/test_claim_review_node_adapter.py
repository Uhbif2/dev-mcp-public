"""Sealed Node.js acceptance executes the registered test inside the submitted revision."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from dev_mcp.fleet.claim_review_bite import ClaimReviewError, _execute


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True, capture_output=True, encoding="utf-8", errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def sealed_node_repo(tmp_path: Path) -> tuple[Path, str, str]:
    assert shutil.which("node"), "Node.js required for sealed Node acceptance regression"
    root = tmp_path / "repo with spaces"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Tests")
    _git(root, "config", "user.email", "tests@example.invalid")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "state.txt").write_text("bad\n", encoding="utf-8")
    (root / "tests" / "runtime.test.mjs").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import {readFileSync} from 'node:fs';\n"
        "test('sealed', () => {\n"
        "  assert.equal(readFileSync('src/state.txt', 'utf8').trim(), 'good');\n"
        "  assert.equal(process.env.NODE_OPTIONS, undefined);\n"
        "  assert.equal(process.env.NODE_PATH, undefined);\n"
        "});\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "bad revision")
    bad = _git(root, "rev-parse", "HEAD")
    (root / "src" / "state.txt").write_text("good\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "good revision")
    return root, bad, _git(root, "rev-parse", "HEAD")


def test_node_tests_run_only_against_sealed_revision(sealed_node_repo, monkeypatch):
    root, bad, good = sealed_node_repo
    monkeypatch.setenv("NODE_OPTIONS", "--definitely-not-a-valid-node-option")
    monkeypatch.setenv("NODE_PATH", str(root))
    args = ("node", "--test", "tests/runtime.test.mjs")

    bad_rc, _ = _execute(root, bad, args, python_executable=None, timeout=40)
    good_rc, good_output = _execute(root, good, args, python_executable=None, timeout=40)

    assert bad_rc != 0
    assert good_rc == 0, good_output
    assert "pass 1" in good_output
    assert _git(root, "rev-parse", "HEAD") == good
    assert _git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("argv", [
    ("node", "-e", "process.exit(0)"),
    ("node", "--require", "foreign.js"),
    ("node", "--test", "-e"),
    ("node", "--test", "tests/does-not-exist.mjs"),
    ("node", "--test", "../escape.mjs"),
    ("node", "--test", "tests/runtime.test.txt"),
    ("other", "--test", "tests/runtime.test.mjs"),
])
def test_node_adapter_rejects_out_of_contract_commands(sealed_node_repo, argv):
    root, _, revision = sealed_node_repo
    with pytest.raises(ClaimReviewError):
        _execute(root, revision, argv, python_executable=None, timeout=30)


def test_node_adapter_rejects_test_that_mutates_snapshot(sealed_node_repo):
    root, _, _ = sealed_node_repo
    (root / "tests" / "mutate.test.mjs").write_text(
        "import test from 'node:test';\n"
        "import {writeFileSync} from 'node:fs';\n"
        "test('write', () => writeFileSync('src/state.txt', 'changed'));\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "mutating test")
    revision = _git(root, "rev-parse", "HEAD")
    with pytest.raises(ClaimReviewError, match="mutated"):
        _execute(root, revision, ("node", "--test", "tests/mutate.test.mjs"),
                 python_executable=None, timeout=40)
