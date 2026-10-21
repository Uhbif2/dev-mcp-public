from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dev_mcp.fleet import inbox, report  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


def iso(delta_min: int = 0) -> str:
    return (NOW + timedelta(minutes=delta_min)).isoformat()


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setattr(report, "_utcnow", lambda: NOW)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE projects(name TEXT PRIMARY KEY,root TEXT NOT NULL,priority INTEGER NOT NULL DEFAULT 5,active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE tasks(project TEXT NOT NULL,tz TEXT NOT NULL,title TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 5,kind TEXT NOT NULL DEFAULT 'work',spec_path TEXT NOT NULL DEFAULT '',
      spec_sha TEXT NOT NULL DEFAULT '',needs TEXT NOT NULL DEFAULT '[]',owns TEXT NOT NULL DEFAULT '[]',
      resources TEXT NOT NULL DEFAULT '[]',acceptance TEXT NOT NULL DEFAULT '[]',author_worker TEXT DEFAULT NULL,
      body TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(project,tz));
    CREATE TABLE workers(id TEXT PRIMARY KEY,label TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'chatgpt',
      first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,revoked_at TEXT DEFAULT NULL,revoke_reason TEXT NOT NULL DEFAULT '');
    CREATE TABLE claims(id INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,tz TEXT NOT NULL,worker_id TEXT NOT NULL,
      state TEXT NOT NULL,started_at TEXT NOT NULL,released_at TEXT DEFAULT NULL);
    CREATE TABLE claim_paths(claim_id INTEGER NOT NULL,path_norm TEXT NOT NULL);
    CREATE TABLE claim_resources(claim_id INTEGER NOT NULL,resource TEXT NOT NULL);
    CREATE TABLE messages(id TEXT PRIMARY KEY,dedup_key TEXT NOT NULL DEFAULT '',to_worker TEXT DEFAULT NULL,to_task TEXT DEFAULT NULL,
      broadcast INTEGER NOT NULL DEFAULT 0,from_kind TEXT NOT NULL,from_id TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL,
      body TEXT NOT NULL,created_at TEXT NOT NULL,delivered_at TEXT DEFAULT NULL,acked_at TEXT DEFAULT NULL);
    CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,kind TEXT NOT NULL,worker_id TEXT DEFAULT NULL,
      project TEXT DEFAULT NULL,tz TEXT DEFAULT NULL,tool TEXT NOT NULL DEFAULT '',path_norm TEXT NOT NULL DEFAULT '',
      rc INTEGER DEFAULT NULL,payload TEXT NOT NULL DEFAULT '{}');
    """)
    yield conn
    conn.close()


def project(db: sqlite3.Connection, root: Path) -> None:
    db.execute("INSERT INTO projects(name,root) VALUES('P',?)", (str(root),))


def task(db: sqlite3.Connection, tz: str, state: str = "ready", **kw: object) -> None:
    db.execute(
        """INSERT INTO tasks(project,tz,title,state,kind,spec_path,created_at,updated_at)
           VALUES('P',?,?,?,?,?,?,?)""",
        (
            tz,
            str(kw.get("title", "")),
            state,
            str(kw.get("kind", "work")),
            str(kw.get("spec_path", "")),
            str(kw.get("created_at", iso(-60))),
            str(kw.get("updated_at", iso(-10))),
        ),
    )


def worker(db: sqlite3.Connection, wid: str, last_seen: str | None = None, revoked: bool = False) -> None:
    db.execute(
        """INSERT INTO workers(id,first_seen,last_seen,revoked_at,revoke_reason)
           VALUES(?,?,?,?,?)""",
        (wid, iso(-120), last_seen or iso(), iso(-1) if revoked else None, "stop" if revoked else ""),
    )


def claim(db: sqlite3.Connection, tz: str, wid: str, *, state: str = "active",
          started: str | None = None, released: str | None = None) -> int:
    cur = db.execute(
        """INSERT INTO claims(project,tz,worker_id,state,started_at,released_at)
           VALUES('P',?,?,?,?,?)""",
        (tz, wid, state, started or iso(-60), released),
    )
    return int(cur.lastrowid)


def event(db: sqlite3.Connection, kind: str, *, tz: str | None = None, wid: str | None = None,
          ts: str | None = None, rc: int | None = None, tool: str = "", path: str = "",
          payload: dict[str, object] | None = None) -> None:
    db.execute(
        """INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            ts or iso(),
            kind,
            wid,
            "P" if tz else None,
            tz,
            tool,
            path,
            rc,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )


