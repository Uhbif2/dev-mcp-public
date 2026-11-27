"""Git subprocesses used by fleet integration must support long Windows paths."""
from pathlib import Path
import subprocess

from dev_mcp.fleet import bite, integrate, manual_commit, snapshot_integrate


def test_integrator_git_enables_longpaths_for_worktree_add(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    worker = integrate.Integrator(object(), tmp_path / "cursor.json", runner=runner)
    result = worker._git(str(tmp_path), "worktree", "add", "--detach", str(tmp_path / "tree"), "HEAD")
    assert result.returncode == 0
    assert calls[0][:5] == ["git", "-c", "core.longpaths=true", "-c", "core.quotepath=off"]
    assert calls[0][5:7] == ["worktree", "add"]


def test_bite_git_enables_longpaths_for_worktree_add():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    bite._git("Z:/project", "worktree", "add", "--detach", "Z:/candidate", "HEAD", runner=runner)
    assert calls[0][:5] == ["git", "-c", "core.longpaths=true", "-C", "Z:/project"]
    assert calls[0][5:7] == ["worktree", "add"]


def test_snapshot_raw_git_enables_longpaths(tmp_path, monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(snapshot_integrate.subprocess, "run", runner)
    assert snapshot_integrate._git_raw(Path(tmp_path), "cat-file", "blob", "abc") == b"ok"
    assert calls[0][:3] == ["git", "-c", "core.longpaths=true"]
    assert calls[0][3:5] == ["cat-file", "blob"]


def test_snapshot_commit_helper_enables_longpaths(tmp_path, monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"deadbeef\n", b"")

    monkeypatch.setattr(manual_commit.subprocess, "run", runner)
    assert manual_commit._git(Path(tmp_path), "rev-parse", "HEAD") == b"deadbeef"
    assert calls[0][:3] == ["git", "-c", "core.longpaths=true"]
    assert calls[0][3:] == ["rev-parse", "HEAD"]


def test_trunk_gate_checkout_and_cleanup_enable_longpaths(tmp_path, monkeypatch):
    from dev_mcp.fleet import exec_preflight

    worker = integrate.Integrator(
        object(), tmp_path / "cursor.json", project_info=lambda _: {},
    )
    monkeypatch.setattr(worker, "_head", lambda root, rev: "a" * 40)
    monkeypatch.setattr(exec_preflight, "project_deps", lambda *_: {})
    monkeypatch.setattr(exec_preflight, "link_deps", lambda *_: [])
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(integrate, "bounded_run", run)
    with worker._gate_checkout(str(tmp_path), "P") as (_tree, revision):
        assert revision == "a" * 40
    assert calls[0][:5] == [
        "git", "-c", "core.longpaths=true", "-c", "core.hooksPath=",
    ]
    assert calls[0][5:7] == ["worktree", "add"]
    assert calls[1][:5] == ["git", "-c", "core.longpaths=true", "worktree", "remove"]
