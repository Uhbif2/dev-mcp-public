"""Transactional, restart-safe deduplication of complete source snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Literal

from .model import IntentEnvelope, PollResult, SourceAdapter, authority_json


@dataclass(frozen=True)
class IntentChange:
    kind: Literal["created", "updated", "cancelled"]
    envelope: IntentEnvelope
    previous_revision: str | None = None
    event_id: int | None = None  # durable delivery ID; consumers must dedup by this ID

    @property
    def key(self) -> tuple[str, str]:
        return self.envelope.key


@dataclass(frozen=True)
class ReconcileResult:
    source_id: str
    applied: bool
    changes: tuple[IntentChange, ...] = ()
    errors: tuple[str, ...] = ()


class IntentRegistry:
    """One SQLite connection per controller; no outcome spawning inside this module.

    Only changed revisions produce signals. A failed/disabled/partial scan leaves
    the last complete snapshot intact, including cancellation state.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection
        with self.db:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS intent_source_revisions (
                    source_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    raw_intent TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('active', 'cancelled')),
                    PRIMARY KEY (source_id, external_id)
                )
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS intent_source_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('created', 'updated', 'cancelled')),
                    source_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    previous_revision TEXT,
                    raw_intent TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0
                )
            """)

    def pending_changes(self, limit: int = 100) -> tuple[IntentChange, ...]:
        """Replay durable signals after a consumer crash; never auto-acknowledge."""
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self.db.execute("""
            SELECT event_id, kind, source_id, external_id, revision,
                   previous_revision, raw_intent, authority, timestamp
              FROM intent_source_events WHERE acknowledged=0
             ORDER BY event_id LIMIT ?
        """, (limit,)).fetchall()
        return tuple(IntentChange(
            row[1], IntentEnvelope(row[2], row[3], row[4], row[6],
                                   json.loads(row[7]), row[8]), row[5], row[0],
        ) for row in rows)

    def ack_change(self, event_id: int) -> bool:
        """ACK within the caller's transaction; caller must commit it.

        Not opening/closing a context here is intentional: callers that use
        the same SQLite DB for outcomes can make effect + ACK atomic.
        """
        if not isinstance(event_id, int) or event_id < 1:
            raise ValueError("event_id must be positive")
        cursor = self.db.execute("""
            UPDATE intent_source_events SET acknowledged=1
             WHERE event_id=? AND acknowledged=0
        """, (event_id,))
        return cursor.rowcount == 1

    def _emit(self, kind: Literal["created", "updated", "cancelled"],
              envelope: IntentEnvelope, previous_revision: str | None) -> IntentChange:
        cursor = self.db.execute("""
            INSERT INTO intent_source_events
                (kind, source_id, external_id, revision, previous_revision,
                 raw_intent, authority, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (kind, envelope.source_id, envelope.external_id, envelope.revision,
              previous_revision, envelope.raw_intent,
              authority_json(envelope.authority), envelope.timestamp))
        return IntentChange(kind, envelope, previous_revision, cursor.lastrowid)

    def poll(self, adapter: SourceAdapter) -> ReconcileResult:
        return self.reconcile(adapter.poll())

    def get(self, source_id: str, external_id: str) -> tuple[IntentEnvelope, str] | None:
        row = self.db.execute("""
            SELECT revision, raw_intent, authority, timestamp, state
              FROM intent_source_revisions WHERE source_id=? AND external_id=?
        """, (source_id, external_id)).fetchone()
        if row is None:
            return None
        return (IntentEnvelope(source_id, external_id, row[0], row[1],
                               json.loads(row[2]), row[3]), row[4])

    def reconcile(self, snapshot: PollResult) -> ReconcileResult:
        if not snapshot.complete or snapshot.errors:
            return ReconcileResult(snapshot.source_id, False, errors=snapshot.errors or ("incomplete_scan",))
        incoming: dict[str, IntentEnvelope] = {}
        for envelope in snapshot.envelopes:
            if envelope.source_id != snapshot.source_id:
                return ReconcileResult(snapshot.source_id, False, errors=("source_mismatch",))
            if envelope.external_id in incoming:
                return ReconcileResult(snapshot.source_id, False, errors=("duplicate_external_id",))
            incoming[envelope.external_id] = envelope
        changes: list[IntentChange] = []
        with self.db:
            known_rows = self.db.execute("""
                SELECT external_id, revision, raw_intent, authority, timestamp, state
                  FROM intent_source_revisions WHERE source_id=?
            """, (snapshot.source_id,)).fetchall()
            known = {row[0]: row for row in known_rows}
            for external_id, envelope in incoming.items():
                previous = known.get(external_id)
                if previous is not None and previous[1] == envelope.revision and previous[5] == "active":
                    continue
                kind = "created" if previous is None or previous[5] == "cancelled" else "updated"
                changes.append(self._emit(kind, envelope, previous[1] if previous else None))
                self.db.execute("""
                    INSERT INTO intent_source_revisions
                        (source_id, external_id, revision, raw_intent, authority, timestamp, state)
                    VALUES (?, ?, ?, ?, ?, ?, 'active')
                    ON CONFLICT(source_id, external_id) DO UPDATE SET
                        revision=excluded.revision, raw_intent=excluded.raw_intent,
                        authority=excluded.authority, timestamp=excluded.timestamp, state='active'
                """, (envelope.source_id, envelope.external_id, envelope.revision,
                      envelope.raw_intent, authority_json(envelope.authority), envelope.timestamp))
            for external_id, row in known.items():
                if external_id in incoming or row[5] != "active":
                    continue
                old = IntentEnvelope(snapshot.source_id, external_id, row[1], row[2],
                                     json.loads(row[3]), row[4])
                changes.append(self._emit("cancelled", old, row[1]))
                self.db.execute("""
                    UPDATE intent_source_revisions SET state='cancelled'
                    WHERE source_id=? AND external_id=?
                """, (snapshot.source_id, external_id))
        return ReconcileResult(snapshot.source_id, True, tuple(changes))
