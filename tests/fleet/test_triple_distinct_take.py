"""Один агент не берёт два варианта тройной стадии и её свод (14.10.2026)."""
from test_queue import StubStore, add_project, add_task, add_worker
from dev_mcp.fleet.queue import take_next

BASE = "intent-abc-understood"


def _finish(store, worker, tz):
    with store.tx() as db:
        db.execute("UPDATE claims SET state='released' WHERE worker_id=? AND tz=?", (worker, tz))
        db.execute("UPDATE tasks SET state='accepted' WHERE tz=?", (tz,))


def test_worker_never_takes_sibling_candidate_or_consensus(tmp_path):
    store = StubStore.open(tmp_path / "f.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_worker(store, "W2")
    for i in (1, 2, 3):
        add_task(store, f"{BASE}-candidate-{i}", owns=[f"/P/c{i}.md"])
    first = take_next(store, "W1")
    assert first is not None and first.tz.startswith(BASE + "-candidate-")
    _finish(store, "W1", first.tz)
    assert take_next(store, "W1") is None          # siblings are left for others
    other = take_next(store, "W2")
    assert other is not None and other.tz != first.tz
    _finish(store, "W2", other.tz)
    add_task(store, BASE, owns=["/P/consensus.md"])
    assert take_next(store, "W1") is None          # consensus — a fourth agent
