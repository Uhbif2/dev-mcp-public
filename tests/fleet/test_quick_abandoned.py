from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_mcp.fleet import quick


def _job(book: quick.Book, state: str, age_h: float, **extra) -> str:
    job = quick.create(book, owner="claude:abcdef", prompt="задача")
    stamp = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
    with book.locked() as jobs:
        jobs[job["id"]].update({"state": state, "updated": stamp, "outbox": [], **extra})
    return job["id"]


def test_answered_jobs_without_owner_activity_close_after_12h(tmp_path: Path) -> None:
    book = quick.Book(tmp_path)
    old = _job(book, "answered-unparked", 13, last_answer="итог")
    parked_old = _job(book, "parked", 20)
    fresh = _job(book, "answered-unparked", 2)
    working = _job(book, "working", 30)
    pending = _job(book, "parked", 30, outbox=["ещё"])
    closed = quick.close_abandoned(book)
    assert set(closed) == {old, parked_old}
    jobs = book.jobs()
    assert jobs[old]["state"] == "closed" and jobs[old]["last_answer"] == "итог"
    assert jobs[old]["closed_reason"]
    assert jobs[fresh]["state"] == "answered-unparked"
    assert jobs[working]["state"] == "working"
    assert jobs[pending]["state"] == "parked"


def test_first_agent_action_proves_delivery(tmp_path: Path) -> None:
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude:abcdef", prompt="задача")
    assert book.jobs()[job["id"]]["state"] == "new" and book.jobs()[job["id"]]["outbox"]
    assert quick.mark_acting(book, quick.lookup(book, quick.tag_of(job["id"])) or job)
    after = book.jobs()[job["id"]]
    assert after["state"] == "working" and not after["outbox"]
    assert after["delivery_proof"] == "agent-action"
    # Второй вызов ничего не меняет; extension больше не получает send.
    assert not quick.mark_acting(book, after)
    assert not any(o["id"] == job["id"] and o.get("send") for o in quick.orders(book))


def test_orders_closes_abandoned_and_issues_close(tmp_path: Path) -> None:
    book = quick.Book(tmp_path)
    jid = _job(book, "answered-unparked", 13, url="https://chatgpt.com/c/x")
    out = quick.orders(book)
    assert any(o["id"] == jid and o.get("close") for o in out)
