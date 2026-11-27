"""03.11.2026: проверка рамки после understood — тень, стадию не блокирует."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from dev_mcp.fleet.store import Store
from dev_mcp.intents import frame_check


def _intent(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".intent").mkdir(parents=True)
    (root / "intent").mkdir()
    (root / ".intent" / "intent.md").write_text("Хочу, чтобы флот сам находил развилки.", encoding="utf-8")
    (root / "intent" / "understood.md").write_text("## Интенция\nСделать линзы.\n", encoding="utf-8")
    (root / "intent" / "owner-brief.md").write_text(
        "## Коротко\nЭто проверка. Она нужна вам.\n\n## Вопросы\nнет\n", encoding="utf-8")
    return root


def _events(store: Store, kind: str) -> list[dict]:
    with store.read() as tx:
        return [json.loads(r["payload"]) for r in tx.query(
            "SELECT payload FROM events WHERE kind=? ORDER BY id", (kind,))]


def test_unavailable_check_records_event_and_does_not_raise(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = _intent(tmp_path)
    monkeypatch.setattr(frame_check, "SCRIPT", tmp_path / "frame_check.py")
    (tmp_path / "frame_check.py").write_text("", encoding="utf-8")

    def no_openrouter(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, b"frame_check unavailable: no key", b"")

    assert frame_check.run(store, "P", "I-understood", root, runner=no_openrouter,
                           data=tmp_path) == "unavailable"
    [event] = _events(store, "intent.frame_check_unavailable")
    assert "код 2" in event["reason"] and event["digest"]
    # та же версия понимания повторно не проверяется (и не тратит деньги)
    assert frame_check.run(store, "P", "I-understood", root, runner=no_openrouter,
                           data=tmp_path) == "skipped"


def test_timeout_is_unavailable_not_failure(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = _intent(tmp_path)
    monkeypatch.setattr(frame_check, "SCRIPT", tmp_path / "frame_check.py")
    (tmp_path / "frame_check.py").write_text("", encoding="utf-8")

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    assert frame_check.run(store, "P", "I-understood", root, runner=slow, data=tmp_path) == "unavailable"
    assert "тайм-аут" in _events(store, "intent.frame_check_unavailable")[0]["reason"]


def test_forks_go_to_event_and_owner_brief(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = _intent(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(frame_check, "SCRIPT", tmp_path / "frame_check.py")
    monkeypatch.setattr(frame_check, "RUNS", runs)
    (tmp_path / "frame_check.py").write_text("", encoding="utf-8")
    fork = {"lens": "words", "question": "Развилки показывать вам или решать самим?",
            "options": ["показывать", "решать"]}
    (runs / "fr-1.json").write_text(json.dumps({"result": {
        "verdict": "forks", "forks": [fork], "served_model": "m", "cost": 0.001,
        "lenses_version": "abc"}}), encoding="utf-8")

    def ok(argv, **kw):
        assert "HTTPS_PROXY" not in kw["env"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"frame_id": "fr-1", "verdict": "forks", "forks": 1}).encode(), b"")

    assert frame_check.run(store, "P", "I-understood", root, runner=ok, data=tmp_path) == "forks"
    [event] = _events(store, "intent.frame_check")
    assert {event[k] for k in ("frame_id", "verdict", "model", "lenses_version")} == {"fr-1", "forks", "m", "abc"}
    assert event["forks"] == [fork] and event["mode"] == "shadow"
    brief = (root / "intent" / "owner-brief.md").read_text(encoding="utf-8")
    assert "## Коротко" in brief and frame_check.SECTION in brief and fork["question"] in brief
    # повтор с новой версией понимания заменяет раздел, а не дописывает второй
    (root / "intent" / "understood.md").write_text("## Интенция\nИначе.\n", encoding="utf-8")
    frame_check.run(store, "P", "I-understood", root, runner=ok, data=tmp_path)
    assert (root / "intent" / "owner-brief.md").read_text(encoding="utf-8").count(frame_check.SECTION) == 1


def test_budget_ceiling_stops_paid_calls(tmp_path, monkeypatch):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = _intent(tmp_path)
    monkeypatch.setattr(frame_check, "SCRIPT", tmp_path / "frame_check.py")
    (tmp_path / "frame_check.py").write_text("", encoding="utf-8")
    (tmp_path / "frame-check.json").write_text('{"enabled": true, "total_usd": 0.01}', encoding="utf-8")
    with store.tx() as tx:
        tx.record_event(kind="intent.frame_check", project="P", tz="old", payload={"cost": 0.02})

    def never(argv, **kw):
        raise AssertionError("платный вызов сверх потолка")

    assert frame_check.run(store, "P", "I-understood", root, runner=never, data=tmp_path) == "unavailable"
    assert "потолок" in _events(store, "intent.frame_check_unavailable")[0]["reason"]
