"""План консорциума 11.10, п.1: гейт один раз на неизменный ключ входов; отложенное — по новому факту."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_integrate import env, event, events, git, make  # noqa: E402,F401
from dev_mcp.fleet.integrate import Batch  # noqa: E402

GATE = f'"{sys.executable}" -c "print(1)"'

@pytest.fixture(autouse=True)
def _no_inherited_pythonpath(monkeypatch):
    """Урок 53: проверка кандидата у интегратора гонит pytest с PYTHONPATH=<снимок>/src;<снимок>;
    ключ гейта сканирует его — тесты мемо и параллельности краснели только там, и ни одна
    правка Fleet Platform не проходила интеграцию. Нужный тесту PYTHONPATH он задаёт сам."""
    monkeypatch.delenv("PYTHONPATH", raising=False)



def test_same_tree_same_command_gates_once(env):
    store, root, _rootn, tmp = env
    integ = make(store, tmp, gate=GATE)
    batch = Batch(project="P", root=str(root), accepts=[{"tz": "TZ-1"}])
    assert integ._gate(batch) is not None
    assert integ._gate(batch) is None  # неизменный HEAD и команда — без повторного прогона
    assert len(events(store, "gate")) == 1
    (root / "new.txt").write_text("x\n", encoding="utf-8")
    git(root, "add", "-A"); git(root, "commit", "-q", "-m", "change")
    assert integ._gate(batch) is not None  # новый HEAD — новый ключ
    assert len(events(store, "gate")) == 2


def test_infrastructure_failure_is_not_memoized(env):
    store, root, _rootn, tmp = env
    integ = make(store, tmp, gate="definitely-not-an-executable-xyz")
    batch = Batch(project="P", root=str(root), accepts=[{"tz": "TZ-1"}])
    first = integ._gate(batch)
    assert first is not None and first["rc"] in (124, 126, 127)
    assert integ._gate(batch) is not None  # сбой запуска — не результат, гоняется снова


def test_pending_retry_waits_for_new_fact(env, monkeypatch):
    store, root, _rootn, tmp = env
    integ = make(store, tmp)
    calls = []
    monkeypatch.setattr(integ, "_pending_accepts", lambda: [{"id": 1, "project": "P", "tz": "TZ-1", "worker_id": "W1"}])
    monkeypatch.setattr(integ, "_commit", lambda b, a: calls.append(a["tz"]) or {"kind": "integrate"})
    integ._save_cursor(10**9)
    integ.tick(); integ.tick()
    assert calls == ["TZ-1"]  # второй тик без нового факта — ничего
    event(store, "submit", "TZ-2")
    integ.tick()
    assert calls == ["TZ-1", "TZ-1"]


def test_pythonpath_dependency_content_changed_rekeys(env, tmp_path: Path, monkeypatch):
    """A stable PYTHONPATH string does not imply stable imported code."""
    store, root, _rootn, tmp = env
    imported = tmp_path / "external-imports"
    imported.mkdir()
    source = imported / "dependency.py"
    source.write_text("VALUE=1\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(imported))
    integ = make(store, tmp)
    monkeypatch.setattr(integ, "_head", lambda *_: "fixed-head")
    batch = Batch(project="P", root=str(root))
    command = f'"{sys.executable}" -B -c "import dependency;print(dependency.VALUE)"'

    def imported_value() -> str:
        return subprocess.run(
            [sys.executable, "-B", "-c", "import dependency;print(dependency.VALUE)"],
            cwd=root, env=os.environ.copy(), capture_output=True, text=True, check=True,
            timeout=10,
        ).stdout.strip()

    first = integ._gate_key(batch, command)
    assert first
    assert imported_value() == "1"
    original_stat = source.stat()
    source.write_text("VALUE=2\n", encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert imported_value() == "2"
    second = integ._gate_key(batch, command)
    assert second and second != first, "content changed at identical PYTHONPATH, size and mtime"

    # Transient interpreter bytecode is not a semantic input.
    cache = imported / "__pycache__"
    cache.mkdir()
    (cache / "dependency.cpython-312.pyc").write_bytes(b"ephemeral")
    assert integ._gate_key(batch, command) == second


def test_missing_pythonpath_dependency_disables_gate_memo(env, tmp_path: Path, monkeypatch):
    store, root, _rootn, tmp = env
    integ = make(store, tmp)
    monkeypatch.setattr(integ, "_head", lambda *_: "fixed-head")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "missing-import-root"))
    batch = Batch(project="P", root=str(root))
    assert integ._gate_key(batch, GATE) == ""


def test_pythonpath_empty_namespace_directory_changes_imports_and_key(env, tmp_path: Path, monkeypatch):
    """PEP 420 imports depend on directories even when no source file exists."""
    store, root, _rootn, tmp = env
    imported = tmp_path / "outside-repo"
    imported.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(imported))
    integ = make(store, tmp)
    monkeypatch.setattr(integ, "_head", lambda *_: "fixed-head")
    batch = Batch(project="P", root=str(root))
    command = f'"{sys.executable}" -B -c "import namespace_only"'

    def resolves_namespace() -> bool:
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import importlib.util;print(bool(importlib.util.find_spec('namespace_only')))"],
            cwd=root, env=os.environ.copy(), capture_output=True, text=True, check=True, timeout=10,
        )
        return result.stdout.strip() == "True"

    before = integ._gate_key(batch, command)
    assert before and not resolves_namespace()
    (imported / "namespace_only").mkdir()  # no files, but now importable
    assert resolves_namespace()
    after = integ._gate_key(batch, command)
    assert after and after != before
