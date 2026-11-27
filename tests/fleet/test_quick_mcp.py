"""MCP quick-result channel, parking and browser wake contracts."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_mcp.fleet import quick
from test_tools import _make_server, _tool_fn


def _setup(tmp_path: Path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "sent": True,
                        "url": "https://chatgpt.com/c/abcdef12-0000"})
    return book, job


def test_mcp_report_is_durable_delivered_and_dom_cannot_overwrite(tmp_path: Path):
    book, job = _setup(tmp_path)
    delivered = []
    result = quick.exchange(book, job["tag"], "files a.py; pytest ok",
                            deliver=lambda j, a: delivered.append((j["answers"], a)),
                            hold_s=0)
    assert result["status"] == "parked"
    stored = quick.get(book, job["id"])
    assert (stored["state"], stored["answers"], stored["last_answer"]) == (
        "parked", 1, "files a.py; pytest ok")
    assert delivered == [(1, "files a.py; pytest ok")]
    assert quick.collect_wait(book, [job["id"]], owner="claude")[0]["last_answer"] == "files a.py; pytest ok"
    assert not quick.report(book, {"id": job["id"], "answer": "broken DOM snapshot"})["new_answer"]
    assert quick.get(book, job["id"])["last_answer"] == "files a.py; pytest ok"
    assert all(not o["open"] and o["idle"] for o in quick.orders(book))


def test_say_reaches_parked_agent_without_browser(tmp_path: Path):
    book, job = _setup(tmp_path)
    outcome = []
    def wait():
        outcome.append(quick.exchange(book, job["tag"], "done",
                                      hold_s=2, poll_s=0.01))
    thread = threading.Thread(target=wait)
    thread.start()
    try:
        for _ in range(150):
            if quick.get(book, job["id"])["state"] == "parked":
                break
            time.sleep(0.01)
        quick.say(book, job["id"], "next task", owner="claude")
        assert all(not o["open"] and o["idle"] for o in quick.orders(book))
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert outcome == [{"status": "task", "message": "next task"}]
    assert quick.get(book, job["id"])["state"] == "working"


def test_close_interrupts_park_and_limit_unparks(tmp_path: Path):
    book, job = _setup(tmp_path)
    result = []
    thread = threading.Thread(target=lambda: result.append(
        quick.exchange(book, job["tag"], "done", hold_s=2, poll_s=0.01)))
    thread.start()
    try:
        for _ in range(150):
            if quick.get(book, job["id"])["state"] == "parked":
                break
            time.sleep(0.01)
        quick.close(book, job["id"], owner="claude")
    finally:
        thread.join(timeout=3)
    assert result and result[0]["status"] == "closed"
    book2 = quick.Book(tmp_path / "other")
    other = quick.create(book2, owner="claude", prompt="task")
    assert quick.exchange(book2, other["tag"], "done", hold_s=0,
                          limit_s=0)["status"] == "limit"
    assert quick.get(book2, other["id"])["state"] == "answered-unparked"


def test_wake_requires_silence_and_honours_ceiling(tmp_path: Path):
    book, job = _setup(tmp_path)
    with book.locked() as jobs:
        jobs[job["id"]]["state"] = "working"
        jobs[job["id"]]["updated"] = (datetime.now(timezone.utc) -
                                     timedelta(minutes=5)).isoformat()
    assert quick.wake_silent(book, {job["tag"]: True}) == 0
    assert quick.wake_silent(book, {}) == 1
    orders = quick.orders(book)
    assert len(orders) == 1 and orders[0]["closeAfterSend"] is True
    assert "fleet_quick" in orders[0]["send"]
    quick.report(book, {"id": job["id"], "sent": True})
    assert quick.wake_silent(book, {}, wake_after_s=0) == 1
    quick.report(book, {"id": job["id"], "sent": True})
    assert quick.wake_silent(book, {}, wake_after_s=0) == 0


def test_existing_mcp_tool_report_and_park(tmp_path: Path, monkeypatch):
    server, store, _, _ = _make_server(tmp_path)
    book = quick.Book(Path(store.path).parent)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "sent": True})
    original = quick.exchange
    monkeypatch.setattr(quick, "exchange", lambda *a, **kw: original(*a, **kw, hold_s=0))
    fn = _tool_fn(server, "fleet_quick")
    assert "parked" in fn(job["tag"], "report", ["complete", "verified"])
    assert quick.get(book, job["id"])["last_answer"] == "complete verified"
    assert "parked" in fn(job["tag"], "park", [])

def test_mcp_answer_notifies_worker_owner_without_dom(tmp_path: Path, monkeypatch):
    from dev_mcp.fleet import owner
    from dev_mcp.fleet.owner import secret_path
    from test_tools import _seed_task

    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    _tool_fn(server, "fleet_join")("tab-a")
    owner.grant(owner.grants_path(secret_path(tmp_path)), "W1", ttl_s=600)
    start = _tool_fn(server, "fleet_quick")
    assert "q1-w1 запущен" in start("W1", "new", ["demo", "work"])
    book = quick.Book(Path(store.path).parent)
    quick.report(book, {"id": "q1-w1", "sent": True})
    original = quick.exchange
    monkeypatch.setattr(quick, "exchange", lambda *a, **kw: original(*a, **kw, hold_s=0))
    assert "parked" in start("Q1-W1", "report", ["complete from MCP"])
    with store.read() as tx:
        message = tx.one("SELECT body FROM messages WHERE to_worker='W1' ORDER BY created_at DESC LIMIT 1")
    assert message and "complete from MCP" in message["body"]
    result = quick.collect_wait(book, ["q1-w1"], owner="W1")
    assert len(result) == 1 and result[0]["last_answer"] == "complete from MCP"
