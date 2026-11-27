"""Repeated checkout failures must not rebuild the same impossible Git HEAD."""
from pathlib import Path
import subprocess

from dev_mcp.fleet import integrate


def _integrator(tmp_path, monkeypatch):
    runner = integrate.Integrator(
        object(), tmp_path / "integrate-state.json",
        runner=lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""),
        project_info=lambda _: {},
    )
    sha = {"value": "a" * 40}
    monkeypatch.setattr(runner, "_head", lambda root, rev: sha["value"])
    return runner, sha


def test_checkout_longpath_failure_is_shared_by_accepts_for_same_head(tmp_path, monkeypatch):
    runner, sha = _integrator(tmp_path, monkeypatch)
    errors = []
    def fail(*args, **kwargs):
        errors.append(list(args[1]))
        return False, "checkout_longpath: error: unable to create file path: Filename too long"
    monkeypatch.setattr(runner, "_candidate_run", fail)
    first = runner._candidate(str(tmp_path), ["one.py"], "P", contents={"one.py": b"x"})
    other = runner._candidate(str(tmp_path), ["two.py"], "P", contents={"two.py": b"y"})
    assert first == other
    assert errors == [["one.py"]]
    # A changed HEAD invalidates the negative cache immediately.
    sha["value"] = "b" * 40
    runner._candidate(str(tmp_path), ["two.py"], "P", contents={"two.py": b"y"})
    assert errors == [["one.py"], ["two.py"]]


def test_checkout_longpath_cache_expires_for_external_git_configuration(tmp_path, monkeypatch):
    runner, _ = _integrator(tmp_path, monkeypatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(integrate.time, "monotonic", lambda: clock["now"])
    calls = []
    monkeypatch.setattr(runner, "_candidate_run", lambda *a, **kw:
                        (calls.append(1) or (False, "checkout_longpath: error: unable to create file X: Filename too long")))
    runner._candidate(str(tmp_path), ["one.py"], "P", contents={"one.py": b"x"})
    clock["now"] += integrate.WORKTREE_LONGPATH_CACHE_S - 1
    runner._candidate(str(tmp_path), ["two.py"], "P", contents={"two.py": b"x"})
    assert len(calls) == 1
    clock["now"] += 2
    runner._candidate(str(tmp_path), ["two.py"], "P", contents={"two.py": b"x"})
    assert len(calls) == 2


def test_checkout_failure_is_classified_without_progress_noise(tmp_path, monkeypatch):
    runner, _ = _integrator(tmp_path, monkeypatch)
    calls = []
    error = ("Preparing worktree (detached HEAD abc)\nUpdating files: 19%\r"
             "error: unable to create file long/filename.txt: Filename too long\n"
             "fatal: unable to checkout working tree\n")
    def failed_git(_root, *args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", error)
    monkeypatch.setattr(runner, "_git", failed_git)
    ok, reason = runner._candidate_run(str(tmp_path), [], "P")
    assert not ok
    assert reason == ("checkout_longpath: error: unable to create file "
                      "long/filename.txt: Filename too long")
    assert len(calls) == 1


def test_other_transient_errors_do_not_poison_project_cache(tmp_path, monkeypatch):
    runner, _ = _integrator(tmp_path, monkeypatch)
    calls = []

    def transient(*args, **kwargs):
        calls.append(args)
        return False, "import: missing runtime dependency"

    monkeypatch.setattr(runner, "_candidate_run", transient)
    for rel in ("one.py", "two.py"):
        runner._candidate(str(tmp_path), [rel], "P", contents={rel: b"x"})
    assert len(calls) == 2
