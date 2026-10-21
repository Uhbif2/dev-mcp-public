from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.model import paths_overlap  # noqa: E402
from dev_mcp.fleet.store import FleetUnavailable, SCHEMA_VERSION, Store  # noqa: E402


EXPECTED_TABLES = {
    "schema_version",
    "projects",
    "tasks",
    "workers",
    "claims",
    "claim_paths",
    "claim_resources",
    "messages",
    "events",
}


def _event(kind: str, **extra):
    return {"kind": kind, **extra}


def _seed_project_worker_task(store: Store, *, worker_ids=("W1", "W2")) -> None:
    now = "2026-09-18T00:00:00+00:00"
    with store.tx() as tx:
        statements = [
            (
                "INSERT INTO projects(name, root, priority, active) VALUES (?, ?, ?, ?)",
                ("dev-mcp", r"C:\repo", 1, 1),
            ),
            (
                """
                INSERT INTO tasks(
                    project, tz, title, state, priority, kind, spec_path, spec_sha,
                    needs, owns, resources, acceptance, author_worker, body,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "dev-mcp",
                    "TZ-RACE",
                    "race",
                    "ready",
                    1,
                    "work",
                    "",
                    "",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    None,
                    "",
                    now,
                    now,
                ),
            ),
        ]
        for worker_id in worker_ids:
            statements.append(
                (
                    """
                    INSERT INTO workers(
                        id, label, kind, first_seen, last_seen, revoked_at, revoke_reason
                    ) VALUES (?, ?, ?, ?, ?, NULL, '')
                    """,
                    (worker_id, worker_id, "chatgpt", now, now),
                )
            )
        tx.changes(statements, event=_event("seed", project="dev-mcp", tz="TZ-RACE"))


def test_open_creates_contract_schema_and_pragmas(tmp_path: Path) -> None:
    db = tmp_path / "fleet.sqlite"
    store = Store.open(str(db))
    assert store.healthy()

    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert EXPECTED_TABLES <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        # foreign_keys is connection-local; Store enables it on every Store connection.
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()

    with store.tx() as tx:
        assert tx.scalar("PRAGMA foreign_keys") == 1
        assert tx.scalar("PRAGMA busy_timeout") == 5000
    store.close()


def test_memory_store_keeps_schema_alive() -> None:
    store = Store.open(":memory:")
    assert store.healthy()
    with store.tx() as tx:
        assert tx.scalar("SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
    store.close()


def test_change_and_event_rollback_together(tmp_path: Path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))

    with pytest.raises(RuntimeError, match="boom"):
        with store.tx() as tx:
            tx.change(
                "INSERT INTO projects(name, root) VALUES (?, ?)",
                ("dev-mcp", r"C:\repo"),
                event=_event("write", project="dev-mcp"),
            )
            raise RuntimeError("boom")

    with store.tx() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM projects") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events") == 0
    store.close()


def test_record_event_is_rejected_outside_transaction(tmp_path: Path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with pytest.raises(RuntimeError, match="only valid inside"):
        store.record_event(kind="write")
    store.close()


def test_touch_worker_updates_last_seen_without_opening_explicit_tx(tmp_path: Path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _seed_project_worker_task(store, worker_ids=("W1",))

    with store.tx() as tx:
        before = tx.scalar("SELECT last_seen FROM workers WHERE id = 'W1'")

    store.touch_worker("W1")

    with store.tx() as tx:
        after = tx.scalar("SELECT last_seen FROM workers WHERE id = 'W1'")

    assert after > before
    store.close()


def _try_claim(
    store: Store,
    worker_id: str,
    path: str,
    barrier: threading.Barrier,
    results: dict[str, bool],
    errors: list[BaseException],
) -> None:
    try:
        barrier.wait(timeout=5)
        with store.tx() as tx:
            active = tx.query(
                """
                SELECT cp.path_norm
                FROM claim_paths cp
                JOIN claims c ON c.id = cp.claim_id
                WHERE c.state = 'active'
                """
            )
            if any(paths_overlap(path, row["path_norm"]) for row in active):
                results[worker_id] = False
                return

            cursor = tx.change(
                """
                INSERT INTO claims(project, tz, worker_id, state, started_at)
                VALUES (?, ?, ?, 'active', ?)
                """,
                ("dev-mcp", "TZ-RACE", worker_id, "2026-09-18T00:00:00+00:00"),
                event=_event(
                    "take",
                    worker_id=worker_id,
                    project="dev-mcp",
                    tz="TZ-RACE",
                ),
            )
            claim_id = cursor.lastrowid
            tx.change(
                "INSERT INTO claim_paths(claim_id, path_norm) VALUES (?, ?)",
                (claim_id, path),
                event=_event(
                    "write",
                    worker_id=worker_id,
                    project="dev-mcp",
                    tz="TZ-RACE",
                    path_norm=path,
                ),
            )
            results[worker_id] = True
    except BaseException as exc:  # surfaced by the owning test
        errors.append(exc)


def test_parallel_transactions_are_serialized_before_sqlite_busy_timeout(tmp_path: Path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    barrier = threading.Barrier(6)
    done: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            # Make SQLite itself impatient. With no process-local writer gate, five threads
            # race BEGIN IMMEDIATE while the winner holds it long enough to trigger "locked".
            conn = store._connection()
            conn.execute("PRAGMA busy_timeout = 50")
            barrier.wait(timeout=3)
            with store.tx() as tx:
                tx.scalar("SELECT COUNT(*) FROM schema_version")
                import time
                time.sleep(0.08)
                done.append(index)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(done) == list(range(6))
    store.close()


def test_parallel_overlapping_claims_have_exactly_one_winner(tmp_path: Path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _seed_project_worker_task(store)

    for _ in range(50):
        with store.tx() as tx:
            tx.changes(
                [
                    ("DELETE FROM claim_paths", ()),
                    ("DELETE FROM claims", ()),
                ],
                event=_event("reset", project="dev-mcp", tz="TZ-RACE"),
            )

        barrier = threading.Barrier(2)
        results: dict[str, bool] = {}
        errors: list[BaseException] = []
        threads = [
            threading.Thread(
                target=_try_claim,
                args=(store, "W1", "c:/repo/ops/probe", barrier, results, errors),
            ),
            threading.Thread(
                target=_try_claim,
                args=(store, "W2", "c:/repo/ops/probe/runner.py", barrier, results, errors),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert sorted(results.values()) == [False, True]

        with store.tx() as tx:
            assert tx.scalar("SELECT COUNT(*) FROM claims WHERE state = 'active'") == 1
            assert tx.scalar("SELECT COUNT(*) FROM claim_paths") == 1

    store.close()


def test_declared_version_with_broken_schema_degrades(tmp_path: Path) -> None:
    db = tmp_path / "broken-schema.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, "2026-09-18T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    store = Store.open(str(db))
    assert store.healthy() is False
    with pytest.raises(FleetUnavailable):
        with store.tx():
            pass


def test_open_corrupt_database_degrades_and_mutations_fail(tmp_path: Path) -> None:
    db = tmp_path / "broken.sqlite"
    db.write_bytes(b"not a sqlite database")

    store = Store.open(str(db))

    assert store.healthy() is False
    with pytest.raises(FleetUnavailable):
        with store.tx():
            pass
    with pytest.raises(FleetUnavailable):
        store.touch_worker("W1")
    with pytest.raises(FleetUnavailable):
        store.backup(str(tmp_path / "backup.sqlite"))


def test_backup_is_readable_and_contains_committed_data(tmp_path: Path) -> None:
    db = tmp_path / "fleet.sqlite"
    backup = tmp_path / "fleet-backup.sqlite"
    store = Store.open(str(db))

    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name, root) VALUES (?, ?)",
            ("dev-mcp", r"C:\repo"),
            event=_event("write", project="dev-mcp"),
        )

    store.backup(str(backup))

    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT name FROM projects").fetchall() == [("dev-mcp",)]
        assert conn.execute("SELECT kind FROM events").fetchall() == [("write",)]
    finally:
        conn.close()
    store.close()