def test_claimed_vs_done_uses_run_event_rc_and_result_file(db: sqlite3.Connection, tmp_path: Path) -> None:
    spec = tmp_path / "TZ-1.md"
    spec.write_text("# TZ\n\n## Итог\n\nСделано.\n", encoding="utf-8")
    project(db, tmp_path)
    task(db, "TZ-1", "taken", spec_path=spec.name)
    worker(db, "W1", iso(-6))
    claim(db, "TZ-1", "W1", started=iso(-246))
    event(db, "write", tz="TZ-1", wid="W1", ts=iso(-120), path="ops/a.py")
    event(db, "write", tz="TZ-1", wid="W1", ts=iso(-110), path="ops/b.py")
    event(db, "run", tz="TZ-1", wid="W1", ts=iso(-5), rc=0, tool="pytest",
          payload={"text": "агент написал rc=17"})
    text = report.claimed_vs_done(db)
    assert "TZ-1" in text
    assert "W1" in text
    assert "писал: ops/a.py(+1)" in text
    assert "pytest rc=0" in text
    assert "rc=17" not in text
    assert "Итог: есть" in text
    assert "молчит 6 мин" in text


def test_claimed_vs_done_marks_empty_result(db: sqlite3.Connection, tmp_path: Path) -> None:
    spec = tmp_path / "TZ-2.md"
    spec.write_text("# TZ\n\n## Итог\n\n<!-- заполняет исполнитель -->\n", encoding="utf-8")
    project(db, tmp_path)
    task(db, "TZ-2", "taken", spec_path=spec.name)
    worker(db, "W2", iso(-31))
    claim(db, "TZ-2", "W2")
    text = report.claimed_vs_done(db)
    assert "Итог: пуст" in text
    assert text.endswith("←")


