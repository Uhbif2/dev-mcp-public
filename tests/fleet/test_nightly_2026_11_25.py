"""Регрессии по кандидатам ночного ревью dev-mcp-self 25.11.2026 (каждый подтверждён чтением исходника).

1. integrate._candidate: ключ памяти PASS не учитывал gate/candidate_gate/deps проекта.
2. snapshot_integrate.commit_blobs: ошибка ls-tree → b'' → режим 100644 вместо 100755 (fail-open).
3. world._acquire_run_slot: при capacity>1 чужие прогоны (scan_processes) не занимали слоты.
4. server search_files: вложенная неограниченная повторяемость вешала поток (re без таймаута).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from dev_mcp import server
from dev_mcp.fleet import snapshot_integrate, world
from dev_mcp.fleet.integrate import Integrator


# ---------------------------------------------------------------- 1
def _integrator(cfg: dict, runs: list) -> Integrator:
    integ = object.__new__(Integrator)
    integ.project_info = lambda project: cfg
    integ._fingerprints = lambda root, changed: {rel: "sha" for rel in changed}
    integ._head = lambda root, ref: "head1"

    def run(root, changed, project, acceptance=None, *, full=False, contents=None):
        runs.append(dict(cfg))
        return True, "ok"

    integ._candidate_run = run
    return integ


def test_candidate_memo_key_includes_gate_config_and_deps(tmp_path):
    (tmp_path / "vendor").mkdir()
    cfg = {"gate": "pytest -q", "candidate_gate": "", "deps": ["vendor"]}
    runs: list = []
    integ = _integrator(cfg, runs)
    assert integ._candidate(str(tmp_path), ["a.py"], "P") == (True, "ok")
    assert integ._candidate(str(tmp_path), ["a.py"], "P") == (True, "ok")
    assert len(runs) == 1                                  # прежняя экономия сохранена
    cfg["candidate_gate"] = "py -3 check_fast.py"
    integ._candidate(str(tmp_path), ["a.py"], "P")
    assert len(runs) == 2                                  # смена candidate_gate — новый прогон
    (tmp_path / "vendor" / "new_dep.py").write_text("x = 1\n", encoding="utf-8")
    integ._candidate(str(tmp_path), ["a.py"], "P")
    assert len(runs) == 3                                  # изменились deps — новый прогон


# ---------------------------------------------------------------- 2
def test_commit_blobs_refuses_when_ls_tree_fails(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(snapshot_integrate, "_git", lambda root, *a, **k: calls.append(a) or b"0" * 40)
    monkeypatch.setattr(snapshot_integrate, "_git_raw", lambda root, *a, **k: None)
    with pytest.raises(snapshot_integrate.CommitRefused, match="ls-tree"):
        snapshot_integrate.commit_blobs(tmp_path, {"run.sh": "b" * 40}, "msg")
    assert not any(a and a[0] in ("update-index", "commit-tree", "update-ref") for a in calls)


def test_git_paths_chunks_long_argv_and_fails_closed(monkeypatch, tmp_path):
    seen = []

    def raw(root, *args, **kw):
        seen.append(len(args))
        return b"x"

    monkeypatch.setattr(snapshot_integrate, "_git_raw", raw)
    paths = [f":(literal)f{i}" for i in range(677)]
    assert snapshot_integrate._git_paths(tmp_path, ("ls-tree", "-z", "HEAD"), paths) == b"x" * 4
    assert max(seen) <= 3 + 1 + snapshot_integrate._PATHS_PER_CALL
    answers = iter([b"x", None, b"x", b"x"])
    monkeypatch.setattr(snapshot_integrate, "_git_raw", lambda root, *a, **k: next(answers))
    assert snapshot_integrate._git_paths(tmp_path, ("ls-tree",), paths) is None


# ---------------------------------------------------------------- 3
def test_multi_slot_counts_foreign_runs(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(available=8 * 1024 ** 3)))
    info = {"capacity": 2, "scan_processes": True, "run_match": r"\bbuild\b"}
    monkeypatch.setattr(world, "_foreign_runs", lambda pattern: ["pid 1 с 10:00: python build.py",
                                                                   "pid 2 с 10:01: python build.py"])
    name = "res-test-foreign"
    slot, holder = world._acquire_run_slot(name, info, "W1", deadline=0)
    assert slot == "" and "вне учёта dev-mcp" in holder
    monkeypatch.setattr(world, "_foreign_runs", lambda pattern: ["pid 1 с 10:00: python build.py"])
    slot, holder = world._acquire_run_slot(name, info, "W1", deadline=0)
    assert slot and holder == ""
    slot2, holder2 = world._acquire_run_slot(name, info, "W2", deadline=0)
    assert slot2 == "" and holder2                          # 1 свой + 1 чужой = ёмкость 2
    world._run_lock(slot).release()
    with world._run_guard:
        world._run_holders.pop(slot, None)


# ---------------------------------------------------------------- 4
@pytest.mark.parametrize("query", [r"(a+)+$", r"(\w*)*x", r"(x+y?)*z", r"(?:a{1,3})+b"])
def test_regex_with_nested_unbounded_repeat_is_refused(query):
    assert "отклонено" in server._regex_risk(query)


@pytest.mark.parametrize("query", [r"foo.*bar", r"\bTZ-\d+\b", r"(ab|cd)+", r"^\s*def (\w+)\(", r"a{2,}b",
                                   r"(?i)fleet_(?:block|submit)"])
def test_ordinary_regex_is_allowed(query):
    assert server._regex_risk(query) == ""
