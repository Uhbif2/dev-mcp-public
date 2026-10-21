"""TZ-310: один классификатор и одинаковый текст для всех поверхностей."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from dev_mcp.fleet import flow
from dev_mcp.fleet.store import Store


NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE workers (id TEXT PRIMARY KEY, last_seen TEXT, revoked_at TEXT)")
    conn.execute("CREATE TABLE claims (worker_id TEXT, state TEXT)")
    return conn


def _worker(conn: sqlite3.Connection, wid: str, age_s: int, *, claimed: bool = True,
            revoked: bool = False) -> None:
    seen = (NOW - timedelta(seconds=age_s)).isoformat()
    conn.execute("INSERT INTO workers VALUES (?,?,?)",
                 (wid, seen, NOW.isoformat() if revoked else None))
    if claimed:
        conn.execute("INSERT INTO claims VALUES (?,'active')", (wid,))


def test_time_boundaries_duplicates_revoked_and_idle() -> None:
    conn = _conn()
    try:
        for wid, seconds in (("fresh", 0), ("at-2m", 120), ("thinking", 121),
                             ("at-10m", 600), ("silent", 601)):
            _worker(conn, wid, seconds)
        _worker(conn, "parking", 15, claimed=False)
        _worker(conn, "stale-unclaimed", 700, claimed=False)
        _worker(conn, "revoked", 15, revoked=True)
        conn.execute("INSERT INTO claims VALUES ('fresh','active')")
        conn.execute("INSERT INTO claims VALUES ('stale-unclaimed','released')")
        expected = {"working": 2, "thinking": 0, "silent": 3, "waiting": 1,
                    "silent_answering": 0, "silent_ended": 0, "silent_unknown": 3}
        assert flow.agent_states(conn, now=NOW) == expected
        assert flow.format_silent_breakdown(expected) == (
            "агенты-молчание: отвечают без вкладки 0 · ход окончен 0 · без следов 3")
        assert flow.format_agent_states(expected) == (
            "агенты: MCP≤2мин 2 · генерация UI 0 · не подтверждены 3 · ждут 1"
        )
    finally:
        conn.close()


def test_production_lookup_uses_index_with_large_claim_history(tmp_path) -> None:
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    conn = sqlite3.connect(store.path)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM claims "
            "WHERE worker_id=? AND state='active'", ('historical-worker',),
        ).fetchall()
        assert any('claims_worker_state' in row[3] and 'SEARCH' in row[3]
                   for row in plan), plan
    finally:
        conn.close()
        store.close()


def test_store_and_sqlite_share_classification(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    seen = NOW.isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)",
                  (str(tmp_path),), event="seed")
        tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) "
                  "VALUES('P','TZ','taken',?,?)", (seen, seen), event="seed")
        tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W1',?,?)",
                  (seen, seen), event="seed")
        tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W2',?,?)",
                  (seen, seen), event="seed")
        tx.change("INSERT INTO claims(worker_id,project,tz,state,started_at) "
                  "VALUES('W1','P','TZ','active',?)", (seen,), event="seed")
    conn = sqlite3.connect(store.path)
    try:
        assert flow.agent_states(store, now=NOW) == flow.agent_states(conn, now=NOW)
        assert flow.format_agent_states(flow.agent_states(store, now=NOW)) == (
            "агенты: MCP≤2мин 1 · генерация UI 0 · не подтверждены 0 · ждут 1"
        )
    finally:
        conn.close()