def test_claimed_vs_done_treats_placeholder_result_as_empty(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    spec = tmp_path / "TZ-PLACEHOLDER.md"
    spec.write_text(
        "# TZ\n\n## Итог\n\n(заполняется исполнителем)\n",
        encoding="utf-8",
    )
    project(db, tmp_path)
    task(db, "TZ-PLACEHOLDER", "taken", spec_path=spec.name)
    worker(db, "W3", iso(-5))
    claim(db, "TZ-PLACEHOLDER", "W3")
    text = report.claimed_vs_done(db)
    assert "Итог: пуст" in text


def test_silence_exception_requires_active_claim_and_zero_writes(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    task(db, "TZ-SILENT", "taken")
    task(db, "TZ-WROTE", "taken")
    worker(db, "W1", iso(-31))
    worker(db, "W2", iso(-31))
    claim(db, "TZ-SILENT", "W1")
    claim(db, "TZ-WROTE", "W2")
    event(db, "write", tz="TZ-WROTE", wid="W2", ts=iso(-10), path="x.py")
    items = report.exceptions(db)
    assert any("МОЛЧАНИЕ P/TZ-SILENT W1" in item for item in items)
    assert not any("TZ-WROTE" in item and "МОЛЧАНИЕ" in item for item in items)


def test_abandoned_with_work_uses_same_30_minute_threshold_as_maintenance(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    project(db, tmp_path)
    task(db, "TZ-WORK", "taken")
    worker(db, "W2", iso(-31))
    claim(db, "TZ-WORK", "W2", started=iso(-60))
    event(db, "write", tz="TZ-WORK", wid="W2", ts=iso(-20), path="x.py")

    items = report.exceptions(db)
    assert any("БРОШЕНО P/TZ-WORK W2" in item for item in items)


def test_imported_legacy_claim_is_not_false_silence_exception(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    project(db, tmp_path)
    task(db, "TZ-LEGACY", "taken")
    worker(db, "W-LEGACY", iso(-180))
    claim(db, "TZ-LEGACY", "W-LEGACY", started=iso(-180))
    event(
        db,
        "take",
        tz="TZ-LEGACY",
        wid="W-LEGACY",
        ts=iso(-180),
        payload={"imported": True, "legacy_claim_fingerprint": "abc"},
    )

    items = report.exceptions(db)
    assert not any("TZ-LEGACY" in item and "МОЛЧАНИЕ" in item for item in items)


def test_foreign_path_exception_is_exact_code(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    task(db, "TZ-1")
    event(db, "deny", tz="TZ-1", wid="W1", path="x.py", payload={"code": "FLEET_FOREIGN_PATH"})
    event(db, "deny", tz="TZ-1", wid="W1", path="y.py", payload={"code": "FLEET_OUT_OF_CLAIM"})
    items = report.exceptions(db)
    foreign = [item for item in items if item.startswith("ЧУЖОЙ ПУТЬ")]
    assert len(foreign) == 1
    assert "x.py" in foreign[0]
    assert "y.py" not in foreign[0]
    # 22.09.2026: запись вне своей зоны — тоже нарушение, но своей строкой
    outside = [item for item in items if item.startswith("ВНЕ ЗОНЫ")]
    assert len(outside) == 1 and "y.py" in outside[0]


def test_two_red_runs_only_when_last_two_are_red(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    task(db, "TZ-RED")
    task(db, "TZ-MIXED")
    event(db, "run", tz="TZ-RED", rc=1, tool="pytest", ts=iso(-3))
    event(db, "run", tz="TZ-RED", rc=2, tool="pytest", ts=iso(-2))
    event(db, "run", tz="TZ-MIXED", rc=1, tool="pytest", ts=iso(-3))
    event(db, "run", tz="TZ-MIXED", rc=0, tool="pytest", ts=iso(-2))
    items = report.exceptions(db)
    assert any("ПРИЁМКА КРАСНАЯ ДВАЖДЫ P/TZ-RED" in item for item in items)
    assert not any("TZ-MIXED" in item and "КРАСНАЯ" in item for item in items)


def test_requeue_exception_only_while_requeue_is_latest_task_event(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    task(db, "TZ-BACK")
    task(db, "TZ-TAKEN")
    event(db, "requeue", tz="TZ-BACK", ts=iso(-2))
    event(db, "requeue", tz="TZ-TAKEN", ts=iso(-3))
    event(db, "take", tz="TZ-TAKEN", ts=iso(-1))
    items = report.exceptions(db)
    assert "ВЕРНУЛАСЬ В ОЧЕРЕДЬ P/TZ-BACK" in items
    assert "ВЕРНУЛАСЬ В ОЧЕРЕДЬ P/TZ-TAKEN" not in items


def test_unacked_message_exception_has_threshold_and_ack_neighbor(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    worker(db, "W1", iso(-1))
    task(db, "TZ-HELD", "taken")
    claim(db, "TZ-HELD", "W1")
    db.execute(
        """INSERT INTO messages(id,to_worker,broadcast,from_kind,kind,body,created_at,acked_at)
           VALUES('m-old','W1',0,'system','note','x',?,NULL)""",
        (iso(-31),),
    )
    db.execute(
        """INSERT INTO messages(id,to_worker,broadcast,from_kind,kind,body,created_at,acked_at)
           VALUES('m-acked','W1',0,'system','note','x',?,?)""",
        (iso(-60), iso(-1)),
    )
    db.execute(
        """INSERT INTO messages(id,to_worker,broadcast,from_kind,kind,body,created_at,acked_at)
           VALUES('m-new','W1',0,'system','note','x',?,NULL)""",
        (iso(-29),),
    )
    # Воркер без активной задачи ack не пришлёт — это не исключение для владельца.
    db.execute(
        """INSERT INTO messages(id,to_worker,broadcast,from_kind,kind,body,created_at,acked_at)
           VALUES('m-gone','W-DONE',0,'owner','broadcast','x',?,NULL)""",
        (iso(-90),),
    )
    items = report.exceptions(db)
    assert any("БЕЗ ACK m-old" in item for item in items)
    assert not any("m-acked" in item or "m-new" in item or "m-gone" in item for item in items)


def test_note_to_closed_or_missing_task_is_not_an_unacked_exception(db: sqlite3.Connection, tmp_path: Path) -> None:
    # 19.10.2026: 10 из 10 «БЕЗ ACK» в пульте были заметками принятым/замороженным задачам.
    project(db, tmp_path)
    task(db, "TZ-DONE", "accepted")
    task(db, "TZ-ICE", "frozen")
    task(db, "TZ-WAIT", "ready")
    for mid, target in (("m-done", "P/TZ-DONE"), ("m-ice", "P/TZ-ICE"),
                        ("m-none", "P/TZ-NOPE"), ("m-wait", "P/TZ-WAIT")):
        db.execute(
            """INSERT INTO messages(id,to_task,broadcast,from_kind,kind,body,created_at,acked_at)
               VALUES(?,?,0,'owner','note','x',?,NULL)""",
            (mid, target, iso(-90)),
        )
    items = report.exceptions(db)
    assert any("БЕЗ ACK m-wait" in item for item in items)
    assert not any(m in item for item in items for m in ("m-done", "m-ice", "m-none"))


def test_empty_queue_exception_requires_recent_free_worker(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    worker(db, "W-FREE", iso(-1))
    items = report.exceptions(db)
    assert any(item.startswith("ОЧЕРЕДЬ ПУСТА:") and "W-FREE" in item for item in items)

    task(db, "TZ-READY", "ready")
    items = report.exceptions(db)
    assert not any(item.startswith("ОЧЕРЕДЬ ПУСТА:") for item in items)


def test_empty_queue_does_not_count_stale_or_revoked_worker(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    worker(db, "W-STALE", iso(-31))
    worker(db, "W-REVOKED", iso(-1), revoked=True)
    assert not any(item.startswith("ОЧЕРЕДЬ ПУСТА:") for item in report.exceptions(db))


def test_submitted_wait_exception_threshold(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    task(db, "TZ-OLD", "submitted", updated_at=iso(-40))
    task(db, "TZ-NEW", "submitted", updated_at=iso(-10))
    event(db, "submit", tz="TZ-OLD", ts=iso(-40))
    event(db, "submit", tz="TZ-NEW", ts=iso(-10))
    items = report.exceptions(db)
    assert any("ЖДЁТ ПРИЁМКИ P/TZ-OLD" in item for item in items)
    assert not any("TZ-NEW" in item and "ЖДЁТ ПРИЁМКИ" in item for item in items)


def test_review_wait_metric_counts_only_submitted_tasks_with_live_review(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    task(db, "TZ-WAIT", "submitted", updated_at=iso(-40))
    event(db, "submit", tz="TZ-WAIT", ts=iso(-40))
    task(db, "review:TZ-WAIT", "ready", kind="review", updated_at=iso(-35))

    # После reject root остаётся submitted, пока чинится generated fix. Это НЕ ожидание review.
    task(db, "TZ-FIX", "submitted", updated_at=iso(-200))
    event(db, "submit", tz="TZ-FIX", ts=iso(-200))
    task(db, "review:TZ-FIX", "accepted", kind="review", updated_at=iso(-190))
    task(db, "fix:TZ-FIX", "ready", kind="fix", updated_at=iso(-10))

    data = report.metrics(db, "2026-09-18T00:00:00+00:00")
    review = data["review_wait_age_min"]
    assert review["n"] == 1
    assert review["median"] == pytest.approx(40.0)
    assert review["max"] == pytest.approx(40.0)


def test_metrics_have_denominators_and_expected_rates(db: sqlite3.Connection, tmp_path: Path) -> None:
    project(db, tmp_path)
    task(db, "TZ-A", "accepted", updated_at=iso(-1))
    task(db, "TZ-B", "ready", updated_at=iso(-1))
    task(db, "REQ-1", "taken", kind="request", created_at=iso(-50), updated_at=iso(-20))
    worker(db, "W1")
    worker(db, "W2")
    claim(db, "TZ-A", "W1", state="released", started=iso(-100), released=iso(-40))
    claim(db, "TZ-B", "W2", state="released", started=iso(-80), released=iso(-20))
    claim(db, "REQ-1", "W1", state="active", started=iso(-20))
    event(db, "accept", tz="TZ-A", wid="W1", ts=iso(-1))
    event(db, "requeue", tz="TZ-B", wid=None, ts=iso(-10))
    event(db, "write", tz="TZ-A", wid="W1", ts=iso(-30), path="a.py")
    event(db, "write", tz="TZ-B", wid="W2", ts=iso(-30), path="b.py")
    event(db, "deny", tz="TZ-B", wid="W2", ts=iso(-29), path="c.py",
          payload={"code": "FLEET_FOREIGN_PATH"})

    data = report.metrics(db, "2026-09-18T00:00:00+00:00")
    assert data["requeue_rate"]["text"] == "1/3"
    assert data["foreign_path_rate"]["text"] == "1/3"
    assert data["owner_interventions_per_accepted"]["text"] == "0/1"
    assert data["request_to_execution_min"]["n"] == 1
    assert data["request_to_execution_min"]["median"] == pytest.approx(30.0)
    assert data["task_time_min"]["n"] == 2
    assert data["claim_attempt_time_min"]["n"] == 2
    assert data["work_task_effort_min"]["n"] == 2
    assert data["work_task_effort_min"]["median"] == pytest.approx(60.0)
    for key in ("requeue_rate", "foreign_path_rate", "owner_interventions_per_accepted"):
        assert set(("n", "N", "text")).issubset(data[key])


def test_work_task_effort_sums_requeued_claim_attempts_per_unique_tz(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    project(db, tmp_path)
    task(db, "TZ-X", "accepted", updated_at=iso(-1))
    worker(db, "W1")
    worker(db, "W2")
    claim(db, "TZ-X", "W1", state="requeued", started=iso(-40), released=iso(-30))
    claim(db, "TZ-X", "W2", state="released", started=iso(-20), released=iso(-5))

    data = report.metrics(db, "2026-09-18T00:00:00+00:00")
    assert data["claim_attempt_time_min"]["n"] == 2
    assert data["work_task_effort_min"]["n"] == 1
    assert data["work_task_effort_min"]["median"] == pytest.approx(25.0)


def _load_cli():
    path = Path(__file__).resolve().parents[2] / "tools" / "fleet.py"
    spec = importlib.util.spec_from_file_location("fleet_owner_cli", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_selftest_is_side_effect_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cli = _load_cli()
    sentinel = tmp_path / "must-not-exist.sqlite"
    monkeypatch.setattr(cli, "DEFAULT_DB", sentinel)
    assert cli.main(["--selftest"]) == 0
    assert "selftest: OK" in capsys.readouterr().out
    assert not sentinel.exists()


def test_owner_broadcast_is_independently_acknowledgeable_per_worker(tmp_path: Path) -> None:
    cli = _load_cli()
    path = tmp_path / "fleet.sqlite"
    store = Store.open(str(path))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
            "VALUES ('W1','one','chatgpt',?,?,'')",
            (iso(), iso()),
        )
        conn.execute(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
            "VALUES ('W2','two','chatgpt',?,?,'')",
            (iso(), iso()),
        )
        conn.execute(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
            "VALUES ('W3','done','chatgpt',?,?,'')",
            (iso(), iso()),
        )
        conn.execute("INSERT INTO projects(name,root,priority,active) VALUES ('P',?,1,1)", (str(tmp_path),))
        for wid, tz in (("W1", "TZ-1"), ("W2", "TZ-2")):
            conn.execute(
                "INSERT INTO tasks(project,tz,title,state,created_at,updated_at) VALUES ('P',?,?,'taken',?,?)",
                (tz, tz, iso(), iso()),
            )
            conn.execute(
                "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES ('P',?,?,'active',?)",
                (tz, wid, iso()),
            )
        conn.commit()

    rc, message = cli._mutate(path, lambda conn: cli._cmd_say(conn, "contract changed"))
    assert rc == 0
    assert "2 воркеров" in message
    assert inbox.pending_for(store, "W3") == [], "отработавшему без задачи рассылка не нужна"

    first = inbox.pending_for(store, "W1")
    second = inbox.pending_for(store, "W2")
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].body == "contract changed"
    assert second[0].body == "contract changed"
    assert first[0].id != second[0].id

    assert inbox.ack(store, "W1", [first[0].id]) == 1
    assert inbox.pending_for(store, "W1") == []
    still_for_two = inbox.pending_for(store, "W2")
    assert [msg.id for msg in still_for_two] == [second[0].id]
    assert inbox.ack(store, "W2", [second[0].id]) == 1
    assert inbox.pending_for(store, "W2") == []


def test_owner_recover_preserves_written_work_and_requeues_stale_claim(tmp_path: Path) -> None:
    cli = _load_cli()
    path = tmp_path / "fleet.sqlite"
    Store.open(str(path))
    old = iso(-180)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO projects(name,root,priority,active) VALUES ('P',?,1,1)", (str(tmp_path),))
        conn.execute(
            "INSERT INTO tasks(project,tz,title,state,created_at,updated_at) VALUES ('P','TZ-1','x','taken',?,?)",
            (old, old),
        )
        conn.execute(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) VALUES ('W1','old','chatgpt',?,?, '')",
            (old, old),
        )
        conn.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES ('P','TZ-1','W1','active',?)",
            (old,),
        )
        conn.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload) "
            "VALUES (?,'write','W1','P','TZ-1','edit_file','owned.py',NULL,'{}')",
            (old,),
        )
        conn.commit()

    rc, message = cli._mutate(path, lambda conn: cli._cmd_recover(conn, "P", "TZ-1", "tab abandoned"))
    assert rc == 0
    assert "файлы сохранены" in message
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'").fetchone()[0] == "ready"
        assert conn.execute("SELECT state FROM claims WHERE project='P' AND tz='TZ-1'").fetchone()[0] == "requeued"
        revoked = conn.execute("SELECT revoked_at,revoke_reason FROM workers WHERE id='W1'").fetchone()
        assert revoked[0] and "owner recover P/TZ-1" in revoked[1]
        payload = json.loads(conn.execute(
            "SELECT payload FROM events WHERE project='P' AND tz='TZ-1' AND kind='requeue' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        assert payload["owner_recovery"] is True and payload["preserved_work"] is True


def test_owner_reactivate_generated_fix_from_frozen_or_rejected(tmp_path: Path) -> None:
    cli = _load_cli()
    path = tmp_path / "fleet.sqlite"
    Store.open(str(path))
    stamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO projects(name,root,priority,active) VALUES ('P',?,1,1)", (str(tmp_path),))
        for tz, state in (("fix:TZ-1", "rejected"), ("fix:TZ-2", "frozen")):
            conn.execute(
                "INSERT INTO tasks(project,tz,title,state,kind,created_at,updated_at) VALUES (?,?,?,? ,'fix',?,?)",
                ("P", tz, tz, state, stamp, stamp),
            )
        conn.commit()

    for tz in ("fix:TZ-1", "fix:TZ-2"):
        rc, message = cli._mutate(
            path,
            lambda conn, tz=tz: cli._cmd_reactivate(conn, "P", tz, "dependency landed"),
        )
        assert rc == 0
        assert "→ ready" in message

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM tasks WHERE project='P' AND tz='fix:TZ-1'").fetchone()[0] == "ready"
        assert conn.execute("SELECT state FROM tasks WHERE project='P' AND tz='fix:TZ-2'").fetchone()[0] == "ready"
        rows = conn.execute("SELECT tz,payload FROM events WHERE project='P' AND kind='reactivate' ORDER BY tz").fetchall()
    assert [row[0] for row in rows] == ["fix:TZ-1", "fix:TZ-2"]
    assert all(json.loads(row[1])["reason"] == "dependency landed" for row in rows)


def test_owner_recover_refuses_live_claim(tmp_path: Path) -> None:
    cli = _load_cli()
    path = tmp_path / "fleet.sqlite"
    Store.open(str(path))
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO projects(name,root,priority,active) VALUES ('P',?,1,1)", (str(tmp_path),))
        conn.execute(
            "INSERT INTO tasks(project,tz,title,state,created_at,updated_at) VALUES ('P','TZ-1','x','taken',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) VALUES ('W1','live','chatgpt',?,?, '')",
            (now, now),
        )
        conn.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES ('P','TZ-1','W1','active',?)",
            (now,),
        )
        conn.commit()
    with pytest.raises(ValueError, match="молчит только"):
        cli._mutate(path, lambda conn: cli._cmd_recover(conn, "P", "TZ-1", "must not steal"))


def test_owner_recover_immediately_requeues_claim_of_revoked_worker(tmp_path: Path) -> None:
    cli = _load_cli()
    path = tmp_path / "fleet.sqlite"
    Store.open(str(path))
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO projects(name,root,priority,active) VALUES ('P',?,1,1)", (str(tmp_path),))
        conn.execute(
            "INSERT INTO tasks(project,tz,title,state,created_at,updated_at) VALUES ('P','TZ-1','x','taken',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason) "
            "VALUES ('W1','dup','chatgpt',?,?,?,?)",
            (now, now, now, "merged duplicate browser label"),
        )
        conn.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES ('P','TZ-1','W1','active',?)",
            (now,),
        )
        conn.commit()

    rc, message = cli._mutate(
        path,
        lambda conn: cli._cmd_recover(conn, "P", "TZ-1", "revoked worker orphan claim"),
    )
    assert rc == 0
    assert "→ ready" in message
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'").fetchone()[0] == "ready"
        assert conn.execute("SELECT state FROM claims WHERE project='P' AND tz='TZ-1'").fetchone()[0] == "requeued"


def test_cli_bootstrap_only_selected_tz(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    root = tmp_path / "Project"
    class _TestPolicy:
        roots = [str(root)]
        write_roots = [str(root)]
    monkeypatch.setattr(cli.Policy, "load", lambda *_args, **_kwargs: _TestPolicy())
    tzdir = root / "ToDo" / "tz"
    tzdir.mkdir(parents=True)
    # Bootstrap now trial-runs a new acceptance against an isolated snapshot.
    # Keep the fixture's declared compile target real, not merely syntactic.
    source = root / "src" / "a.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    for tz, filename in (("TZ-OLD", "TZ-OLD-history.md"), ("TZ-A", "TZ-A-active.md")):
        (tzdir / filename).write_text(
            "~~~yaml\n"
            f"tz: {tz}\n"
            "owns: [src/a.py]\n"
            "acceptance: [python -m py_compile src/a.py]\n"
            "~~~\n\n"
            f"# {tz}\n",
            encoding="utf-8",
        )
    # parse_spec expects fenced yaml; replace the neutral delimiter after composing.
    for spec_path in tzdir.glob("TZ-*.md"):
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8").replace("~~~", "`" * 3),
            encoding="utf-8",
        )

    path = tmp_path / "fleet.sqlite"
    rc = cli.main(["--db", str(path), "bootstrap", "P", str(root), "TZ-A"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inserted"] == 1
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT tz,state FROM tasks WHERE project='P' ORDER BY tz"
        ).fetchall()
    assert rows == [("TZ-A", "ready")]


def test_cli_does_not_crash_on_empty_or_broken_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli = _load_cli()
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert cli.main(["--db", str(empty), "report"]) == 0
    assert "пуст или недоступен" in capsys.readouterr().out

    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"this is not sqlite")
    rc = cli.main(["--db", str(broken), "report"])
    captured = capsys.readouterr()
    assert rc in (0, 2)
    assert "Traceback" not in captured.err


def test_metrics_reject_bad_since(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        report.metrics(db, "not-a-date")


