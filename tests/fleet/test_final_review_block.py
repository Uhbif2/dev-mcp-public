"""Final-review infrastructure dependency: failure is not a review verdict."""
from __future__ import annotations

import json

import pytest

from dev_mcp.fleet import deploy_events, queue, review
from test_queue import StubStore, add_project, add_task, add_worker, now


GATE = "py -3 tools/gate_head.py"
DEPENDENCY = "request:abcdef12"


@pytest.fixture
def lane(tmp_path, monkeypatch):
    store = StubStore.open(tmp_path / "final_review.sqlite")
    monkeypatch.setattr(deploy_events, "tick", lambda _: {})
    add_project(store, "P")
    for worker in ("W-author", "W-review", "W-next", "W-infra-author", "W-infra-review"):
        add_worker(store, worker)
    add_task(store, "TZ-source", state="submitted", kind="work", author_worker="W-author")
    add_task(store, DEPENDENCY, kind="request", author_worker="W-infra-author")
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET acceptance=? WHERE project='P' AND tz='TZ-source'",
                   (json.dumps([GATE]),))
    final = review.open_review(store, "P", "TZ-source", final_note="environment unavailable")
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='taken' WHERE project='P' AND tz=?", (final,))
        tx.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P',?,'W-review','active',?)", (final, now(-1)))
    return store, final


def run_event(store, final, worker="W-review", rc=2, argv=None):
    with store.tx() as tx:
        tx.record_event(
            kind="run", project="P", tz=final, worker_id=worker,
            tool="run", rc=rc, payload={"argv": argv or ["py", "-3", "tools/gate_head.py"]})


def task_state(store, tz):
    with store.read() as tx:
        return str(tx.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (tz,))["state"])


def test_final_review_fails_then_freezes_until_independent_request_accept_and_reaccepts(lane):
    store, final = lane
    for _ in range(3):
        run_event(store, final)
    queue.block(store, "W-review", f"blocked: infrastructure run adapter. dependency {DEPENDENCY}")
    assert task_state(store, final) == "frozen"
    assert task_state(store, "TZ-source") == "submitted"
    with store.read() as tx:
        row = tx.one("SELECT needs,acceptance FROM tasks WHERE project='P' AND tz=?", (final,))
        assert json.loads(row["needs"]) == [DEPENDENCY]
        assert json.loads(row["acceptance"]) == [GATE]
        event = tx.one("SELECT payload FROM events WHERE project='P' AND tz=? "
                       "AND kind='block' ORDER BY id DESC LIMIT 1", (final,))
        details = json.loads(event["payload"])
        assert details["reactivate_policy"] == {
            "kind": "accepted_work", "target": f"P/{DEPENDENCY}",
            "authority": "fleet_task_events",
        }
        assert len(details["failed_acceptance_run_ids"]) == 3
    assert queue.thaw_frozen(store, after_min=0) == []
    assert task_state(store, final) == "frozen"
    # No attempt on the red acceptance becomes an accept/reject verdict.
    with pytest.raises(ValueError, match="no active review"):
        review.decide(store, "W-review", "accept", "not green")

    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='accepted' WHERE project='P' AND tz=?",
                   (DEPENDENCY,))
        tx.record_event(kind="accept", project="P", tz=DEPENDENCY,
                        worker_id="W-infra-review")
    assert queue.thaw_frozen(store, after_min=999) == [
        f"P/{final}: verified independent evidence"]
    assert task_state(store, final) == "ready"
    assert task_state(store, "TZ-source") == "submitted"
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='taken' WHERE project='P' AND tz=?", (final,))
        tx.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P',?,'W-next','active',?)", (final, now(-1)))
    with pytest.raises(ValueError, match="successful run"):
        review.decide(store, "W-next", "accept", "still unproven")
    run_event(store, final, worker="W-next", rc=0)
    review.decide(store, "W-next", "accept", "acceptance now green")
    assert task_state(store, "TZ-source") == "accepted"
    assert task_state(store, final) == "accepted"
    assert queue.thaw_frozen(store, after_min=0) == []


@pytest.mark.parametrize("failure", [
    "too_few", "wrong_command", "previous_green", "wrong_dependency",
    "missing_dependency", "nonfinal", "accepted_dependency",
])
def test_final_review_freeze_rejects_unverified_or_nonfinal_conditions(lane, failure):
    store, final = lane
    if failure == "nonfinal":
        with store.tx() as tx:
            tx.execute("UPDATE tasks SET body=REPLACE(body, ?, '') "
                       "WHERE project='P' AND tz=?", (review.FINAL_MARK, final))
    if failure == "accepted_dependency":
        with store.tx() as tx:
            tx.execute("UPDATE tasks SET state='accepted' WHERE project='P' AND tz=?",
                       (DEPENDENCY,))
    for _ in range(2 if failure == "too_few" else 3):
        run_event(store, final, argv=["py", "-3", "other.py"] if failure == "wrong_command"
                  else None)
    if failure == "previous_green":
        run_event(store, final, rc=0)
    ref = ("request:feed0123" if failure == "missing_dependency" else
           "TZ-ghost" if failure == "wrong_dependency" else DEPENDENCY)
    with pytest.raises(ValueError):
        queue.block(store, "W-review", f"blocked: infrastructure. dependency {ref}")
    assert task_state(store, final) == "taken"
    assert task_state(store, "TZ-source") == "submitted"


def test_final_review_cannot_thaw_with_self_authored_dependency_accept(lane):
    store, final = lane
    for _ in range(3):
        run_event(store, final)
    queue.block(store, "W-review", f"blocked: infrastructure. dependency {DEPENDENCY}")
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='accepted' WHERE project='P' AND tz=?",
                   (DEPENDENCY,))
        tx.record_event(kind="accept", project="P", tz=DEPENDENCY,
                        worker_id="W-infra-author")
    assert queue.thaw_frozen(store, after_min=0) == []
    assert task_state(store, final) == "frozen"
