"""W11-4: owner alarm classifications, not just event-name matching."""
from __future__ import annotations

import sqlite3

from dev_mcp.fleet import quick, report
from test_report import NOW, claim, db, event, iso, project, task  # noqa: F401


def test_quick_operator_scope_is_a_denied_write_not_an_unscoped_agent(
    db: sqlite3.Connection, tmp_path, monkeypatch,
) -> None:
    project(db, tmp_path)
    task(db, "TZ-1")
    path = str(tmp_path / "qt_settings.py")
    event(db, "deny", tz="TZ-1", wid="Q4-2BB72F", path=path,
          payload={"code": "FLEET_FOREIGN_PATH"})
    event(db, "deny", tz="TZ-1", wid="Q4-2BB72F", path=path,
          payload={"code": "FLEET_FOREIGN_PATH"})
    job = {"tag": "Q4-2BB72F", "owner": "claude:2bb72f",
           "scope": [str(tmp_path)], "root": "",
           "created": iso(-50), "state": "parked"}
    monkeypatch.setattr(quick, "lookup", lambda book, agent: job)

    class Conn:
        path = str(tmp_path / "fleet.sqlite")

        def execute(self, sql, params=()):
            return db.execute(sql, params)

    items = report.exceptions(Conn())
    relevant = [x for x in items if "Q4-2BB72F" in x]
    assert len(relevant) == 1
    assert relevant[0].startswith("ОТКАЗ QUICK В SCOPE")
    assert "(повторов 2)" in relevant[0]
    assert not any(x.startswith("ЧУЖОЙ ПУТЬ") for x in relevant)

    job["scope"] = [str(tmp_path / "elsewhere")]
    items = report.exceptions(Conn())
    assert any(x.startswith("ЧУЖОЙ ПУТЬ") and "Q4-2BB72F" in x for x in items)
    assert not any(x.startswith("ОТКАЗ QUICK В SCOPE") for x in items)

    job["scope"] = [str(tmp_path)]
    job["closed_at"] = iso(-10)
    items = report.exceptions(Conn())
    assert any(x.startswith("ЧУЖОЙ ПУТЬ") and "Q4-2BB72F" in x for x in items)


def test_only_actual_guard_events_are_foreign_path_alarms(db, tmp_path) -> None:
    project(db, tmp_path)
    task(db, "TZ-1")
    event(db, "read", tz="TZ-1", wid="W1", path="guard.py",
          payload={"code": "FLEET_FOREIGN_PATH"})
    event(db, "deny", tz="TZ-1", wid="W1", path="real.py",
          payload={"code": "FLEET_FOREIGN_PATH"})
    assert [item for item in report.exceptions(db) if "ЧУЖОЙ ПУТЬ" in item] == [
        "ЧУЖОЙ ПУТЬ P/TZ-1 W1: real.py"]


def test_quick_without_project_context_is_not_a_mystery_target(db, tmp_path) -> None:
    project(db, tmp_path)
    event(db, "deny", wid="Q3-A6DECB", path="file.py",
          payload={"code": "FLEET_FOREIGN_PATH"})
    assert "ЧУЖОЙ ПУТЬ Q3-A6DECB: file.py" in report.exceptions(db)


def test_stale_submissions_without_live_review_are_not_review_queue(db, tmp_path) -> None:
    project(db, tmp_path)
    task(db, "TZ-NO-REVIEW", "submitted", updated_at=iso(-90))
    task(db, "TZ-FROZEN", "submitted", updated_at=iso(-90))
    task(db, "review:TZ-FROZEN", "frozen", kind="review")
    task(db, "request:unrouted", "submitted", kind="request", updated_at=iso(-90))
    for tz in ("TZ-NO-REVIEW", "TZ-FROZEN", "request:unrouted"):
        event(db, "submit", tz=tz, ts=iso(-90))
    items = report.exceptions(db)
    assert not any("ЖДЁТ ПРИЁМКИ" in x for x in items)
    assert any("ПРИЁМКА ЗАМОРОЖЕНА P: 1 — TZ-FROZEN" in x for x in items)
    assert any("СДАЧА БЕЗ МАРШРУТА ПРИЁМКИ P: 2" in x for x in items)


def test_unread_task_messages_are_grouped_and_submitted_messages_not_actionable(
    db, tmp_path,
) -> None:
    project(db, tmp_path)
    task(db, "TZ-READY", "ready")
    task(db, "TZ-SUBMITTED", "submitted")
    for mid, target in (("m-1", "P/TZ-READY"), ("m-2", "P/TZ-READY"),
                        ("m-old-submitted", "P/TZ-SUBMITTED")):
        db.execute(
            "INSERT INTO messages(id,to_task,broadcast,from_kind,kind,body,created_at)"
            " VALUES(?,?,0,'owner','note','text',?)",
            (mid, target, iso(-65)),
        )
    alarms = report.exceptions(db)
    assert any(x.startswith("БЕЗ ACK 2 сообщений (note) → P/TZ-READY:") for x in alarms)
    assert not any("m-old-submitted" in x or "TZ-SUBMITTED" in x and "БЕЗ ACK" in x
                   for x in alarms)
    assert len([x for x in alarms if x.startswith("БЕЗ ACK")]) == 1
