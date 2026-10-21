from __future__ import annotations

import importlib.util
import json
import sqlite3
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fleet_console", REPO / "tools" / "fleet.py")
assert SPEC and SPEC.loader
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    fleet._create_schema(conn)
    conn.execute("INSERT INTO projects(name,root) VALUES('P','C:/tmp/P')")
    conn.execute(
        "INSERT INTO tasks(project,tz,state,acceptance,created_at,updated_at) "
        "VALUES('P','fix:TZ-1','ready',?,?,?)",
        (json.dumps(["old"]), fleet._now(), fleet._now()),
    )
    return conn


def test_set_acceptance_updates_and_audits() -> None:
    conn = make_conn()
    try:
        message = fleet._cmd_set_acceptance(conn, "P", "fix:TZ-1", ["new-a", "new-b"])
        assert "acceptance обновлена" in message
        row = conn.execute(
            "SELECT acceptance FROM tasks WHERE project='P' AND tz='fix:TZ-1'"
        ).fetchone()
        assert json.loads(row["acceptance"]) == ["new-a", "new-b"]
        event = conn.execute(
            "SELECT kind,payload FROM events WHERE project='P' AND tz='fix:TZ-1' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["kind"] == "acceptance_update"
        payload = json.loads(event["payload"])
        assert payload["old"] == ["old"]
        assert payload["new"] == ["new-a", "new-b"]
    finally:
        conn.close()


def test_set_acceptance_refuses_active_claim() -> None:
    conn = make_conn()
    try:
        now = fleet._now()
        conn.execute(
            "INSERT INTO workers(id,first_seen,last_seen) VALUES('W1',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P','fix:TZ-1','W1','active',?)",
            (now,),
        )
        with pytest.raises(ValueError, match="active claim"):
            fleet._cmd_set_acceptance(conn, "P", "fix:TZ-1", ["new"])
    finally:
        conn.close()


def test_set_acceptance_refuses_accepted_task() -> None:
    conn = make_conn()
    try:
        conn.execute(
            "UPDATE tasks SET state='accepted' WHERE project='P' AND tz='fix:TZ-1'"
        )
        with pytest.raises(ValueError, match="accepted"):
            fleet._cmd_set_acceptance(conn, "P", "fix:TZ-1", ["new"])
    finally:
        conn.close()


def test_tell_addresses_worker_or_task_holder_and_refuses_questions() -> None:
    conn = make_conn()
    try:
        now = fleet._now()
        conn.execute("INSERT INTO workers(id,kind,label,first_seen,last_seen) VALUES('W7','chat','x',?,?)", (now, now))
        conn.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('P','fix:TZ-1','W7','active',?)",
            (now,),
        )
        assert "→ W7" in fleet._cmd_tell(conn, "W7", "Отчитайся одной строкой.")
        assert "получатель: W7" in fleet._cmd_tell(conn, "P/fix:TZ-1", "Сначала прогони приёмку.")
        rows = conn.execute("SELECT to_worker,to_task,from_kind,kind FROM messages ORDER BY created_at,id").fetchall()
        assert {(r["to_worker"], r["to_task"]) for r in rows} == {("W7", None), (None, "P/fix:TZ-1")}
        assert all(r["from_kind"] == "owner" and r["kind"] == "note" for r in rows)
        with pytest.raises(ValueError):
            fleet._cmd_tell(conn, "W7", "Ты закончил?")
        with pytest.raises(ValueError):
            fleet._cmd_tell(conn, "W999", "Стоп.")
    finally:
        conn.close()


def test_memory_state_by_probe_and_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet, "MEMORY_PROBE_CACHE", tmp_path / "probe.json")
    calls = []

    def down() -> dict:
        calls.append(1)
        return {"status": "down", "checks": {"neo4j": {"status": "down"}, "embedder": {"status": "ok"}}}

    assert fleet.memory_state(True, now=1000.0, probe=down) == "память НЕ РАБОТАЕТ (neo4j)"
    # сбой держится минуту, потом проба повторяется: память могла уже подняться
    assert fleet.memory_state(True, now=1030.0, probe=down) == "память НЕ РАБОТАЕТ (neo4j)"
    assert len(calls) == 1
    ok = lambda: {"status": "ok", "checks": {}}  # noqa: E731
    assert fleet.memory_state(True, now=1061.0, probe=ok) == "память ок"
    # успех держится полный TTL
    assert fleet.memory_state(True, now=1061.0 + fleet.MEMORY_PROBE_TTL_S - 1, probe=down) == "память ок"
    assert len(calls) == 1
    assert fleet.memory_state(False, probe=ok) == "память ЛЕЖИТ (core не слушает)"


def test_memory_state_probe_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet, "MEMORY_PROBE_CACHE", tmp_path / "probe.json")

    def boom() -> dict:
        raise TimeoutError

    assert fleet.memory_state(True, now=1.0, probe=boom) == "память НЕ ОТВЕЧАЕТ (TimeoutError)"


