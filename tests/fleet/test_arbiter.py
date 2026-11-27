"""LEAD-SYSTEM-DESIGN 3.2: арбитр — суточный read-only аудит, находки в брифе, задач не создаёт."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_mcp.fleet import arbiter, goals, outcome, product, project_config
from dev_mcp.fleet.store import Store


def test_arbiter_reports_invariants_once_a_day_without_tasks(tmp_path: Path, monkeypatch):
    root = tmp_path / "Proj"
    (root / "ToDo" / "lead").mkdir(parents=True)
    (root / "intent").mkdir()
    (root / "intent" / "goal.md").write_text("## Сценарии\n\n1. **Первый.** т\n2. **Второй.** т\n",
                                             encoding="utf-8")
    (root / "ToDo" / "lead" / "LEDGER.json").write_text(
        json.dumps({"obligations": [{"id": "O-a", "source": "S01", "promise": "p"}]}), encoding="utf-8")
    monkeypatch.setattr(arbiter, "REPORT", tmp_path / "arbiter-report.json")
    monkeypatch.setattr(project_config, "project_root", lambda _p: str(root))
    monkeypatch.setattr(goals, "project_info", lambda _p: {"witness": [{"id": "w", "cmd": ["x"]}]})
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    outcome.install_schema(store)
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)", (str(root),),
                  event={"kind": "test_setup"})
    product._seen.clear()
    product.sync(store)
    with store.read() as tx:
        tasks_before = tx.scalar("SELECT COUNT(*) FROM tasks")
    now = datetime.now(timezone.utc)
    summary = arbiter.run(store, now=now)
    found = summary["projects"][0]["findings"]
    assert "покрытие: 1 из 2 пунктов канона без O-*" in found
    assert "приборы: без приёмки 1, sha без квитанции 0" in found
    assert arbiter.run(store, now=now + timedelta(hours=1)) is None  # раз в сутки
    assert arbiter.run(store, now=now + timedelta(hours=25)) is not None
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks") == tasks_before  # находка — не задача
    assert (tmp_path / "arbiter-report.json").is_file()
    assert any(line.startswith("Арбитр") for line in product.brief_lines(store, "P"))
