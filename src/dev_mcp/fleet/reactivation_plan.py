"""Read-only thaw planning followed by small, optimistic, witnessed writes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


def _values(rows: Any) -> tuple:
    return tuple(tuple(row) for row in rows)


@dataclass
class Change:
    sql: str
    params: tuple
    event: dict
    reads: list[tuple[str, tuple, tuple]]


class ThawPlanner:
    def __init__(self, reader: Any):
        self.reader = reader
        self.reads: list[tuple[str, tuple, tuple]] = []
        self.changes: list[Change] = []
        self.cache: dict[tuple, Any] = {}

    def query(self, sql: str, params: Any = ()) -> Any:
        key = (sql, tuple(params))
        if key not in self.cache:
            self.cache[key] = self.reader.query(sql, params)
        rows = self.cache[key]
        self.reads.append((sql, tuple(params), _values(rows)))
        return rows

    def one(self, sql: str, params: Any = ()) -> Any:
        rows = self.query(sql, params)
        if len(rows) > 1:
            raise RuntimeError('expected at most one thaw witness')
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Any = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row is not None else None

    def project_tasks(self, project: str) -> Any:
        key=('project-tasks',project)
        if key not in self.cache:
            self.cache[key]=self.reader.query('SELECT tz,state FROM tasks WHERE project=?',(project,))
        # This index is for canonical spelling only. Decisions witness the
        # particular dependency rows, not every unrelated task in the project.
        return self.cache[key]

    def begin_subject(self, project: str, tz: str) -> None:
        self.reads = []
        self.one('SELECT * FROM tasks WHERE project=? AND tz=?', (project, tz))
        self.one("SELECT MAX(id) FROM events WHERE project=? AND tz=? AND kind='accept'", (project, tz))

    def change(self, sql: str, params: Any = (), *, event: dict) -> None:
        self.changes.append(Change(sql, tuple(params), event, list(self.reads)))

    def apply(self, store: Any, validate: Callable[[Any, dict], bool]) -> set[tuple[str, str]]:
        applied = set()
        for change in self.changes:
            with store.tx() as tx:
                # Only potential transitions re-read their actual witnesses.
                # Unchanged frozen tasks never acquire the SQLite writer.
                if any(_values(tx.query(sql, params)) != expected
                       for sql, params, expected in change.reads):
                    continue
                if not validate(tx, change.event):
                    continue
                if any(_values(tx.query(sql, params)) != expected
                       for sql, params, expected in change.reads):
                    continue
                cursor = tx.change(change.sql, change.params, event=change.event)
                if cursor is None or cursor.rowcount:
                    applied.add((str(change.event['project']), str(change.event['tz'])))
        return applied
