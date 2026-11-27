"""W9-6: slow preparation must not hold Store's global writer gate."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

import pytest

from dev_mcp.fleet import judgment, lessons, owner_grants, review
from dev_mcp.fleet.store import Store


@pytest.fixture
def store(tmp_path: Path):
    db = Store.open(str(tmp_path / "registry.sqlite"))
    root = tmp_path / "P"
    (root / "ToDo" / "decisions").mkdir(parents=True)
    with db.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)",
            (str(root),), event={"kind": "seed"},
        )
    yield db
    db.close()


def add(store: Store, tz: str, *, kind: str = "work", state: str = "frozen") -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('P',?,?,?,1,?,'','','[]','[]','[]','[]','',?,?)",
            (tz, tz, state, kind, stamp, stamp),
            event={"kind": "seed"},
        )


def measured_transactions(store: Store, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    durations: list[float] = []
    original = store.tx

    @contextmanager
    def timed():
        start = time.monotonic()
        try:
            with original() as tx:
                yield tx
        finally:
            durations.append(time.monotonic() - start)

    monkeypatch.setattr(store, "tx", timed)
    return durations


def test_slow_owner_file_read_outside_judgment_tx(store: Store, monkeypatch) -> None:
    add(store, "TZ-90")
    times = measured_transactions(store, monkeypatch)

    def slow_files(_root):
        assert store._gate.depth == 0
        time.sleep(1.05)
        return []

    monkeypatch.setattr(owner_grants, "file_decisions", slow_files)
    item = {"project": "P", "tz": "TZ-90", "reason": "owner gate",
            "gate": True, "needed_for": 3, "age_min": 30 * 60, "title": "gate"}
    assert judgment.open_triages(
        store, lambda: {"clusters": [], "items": [item]},
    ) == ["P/triage:1"]
    assert times and max(times) < 1.0


def test_slow_park_parse_outside_judgment_tx(store: Store, monkeypatch) -> None:
    add(store, "TZ-1")
    start = datetime.now(timezone.utc) - timedelta(hours=74)
    with store.tx() as tx:
        tx.record_event(kind="proposal", project="P", tz="TZ-1",
                        payload={"action": "park", "reversible": True},
                        ts=start.isoformat())
    times = measured_transactions(store, monkeypatch)
    original = judgment._parse

    def slow_parse(value):
        assert store._gate.depth == 0
        time.sleep(1.05)
        return original(value)

    monkeypatch.setattr(judgment, "_parse", slow_parse)
    assert judgment.revisit_parked(store, now=datetime.now(timezone.utc)) == ["P/TZ-1"]
    assert times and max(times) < 1.0


def test_slow_review_parse_outside_judgment_tx(store: Store, monkeypatch) -> None:
    add(store, "TZ-1", state="accepted")
    add(store, "review:TZ-1", kind="review")
    times = measured_transactions(store, monkeypatch)

    def slow_review(*_args):
        assert store._gate.depth == 0
        time.sleep(1.05)
        return "P", "TZ-1"

    monkeypatch.setattr(review, "_review_target", slow_review)
    assert judgment.retire_obsolete_reviews(store) == ["P/review:TZ-1"]
    assert times and max(times) < 1.0


def _seed_pool(store: Store) -> None:
    add(store, "TZ-1")
    with store.tx() as tx:
        for _ in range(lessons.POOL_N):
            tx.record_event(kind="reject", project="P", tz="TZ-1",
                            payload={"note": "review rejection " + "x" * 100})
            event_id = int(tx.scalar("SELECT MAX(id) FROM events"))
            tx.record_event(kind="lesson_scan", project="P", tz="TZ-1",
                            payload={"ev": event_id, "g": 0.95, "lesson": None, "p": None})


def test_slow_lessons_mkdir_outside_tx(store: Store, monkeypatch) -> None:
    _seed_pool(store)
    times = measured_transactions(store, monkeypatch)
    original = Path.mkdir

    def slow_mkdir(self, *args, **kwargs):
        if self.name == "lessons":
            assert store._gate.depth == 0
            time.sleep(1.05)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", slow_mkdir)
    assert lessons.open_mining(store) == ["P/triage:1"]
    assert times and max(times) < 1.0


def test_lessons_rechecks_version_after_filesystem_step(store: Store, monkeypatch) -> None:
    _seed_pool(store)
    original = Path.mkdir
    injected = False

    def concurrent_write(self, *args, **kwargs):
        nonlocal injected
        if self.name == "lessons" and not injected:
            injected = True
            with store.tx() as tx:
                tx.record_event(kind="test_concurrent", project="P", payload={})
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", concurrent_write)
    assert lessons.open_mining(store) == []
    assert lessons.open_mining(store) == ["P/triage:1"]
