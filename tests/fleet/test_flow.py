"""Метрики потока v2: каждое определение из fleet/flow.py на маленьком реестре."""
from __future__ import annotations

import json
import sqlite3

from dev_mcp.fleet import flow


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (project TEXT, tz TEXT, kind TEXT, acceptance TEXT)")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, "
                 "worker_id TEXT, project TEXT, tz TEXT, rc INTEGER, payload TEXT DEFAULT '{}')")
    return conn


def _ev(conn, ts, kind, worker=None, project="P", tz=None, rc=None, **payload):
    conn.execute("INSERT INTO events (ts,kind,worker_id,project,tz,rc,payload) VALUES (?,?,?,?,?,?,?)",
                 (ts, kind, worker, project, tz, rc, json.dumps(payload)))


def test_first_pass_counts_first_verdict_not_accepted_tasks() -> None:
    conn = _db()
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [
        ("P", "TZ-1", "work", '["pytest"]'), ("P", "TZ-2", "work", '["pytest"]'),
        ("P", "fix:TZ-2", "fix", '["pytest"]'), ("P", "TZ-3", "work", "[]"),
    ])
    _ev(conn, "2026-09-27T01:00:00+00:00", "accept", tz="TZ-1")
    _ev(conn, "2026-09-27T01:00:00+00:00", "reject", tz="TZ-2")
    _ev(conn, "2026-09-27T02:00:00+00:00", "accept", tz="fix:TZ-2")
    _ev(conn, "2026-09-27T02:00:00+00:00", "accept", tz="review:TZ-3")  # ревью-задачи не в счёт
    _ev(conn, "2026-09-27T03:00:00+00:00", "reject", tz="TZ-3")  # без приёмки — не утечка

    v = flow.verdicts(conn, "2026-09-27")

    assert v["first_pass"] == {"n": 1, "N": 3}
    assert v["leak"] == {"n": 1, "N": 3}
    assert v["accepted_per_day"] == {"2026-09-27": 2}


def test_turn_ends_split_by_gap_and_classified() -> None:
    entries = [
        {"ts": "2026-09-27T10:00:00+00:00", "agent": "W1", "tool": "fleet_take"},
        {"ts": "2026-09-27T10:01:00+00:00", "agent": "W1", "tool": "read_file"},
        {"ts": "2026-09-27T10:02:00+00:00", "agent": "W1", "tool": "run"},
        {"ts": "2026-09-27T10:30:00+00:00", "agent": "W1", "tool": "edit_file"},
        {"ts": "2026-09-27T10:31:00+00:00", "agent": "W1", "tool": "fleet_take"},
        {"ts": "2026-09-27T10:31:00+00:00", "agent": "", "tool": "fleet_list"},  # расширение
    ]
    t = flow.turns(entries)
    assert t["n"] == 2
    assert t["ends"] == {"после запуска команды": 1, "пустая очередь / парковка": 1}


def test_wakes_confirmed_only_by_token() -> None:
    conn = _db()
    _ev(conn, "2026-09-27T10:00:00+00:00", "wake_order", worker="W1", wake="w-1")
    _ev(conn, "2026-09-27T10:00:30+00:00", "read", worker="W1")
    _ev(conn, "2026-09-27T11:00:00+00:00", "wake_order", worker="W2", wake="w-2")
    _ev(conn, "2026-09-27T11:00:40+00:00", "wake_ack", worker="W2", wake="w-2")
    w = flow.wakes(conn, "2026-09-27")
    assert w["orders"] == 2
    assert w["confirmed"] == 1
    assert w["answered"] == 2


def test_gates_trunk_state_is_last_run() -> None:
    conn = _db()
    _ev(conn, "2026-09-27T01:00:00+00:00", "gate", rc=0, dur_s=60)
    _ev(conn, "2026-09-27T02:00:00+00:00", "gate", rc=1, dur_s=80)
    _ev(conn, "2026-09-27T02:01:00+00:00", "styk_open")
    g = flow.gates(conn, "2026-09-27")["P"]
    assert (g["runs"], g["red"], g["styk"], g["trunk_green"]) == (2, 1, 1, False)
    assert g["max_s"] == 80
    assert any("КРАСНЫЙ" in line for line in flow.format_flow(flow.flow(conn, [], "2026-09-27")))
