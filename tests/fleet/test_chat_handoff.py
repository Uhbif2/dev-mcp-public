"""Conversation handoff state and quick-agent browser succession, no Chrome required."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from dev_mcp.fleet import chat_handoff, quick

OLD = "https://chatgpt.com/c/6af9c383-1111-2222-3333-111111111111"
NEXT = "https://chatgpt.com/c/6b01b382-1111-2222-3333-222222222222"


def _new(tmp_path: Path):
    book = chat_handoff.Book(tmp_path)
    quick_book = quick.Book(tmp_path)
    job = quick.create(quick_book, owner="claude", prompt="investigate the failure",
                       project="dev-mcp", root=str(tmp_path), scope=[str(tmp_path)])
    quick.report(quick_book, {"id": job["id"], "url": OLD, "sent": True})
    return book, quick_book, job["tag"]


def test_pressure_is_single_request_and_saves_file_before_successor(tmp_path):
    book, quick_book, tag = _new(tmp_path)
    assert chat_handoff.observe(book, agent=tag, url=OLD, fill=0.849, chars=169800)["action"] == "none"
    req = chat_handoff.observe(book, agent=tag, url=OLD, fill=0.85, chars=170000)
    again = chat_handoff.observe(book, agent=tag, url=OLD, fill=0.95, chars=190000)
    assert req["action"] == again["action"] == "request"
    assert req["token"] == again["token"]
    assert "⟦надзор⟧" in req["text"]
    with pytest.raises(ValueError, match="file not saved"):
        chat_handoff.prepare(book, agent=tag, url=OLD, token=req["token"],
                             quick_book=quick_book)
    md = ("# Цель\nПродолжить расследование.\n\n# Сделано и подтверждения\n"
          "Проверено состояние предыдущего чата и идентификатор задачи.\n\n"
          "# Решения\nСохраняем прежний agent и scope, не создавая новый claim.\n")
    saved = chat_handoff.save(book, agent=tag, url=OLD, content=md)
    assert saved["marker"] == f"⟦HANDOFF_READY:{req['token']}⟧"
    assert Path(saved["file"]).read_text(encoding="utf-8") == md.strip() + "\n"
    with pytest.raises(ValueError, match="overwrite"):
        chat_handoff.save(book, agent=tag, url=OLD, content=md + " altered")
    prepared = chat_handoff.prepare(book, agent=tag, url=OLD, token=req["token"],
                                    quick_book=quick_book)
    assert prepared["action"] == "prepared"
    assert md in prepared["start"]
    j = quick.get(quick_book, tag)
    assert j["tag"] == f"{tag}.2" and j["owner"] == "claude"
    assert j["scope"] == [str(tmp_path)] and j["url"] == ""
    assert len(quick.orders(quick_book)) == 1
    assert quick.orders(quick_book)[0]["handoffOldUrl"] == OLD
    assert f"{tag}.2" in quick.orders(quick_book)[0]["send"]
    # A repeated prepare cannot queue a second successor or override a saved file.
    assert chat_handoff.prepare(book, agent=tag, url=OLD, token=req["token"],
                                quick_book=quick_book) == prepared
    assert len(quick.get(quick_book, tag)["outbox"]) == 1
    # Stale old-tab URL cannot attach itself as the new successor.
    quick.report(quick_book, {"id": tag, "url": OLD})
    assert quick.get(quick_book, tag)["url"] == ""
    quick.report(quick_book, {"id": tag, "url": NEXT, "sent": True})
    new = quick.get(quick_book, tag)
    assert new["url"] == NEXT
    assert new["chat_handoff"]["status"] == "activated"
    assert new["chat_handoff"]["old_url"] == OLD
    active = chat_handoff.activated(book, agent=tag, url=OLD,
                                   successor_url=NEXT, token=req["token"])
    assert active["successor_url"] == NEXT
    assert chat_handoff.activated(book, agent=tag, url=OLD,
                                  successor_url=NEXT, token=req["token"]) == active
    with pytest.raises(PermissionError):
        chat_handoff.activated(book, agent=tag, url=OLD,
                               successor_url="https://chatgpt.com/c/ffffffff", token=req["token"])


def test_emergency_banner_without_file_uses_bounded_journal_context(tmp_path):
    book, quick_book, tag = _new(tmp_path)
    request = chat_handoff.observe(book, agent=tag, url=OLD, fill=0, chars=0, full=True)
    assert request["action"] == "recover"
    prepared = chat_handoff.prepare(book, agent=tag, url=OLD, full=True,
                                    context="Journal: last rc=0, check live claim.",
                                    quick_book=quick_book)
    assert prepared["emergency"] is True
    assert "Journal: last rc=0" in Path(prepared["file"]).read_text(encoding="utf-8")
    assert quick.get(quick_book, tag)["chat_handoff"]["status"] == "pending"
    assert len(quick.get(quick_book, tag)["outbox"]) == 1
    assert len(book.entries()) == 1


def test_refuse_foreign_url_marker_and_second_pending_successor(tmp_path):
    book, quick_book, tag = _new(tmp_path)
    req = chat_handoff.observe(book, agent=tag, url=OLD, fill=1, chars=200000)
    with pytest.raises(PermissionError):
        chat_handoff.prepare(book, agent=tag, url=OLD, token="badtoken",
                             quick_book=quick_book)
    with pytest.raises(ValueError):
        chat_handoff.save(book, agent=tag, url=NEXT, content="a" * 300)
    with pytest.raises(ValueError):
        chat_handoff.observe(book, agent=tag, url="https://example.com/c/foo", fill=1, chars=1)
    chat_handoff.save(book, agent=tag, url=OLD, content="a" * 300)
    quick.prepare_successor(quick_book, tag, OLD, "a" * 300, req["token"])
    with pytest.raises(PermissionError):
        quick.prepare_successor(quick_book, tag, OLD, "b" * 300, "different-token")


def test_audit_usage_counts_tool_arguments_and_delivered_responses_once(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "audit.jsonl"
    def append(length, output):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"agent":"Q88-TEST", "args":{
                "content":{"sha256":"deadbeefaaaa", "length":length}},
                "result_bytes":output, "tool":"write_file"}) + "\n")
    append(100_000, 100_000)
    first = chat_handoff.audit_usage("Q88-TEST", logs_dir=logs, now=200)
    assert first["chars"] == 112_000 and first["calls"] == 1
    assert chat_handoff.audit_usage("Q88-TEST", logs_dir=logs, now=201) == first
    append(33_000, 40)
    second = chat_handoff.audit_usage("Q88-TEST", logs_dir=logs, now=261)
    assert second["chars"] == 145_040 and second["calls"] == 2
    assert second["fill"] == round(145_040 / 3_000_000, 4)
