"""Regression: POSIX shell tokenization corrupts absolute Windows gate paths."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dev_mcp.fleet.integrate import Batch, Integrator
from dev_mcp.fleet.store import Store


@pytest.mark.parametrize("command,expected", [
    (r"py -3 C:\hub\dev-mcp\tools\gate_head.py",
     ["py", "-3", r"C:\hub\dev-mcp\tools\gate_head.py"]),
    (r'py -3 "C:\Program Files\tools\gate_head.py"',
     ["py", "-3", r"C:\Program Files\tools\gate_head.py"]),
    ("py -3 tools/check_all.py", ["py", "-3", "tools/check_all.py"]),
])
def test_gate_preserves_trusted_windows_paths(tmp_path, command, expected):
    store = Store.open(str(tmp_path / "registry.sqlite"))
    seen = []

    def runner(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "gate passed", "")

    try:
        with store.tx() as tx:
            tx.change("INSERT INTO projects(name,root,active) VALUES(?,?,1)",
                      ("Fleet Platform", str(tmp_path)), event="seed")
        integrator = Integrator(store, tmp_path / "integrate-state.json", runner=runner,
                                project_info=lambda _: {"gate": command, "commit": True})
        batch = Batch(project="Fleet Platform", root=str(tmp_path),
                      accepts=[{"tz": "fix:TZ-37"}])
        recorded = integrator._gate_locked(batch)
        assert recorded is not None
        assert len(seen) == 1
        assert seen[0][0] == expected
        assert seen[0][1]["shell"] is False
        assert seen[0][1]["cwd"] == str(tmp_path)
        with store.read() as tx:
            assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='gate' AND rc=0") == 1
    finally:
        store.close()


def test_git_head_gate_proves_entire_candidate_in_gitless_snapshot(tmp_path):
    """Regression: nested gate_head failed because a snapshot intentionally has no .git.

    Exercise the real _candidate Git worktree and preflight/pytest paths. The
    venv launcher is replaced by the test runner's Python only in this test;
    no global executable or installed Scheduled Task is modified.
    """
    root = tmp_path / "project"
    root.mkdir()

    def git(*parts):
        return subprocess.run(["git", *parts], cwd=root, check=True,
                              capture_output=True, text=True, encoding="utf-8")

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    package = root / "src" / "pkg"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    module.write_text("value = 1\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_behavior.py").write_text(
        "from pkg import value\n\ndef test_candidate():\n    assert value == 2\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "baseline")
    original = git("rev-parse", "HEAD").stdout.strip()
    fake_python = root / ".venv" / "Scripts" / "python.exe"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("test runner substitutes verified interpreter", encoding="utf-8")
    module.write_text("value = 2\n", encoding="utf-8")

    observed = []

    def runner(argv, **kwargs):
        executable = list(argv)
        if executable[0] == str(fake_python):
            executable[0] = sys.executable
        if "-m" in executable and "pytest" in executable:
            cwd = Path(kwargs["cwd"])
            observed.append((list(executable), cwd, kwargs["env"]["PYTHONPATH"]))
            assert not (cwd / ".git").exists()
            assert (cwd / "src" / "pkg" / "__init__.py").read_text(
                encoding="utf-8"
            ) in ("value = 2\n", "value = 3\n")
            assert kwargs["env"]["PYTHONPATH"].split(__import__("os").pathsep)[0] == str(
                cwd / "src"
            )
        return subprocess.run(executable, **kwargs)

    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        integrator = Integrator(
            store, tmp_path / "cursor.json", runner=runner,
            project_info=lambda _: {
                "gate": subprocess.list2cmdline(['py', '-3', str(Path(__file__).resolve().parents[2] / 'tools/gate_head.py')]),
                "commit": True,
            },
        )
        ok, reason = integrator._candidate(
            str(root), ["src/pkg/__init__.py"], "Fleet Platform",
        )
        assert ok, reason
        assert len(observed) == 2  # full pytest, then --collect-only
        assert "--collect-only" not in observed[0][0]
        assert "--collect-only" in observed[1][0]
        assert git("rev-parse", "HEAD").stdout.strip() == original

        module.write_text("value = 3\n", encoding="utf-8")
        allowed, reason = integrator._candidate(
            str(root), ["src/pkg/__init__.py"], "Fleet Platform",
        )
        assert not allowed and "gate_launch_failed" in reason
        assert git("rev-parse", "HEAD").stdout.strip() == original
        assert module.read_text(encoding="utf-8") == "value = 3\n"
    finally:
        store.close()


def test_candidate_gate_replaces_a_slow_whole_project_gate_on_the_candidate(tmp_path):
    """Beta App 19.10.2026: check_all (13-87 min) on every candidate hit the 300 s budget."""
    root = tmp_path / "project"
    root.mkdir()
    def git(*parts):
        return subprocess.run(["git", *parts], cwd=root, check=True,
                              capture_output=True, text=True, encoding="utf-8")
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (root / "slow_gate.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    (root / "app.py").write_text("value = 2\n", encoding="utf-8")
    gate = subprocess.list2cmdline([sys.executable, "slow_gate.py"])
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        info = {"gate": gate, "commit": True}
        refused, _ = Integrator(store, tmp_path / "c1.json", project_info=lambda _: info)._candidate(
            str(root), ["app.py"], "P")
        assert not refused
        info = {**info, "candidate_gate": subprocess.list2cmdline([sys.executable, "-c", "pass"])}
        ok, reason = Integrator(store, tmp_path / "c2.json", project_info=lambda _: info)._candidate(
            str(root), ["app.py"], "P")
        assert ok, reason
    finally:
        store.close()
