"""Урок 42: интегратор не начинает заново после перезапуска посреди большого тика."""
from __future__ import annotations

import json

import pytest

from dev_mcp.fleet import integrate
from tests.fleet.test_integrate import add_task, env, event, make  # noqa: F401


def _cursor(tmp):
    return json.loads((tmp / "integrate-state.json").read_text(encoding="utf-8"))["last_accept_id"]


def test_tick_is_bounded_and_cursor_survives_crash(env, monkeypatch) -> None:
    store, root, rootn, tmp = env
    monkeypatch.setattr(integrate, "TICK_BATCH", 3)
    integ = make(store, tmp)
    for n in range(5):
        add_task(store, f"TZ-{n}", [f"{rootn}/f{n}.txt"])
        (root / f"f{n}.txt").write_text(str(n), encoding="utf-8")
        event(store, "accept", f"TZ-{n}")
    with store.tx() as tx:
        ids = [int(r["id"]) for r in tx.query("SELECT id FROM events WHERE kind='accept' ORDER BY id")]

    real = integ._commit
    calls = []

    def crash_on_second(batch, accept):
        calls.append(accept["tz"])
        if len(calls) == 2:
            raise RuntimeError("dev restarted mid-tick")
        return real(batch, accept)

    monkeypatch.setattr(integ, "_commit", crash_on_second)
    with pytest.raises(RuntimeError):
        integ.tick()
    assert _cursor(tmp) == ids[0]  # первая приёмка не будет пройдена повторно

    monkeypatch.setattr(integ, "_commit", real)
    integ.tick()
    assert _cursor(tmp) == ids[3]  # порция ограничена TICK_BATCH=3
    integ.tick()
    assert _cursor(tmp) == ids[4]


def test_todo_only_commit_skips_candidate_validation(env, monkeypatch) -> None:
    """Урок 44: ToDo-документы не гоняют проверку кандидата."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / "ToDo").mkdir()
    (root / "ToDo" / "note.md").write_text("x", encoding="utf-8")
    add_task(store, "TZ-9", [f"{rootn}/todo/note.md"])
    event(store, "accept", "TZ-9")

    def boom(*_a, **_k):
        raise AssertionError("candidate validation must not run for ToDo-only")

    monkeypatch.setattr(integ, "_candidate", boom)
    out = integ.tick()
    [ev] = [e for e in out if e["kind"] == "integrate"]
    assert ev["payload"].get("commit")


def test_fresh_accepts_limit_pending_revalidation(env, monkeypatch) -> None:
    """Урок 46: свежие приёмки не ждут часами за дорогими отложенными."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    seen = []
    fake_pending = [{"id": 10_000 + n, "project": "P", "tz": f"OLD-{n}", "worker_id": "W"}
                    for n in range(6)]
    monkeypatch.setattr(integ, "_pending_accepts", lambda: list(fake_pending))
    real = integ._commit

    def track(batch, accept):
        seen.append(accept["tz"])
        return real(batch, accept)

    monkeypatch.setattr(integ, "_commit", track)
    add_task(store, "TZ-N", [f"{rootn}/n.txt"])
    (root / "n.txt").write_text("n", encoding="utf-8")
    event(store, "accept", "TZ-N")
    integ.tick()
    assert seen[0] == "TZ-N"
    assert sum(t.startswith("OLD-") for t in seen) == integrate.PENDING_WITH_FRESH
