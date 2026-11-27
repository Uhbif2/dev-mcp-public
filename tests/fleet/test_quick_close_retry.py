"""Closed quick-agent tabs get repeated URL-based close orders for 24 hours."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_mcp.fleet import quick


def test_closed_quick_orders_repeat_after_tab_closed_ack(tmp_path: Path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="work")
    url = "https://chatgpt.com/c/abcdef12-1111"
    quick.report(book, {"id": job["id"], "url": url, "sent": True})
    closed = quick.close(book, job["id"], owner="claude")
    original_closed = closed["closed_at"]
    quick.report(book, {"id": job["id"], "tab_closed": True})
    assert quick.get(book, job["id"])["closed_at"] == original_closed

    for _ in range(2):  # idempotent even if the extension already acknowledged close
        command = next(item for item in quick.orders(book) if item["id"] == job["id"])
        assert command == {"id": job["id"], "label": "pult-" + job["id"],
                           "url": url, "close": True}

    # A later extension poll must never reset the close retention clock.
    with book.locked() as jobs:
        jobs[job["id"]]["closed_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        jobs[job["id"]]["updated"] = datetime.now(timezone.utc).isoformat()
    assert not any(item["id"] == job["id"] for item in quick.orders(book))


def test_close_retry_ignores_invalid_time_and_missing_url(tmp_path: Path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="work")
    quick.close(book, job["id"], owner="claude")
    assert quick.orders(book) == []  # never instruct extension to close by label alone
    with book.locked() as jobs:
        jobs[job["id"]]["url"] = "https://chatgpt.com/c/abcdef12-2222"
        jobs[job["id"]]["closed_at"] = "not-an-iso-date"
    assert quick.orders(book) == []
