from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class FleetUnavailable(RuntimeError):
    """The fleet registry cannot safely serve a mutating operation."""


_SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE projects (
        name        TEXT PRIMARY KEY,
        root        TEXT NOT NULL,
        priority    INTEGER NOT NULL DEFAULT 5,
        active      INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE tasks (
        project     TEXT NOT NULL REFERENCES projects(name),
        tz          TEXT NOT NULL,
        title       TEXT NOT NULL DEFAULT '',
        state       TEXT NOT NULL,
        priority    INTEGER NOT NULL DEFAULT 5,
        kind        TEXT NOT NULL DEFAULT 'work',
        spec_path   TEXT NOT NULL DEFAULT '',
        spec_sha    TEXT NOT NULL DEFAULT '',
        needs       TEXT NOT NULL DEFAULT '[]',
        owns        TEXT NOT NULL DEFAULT '[]',
        resources   TEXT NOT NULL DEFAULT '[]',
        acceptance  TEXT NOT NULL DEFAULT '[]',
        author_worker TEXT DEFAULT NULL,
        body        TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (project, tz)
    )
    """,
    """
    CREATE TABLE workers (
        id            TEXT PRIMARY KEY,
        label         TEXT NOT NULL DEFAULT '',
        kind          TEXT NOT NULL DEFAULT 'chatgpt',
        first_seen    TEXT NOT NULL,
        last_seen     TEXT NOT NULL,
        revoked_at    TEXT DEFAULT NULL,
        revoke_reason TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE claims (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project     TEXT NOT NULL,
        tz          TEXT NOT NULL,
        worker_id   TEXT NOT NULL REFERENCES workers(id),
        state       TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        released_at TEXT DEFAULT NULL,
        FOREIGN KEY (project, tz) REFERENCES tasks(project, tz)
    )
    """,
    """
    CREATE TABLE claim_paths (
        claim_id    INTEGER NOT NULL REFERENCES claims(id),
        path_norm   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE claim_resources (
        claim_id    INTEGER NOT NULL REFERENCES claims(id),
        resource    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE messages (
        id          TEXT PRIMARY KEY,
        dedup_key   TEXT NOT NULL DEFAULT '',
        to_worker   TEXT DEFAULT NULL,
        to_task     TEXT DEFAULT NULL,
        broadcast   INTEGER NOT NULL DEFAULT 0,
        from_kind   TEXT NOT NULL,
        from_id     TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL,
        body        TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        delivered_at TEXT DEFAULT NULL,
        acked_at    TEXT DEFAULT NULL
    )
    """,
    """
    CREATE TABLE events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        kind        TEXT NOT NULL,
        worker_id   TEXT DEFAULT NULL,
        project     TEXT DEFAULT NULL,
        tz          TEXT DEFAULT NULL,
        tool        TEXT NOT NULL DEFAULT '',
        path_norm   TEXT NOT NULL DEFAULT '',
        rc          INTEGER DEFAULT NULL,
        payload     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX events_worker_ts ON events(worker_id, ts)",
    "CREATE INDEX events_path ON events(path_norm, ts)",
)

_MIGRATIONS: dict[int, tuple[str, ...]] = {1: _SCHEMA_V1}
_PERF_INDEXES = (
    # The task table contains large specs. Queue forecasts read dependency
    # columns for every task; a covering index avoids paging all spec bodies
    # through a memory-constrained host on every extension tick.
    "CREATE INDEX IF NOT EXISTS tasks_dependency_snapshot ON tasks(project,tz,state,needs,resources)",
    "CREATE INDEX IF NOT EXISTS tasks_judgment_snapshot ON tasks(project,tz,title,state,kind,spec_path,needs)",
    "CREATE INDEX IF NOT EXISTS tasks_ownership_snapshot ON tasks(project,tz,state,owns)",
    "CREATE INDEX IF NOT EXISTS tasks_state_kind_project ON tasks(state,kind,project)",
    "CREATE INDEX IF NOT EXISTS tasks_kind_state_project ON tasks(kind,state,project)",
    "CREATE INDEX IF NOT EXISTS claims_worker_state ON claims(worker_id, state)",
    "CREATE INDEX IF NOT EXISTS events_kind_id ON events(kind, id)",
    "CREATE INDEX IF NOT EXISTS events_project_kind_id ON events(project, kind, id)",
    "CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts)",
    "CREATE INDEX IF NOT EXISTS events_task_kind ON events(project, tz, kind)",
    "CREATE INDEX IF NOT EXISTS events_tool ON events(tool)",
)

_EXPECTED_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "schema_version": ("version", "applied_at"),
    "projects": ("name", "root", "priority", "active"),
    "tasks": (
        "project",
        "tz",
        "title",
        "state",
        "priority",
        "kind",
        "spec_path",
        "spec_sha",
        "needs",
        "owns",
        "resources",
        "acceptance",
        "author_worker",
        "body",
        "created_at",
        "updated_at",
    ),
    "workers": (
        "id",
        "label",
        "kind",
        "first_seen",
        "last_seen",
        "revoked_at",
        "revoke_reason",
    ),
    "claims": (
        "id",
        "project",
        "tz",
        "worker_id",
        "state",
        "started_at",
        "released_at",
    ),
    "claim_paths": ("claim_id", "path_norm"),
    "claim_resources": ("claim_id", "resource"),
    "messages": (
        "id",
        "dedup_key",
        "to_worker",
        "to_task",
        "broadcast",
        "from_kind",
        "from_id",
        "kind",
        "body",
        "created_at",
        "delivered_at",
        "acked_at",
    ),
    "events": (
        "id",
        "ts",
        "kind",
        "worker_id",
        "project",
        "tz",
        "tool",
        "path_norm",
        "rc",
        "payload",
    ),
}
_EXPECTED_INDEXES = {"events_worker_ts", "events_path"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version"
    ).fetchone()
    version = int(row[0]) if row is not None else 0
    if version != SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"database schema version {version} does not match supported {SCHEMA_VERSION}"
        )

    for table, expected_columns in _EXPECTED_TABLE_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        actual_columns = tuple(str(row[1]) for row in rows)
        if actual_columns != expected_columns:
            raise sqlite3.DatabaseError(
                f"invalid schema for {table}: expected {expected_columns}, got {actual_columns}"
            )

    indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    missing_indexes = _EXPECTED_INDEXES - indexes
    if missing_indexes:
        raise sqlite3.DatabaseError(
            f"missing required indexes: {', '.join(sorted(missing_indexes))}"
        )


# Замок записи реестра (07.10.2026). Затор 04:55–05:02 UTC: семь минут ни один вызов не кончался,
# потом все разом — потоки ждали _write_lock без срока. Теперь ожидание ограничено, а не дождавшийся
# пишет в slow-tx.log рядом с базой ТЕКУЩИЙ стек держателя; транзакции дольше SLOW_TX_S — тоже.
LOCK_WAIT_S = float(os.environ.get("DEV_MCP_FLEET_LOCK_WAIT_S", "30") or 30)
SLOW_TX_S = 3.0


class _WriteGate:
    """Один замок записи на ФАЙЛ базы, а не на объект Store: сервер, интегратор и поток суждений
    открывают свои Store в одном процессе и с отдельными замками сталкивались уже в SQLite —
    `database is locked` через 5 с (07.10.2026)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.holder: tuple[int, str, float, str] | None = None
        self.depth = 0
        self.dumped_at = 0.0


_GATES: dict[str, _WriteGate] = {}
_GATES_LOCK = threading.Lock()


def _shared_gate(path: str) -> _WriteGate:
    if not path or path == ":memory:":
        return _WriteGate()
    key = os.path.normcase(os.path.abspath(path))
    with _GATES_LOCK:
        return _GATES.setdefault(key, _WriteGate())


def _caller() -> str:
    frames = [f for f in traceback.extract_stack(limit=14)[:-3] if "dev_mcp" in f.filename]
    return " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames[-5:]))


class Store:
    def __init__(self, path: str, *, available: bool, open_error: str = "") -> None:
        self.path = path
        self._available = available
        self.open_error = open_error
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        # SQLite/WAL still permits only one writer. MCP handles many tool calls in parallel,
        # and every Store.tx() uses BEGIN IMMEDIATE for atomic queue decisions. Without a
        # process-local gate those threads form a writer thundering herd and some exhaust
        # busy_timeout with "database is locked". Serialize this Store's writers before they
        # reach SQLite; external processes are still covered by PRAGMA busy_timeout.
        self._gate = _shared_gate(path)
        self._write_lock = self._gate.lock

    def _acquire_write(self) -> None:
        gate = self._gate
        if not gate.lock.acquire(timeout=LOCK_WAIT_S):
            holder = gate.holder
            self._slow_log("ЗАТОР", holder, waiter=_caller())
            who = f"{holder[1]}, {time.monotonic() - holder[2]:.0f} с, {holder[3]}" if holder else "?"
            raise FleetUnavailable(
                f"реестр флота занят дольше {LOCK_WAIT_S:.0f} с (держит: {who}) — повтори вызов"
            )
        if gate.depth == 0:
            gate.holder = (threading.get_ident(), threading.current_thread().name, time.monotonic(), _caller())
        gate.depth += 1

    def _release_write(self) -> None:
        gate = self._gate
        gate.depth -= 1
        if gate.depth == 0:
            holder, gate.holder = gate.holder, None
            if holder is not None and time.monotonic() - holder[2] > SLOW_TX_S:
                self._slow_log("долгая транзакция", holder)
        gate.lock.release()

    def _slow_log(self, what: str, holder: tuple[int, str, float, str] | None, waiter: str = "") -> None:
        try:
            if not self.path or self.path == ":memory:":
                return
            now = time.monotonic()
            lines = [f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {what}"]
            if holder is not None:
                lines.append(f"  держит {holder[1]} {now - holder[2]:.1f} с; взял в: {holder[3]}")
                # Стек держателя прямо сейчас — один раз в минуту, иначе каждый ждущий напишет свой.
                if what == "ЗАТОР" and now - self._gate.dumped_at > 60:
                    self._gate.dumped_at = now
                    frame = sys._current_frames().get(holder[0])
                    if frame is not None:
                        lines += ["  " + ln.rstrip() for ln in traceback.format_stack(frame)[-14:]]
            if waiter:
                lines.append(f"  ждал: {waiter}")
            with open(Path(self.path).with_name("slow-tx.log"), "a", encoding="utf-8") as fh:
                fh.write(chr(10).join(lines) + chr(10))
        except Exception:
            pass

    @classmethod
    def open(cls, path: str) -> "Store":
        db_path = os.fspath(path)
        store = cls(db_path, available=False)
        conn: sqlite3.Connection | None = None
        try:
            if db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                db_path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            cls._apply_migrations(conn)
            # Индексы — ускорение, не схема: идемпотентно, без версии (07.10.2026: запросы по виду
            # события и по задаче сканировали 120 тыс. строк под замком записи).
            for statement in _PERF_INDEXES:
                conn.execute(statement)
            _validate_schema(conn)
        except (OSError, sqlite3.Error) as exc:
            store.open_error = f"{type(exc).__name__}: {exc}"
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            return store
        else:
            store._available = True
            if db_path == ":memory:":
                store._local.connection = conn
                with store._connections_lock:
                    store._connections.append(conn)
            else:
                conn.close()
            return store

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_version"
            ).fetchone()
            current = int(row["version"] if isinstance(row, sqlite3.Row) else row[0])
            if current > SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    f"database schema version {current} is newer than supported {SCHEMA_VERSION}"
                )
            for version in range(current + 1, SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise sqlite3.DatabaseError(f"missing migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (version, _utcnow()),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _require_available(self) -> None:
        if not self._available:
            self._retry_open()
        if not self._available:
            detail = f": {self.open_error}" if self.open_error else ""
            raise FleetUnavailable(f"fleet store is unavailable{detail}")

    def _retry_open(self) -> None:
        """A failed open must not latch forever (13.10: a busy DB at server start
        left every MCP tool answering "database is locked" while the controller,
        on its own Store, kept working; the fleet stood for 30 min)."""
        if self.path == ":memory:":
            return
        now = time.monotonic()
        if now - getattr(self, "_last_retry", 0.0) < 5.0:
            return
        self._last_retry = now
        fresh = type(self).open(self.path)
        if fresh._available:
            self.open_error = ""
            self._available = True
        else:
            self.open_error = fresh.open_error

    def _connection(self) -> sqlite3.Connection:
        self._require_available()
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            return conn
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise FleetUnavailable(f"cannot open fleet database: {exc}") from exc
        self._local.connection = conn
        with self._connections_lock:
            self._connections.append(conn)
        return conn

    def tx(self) -> AbstractContextManager["Txn"]:
        return _TxContext(self)

    def read(self) -> AbstractContextManager["Txn"]:
        """Снимок только для чтения — без замка записи (07.10.2026: чтение под BEGIN IMMEDIATE
        держало замок секундами — `checkpoint.due` на каждом ответе, `peek_next`, спрос супервизора —
        и вызовы флота стояли в очереди за ним). В WAL читатели писателю не мешают."""
        return _ReadContext(self)

    def healthy(self) -> bool:
        if not self._available:
            self._retry_open()
        if not self._available:
            return False

        if self.path == ":memory:":
            try:
                _validate_schema(self._connection())
                return True
            except (FleetUnavailable, sqlite3.Error):
                return False

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=0.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.execute("PRAGMA busy_timeout = 0")
            _validate_schema(conn)
            return True
        except (OSError, sqlite3.Error):
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def record_event(self, **kw: Any) -> None:
        self._require_available()
        txn = getattr(self._local, "active_tx", None)
        if txn is None:
            raise RuntimeError("record_event() is only valid inside Store.tx()")
        txn.record_event(**kw)

    def touch_worker(self, worker_id: str) -> None:
        self._require_available()
        try:
            self._acquire_write()
            try:
                self._connection().execute(
                    "UPDATE workers SET last_seen = ? WHERE id = ? AND revoked_at IS NULL",
                    (_utcnow(), worker_id),
                )
            finally:
                self._release_write()
        except sqlite3.Error as exc:
            raise FleetUnavailable(f"cannot update worker heartbeat: {exc}") from exc

    def backup(self, dest: str) -> None:
        self._require_available()
        dest_path = os.fspath(dest)
        if os.path.abspath(dest_path) == os.path.abspath(self.path):
            raise ValueError("backup destination must differ from source")
        if getattr(self._local, "active_tx", None) is not None:
            raise RuntimeError("backup() cannot run inside Store.tx()")

        target: sqlite3.Connection | None = None
        try:
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            source = self._connection()
            target = sqlite3.connect(
                dest_path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            source.backup(target)
        except (OSError, sqlite3.Error) as exc:
            raise FleetUnavailable(f"cannot back up fleet database: {exc}") from exc
        finally:
            if target is not None:
                try:
                    target.close()
                except sqlite3.Error:
                    pass

    def close(self) -> None:
        with self._connections_lock:
            connections = self._connections
            self._connections = []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        if hasattr(self._local, "connection"):
            del self._local.connection
        if hasattr(self._local, "active_tx"):
            del self._local.active_tx


class Txn:
    def __init__(self, store: Store, conn: sqlite3.Connection) -> None:
        self._store = store
        self._conn = conn

    def query(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[sqlite3.Row]:
        try:
            return list(self._conn.execute(sql, tuple(params)).fetchall())
        except sqlite3.Error as exc:
            raise FleetUnavailable(f"fleet query failed: {exc}") from exc

    def one(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError(f"expected one row, got {len(rows)}")
        return rows[0]

    def scalar(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def change(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        event: Mapping[str, Any] | str,
    ) -> sqlite3.Cursor:
        try:
            cursor = self._conn.execute(sql, tuple(params))
            self._record_event(event)
            return cursor
        except sqlite3.Error as exc:
            raise FleetUnavailable(f"fleet change failed: {exc}") from exc

    def changes(
        self,
        statements: Iterable[tuple[str, Sequence[Any]]],
        *,
        event: Mapping[str, Any] | str,
    ) -> list[sqlite3.Cursor]:
        cursors: list[sqlite3.Cursor] = []
        try:
            for sql, params in statements:
                cursors.append(self._conn.execute(sql, tuple(params)))
            self._record_event(event)
            return cursors
        except sqlite3.Error as exc:
            raise FleetUnavailable(f"fleet change failed: {exc}") from exc

    def record_event(self, **kw: Any) -> None:
        self._record_event(kw)

    def _record_event(self, event: Mapping[str, Any] | str) -> None:
        if isinstance(event, str):
            event_data: Mapping[str, Any] = {"kind": event}
        else:
            event_data = event

        kind = str(event_data.get("kind") or "")
        if not kind:
            raise ValueError("event kind is required")

        payload = event_data.get("payload", {})
        if not isinstance(payload, str):
            payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )

        self._conn.execute(
            """
            INSERT INTO events(
                ts, kind, worker_id, project, tz, tool, path_norm, rc, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event_data.get("ts") or _utcnow()),
                kind,
                event_data.get("worker_id"),
                event_data.get("project"),
                event_data.get("tz"),
                str(event_data.get("tool") or ""),
                str(event_data.get("path_norm") or ""),
                event_data.get("rc"),
                payload,
            ),
        )


