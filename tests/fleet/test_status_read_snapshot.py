from __future__ import annotations

import contextlib
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from dev_mcp.fleet import queue, tools
from dev_mcp.fleet.store import Store


def seed(tmp_path):
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        for i in range(12):
            project = f'P{i:02d}'
            tx.change('INSERT INTO projects(name,root,priority,active) VALUES(?,?,?,1)',
                      (project, str(tmp_path / project), i), event='seed')
            tx.change('INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES(?,?,?,?,?)',
                      (project, 'TZ-1', 'ready' if i % 2 else 'frozen', now, now), event='seed')
    return store


def test_project_forecast_builds_dependency_snapshot_once(tmp_path):
    store = seed(tmp_path)
    queries = []
    store._connection().set_trace_callback(queries.append)
    result = queue.peek_projects(store)
    scans = [q for q in queries if q == 'SELECT project,tz,state,needs,resources FROM tasks']
    assert len(scans) == 1, scans
    assert [p for p, _, _ in result] == [f'P{i:02d}' for i in range(12)]
    assert [n for _, _, n in result] == [i % 2 for i in range(12)]


def test_blocker_explanation_never_acquires_write_transaction(tmp_path, monkeypatch):
    store = seed(tmp_path)
    ctx = tools.FleetContext(store)

    @contextlib.contextmanager
    def forbidden_write():
        raise AssertionError('status must not take a writer lock')
        yield

    monkeypatch.setattr(store, 'tx', forbidden_write)
    lines = tools._project_blocker_lines(ctx)
    assert any('P00' in line and 'frozen' in line for line in lines)


def test_blocker_explanation_finishes_while_another_writer_is_busy(tmp_path):
    store = seed(tmp_path)
    writer_ready = threading.Event()
    release = threading.Event()
    read_finished = threading.Event()
    results = []

    def writer():
        with store.tx():
            writer_ready.set()
            release.wait(3)

    def reader():
        try:
            results.extend(tools._project_blocker_lines(tools.FleetContext(store)))
        finally:
            read_finished.set()

    worker = threading.Thread(target=writer)
    worker.start()
    assert writer_ready.wait(2)
    client = threading.Thread(target=reader)
    client.start()
    try:
        assert read_finished.wait(1), 'read-only status queued behind writer'
        assert results
    finally:
        release.set()
        worker.join(4)
        client.join(4)
