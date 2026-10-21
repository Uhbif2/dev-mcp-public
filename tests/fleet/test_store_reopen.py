"""A store that failed to open (busy DB at server start) recovers on next use."""
import sqlite3
from pathlib import Path

from dev_mcp.fleet.store import Store


def test_failed_open_recovers_when_db_frees(tmp_path: Path) -> None:
    db = tmp_path / "fleet.sqlite"
    Store.open(str(db))                       # create schema
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN EXCLUSIVE")         # hold the lock like a busy startup
    store = Store(str(db), available=False, open_error="OperationalError: database is locked")
    assert not store.healthy()
    holder.execute("ROLLBACK"); holder.close()
    store._last_retry = 0.0
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM schema_version") >= 1
    assert store.open_error == ""
