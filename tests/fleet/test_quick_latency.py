"""Quick end-to-end timestamps, stale wait cursor, and startup alarms."""
from __future__ import annotations

import importlib.util
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dev_mcp.fleet import quick


def _cli():
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_latency_cli", Path(__file__).resolve().parents[2] / "tools" / "fleet.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_answered_unparked_wait_acknowledges_once_and_next_reply_wakes(tmp_path, monkeypatch):
    cli = _cli()
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "sent": True})
    assert quick.exchange(book, job["tag"], "finished", hold_s=0, limit_s=0)["status"] == "limit"
    assert quick.get(book, job["id"])["state"] == "answered-unparked"
    assert quick.collect_wait(book, [job["id"]], owner="claude")
    assert quick.get(book, job["id"])["delivered"] == 1
    ns = SimpleNamespace(action="wait", words=[job["id"]], timeout=0, after=None, all=False)
    code, output = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 1 and "нового ответа" in output
    # Explicit --after is an intentional replay of the already-read reply.
    ns.after = 0
    code, output = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0 and "finished" in output
    ns.after = 1
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 1
    # Without --after the already-read reply must cause a poll, not a wake.
    polls = []
    ns.after, ns.timeout = None, 1

    def receive_new_reply(_seconds):
        polls.append("waiting")
        assert quick.exchange(book, job["tag"], "second reply",
                              hold_s=0, limit_s=0)["status"] == "limit"

    monkeypatch.setattr(cli.time, "sleep", receive_new_reply)
    code, output = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0 and "second reply" in output and polls == ["waiting"]
    quick.close(book, job["id"], owner="claude")
    assert quick.get(book, job["id"])["delivered"] == 2
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 1
    ns.action = "show"
    assert "second reply" in cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[1]


def test_closed_unread_mcp_answer_is_returned_exactly_once(tmp_path):
    cli = _cli()
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "sent": True})
    quick.exchange(book, job["tag"], "last report", hold_s=0, limit_s=0)
    quick.close(book, job["id"], owner="claude")
    ns = SimpleNamespace(action="wait", words=[job["id"]], timeout=0, after=None, all=False)
    assert "last report" in cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[1]
    assert quick.get(book, job["id"])["delivered"] == 1
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 1


def test_concurrent_waiters_and_book_writer_preserve_owner_cursor(tmp_path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "sent": True})
    quick.exchange(book, job["tag"], "finished", hold_s=0, limit_s=0)
    start = threading.Barrier(4)
    results = []
    errors = []

    def waiter():
        try:
            start.wait(timeout=5)
            results.append(quick.collect_wait(quick.Book(tmp_path), [job["id"]], owner="claude"))
        except Exception as exc:
            errors.append(exc)

    def writer():
        try:
            start.wait(timeout=5)
            quick.say(quick.Book(tmp_path), job["id"], "new instruction", owner="claude")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=waiter), threading.Thread(target=waiter),
               threading.Thread(target=writer)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert [len(picked) for picked in results].count(1) == 1
    stored = quick.Book(tmp_path).jobs()[job["id"]]
    assert stored["delivered"] == stored["answers"] == 1
    assert stored["outbox"] == ["new instruction"]
    assert not quick.collect_wait(book, [job["id"]], owner="claude")


def test_queued_is_not_delivered_and_timestamps_track_actual_send(tmp_path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    quick.say(book, job["id"], "second message", owner="claude")
    queued = quick.get(book, job["id"])
    assert queued["state"] == "new" and not queued["first_delivered_at"]
    assert queued["outbox_since_at"]
    quick.report(book, {"id": job["id"], "url": "https://chatgpt.com/c/example"})
    opened = quick.get(book, job["id"])
    assert opened["chat_opened_at"] and not opened["first_delivered_at"]
    quick.report(book, {"id": job["id"], "sent": True})
    delivered = quick.get(book, job["id"])
    assert delivered["state"] == "working" and delivered["first_delivered_at"]
    assert delivered["outbox"] == ["second message"] and delivered["outbox_since_at"]
    assert "задержки: new" in quick.render(delivered)
    quick.report(book, {"id": job["id"], "answer": "finished"})
    assert quick.get(book, job["id"])["reported_at"]
    assert quick.collect_wait(book, [job["id"]], owner="claude") == []  # DOM+pending
    quick.report(book, {"id": job["id"], "sent": True})
    assert quick.collect_wait(book, [job["id"]], owner="claude")
    assert quick.get(book, job["id"])["notified_at"]


def test_audit_excludes_fleet_quick_and_summary_uses_first_action(tmp_path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    created = datetime.fromisoformat(job["created"])
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "audit.jsonl").write_text("\n".join(json.dumps({
        "agent": job["tag"], "tool": tool, "ts": (created + timedelta(seconds=delay)).isoformat(),
    }) for tool, delay in [("fleet_quick", 30), ("read_file", 120), ("run", 240)]) + "\n")
    actions = quick.first_actions(logs, [job])
    assert actions[job["tag"]] == (created + timedelta(seconds=120)).isoformat()
    decorated = quick.with_action(job, actions)
    assert "первое действие 2м00с" in quick.render(decorated)
    summary = quick.recent_summary(book, actions, now=created + timedelta(hours=1))
    assert "медиана 2м00с" in summary and "p90 2м00с" in summary


def test_first_action_and_unsent_alarms_include_extension_reason(tmp_path):
    now = datetime.now(timezone.utc)
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    with book.locked() as jobs:
        item = jobs[job["id"]]
        item["new_at"] = (now - timedelta(minutes=12)).isoformat()
        item["outbox_since_at"] = (now - timedelta(minutes=6)).isoformat()
        item["send_pause"] = {"since": now.isoformat(), "reason": "Chrome timers throttled"}
    alarms = quick.pause_alarms(book, now=now, actions={})
    assert len(alarms) == 2
    assert "ждёт отправки 6 мин" in alarms[0] and "Chrome timers throttled" in alarms[0]
    assert "нет первого действия 12 мин" in alarms[1]
    with book.locked() as jobs:
        jobs[job["id"]]["first_action_at"] = now.isoformat()
        jobs[job["id"]]["outbox"] = []
    assert not quick.pause_alarms(book, now=now, actions={})
