"""Regression: fleet result writes must be bound to the exact claim window and owns."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from dev_mcp.fleet import report

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fleet_result_writes_cli", REPO / "tools" / "fleet.py")
assert SPEC and SPEC.loader
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)

T0 = "2026-11-16T11:00:00+00:00"
T1 = "2026-11-16T11:10:00+00:00"
T2 = "2026-11-16T11:20:00+00:00"
T3 = "2026-11-16T11:30:00+00:00"
T4 = "2026-11-16T11:40:00+00:00"


def registry() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    fleet._create_schema(db)
    db.execute("INSERT INTO projects(name,root) VALUES('P','c:/project')")
    db.execute("INSERT INTO workers(id,first_seen,last_seen) VALUES('W1',?,?)", (T0, T4))
    for tz in ("TZ-1", "TZ-2"):
        db.execute(
            "INSERT INTO tasks(project,tz,title,state,created_at,updated_at) VALUES('P',?,?,'ready',?,?)",
            (tz, tz, T0, T4),
        )
    return db


def claim(db: sqlite3.Connection, tz: str, start: str, end: str, owns: list[str]) -> int:
    cur = db.execute(
        "INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
        "VALUES('P',?,'W1','released',?,?)", (tz, start, end),
    )
    cid = int(cur.lastrowid)
    db.executemany(
        "INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
        [(cid, path) for path in owns],
    )
    return cid


def write(db: sqlite3.Connection, tz: str, ts: str, path: str, cid: int | None = None,
          *, project: str | None = "P") -> None:
    payload = {} if cid is None else {"claim_id": cid}
    db.execute(
        "INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,payload) "
        "VALUES(?,'write','W1',?,?,'edit_file',?,?)",
        (ts, project, tz, path, json.dumps(payload)),
    )


def test_claimed_report_excludes_other_take_and_events_after_release() -> None:
    with registry() as db:
        a = claim(db, "TZ-1", T0, T1, ["c:/project/a.py"])
        b = claim(db, "TZ-2", T2, T3, ["c:/project/b.py"])
        write(db, "TZ-1", "2026-11-16T11:05:00+00:00", "c:/project/a.py", a)
        write(db, "TZ-2", "2026-11-16T11:25:00+00:00", "c:/project/b.py", b)
        # Same agent's following task: late write is mislabeled as TZ-1.
        write(db, "TZ-1", "2026-11-16T11:27:00+00:00", "c:/project/leaked.py")
        write(db, "TZ-2", "2026-11-16T11:26:00+00:00", "c:/project/outside.py", b)
        write(db, "TZ-2", "2026-11-16T11:27:00+00:00", "c:/project/suspect.py", a)

        lines = report.claimed_vs_done(db).splitlines()
        first = next(row for row in lines if row.startswith("TZ-1 "))
        second = next(row for row in lines if row.startswith("TZ-2 "))
        assert "писал: a.py" in first
        assert "leaked.py" not in first
        assert "b.py" not in first
        assert "писал: b.py" in second
        assert "outsi" not in second.split("писал:", 1)[1].split("вне зоны:", 1)[0]
        assert "вне owns: outside.py" in second

        # Next take of TZ-1, same worker, has no writes: old claim must not leak.
        claim(db, "TZ-1", T3, T4, ["c:/project/new.py"])
        lines = report.claimed_vs_done(db).splitlines()
        first = next(row for row in lines if row.startswith("TZ-1 "))
        assert "писал: —" in first
        assert "a.py" not in first


def test_result_separates_verified_files_from_foreign_and_unbound() -> None:
    with registry() as db:
        a = claim(db, "TZ-1", T0, T1, ["c:/project/a.py", "c:/project/screens/**"])
        b = claim(db, "TZ-2", T2, T3, ["c:/project/b.py"])
        write(db, "TZ-1", "2026-11-16T11:05:00+00:00", "c:/project/a.py", a)
        write(db, "TZ-1", "2026-11-16T11:06:00+00:00", "c:/project/screens/img.png", a)
        write(db, "TZ-1", "2026-11-16T11:07:00+00:00", "c:/project/foreign.py", a)
        write(db, "TZ-1", "2026-11-16T11:27:00+00:00", "c:/project/late.py")
        write(db, "TZ-1", "2026-11-16T11:08:00+00:00", "c:/project/bad-id.py", b)
        write(db, "TZ-2", "2026-11-16T11:25:00+00:00", "c:/project/b.py", b)

        result = fleet.task_result(db, "P/TZ-1")
        verified = result.split("файлы записаны (2):", 1)[1].split("вне owns", 1)[0]
        assert "a.py" in verified and "screens/img.png" in verified
        assert "foreign.py" not in verified
        assert "late.py" not in verified
        assert "b.py" not in result
        assert "вне owns (1):\n  c:/project/foreign.py" in result
        assert "без подтверждённого claim/owns (2):" in result
        assert "late.py" in result and "bad-id.py" in result


def test_missing_owns_is_explicitly_unverified() -> None:
    with registry() as db:
        cid = claim(db, "TZ-1", T0, T1, [])
        write(db, "TZ-1", "2026-11-16T11:05:00+00:00", "c:/project/legacy.py", cid)
        row = report.claimed_vs_done(db)
        assert "писал: legacy.py [owns не подтверждены]" in row
        result = fleet.task_result(db, "P/TZ-1")
        assert "файлы записаны (" not in result
        assert "без подтверждённого claim/owns (1):" in result
