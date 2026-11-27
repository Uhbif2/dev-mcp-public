"""Ночное ревью: итог не закрывается на промежуточной реплике, зависшая ночь не блокирует линию.

Регрессии 24.11.2026: 21.11 Beta App — отчёт «ответ агента невалиден», потому что итоговое
задание закрыли на реплике «Проверяю исходные файлы…»; dev-mcp-self — итог 18.11 без ответа,
status=running, и следующие ночи не запускались. Без git: plan подменён.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet import nightly_review as nr, quick

MSK = timezone(timedelta(hours=3))


def night(days: int = 0) -> datetime:
    return datetime(2026, 11, 17, 2, 30, tzinfo=MSK) + timedelta(days=days)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    chunk = {"kind": "rotation", "directory": "(entire tree)",
             "files": [{"path": "app/one.py", "weight": 10, "start": 1, "end": 10}],
             "changed_lines": 0}
    monkeypatch.setattr(nr, "plan", lambda root, since="24h", cursor=0: ([dict(chunk)], cursor + 1))
    data, reports = tmp_path / "data", tmp_path / "reports"
    kwargs = dict(roots={"demo": root}, data=data, reports=reports)
    return quick.Book(data), data, reports, kwargs


FINDING = {"severity": "high", "kind": "bug", "location": "app/one.py:3",
           "summary": "broken precondition", "evidence": "returns None"}


def _to_summary(book, data, kwargs, when):
    nr.drive(["demo"], now=when, **kwargs)
    state = nr._load(nr.state_path(data, "demo"))
    chunk = book.jobs()[state["jobs"][0]["id"]]
    quick.exchange(book, chunk["tag"], json.dumps({"findings": [FINDING]}), hold_s=0)
    nr.drive(["demo"], now=when, **kwargs)
    quick.orders(book)  # зависимость готова — итог из waiting в new
    state = nr._load(nr.state_path(data, "demo"))
    return state, book.jobs()[state["summary_job"]]


def _all_text(job: dict) -> str:
    return json.dumps([job.get("outbox", []), job.get("inbox", [])], ensure_ascii=False)


def test_interim_summary_reply_is_not_final_and_nudged_once(env):
    book, data, reports, kwargs = env
    state, summary = _to_summary(book, data, kwargs, night())
    quick.exchange(book, summary["tag"], "Проверяю исходные файлы по приоритетным находкам.", hold_s=0)
    result = nr.drive(["demo"], now=night(), **kwargs)
    assert result["demo"]["status"] == "running"
    job = book.jobs()[summary["id"]]
    assert job["state"] != "closed"
    assert _all_text(job).count("Итог ещё не сдан") == 1
    nr.drive(["demo"], now=night(), **kwargs)
    assert _all_text(book.jobs()[summary["id"]]).count("Итог ещё не сдан") == 1

    final = json.dumps({"findings": [FINDING], "duplicates": [], "disputed": []})
    quick.exchange(book, summary["tag"], final, hold_s=0)
    result = nr.drive(["demo"], now=night(), **kwargs)
    assert result["demo"]["status"] == "complete"
    report = Path(result["demo"]["report"]).read_text(encoding="utf-8")
    assert "app/one.py:3" in report and "невалиден" not in report
    assert book.jobs()[summary["id"]]["state"] == "closed"


def test_interim_summary_is_accepted_after_grace(env):
    book, data, reports, kwargs = env
    state, summary = _to_summary(book, data, kwargs, night())
    quick.exchange(book, summary["tag"], "Проверяю…", hold_s=0)
    late = night() + timedelta(seconds=nr.SUMMARY_GRACE_S + 60)
    with book.locked() as jobs:
        jobs[summary["id"]]["answered_at"] = night().astimezone(timezone.utc).isoformat()
    result = nr.drive(["demo"], now=late, **kwargs)
    assert result["demo"]["status"] == "complete"
    assert "невалиден" in Path(result["demo"]["report"]).read_text(encoding="utf-8")


def test_unfinished_night_is_closed_by_next_window_and_line_continues(env):
    book, data, reports, kwargs = env
    nr.drive(["demo"], now=night(), **kwargs)
    old = nr._load(nr.state_path(data, "demo"))
    old_id = old["jobs"][0]["id"]
    assert old["status"] == "running"
    # Днём ночь не трогаем: окно 02–05 МСК.
    nr.drive(["demo"], now=night() + timedelta(hours=10), **kwargs)
    assert nr._load(nr.state_path(data, "demo"))["date"] == "2026-11-17"

    result = nr.drive(["demo"], now=night(1), **kwargs)
    state = nr._load(nr.state_path(data, "demo"))
    assert state["date"] == "2026-11-18" and state["status"] == "running"
    assert result["demo"]["status"] == "running"
    assert book.jobs()[old_id]["state"] == "closed"
    assert state["jobs"] and state["jobs"][0]["id"] != old_id
    report = (reports / "2026-11-17-demo.md").read_text(encoding="utf-8")
    assert "Ночь не завершена" in report and "ответили 0 из 1" in report


def test_abandon_keeps_an_existing_report(env):
    book, data, reports, kwargs = env
    reports.mkdir(parents=True)
    (reports / "2026-11-17-demo.md").write_text("original\n", encoding="utf-8")
    state = {"date": "2026-11-17", "jobs": [], "chunks": [{}], "status": "running"}
    nr.abandon(book, "demo", state, reports, "2026-11-18")
    assert (reports / "2026-11-17-demo.md").read_text(encoding="utf-8") == "original\n"
    assert state["status"] == "complete" and state["abandoned"] == "2026-11-18"


def test_scheduler_logs_project_errors(monkeypatch, caplog):
    stop = threading.Event()

    def fake_drive(projects, now=None):
        stop.set()
        return {"P": {"error": "git timeout"}}

    monkeypatch.setattr(nr, "configured", lambda config: ["P"])
    monkeypatch.setattr(nr, "drive", fake_drive)
    with caplog.at_level(logging.WARNING, logger=nr.LOG.name):
        nr.scheduler(stop, tick_seconds=0)
    assert any("git timeout" in r.getMessage() for r in caplog.records)
