"""Deep Research queue / extension transport contract with a fake browser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet import research
from dev_mcp.fleet.store import Store


NOW = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def registry(tmp_path: Path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "project"
    root.mkdir()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            ("Demo", str(root)), event="test.seed",
        )
        tx.change(
            """INSERT INTO workers(id,label,kind,first_seen,last_seen)
               VALUES('W1','test','chatgpt',?,?)""",
            (NOW.isoformat(), NOW.isoformat()), event="test.seed",
        )
    yield store, root
    store.close()


def start(store, n=1, at=NOW):
    return [
        research.request(store, agent="W1", question=f"Question {i}", project="Demo", now=at)
        for i in range(n)
    ]


def test_fake_extension_channel_report_file_and_inbox(registry):
    store, root = registry
    request_id = start(store)[0]
    assert isinstance(request_id, str) and request_id.startswith("r-")
    channel = research.pending(store, now=NOW + timedelta(minutes=1))
    assert [r["id"] for r in channel] == [request_id]
    assert channel[0]["question"] == "Question 0"
    report = "# Research\n\nSource: https://example.org\nComplete report."
    result = research.finish(store, request_id=request_id, status="complete",
                             report=report, now=NOW + timedelta(minutes=2))
    assert result["status"] == "completed"
    path = root / "intent" / "research" / f"{request_id}.md"
    assert path.read_text(encoding="utf-8") == report
    assert research.pending(store, now=NOW + timedelta(minutes=3)) == []
    with store.read() as tx:
        notes = tx.query("SELECT to_worker,body FROM messages")
        assert len(notes) == 1
        assert notes[0]["to_worker"] == "W1"
        assert request_id in notes[0]["body"]
        events = tx.query("SELECT kind FROM events WHERE kind LIKE 'research.%' ORDER BY id")
    assert [e["kind"] for e in events] == [
        "research.requested", "research.completed", "research.notified"
    ]
    assert research.finish(store, request_id=request_id, status="complete",
                           report=report, now=NOW + timedelta(minutes=3))["status"] == "completed"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM messages") == 1


def test_concurrent_limit_releases_after_completed_or_unavailable(registry):
    store, _ = registry
    ids = start(store, 3)
    assert all(isinstance(r, str) for r in ids)
    denied = start(store)[0]
    assert isinstance(denied, dict) and denied["status"] == "unavailable"
    assert "3" in denied["reason"]
    result = research.finish(store, request_id=ids[0], status="unavailable",
                             reason="UI missing", now=NOW + timedelta(minutes=1))
    assert result["status"] == "unavailable"
    assert isinstance(start(store, at=NOW + timedelta(minutes=2))[0], str)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM messages") == 1


def test_ten_per_utc_day_even_if_requests_finish(registry):
    store, _ = registry
    for i in range(10):
        moment = NOW + timedelta(minutes=i)
        request_id = start(store, at=moment)[0]
        assert isinstance(request_id, str), i
        research.finish(store, request_id=request_id, status="unavailable",
                        reason="unavailable in browser", now=moment)
    denied = start(store, at=NOW + timedelta(minutes=10))[0]
    assert isinstance(denied, dict)
    assert "10" in denied["reason"]
    assert isinstance(start(store, at=NOW + timedelta(days=1))[0], str)


def test_expire_exactly_at_forty_minutes_and_notify(registry):
    store, _ = registry
    request_id = start(store)[0]
    assert research.pending(store, now=NOW + timedelta(minutes=39, seconds=59))
    assert research.pending(store, now=NOW + timedelta(minutes=40)) == []
    with store.read() as tx:
        msg = tx.one("SELECT body FROM messages")
        assert "таймаут 40 минут" in msg["body"]
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='research.unavailable'") == 1
    assert research.pending(store, now=NOW + timedelta(minutes=45)) == []
    assert research.finish(store, request_id=request_id, status="complete",
                           report="Too late", now=NOW + timedelta(minutes=45))["status"] == "unavailable"


def test_fail_closed_validation(registry):
    store, _ = registry
    for q in ("", " ", "x" * 8001):
        with pytest.raises(ValueError):
            research.request(store, agent="W1", project="Demo", question=q, now=NOW)
    with pytest.raises(ValueError):
        research.request(store, agent="W1", project="missing", question="Hi", now=NOW)
    request_id = start(store)[0]
    for kw in [
        {"status": "complete", "report": ""},
        {"status": "complete", "report": "x" * (research.MAX_REPORT + 1)},
        {"status": "unavailable", "reason": ""},
        {"status": "unknown"},
    ]:
        with pytest.raises(ValueError):
            research.finish(store, request_id=request_id, now=NOW, **kw)
    with pytest.raises(ValueError):
        research.finish(store, request_id="../../etc", status="complete", report="x", now=NOW)


def test_persistence_after_registry_reopen(registry):
    store, root = registry
    request_id = start(store)[0]
    reopened = Store.open(store.path)
    try:
        assert research.pending(reopened, now=NOW)[0]["id"] == request_id
        result = research.finish(reopened, request_id=request_id, status="complete",
                                 report="Persisted", now=NOW)
        assert Path(result["path"]).read_text(encoding="utf-8") == "Persisted"
    finally:
        reopened.close()
