"""Archive/replay safety and 24h behavior of the live, free-model overseer."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from dev_mcp.fleet import overseer
from tools import overseer_replay


CID = "00000001-0000-4000-8000-000000000001"
URL = f"https://chatgpt.com/c/{CID}"
OWNER = "Проверь все компоненты и покажи результат живого теста."
REPLY = "Я проверил все компоненты и показал результат, но без доказательства."


def generated():
    return {
        "goal": "Проверить компоненты и получить результат тестирования.",
        "goal_quote": "Проверь все компоненты и покажи результат",
        "reply_summary": "Заявил о проверке без доказательств.",
        "reply_quote": "проверил все компоненты и показал результат",
        "verdict": "drift",
        "issue": "нет доказательств проверки компонентов",
        "message": "В ответе проверил все компоненты и показал результат, но не приложил тесты. Запусти проверку.",
        "confidence": 0.96,
    }


def snap(n, *, reply=REPLY, owners=None):
    return {"url": URL, "user_messages": owners or [OWNER], "last_assistant": reply,
            "last_role": "assistant", "n_messages": n, "composer_empty": True}


@pytest.fixture(autouse=True)
def safe_shared_redactor(monkeypatch):
    monkeypatch.setattr(overseer, "_redactor", lambda: lambda text: (text, {}))


@pytest.fixture
def archive(tmp_path, monkeypatch):
    book = overseer.Book(tmp_path)
    overseer.add(book, URL, "sample", "send")
    monkeypatch.setattr(overseer, "_local_generate", lambda _: generated())
    opts = {"history_fn": lambda *args: {"actions": [], "count": 0},
            "root_fn": lambda _: None}
    first = overseer.observe(book, snap(2), **opts)
    second = overseer.observe(book, snap(4), **opts)
    assert first["action"] == "send"
    assert second["why"] == "duplicate_semantics"
    return book


def test_archive_secret_patterns_and_paths():
    raw = (
        "C:\\Users\\owner\\my-project\\build.txt "
        "sk-abcdefghij123456 ghp_abcdefghijklmnopqrstuvwxyz1234 "
        "Bearer eyJhbGciOiJhbGciOiJIUzI1NiJ9.something "
        + "F" * 64 + " " + "A" * 90 + " password=topsecret"
    )
    safe = overseer._archive_redact(raw)
    assert "C:\\Users\\owner\\my-project\\build.txt" in safe
    for secret in ("sk-abcdefghij", "ghp_abcdefghij", "eyJhbGci", "F" * 64,
                   "A" * 90, "topsecret"):
        assert secret not in safe
    assert safe.count("[REDACTED]") >= 5


def test_live_archive_records_turns_owner_delta_and_redacts(tmp_path, monkeypatch):
    book = overseer.Book(tmp_path)
    overseer.add(book, URL, "sample", "send")
    monkeypatch.setattr(overseer, "_local_generate", lambda _: generated())
    opts = {"history_fn": lambda *args: {"actions": ["run --token sk-abcdefabcdef1234"], "count": 1},
            "probes_fn": lambda *args: [], "root_fn": lambda _: None}
    result = overseer.observe(book, snap(2, reply=REPLY + " sk-abcdefabcdef1234"), **opts)
    assert result["action"] == "send"
    overseer.observe(book, snap(4, owners=[
        OWNER, "Исправь обработку ошибок. Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"]), **opts)
    rows = [json.loads(s) for s in book.reply_archive(CID).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert len(rows[0]["owner_replies"]) == 1 and rows[0]["owner_replies"][0] == OWNER
    assert len(rows[1]["owner_replies"]) == 1
    assert "Исправь обработку ошибок" in rows[1]["owner_replies"][0]
    assert rows[0]["turn"] != rows[1]["turn"]
    assert all(len(r["reply"]) <= 6000 for r in rows)
    assert all(sum(map(len, r["owner_replies"])) <= 2000 for r in rows)
    assert rows[0]["mode"] == "send"
    archive_bytes = book.reply_archive(CID).read_text(encoding="utf-8")
    assert "sk-abcdef" not in archive_bytes
    assert "Bearer ABCDE" not in archive_bytes


def test_storage_caps_even_when_archived_directly(tmp_path):
    book = overseer.Book(tmp_path)
    book.archive_turn(CID, {
        "turn": "large", "reply": "Подробный ответ агента. " * 900,
        "owner_replies": ["Первое сообщение. " * 150, "Последний запрос. " * 150],
        "verdict": "continue", "mode": "send", "skipped_reason": "no_useful_intervention"})
    row = json.loads(book.reply_archive(CID).read_text(encoding="utf-8").strip())
    assert len(row["reply"]) == 6000
    assert sum(len(s) for s in row["owner_replies"]) == 2000
    assert row["owner_replies"] == [("Последний запрос. " * 150)[-2000:]]


def test_archive_rotation_preserves_complete_jsonl(tmp_path, monkeypatch):
    book = overseer.Book(tmp_path)
    monkeypatch.setattr(overseer, "MAX_REPLY_ARCHIVE_BYTES", 850)
    for index in range(12):
        book.archive_turn(CID, {"turn": str(index), "reply": "Ответ " + ("я" * 160),
                                "owner_replies": ["Проверь тест"], "verdict": "continue",
                                "mode": "send", "sent_text": "", "skipped_reason": "continue"})
    data = book.reply_archive(CID).read_bytes()
    rows = [json.loads(s) for s in data.decode("utf-8").splitlines()]
    assert len(data) <= 850
    assert rows[-1]["turn"] == "11" and rows[0]["turn"] != "0"


def test_replay_fixture_same_model_guards_and_no_writes(archive):
    path = archive.reply_archive(CID)
    before = path.read_bytes()
    outputs = []
    def local_only(evidence):
        outputs.append(evidence)
        return generated()
    report = overseer_replay.replay_archive(path, generate=local_only)
    assert len(outputs) == 2
    assert outputs[0]["owner_instructions"] == [OWNER]
    assert report["summary"] == {
        "turns": 2, "messages": 1, "silences": 1, "duplicates": 1,
        "old_messages": 1, "old_silences": 1}
    assert "duplicate_semantics" in report["rows"][1]["after"]
    assert path.read_bytes() == before


def test_stats_24h_turns_silence_reasons_and_owner_reactions(tmp_path, monkeypatch):
    book = overseer.Book(tmp_path)
    fixed = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(overseer, "_now", lambda: fixed.isoformat())
    book.archive_turn(CID, {"turn": "1", "reply": "R", "mode": "send",
                            "sent_text": "M", "skipped_reason": ""})
    book.archive_turn(CID, {"turn": "2", "reply": "R", "mode": "send",
                            "sent_text": "", "skipped_reason": "duplicate_semantics"})
    book.archive_turn(CID, {"turn": "3", "reply": "R", "mode": "shadow",
                            "sent_text": "", "skipped_reason": "shadow"})
    book.log({"chat": CID, "event": "owner_reply", "after_verdict": "drift"})
    book.log({"chat": CID, "event": "owner_reply", "after_verdict": ""})
    assert overseer.stats_24h(book, CID, now=fixed) == {
        "turns": 3, "messages": 1,
        "silences": {"duplicate_semantics": 1, "shadow": 1},
        "owner_reactions": 1}