class _ReadTxn(Txn):
    def change(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("Store.read() только для чтения — пиши в Store.tx()")

    changes = record_event = _record_event = change  # type: ignore[assignment]


class _ReadContext(AbstractContextManager[Txn]):
    def __init__(self, store: Store) -> None:
        self._store = store
        self._own = False

    def __enter__(self) -> Txn:
        self._store._require_available()
        active = getattr(self._store._local, "active_tx", None)
        if active is not None:
            return active  # внутри своей транзакции читаем её же
        conn = self._store._connection()
        try:
            conn.execute("BEGIN")  # отложенная: снимок без замка записи
        except sqlite3.Error as exc:
            raise FleetUnavailable(f"cannot begin fleet read: {exc}") from exc
        self._own = True
        return _ReadTxn(self._store, conn)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._own:
            try:
                self._store._connection().rollback()
            except sqlite3.Error:
                pass
        return False


class _TxContext(AbstractContextManager[Txn]):
    def __init__(self, store: Store) -> None:
        self._store = store
        self._txn: Txn | None = None
        self._write_lock_held = False

    def __enter__(self) -> Txn:
        self._store._require_available()
        if getattr(self._store._local, "active_tx", None) is not None:
            raise RuntimeError("nested Store.tx() is not supported")

        # Queue decisions are read+write atomic units, so BEGIN IMMEDIATE is intentional.
        # Serialize them inside this process first; otherwise concurrent MCP threads all wait
        # independently in SQLite and can time out even though each transaction is short.
        self._store._acquire_write()
        self._write_lock_held = True
        conn = self._store._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self._store._release_write()
            self._write_lock_held = False
            raise FleetUnavailable(f"cannot begin fleet transaction: {exc}") from exc

        self._txn = Txn(self._store, conn)
        self._store._local.active_tx = self._txn
        return self._txn

    def __exit__(self, exc_type, exc, tb) -> bool:
        txn = self._txn
        if txn is None:
            return False

        conn = txn._conn
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        except sqlite3.Error as db_exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise FleetUnavailable(f"cannot finish fleet transaction: {db_exc}") from db_exc
        finally:
            if getattr(self._store._local, "active_tx", None) is txn:
                del self._store._local.active_tx
            self._txn = None
            if self._write_lock_held:
                self._store._release_write()
                self._write_lock_held = False
        return False
