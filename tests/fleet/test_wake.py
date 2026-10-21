"""Будильник: приказ разбудить замолчавшего агента, повтор, отказ и возврат задачи."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import wake  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402

T0 = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
URL = "https://chatgpt.com/g/g-p-x/c/0123abcd-4567"


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


@pytest.fixture()
def env(tmp_path: Path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    stamp = iso(T0 - timedelta(hours=1))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P','C:/P',1,1)",
                  (), event={"kind": "test_setup"})
        for tz, state, needs in [("TZ-1", "taken", []), ("TZ-2", "ready", ["TZ-1"]),
                                 ("TZ-3", "ready", ["TZ-1"]), ("TZ-9", "taken", [])]:
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                "resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES('P',?,?,?,5,'work','','',?,'[]','[]','[]',NULL,'b',?,?)",
                (tz, tz, state, json.dumps(needs), stamp, stamp), event={"kind": "test_setup"})
        for wid, label, tz in [("W1", "f-a", "TZ-1"), ("W9", "f-b", "TZ-9")]:
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,'chatgpt',?,?)",
                      (wid, label, stamp, iso(T0)), event={"kind": "test_setup"})
            tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('P',?,?,'active',?)",
                      (tz, wid, stamp), event={"kind": "test_setup"})
    book = wake.ChatBook(tmp_path / "chats.json")
    return store, book


def state(store: Store, tz: str) -> tuple[str, str | None]:
    with store.tx() as tx:
        task = tx.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (tz,))
        claim = tx.one("SELECT c.state, w.revoked_at FROM claims c JOIN workers w ON w.id=c.worker_id "
                       "WHERE c.project='P' AND c.tz=? ORDER BY c.id DESC", (tz,))
    return str(task["state"]), claim["revoked_at"]


def test_book_accepts_only_chat_urls(env) -> None:
    _, book = env
    n = book.remember(f"f-a {URL}\nf-x https://evil.example/c/1\nмусор\n")
    assert n == 1 and book.url("f-a") == URL and book.url("f-x") == ""


def test_ladder_wake_retry_then_give_up(env) -> None:
    store, book = env
    book.remember(f"f-a {URL}")
    # Тишина 3 мин — рано.
    assert wake.orders(store, book, now=T0 + timedelta(minutes=3)) == []
    # 5 мин — приказ №1; задача с 2 ждущими идёт первой, текст несёт worker_id и fleet_resume.
    first = wake.orders(store, book, now=T0 + timedelta(minutes=5))
    assert [(o["label"], o["attempt"], o["blocks"]) for o in first] == [("f-a", 1, 2)]
    assert "fleet_resume(agent='W1', wake='w-" in first[0]["text"] and first[0]["url"] == URL
    assert wake.order_lines(first)[0].startswith(f"разбудить: f-a {URL} :: Продолжай P/TZ-1")
    # Через минуту — не повторяем.
    assert wake.orders(store, book, now=T0 + timedelta(minutes=6)) == []
    # Ещё 8 мин тишины — приказ №2, после него приказов нет.
    second = wake.orders(store, book, now=T0 + timedelta(minutes=13, seconds=30))
    assert [o["attempt"] for o in second] == [2]
    assert wake.orders(store, book, now=T0 + timedelta(minutes=40)) == []
    # Не сдаёмся раньше 8 мин после последнего приказа.
    assert wake.give_up(store, book, now=T0 + timedelta(minutes=15)) == []
    assert state(store, "TZ-1") == ("taken", None)
    # Потом задача уходит в очередь с сохранённой работой, воркер отозван.
    assert "P/TZ-1" in wake.give_up(store, book, now=T0 + timedelta(minutes=22))
    task_state, revoked = state(store, "TZ-1")
    assert task_state == "ready" and revoked


def test_agent_that_answered_is_not_woken_again(env) -> None:
    store, book = env
    book.remember(f"f-a {URL}")
    assert wake.orders(store, book, now=T0 + timedelta(minutes=5))
    # Агент ответил в 08:06 — старые приказы не считаются, нужна новая тишина ≥ 4 мин.
    with store.tx() as tx:
        tx.change("UPDATE workers SET last_seen=? WHERE id='W1'", (iso(T0 + timedelta(minutes=6)),),
                  event={"kind": "test_setup"})
    assert wake.orders(store, book, now=T0 + timedelta(minutes=8)) == []
    again = wake.orders(store, book, now=T0 + timedelta(minutes=11))
    assert [o["attempt"] for o in again] == [1]


def test_unknown_chat_gives_up_after_20_min_not_180(env) -> None:
    store, book = env  # у f-b адрес чата не известен
    assert all(o["label"] != "f-b" for o in wake.orders(store, book, now=T0 + timedelta(minutes=10)))
    assert "P/TZ-9" not in wake.give_up(store, book, now=T0 + timedelta(minutes=15))
    assert "P/TZ-9" in wake.give_up(store, book, now=T0 + timedelta(minutes=21))
    assert state(store, "TZ-9")[0] == "ready"
