"""Точка продолжения ведущего из реестра (08.11)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import continuity, lead  # noqa: E402
from tests.fleet.test_lead_admin import _task, env  # noqa: E402,F401


def test_empty_registry_gives_no_lines(env):
    store, _ = env
    assert continuity.continuity_lines(store, "Q") == []


def test_milestone_and_open_instruction_appear(env):
    store, _ = env
    note = "веха: O-android-update L1→L3\nзабытое: письмо Ивану записано в log/2"
    with store.tx() as tx:
        tx.record_event(kind="submit", worker_id="W1", project="P", tz="TZ-LEAD", payload={"note": note})
        tx.record_event(kind="submit", worker_id="W1", project="P", tz="TZ-LEAD", payload={"note": "прогресс"})
        tx.record_event(kind="checkpoint", worker_id="W1", project="P", tz="TZ-LEAD",
                        payload={"done": "собрал сборку", "next": "замерить на телефоне"})
        tx.record_event(kind="instruction", project="P", tz="TZ-LEAD",
                        payload={"id": "i-1", "author": "owner", "text": "сделай экран входа"})
    text = "\n".join(continuity.continuity_lines(store, "P"))
    assert "O-android-update L1→L3" in text and "письмо Ивану" in text
    assert "замерить на телефоне" in text
    assert "[i-1]" in text and "сделай экран входа" in text
    assert "Q" not in text and len(text) <= continuity.BUDGET
    assert lead.is_lead("TZ-LEAD")


def test_block_failure_does_not_drop_others(env, monkeypatch):
    store, _ = env
    with store.tx() as tx:
        tx.record_event(kind="instruction", project="P", tz="TZ-LEAD", payload={"id": "i-2", "text": "задача"})
    monkeypatch.setattr(continuity, "_milestones", lambda *a: 1 / 0)
    monkeypatch.setattr(continuity, "_obstacles", lambda *a: 1 / 0)
    assert any("i-2" in x for x in continuity.continuity_lines(store, "P"))


def test_state_md_over_limit_gives_one_line(env):
    from dev_mcp.fleet import continuity
    store, root = env
    (root / "ToDo/lead/STATE.md").write_text("x" * 5000, encoding="utf-8")
    assert any("STATE.md 4 КБ" in line for line in continuity.continuity_lines(store, "P"))
    (root / "ToDo/lead/STATE.md").write_text("короткое", encoding="utf-8")
    assert not any("STATE.md" in line for line in continuity.continuity_lines(store, "P"))
