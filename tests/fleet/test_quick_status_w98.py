"""Regression coverage for quick URL acknowledgements and unsent age on CLI."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dev_mcp.fleet import quick


def _cli():
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_w98_cli", Path(__file__).resolve().parents[2] / "tools" / "fleet.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quick_url_from_extension_after_first_mcp_action(tmp_path):
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="task")
    assert quick.mark_acting(book, job)
    working = quick.get(book, job["id"])
    assert working["state"] == "working" and not working["url"]
    assert "чат адрес не получен" in quick.render(working)
    url = "https://chatgpt.com/c/abcdefab-1234"
    assert quick.report(book, {"id": job["id"], "url": url})["ok"]
    updated = quick.get(book, job["id"])
    assert updated["url"] == url and updated["chat_opened_at"]
    assert updated["first_delivered_at"] and not updated["outbox"]
    quick.report(book, {"id": job["id"], "sent": True, "url": url})
    assert quick.get(book, job["id"])["url"] == url


def test_quick_new_list_and_show_expose_outbox_age_and_last_reason(tmp_path, monkeypatch):
    # Владелец CLI берётся из сессии Claude Code; тест не должен зависеть от того, где запущен.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    book = quick.Book(tmp_path)
    job = quick.create(book, owner=quick.claude_owner(None), prompt="task")
    with book.locked() as jobs:
        item = jobs[job["id"]]
        item["outbox_since_at"] = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
        item["send_pause"] = {"reason": "No response from ChatGPT", "failures": 2}
    cli = _cli()
    ns = SimpleNamespace(action="list", words=[], all=False)
    code, listing = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0
    assert f"{job['id']} [new]" in listing
    assert "ждёт отправки 12 мин" in listing
    assert "последняя причина: No response from ChatGPT" in listing
    ns.action, ns.words = "show", [job["id"]]
    code, detail = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0 and "ждёт отправки 12 мин" in detail
    assert "последняя причина: No response from ChatGPT" in detail
    quick.mark_acting(book, job)
    assert "ждёт отправки" not in quick.render(quick.get(book, job["id"]))
