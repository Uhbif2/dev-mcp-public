"""Wake protection while ChatGPT is generating a long answer."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import flow, wake  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402


T0 = datetime(2026, 10, 13, 8, tzinfo=timezone.utc)
URL = "https://chatgpt.com/g/g-p-x/c/0123abcd-4567"


@pytest.fixture()
def env(tmp_path: Path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    before = (T0 - timedelta(hours=1)).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P','C:/P',1,1)",
                  (), event={"kind": "setup"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES('P','TZ','TZ','taken',5,'work','','','[]','[]','[]','[]',NULL,'b',?,?)",
            (before, before), event={"kind": "setup"},
        )
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES('W1','f-a','chatgpt',?,?)",
                  (before, T0.isoformat()), event={"kind": "setup"})
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('P','TZ','W1','active',?)",
                  (before,), event={"kind": "setup"})
    book = wake.ChatBook(tmp_path / "chats.json")
    book.remember("f-a " + URL)
    return store, book


def issue(env, minutes: float) -> dict:
    store, book = env
    orders = wake.orders(store, book, now=T0 + timedelta(minutes=minutes))
    assert len(orders) == 1
    return orders[0]


def report(env, order: dict, state: str, minutes: float) -> bool:
    return wake.report_state(env[0], wake_id=order["wake"], state=state,
                             now=T0 + timedelta(minutes=minutes))


def current_state(env) -> tuple[str, str | None]:
    with env[0].read() as tx:
        task = tx.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ'")
        worker = tx.one("SELECT revoked_at FROM workers WHERE id='W1'")
    return task["state"], worker["revoked_at"]


def test_thinking_25_min_is_protected_and_probes_do_not_advance_ladder(env) -> None:
    first = issue(env, 5)
    assert report(env, first, "generating", 5.1)
    assert not report(env, first, "generating", 5.2)  # duplicate is idempotent
    second = issue(env, 13.5)
    assert second["attempt"] == 1  # generating order is not counted
    assert report(env, second, "generating", 13.6)
    third = issue(env, 22)
    assert third["attempt"] == 1
    assert report(env, third, "generating", 22.1)
    assert wake.give_up(env[0], env[1], now=T0 + timedelta(minutes=25)) == []
    assert current_state(env) == ("taken", None)


def test_idle_reports_count_toward_requeue(env) -> None:
    first = issue(env, 5)
    assert report(env, first, "idle", 5.1)
    second = issue(env, 13.5)
    assert second["attempt"] == 2
    assert report(env, second, "idle", 13.6)
    assert wake.give_up(env[0], env[1], now=T0 + timedelta(minutes=19)) == []
    assert wake.give_up(env[0], env[1], now=T0 + timedelta(minutes=22)) == ["P/TZ"]
    assert current_state(env)[0] == "ready"
    assert current_state(env)[1] is not None


def test_generating_caps_after_45_minutes_without_a_tool_call(env) -> None:
    first = issue(env, 5)
    report(env, first, "generating", 5.1)
    assert wake.give_up(env[0], env[1], now=T0 + timedelta(minutes=50)) == []
    assert wake.give_up(env[0], env[1], now=T0 + timedelta(minutes=51)) == ["P/TZ"]
    assert current_state(env)[0] == "ready"


def test_generating_then_idle_ends_protection_and_metrics_count_once(env) -> None:
    first = issue(env, 5)
    report(env, first, "generating", 5.1)
    report(env, first, "idle", 5.2)
    second = issue(env, 13.5)
    assert second["attempt"] == 2
    report(env, second, "error", 13.6)
    assert wake.give_up(env[0], env[1], now=T0 + timedelta(minutes=22)) == ["P/TZ"]
    with env[0].read() as tx:
        result = flow.wakes(tx.conn, T0.isoformat()) if hasattr(tx, "conn") else None
    if result is None:
        result = flow.wakes(env[0]._connection(), T0.isoformat())
    assert result["orders"] == 2
    assert result["generating"] == 1
    assert result["gave_up"] == 1
    assert any("из них при generating 1, отборов 1" in line for line in flow.format_flow({
        "since": "test",
        "verdicts": {"leak": {"n": 0, "N": 0}, "first_pass": {"n": 0, "N": 0},
                     "accepted_per_day": {}},
        "turns": {"n": 0, "median_min": None, "median_calls": None, "ends": {}},
        "parking": {"n": 0, "outcomes": {}},
        "wakes": result, "gates": {},
    }))


def test_forged_token_and_stale_report_are_ignored(env) -> None:
    with pytest.raises(ValueError, match="not issued"):
        wake.report_state(env[0], wake_id="w-123456", state="generating")
    first = issue(env, 5)
    with env[0].tx() as tx:
        tx.change("UPDATE workers SET last_seen=? WHERE id='W1'",
                  ((T0 + timedelta(minutes=6)).isoformat(),), event={"kind": "setup"})
    assert not report(env, first, "generating", 7)
    assert issue(env, 11)["attempt"] == 1
