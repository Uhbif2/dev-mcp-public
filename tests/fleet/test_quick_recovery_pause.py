"""Quick MCP finished-answer, send-backoff, and restart recovery regressions."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dev_mcp.fleet import quick
from test_tools import _make_server, _tool_fn


def _cli():
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_recovery_cli", Path(__file__).resolve().parents[2] / "tools" / "fleet.py")
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_mcp_report_unblocks_cli_wait_even_with_pending_outbox(tmp_path):
    cli = _cli()
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "sent": True})
    quick.say(book, job["id"], "new queued message", owner="claude")
    assert quick.exchange(book, job["tag"], "MCP answer", hold_s=0)["status"] == "parked"
    assert quick.get(book, job["id"])["outbox"]
    ns = SimpleNamespace(action="wait", words=[job["id"]], timeout=0,
                         after=None, all=False)
    code, output = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0 and "MCP answer" in output
    assert quick.get(book, job["id"])["delivered"] == 1
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 1


def test_dom_answer_still_waits_for_queued_message(tmp_path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "sent": True})
    quick.report(book, {"id": job["id"], "answer": "DOM answer"})
    quick.say(book, job["id"], "message queued", owner="claude")
    assert not quick.collect_wait(book, [job["id"]], owner="claude")
    quick.report(book, {"id": job["id"], "sent": True})
    assert quick.collect_wait(book, [job["id"]], owner="claude")


def test_extension_send_pause_is_bounded_visible_and_cleared(tmp_path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    until = (datetime.now(timezone.utc) + timedelta(minutes=40)).isoformat()
    quick.report(book, {
        "id": job["id"], "send_paused": True, "send_pause_until": until,
        "send_pause_reason": "retry\n" + "x" * 500, "send_failures": 100000,
    })
    saved = quick.get(book, job["id"])["send_pause"]
    assert saved["failures"] == 1000 and len(saved["reason"]) == 180
    assert "\n" not in saved["reason"] and saved["until"] == until
    assert "пауза отправки до" in quick.render(quick.get(book, job["id"]))
    with book.locked() as jobs:
        jobs[job["id"]]["send_pause"]["since"] = (
            datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
    alarm = quick.pause_alarms(book)
    assert len(alarm) == 1 and "q1: отправка на паузе" in alarm[0]
    assert not quick.pause_alarms(book, threshold_s=3600)
    quick.report(book, {"id": job["id"], "send_paused": False})
    assert "send_pause" not in quick.get(book, job["id"])
    quick.report(book, {"id": job["id"], "send_paused": True,
                        "send_pause_reason": "backoff"})
    quick.report(book, {"id": job["id"], "sent": True, "send_paused": True})
    assert "send_pause" not in quick.get(book, job["id"])


def test_restart_first_any_agent_tool_restores_park_and_delivers_say(tmp_path, monkeypatch):
    server, store, root, _ = _make_server(tmp_path)
    book = quick.Book(Path(store.path).parent)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "sent": True})
    assert quick.exchange(book, job["tag"], "done", hold_s=0)["status"] == "parked"
    assert quick.get(book, job["id"])["park_wanted"]
    # Simulate the request being lost across restart, leaving an unparked
    # state with a persistent park intent in the same jobs.json.
    with book.locked() as jobs:
        jobs[job["id"]]["state"] = "answered-unparked"
    quick.say(book, job["id"], "more work", owner="claude")
    source = root / "input.txt"
    source.write_text("data")
    monkeypatch.setattr(quick, "_PARK_EPOCH", "after-server-restart")
    read = _tool_fn(server, "read_file")
    returned = read(str(source), agent=job["tag"])
    assert "восстановление парковки" in returned
    assert "command='park'" in returned
    assert quick.get(book, job["id"])["state"] == "parked"
    assert "восстановление парковки" not in read(str(source), agent=job["tag"])
    resumed = quick.exchange(book, job["tag"], hold_s=0)
    assert resumed == {"status": "task", "message": "more work"}
    assert not quick.get(book, job["id"])["park_wanted"]


def test_cli_exceptions_print_long_send_pause(tmp_path, monkeypatch, capsys):
    from dev_mcp.fleet.store import Store

    cli = _cli()
    database = tmp_path / "fleet.sqlite"
    store = Store.open(str(database))
    store.close()
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    quick.report(book, {"id": job["id"], "send_paused": True,
                        "send_pause_reason": "rate limited",
                        "send_pause_until": (datetime.now(timezone.utc) +
                                             timedelta(minutes=20)).isoformat()})
    with book.locked() as jobs:
        jobs[job["id"]]["send_pause"]["since"] = (
            datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    monkeypatch.setattr(cli, "platform_line", lambda: "test platform")
    monkeypatch.setattr(cli, "exception_items", lambda _conn: [])
    assert cli.main(["--db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "! q1: отправка на паузе 11 мин" in output
    assert "rate limited" in output


def test_intentional_park_limit_is_not_revived(tmp_path, monkeypatch):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    assert quick.exchange(book, job["tag"], "done", hold_s=0, limit_s=0)["status"] == "limit"
    monkeypatch.setattr(quick, "_PARK_EPOCH", "next-server")
    assert quick.park_restart_notice(book, job["tag"]) == ""
    assert quick.get(book, job["id"])["state"] == "answered-unparked"
