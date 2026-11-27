"""Store.read() реентерабелен в потоке (20.11.2026).

Регрессия: fleet_list строил текст будки ведущего внутри `with store.read()`, а
wake.wake_text → lead.focus_lines открывал второй read() — «cannot start a transaction within
a transaction», и весь fleet_list отдавал расширению ошибку.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.store import Store  # noqa: E402


def _store(tmp_path: Path) -> Store:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P','C:/P',1,1)",
                  (), event={"kind": "test_setup"})
    return store


def test_nested_read_reuses_outer_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.read() as outer:
        assert outer.one("SELECT name FROM projects")["name"] == "P"
        with store.read() as inner:
            assert inner.one("SELECT count(*) AS n FROM projects")["n"] == 1
        # Внутренний выход не закрыл снимок внешнего.
        assert outer.one("SELECT name FROM projects")["name"] == "P"
        with store.read() as again:
            assert again.one("SELECT name FROM projects")["name"] == "P"
    conn = store._connection()
    assert not conn.in_transaction
    # После выхода запись идёт как обычно.
    with store.tx() as tx:
        tx.change("UPDATE projects SET priority=2 WHERE name='P'", (), event={"kind": "test"})
    with store.read() as rx:
        assert rx.one("SELECT priority FROM projects")["priority"] == 2


def test_read_recovers_from_leftover_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conn = store._connection()
    conn.execute("BEGIN")  # след оборванного вызова в этом потоке
    with store.read() as rx:
        assert rx.one("SELECT name FROM projects")["name"] == "P"
    assert not conn.in_transaction


def test_lead_wake_text_inside_read_does_not_fail(tmp_path: Path, monkeypatch) -> None:
    from dev_mcp.fleet import goals, lead_focus, wake

    focus = {"lead_focus": {"action": "показать результат", "evidence": "скриншот"}}
    monkeypatch.setattr(goals, "project_info", lambda project: focus)
    monkeypatch.setattr(goals, "goal_block", lambda project: "Цель: тест")
    monkeypatch.setattr(lead_focus, "continuous", lambda project: True)
    monkeypatch.setattr(lead_focus, "state", lambda tx, project: ("", ""))
    monkeypatch.setattr(lead_focus, "_epoch", lambda tx, project, focus: None)
    store = _store(tmp_path)
    item = {"project": "P", "tz": "TZ-LEAD", "worker_id": "W1", "wake": "w-1"}
    with store.read():
        text = wake.wake_text(item, store)
    assert "fleet_resume(agent='W1', wake='w-1')" in text
