"""Контрольная точка, руль в конверте, подтверждение будки токеном, задача в ответе сдачи."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_mcp.fleet import checkpoint

from test_tools import _make_server, _seed_task, _tool_fn


def _age_claim(store, minutes: float) -> None:
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with store.tx() as tx:
        tx.change("UPDATE claims SET started_at=? WHERE state='active'", (old,), event={"kind": "test"})


def test_checkpoint_needs_claim_and_shows_in_resume(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    join, take = _tool_fn(server, "fleet_join"), _tool_fn(server, "fleet_take")
    cp, resume = _tool_fn(server, "fleet_checkpoint"), _tool_fn(server, "fleet_resume")
    join("tab")
    assert "активной задачи нет" in cp("W1", "x", "y")
    take("W1")
    assert "нужны хотя бы" in cp("W1", "", "y")
    saved = cp("W1", "парсер готов, 3 теста зелёные", "дописать CLI", rejected="regex — медленно")
    assert "demo/TZ-1 сохранена" in saved
    text = resume("W1")
    assert "Контрольная точка — продолжай с неё" in text
    assert "следующий шаг: дописать CLI" in text and "отвергнуто: regex — медленно" in text


def test_steering_asks_for_checkpoint_once_per_window(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    # коннектор переподключён: новый инструмент уже кто-то вызвал — значит, его видят подключившиеся после
    with store.tx() as tx:
        tx.record_event(kind="write", worker_id="W0", tool="apply_patch", payload={})
    _tool_fn(server, "fleet_join")("tab")
    _tool_fn(server, "fleet_take")("W1")
    read_file = _tool_fn(server, "read_file")
    (root / "owned.txt").write_text("x\n", encoding="utf-8")
    assert "руль:" not in read_file(str(root / "owned.txt"), agent="W1")
    _age_claim(store, checkpoint.DUE_MIN + 1)
    first = read_file(str(root / "owned.txt"), agent="W1")
    assert "руль:" in first and "fleet_checkpoint(agent='W1'" in first
    assert "руль:" not in read_file(str(root / "owned.txt"), agent="W1")  # не спамит
    assert checkpoint.due(store, "W1")  # условие держится, пока точки нет
    _tool_fn(server, "fleet_checkpoint")("W1", "done", "next")
    assert checkpoint.due(store, "W1") == ""


def test_wake_token_is_acknowledged_with_delay(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    _tool_fn(server, "fleet_join")("tab")
    _tool_fn(server, "fleet_take")("W1")
    with store.tx() as tx:
        tx.record_event(kind="wake_order", worker_id="W1", project="demo", tz="TZ-1",
                        payload={"claim_id": 1, "wake": "w-abc123"})
    _tool_fn(server, "fleet_resume")("W1", wake="w-abc123")
    with store.tx() as tx:
        row = tx.one("SELECT payload FROM events WHERE kind='wake_ack'")
    assert row is not None and '"wake":"w-abc123"' in row["payload"] and '"delay_s":' in row["payload"]
    assert '"delay_s":null' not in row["payload"]


def test_submit_answer_carries_next_task(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    _tool_fn(server, "fleet_join")("tab")
    _tool_fn(server, "fleet_take")("W1")
    _seed_task(store, root, tz="TZ-2")
    (root / "owned.txt").write_text("done\n", encoding="utf-8")
    # TZ-2 делит owns с TZ-1: после сдачи зона свободна, задачу можно выдать сразу
    out = _tool_fn(server, "fleet_submit")("W1", "ready")
    assert "сдана на приёмку" in out
    assert "Следующая задача уже выдана тебе" in out and "demo/TZ-2" in out
    with store.tx() as tx:
        assert tx.one("SELECT worker_id FROM claims WHERE tz='TZ-2' AND state='active'")["worker_id"] == "W1"


def test_no_checkpoint_nudge_while_connector_is_stale(tmp_path: Path) -> None:
    """Пока новых инструментов никто не вызывал, руль не зовёт fleet_checkpoint: у агента его нет."""
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    _tool_fn(server, "fleet_join")("tab")
    _tool_fn(server, "fleet_take")("W1")
    _age_claim(store, checkpoint.DUE_MIN + 1)
    assert checkpoint.due(store, "W1") == ""
