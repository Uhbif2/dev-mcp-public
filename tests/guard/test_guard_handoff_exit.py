"""Урок 39: успешная передача управления — не авария; урок 40: кириллица в путях."""
from __future__ import annotations

import json

from tests.guard.test_guard_platform_core import git, lane, make_guard  # noqa: F401
from tests.guard.test_guard_self_update import FakeChild, _load_supervisor


def _setup(tmp_path, monkeypatch, supervisor):
    runtime = tmp_path / "data" / "guard-runtime"
    for version in ("old", "new"):
        (runtime / version / "guard").mkdir(parents=True)
        (runtime / version / "guard/__main__.py").write_text("# immutable")
    state = tmp_path / "state.json"
    journal = tmp_path / "events.jsonl"
    pulse = tmp_path / "guard-heartbeat.json"
    for name, value in (("HERE", runtime), ("STATE_DIR", tmp_path), ("STATE_FILE", state),
                        ("JOURNAL", journal), ("PULSE_FILE", pulse)):
        monkeypatch.setattr(supervisor, name, value)
    state.write_text(json.dumps({
        "guard_known_good": "old",
        "self_update": {"candidate": "goodSHA", "previous": "old",
                        "version": "new", "token": "tok"}}), encoding="utf-8")
    return state, journal, pulse


def test_old_supervisor_exits_quietly_after_confirmed_handoff(tmp_path, monkeypatch):
    supervisor = _load_supervisor()
    state, journal, pulse = _setup(tmp_path, monkeypatch, supervisor)
    child = FakeChild()
    child.exited = True  # старый страж вышел после пульса преемника
    pulse.write_text(json.dumps({"pid": 9999, "version": "new", "handoff_token": "tok",
                                 "time": "t"}), encoding="utf-8")
    result = supervisor.supervise_once("old", launch=lambda *_: child,
                                       clock=lambda: 0, sleep=lambda _s: None)
    assert result == supervisor.HANDED_OFF
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["self_update"]["version"] == "new"
    assert "guard_blocked_candidate" not in saved
    assert "handoff_exit" in journal.read_text(encoding="utf-8")


def test_old_child_exit_without_successor_pulse_still_reverts(tmp_path, monkeypatch):
    supervisor = _load_supervisor()
    state, _journal, pulse = _setup(tmp_path, monkeypatch, supervisor)
    child = FakeChild()
    child.exited = True
    pulse.write_text(json.dumps({"pid": 9999, "version": "new", "handoff_token": "WRONG",
                                 "time": "t"}), encoding="utf-8")
    assert supervisor.supervise_once("old", launch=lambda *_: child,
                                     clock=lambda: 0, sleep=lambda _s: None) == "old"
    assert json.loads(state.read_text(encoding="utf-8"))["guard_blocked_candidate"] == "goodSHA"


def test_main_stops_after_handoff(monkeypatch):
    supervisor = _load_supervisor()
    calls = []
    monkeypatch.setattr(supervisor, "_choose", lambda _s: "old")
    monkeypatch.setattr(supervisor, "_json", lambda _p: {})
    monkeypatch.setattr(supervisor, "supervise_once",
                        lambda v, t="": calls.append(v) or getattr(supervisor, "HANDED_OFF", "x"))

    def no_loop(_s):
        raise AssertionError("supervisor kept looping after handoff")

    monkeypatch.setattr(supervisor.time, "sleep", no_loop)
    supervisor.main([])
    assert calls == ["old"]


def test_cyrillic_todo_commit_is_reviewed_as_todo(lane):
    live, work, _db, _state = lane
    prior = git(work, "rev-parse", "HEAD")
    (work / "ToDo").mkdir(exist_ok=True)
    (work / "ToDo" / "TZ-67-сторож-живости.md").write_text("x", encoding="utf-8")
    git(work, "add", "ToDo")
    git(work, "commit", "-m", "draft")
    guard, _ = make_guard(lane)
    assert guard._unreviewed(prior, git(work, "rev-parse", "HEAD")) == []


def _load_from(path, monkeypatch, env=None):
    import importlib.machinery
    import importlib.util
    if env is None:
        monkeypatch.delenv("DEV_MCP_GUARD_RUNTIME", raising=False)
    else:
        monkeypatch.setenv("DEV_MCP_GUARD_RUNTIME", str(env))
    loader = importlib.machinery.SourceFileLoader("rg_staged", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_staged_launcher_resolves_shared_runtime(tmp_path, monkeypatch):
    """Урок 41: копия лончера в папке версии ищет версии в общем каталоге."""
    import shutil
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / "guard" / "run_guard.pyw"
    runtime = tmp_path / "live" / "data" / "guard-runtime"
    staged = runtime / "v2"
    (staged / "guard").mkdir(parents=True)
    (staged / "guard" / "__main__.py").write_text("# v2")
    shutil.copy2(source, staged / "run_guard.pyw")
    shutil.copy2(source, runtime / "run_guard.pyw")
    for env in (None, runtime):
        module = _load_from(staged / "run_guard.pyw", monkeypatch, env)
        assert module.HERE == runtime.resolve()
        assert module.LIVE == (tmp_path / "live").resolve()
        assert module._installed("v2")
    stable = _load_from(runtime / "run_guard.pyw", monkeypatch)
    assert stable.HERE == runtime.resolve()
