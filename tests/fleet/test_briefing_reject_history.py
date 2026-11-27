"""Regression for persistent reviewer history on reactivated tasks."""
import json

from dev_mcp.fleet import briefing
from dev_mcp.fleet.store import Store


def test_reactivated_task_brief_contains_full_reject_chain(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES('P',?,0,1)",
            (str(tmp_path),), event="seed",
        )
        for tz, state, kind in (("TZ-9", "ready", "work"),
                                ("fix:TZ-9", "rejected", "fix"),
                                ("fix:TZ-9:2", "ready", "fix"),
                                ("TZ-10", "ready", "work")):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,kind,body,created_at,updated_at) "
                "VALUES('P',?,?,?,?,'Original assignment','t','t')",
                (tz, tz, state, kind), event="seed",
            )
        tx.record_event(kind="reject", project="P", tz="TZ-9",
                        payload={"note": "Первый отказ: полный текст\nстрока два"})
        tx.record_event(kind="reject", project="P", tz="fix:TZ-9",
                        payload={"note": "Повторный отказ: подробности исправления"})
        tx.record_event(kind="reject", project="P", tz="TZ-10",
                        payload={"note": "Не относится к этой задаче"})
    with store.read() as tx:
        row = tx.one("SELECT * FROM tasks WHERE project='P' AND tz='fix:TZ-9:2'")
    text = briefing.briefing(store, "W-new", briefing._task_from_row(row))
    assert "Повторный отказ: подробности исправления" in text
    assert "Первый отказ: полный текст\nстрока два" in text
    assert text.index("Повторный отказ") < text.index("Первый отказ")
    assert "Не относится к этой задаче" not in text
    store.close()