def test_platform_line_uses_server_agent_states_not_gateway_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_conn()
    try:
        now = fleet._now()
        for wid, kind in (("W1", "chatgpt"), ("W2", "chatgpt"), ("W3", "bot"),
                          ("W4", "chatgpt")):
            conn.execute("INSERT INTO workers(id,kind,first_seen,last_seen) VALUES(?,?,?,?)",
                         (wid, kind, now, now))
        for tz in ("TZ-2", "TZ-3", "TZ-4"):
            conn.execute(
                "INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('P',?,'taken',?,?)",
                (tz, now, now),
            )
        for tz, worker, state in (("fix:TZ-1", "W1", "active"),
                                  ("TZ-2", "W2", "active"),
                                  ("TZ-3", "W3", "active"),
                                  ("TZ-4", "W4", "released")):
            conn.execute(
                "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('P',?,?,?,?)",
                (tz, worker, state, now),
            )
        class NoNetwork:
            def open(self, *args, **kwargs):
                raise OSError("offline fixture")

        monkeypatch.setattr(urllib.request, "build_opener", lambda *args: NoNetwork())
        monkeypatch.setattr(fleet, "_gateway_health_by_ip",
                            lambda: {"queue_depth": 4, "inflight": 99})
        monkeypatch.setattr(fleet, "_port_open", lambda _port: True)
        monkeypatch.setattr(fleet, "_open_existing", lambda _db: conn)
        monkeypatch.setattr(fleet, "memory_state", lambda _up: "память ок")
        line = fleet.platform_line(health_timeouts=(0.001,))
        assert "агенты: MCP≤2мин 3 · генерация UI 0 · не подтверждены 0 · ждут 1" in line
        assert "очередь 4" in line
        assert "в работе 99" not in line
    finally:
        conn.close()


def test_stuck_lines_counts_real_chatgpt_eligible_for_gemini_cross_review(
    tmp_path: Path,
) -> None:
    """why must not hide a reverse review simply because the worker ID is blank."""
    from dev_mcp.fleet import queue
    from dev_mcp.fleet.store import Store

    db = tmp_path / "fleet.sqlite"
    store = Store.open(str(db))
    now = fleet._now()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,5,1)",
                  ("Demo", str(tmp_path)), event="seed")
        for wid, kind in (("Wauthor", "gemini"), ("Wreview", "chatgpt"),
                          ("Wbot", "gemini"), ("Wrevoked", "chatgpt")):
            tx.change("INSERT INTO workers(id,kind,first_seen,last_seen) VALUES(?,?,?,?)",
                      (wid, kind, now, now), event="seed")
        tx.change("UPDATE workers SET revoked_at=? WHERE id='Wrevoked'", (now,), event="seed")
        tx.change(
            "INSERT INTO tasks(project,tz,state,kind,author_worker,created_at,updated_at) "
            "VALUES('Demo','cross-review:8','ready','cross_review','Wauthor',?,?)",
            (now, now), event="seed",
        )

    with store.read() as tx:
        assert not list(queue._available(tx, "", "Demo"))
        assert not list(queue._available(tx, "Wbot", "Demo"))
        assert [row[0]["tz"] for row in queue._available(tx, "Wreview", "Demo")] == ["cross-review:8"]

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        assert "готовых 1: можно выдать 1" in fleet.stuck_lines(conn, "Demo")[0]
        # A revoked/other-model or author identity must never count as eligible.
        with store.tx() as tx:
            tx.change("UPDATE workers SET revoked_at=? WHERE id='Wreview'", (now,), event="seed")
        lines = fleet.stuck_lines(conn, "Demo")
        assert "готовых 1: можно выдать 0" in lines[0]
        assert "перекрёстное ревью: не этому агенту 1" in lines[0]
    finally:
        conn.close()


def test_grant_records_owner_decision_thaws_named_task_and_reaches_briefing(tmp_path: Path) -> None:
    import sys
    sys.path.insert(0, str(REPO / "src"))
    from dev_mcp.fleet.owner_grants import grants_for

    conn = make_conn()
    conn.execute("UPDATE tasks SET state='frozen'")
    out = fleet._cmd_grant(conn, "P", "перенос полигона на второй ПК разрешаю", ["fix:TZ-1"])
    assert "разморожено: fix:TZ-1" in out
    assert conn.execute("SELECT state FROM tasks").fetchone()[0] == "ready"
    fleet._cmd_grant(conn, "P", "технические решения делегирую оркестратору", [])

    class Tx:
        def query(self, sql, params=()):
            return conn.execute(sql, params).fetchall()

    lines = grants_for(Tx(), "P", "review:fix:TZ-1:2")
    assert any("второй ПК" in line for line in lines)
    assert any("весь проект" in line for line in lines)
    assert grants_for(Tx(), "P", "TZ-9") and all("второй ПК" not in l or "разморозка" in l
                                                for l in grants_for(Tx(), "P", "TZ-9"))
    with pytest.raises(ValueError):
        fleet._cmd_grant(conn, "P", "", [])
