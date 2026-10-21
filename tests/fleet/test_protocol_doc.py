"""Регрессия: инструкции флота не должны направлять общие данные в память ChatGPT."""

from pathlib import Path


PROTOCOL = Path(__file__).resolve().parents[2] / "docs" / "FLEET-PROTOCOL.md"


def test_protocol_forbids_chatgpt_memory_writes() -> None:
    text = PROTOCOL.read_text(encoding="utf-8")

    assert "Не сохраняй ничего в память ChatGPT" in text
    assert "память ChatGPT общая для всех" in text
    assert "Не вызывай запись в память" in text


def test_protocol_persists_state_only_in_project_and_registry() -> None:
    text = PROTOCOL.read_text(encoding="utf-8")

    assert "только в реестре флота и файлах проекта" in text
    assert "fleet_checkpoint" in text
