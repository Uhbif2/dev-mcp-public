"""Latency and query-count regression for the fleet_list queue hot path.

Direct invocation with --live makes a read-only SQLite backup to a temporary
directory and prints comparative timings; it never modifies the live registry.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import queue, tools
from dev_mcp.fleet.claim_review_bite import _project_python_argv
from dev_mcp.fleet.store import Store


def test_sealed_py_launcher_uses_project_python_without_launcher_selector():
    python = Path("project-venv-python.exe")
    command = ("py", "-3", "-m", "pytest", "-q", "tests/fleet/test_fleet_list_latency.py")
    assert _project_python_argv(python, command) == [
        str(python), "-m", "pytest", "-q", "tests/fleet/test_fleet_list_latency.py"
    ]
    assert _project_python_argv(python, ("python", "-m", "pytest")) == [
        str(python), "-m", "pytest"
    ]
    assert _project_python_argv(python, ("py.exe", "-3", "-V")) == [
        str(python), "-V"
    ]


def _seed(path: Path, *, projects: int = 12, ready: int = 120, events: int = 4000) -> Store:
    store = Store.open(str(path))
    assert store.healthy(), store.open_error
    now = "2026-10-14T00:00:00+00:00"
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,?,1)",
            ((f"Project-{p:02}", f"/repo/p{p}", p % 4) for p in range(projects)),
        )
        records = []
        for n in range(ready + 320):
            p = n % projects
            project = f"Project-{p:02}"
            records.append((
                project, f"T-{n:04}", "ready" if n < ready else "accepted",
                n % 5, "work", json.dumps([f"/repo/p{p}/f{n}.py"]),
                now, now,
            ))
        db.executemany(
            "INSERT INTO tasks(project,tz,state,priority,kind,owns,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            records,
        )
        db.executemany(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            ((now, "noop", f"Project-{n % projects:02}", f"T-{n % max(ready, 1):04}", "{}")
             for n in range(events)),
        )
    return store


def _legacy_project_peek(store: Store):
    """The pre-TZ-332 implementation, kept for reproducible measurements."""
    with store.read() as tx:
        names = [
            str(r["name"]) for r in tx.query(
                "SELECT name FROM projects WHERE active=1 ORDER BY priority ASC,name ASC"
            )
        ]
        return [
            (name, queue._blind_preview_task(rows[0]) if rows else None, len(rows))
            for name in names
            for rows in [[row for row, _, _ in queue._available(tx, "", name)]]
        ]


def test_peek_projects_single_global_pass_and_matches_prior_semantics(tmp_path):
    store = _seed(tmp_path / "large.sqlite")
    expected = _legacy_project_peek(store)
    with patch.object(queue, "_available", wraps=queue._available) as available:
        started = time.perf_counter()
        actual = queue.peek_projects(store)
        duration = time.perf_counter() - started
    assert available.call_count == 1, "one eligibility computation for ALL projects"
    assert [(p, t.tz if t else None, n) for p, t, n in actual] == [
        (p, t.tz if t else None, n) for p, t, n in expected
    ]
    assert len(actual) == 12
    assert sum(n for _, _, n in actual) == 120
    assert duration < 1, f"peek_projects took {duration:.3f}s"


def test_project_preview_keeps_reservations_isolated_between_projects(tmp_path):
    store = _seed(tmp_path / "overlap.sqlite", projects=2, ready=2, events=100)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tasks SET kind='fix',owns=? WHERE project=? AND tz=?",
                   (json.dumps(["/shared"]), "Project-00", "T-0000"))
        db.execute("UPDATE tasks SET owns=? WHERE project=? AND tz=?",
                   (json.dumps(["/shared/free.py"]), "Project-01", "T-0001"))
        db.execute("INSERT INTO workers(id,first_seen,last_seen) VALUES('W','now','now')")
        db.execute("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                   "VALUES('Project-00','T-0002','W','active','now')")
        db.execute("INSERT INTO claim_paths(claim_id,path_norm) VALUES(last_insert_rowid(),?)",
                   ("/shared/occupied.py",))
    expected = _legacy_project_peek(store)
    actual = queue.peek_projects(store)
    assert [(p, n) for p, _, n in actual] == [(p, n) for p, _, n in expected]
    assert dict((p, n) for p, _, n in actual)["Project-01"] == 1


def test_blocker_read_does_not_wait_for_maintenance_write_lock(tmp_path):
    store = _seed(tmp_path / "locked.sqlite", ready=0, events=100)
    held, release = threading.Event(), threading.Event()

    def writer():
        with store.tx():
            held.set()
            release.wait(3)

    worker = threading.Thread(target=writer)
    worker.start()
    assert held.wait(2)
    try:
        started = time.perf_counter()
        lines = tools._project_blocker_lines(tools.FleetContext(store))
        elapsed = time.perf_counter() - started
        assert len(lines) == 12
        assert elapsed < 1, f"fleet_list blocker read waited {elapsed:.3f}s for writer"
    finally:
        release.set()
        worker.join(timeout=3)
        assert not worker.is_alive()


def _live_benchmark(path: Path) -> None:
    if not path.is_file():
        print(f"LIVE_UNAVAILABLE: {path}")
        return
    with tempfile.TemporaryDirectory(prefix="tz332-") as td:
        dst = Path(td) / "fleet.sqlite"
        uri = path.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=1)) as source:
            with contextlib.closing(sqlite3.connect(dst)) as target:
                source.backup(target)
        store = Store.open(str(dst))
        assert store.healthy(), store.open_error
        with store.read() as tx:
            sizes = {
                table: tx.scalar(f"SELECT COUNT(*) FROM {table}")
                for table in ("projects", "tasks", "events", "claims")
            }
            plan = tx.query(
                "EXPLAIN QUERY PLAN SELECT t.*,p.priority AS project_priority,"
                "p.root AS project_root FROM tasks t JOIN projects p ON p.name=t.project "
                "WHERE t.state='ready' AND p.active=1 AND NOT "
                "(t.kind='triage' AND t.tz LIKE 'watch:%' AND EXISTS "
                "(SELECT 1 FROM events e WHERE e.project=t.project AND e.tz=t.tz "
                "AND e.kind='accept'))"
            )
        print("COPY", str(dst), "COUNTS", sizes)
        print("EXPLAIN", "; ".join(str(tuple(r)) for r in plan))
        for label, fn in (
            ("legacy_project_per_name", lambda: _legacy_project_peek(store)),
            ("peek_projects", lambda: queue.peek_projects(store)),
            ("peek_next", lambda: queue.peek_next(store)),
        ):
            samples = []
            for _ in range(2):
                t = time.perf_counter()
                result = fn()
                samples.append(round(1000 * (time.perf_counter() - t), 2))
            print(label, "ms", samples, "rows", len(result) if isinstance(result, list) else result[1])
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live:
        _live_benchmark(Path("C:/hub/dev-mcp/data/fleet/fleet.sqlite"))
