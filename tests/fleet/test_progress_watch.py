from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_mcp.fleet import progress_watch, watchdog
from dev_mcp.fleet.store import Store


def test_task_taken_repeatedly_without_accept_is_a_loop(tmp_path: Path) -> None:
    store = Store.open(str(tmp_path / "f.sqlite"))
    now = datetime.now(timezone.utc)
    with store.tx() as tx:
        for i in range(5):
            tx.record_event(kind="take", project="P", tz="TZ-1", payload={})
    with store.read() as tx:
        kinds = [f.kind for f in progress_watch.findings(tx, now + timedelta(seconds=1), watchdog._issue)]
    assert "loop_task" in kinds


def test_accepted_task_is_not_a_loop(tmp_path: Path) -> None:
    store = Store.open(str(tmp_path / "f.sqlite"))
    now = datetime.now(timezone.utc)
    with store.tx() as tx:
        for i in range(5):
            tx.record_event(kind="take", project="P", tz="TZ-1", payload={})
        tx.record_event(kind="accept", project="P", tz="TZ-1", payload={})
    with store.read() as tx:
        kinds = [f.kind for f in progress_watch.findings(tx, now + timedelta(seconds=1), watchdog._issue)]
    assert "loop_task" not in kinds
