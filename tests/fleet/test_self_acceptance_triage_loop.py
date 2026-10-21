"""A terminal self-acceptance triage must not re-open without new evidence."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet import queue
from dev_mcp.fleet.store import Store


CMD = "py -3 -m pytest -q tests/fleet/test_self_acceptance_triage_loop.py"
SUBJECT = "self-acceptance:TZ-330"


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    db = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "P"
    (root / "ToDo" / "decisions").mkdir(parents=True)
    with db.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("P", str(root)), event={"kind": "setup"})
    return db


def open_triage(store: Store, reason: str = "blocked: until: " + CMD) -> None:
    with store.tx() as tx:
        queue._self_acceptance_triage(tx, "P", "TZ-330", reason)


def triages(store: Store) -> list[dict]:
    with store.read() as tx:
        return [dict(r) for r in tx.query(
            "SELECT tz,state,body FROM tasks WHERE project='P' AND kind='triage' ORDER BY tz")]


@pytest.mark.parametrize("terminal_state", ["rejected", "accepted"])
def test_terminal_triage_is_not_reopened_by_same_or_changed_block_reason(
    store: Store, terminal_state: str,
) -> None:
    open_triage(store)
    first = triages(store)
    assert len(first) == 1
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state=? WHERE project='P' AND tz=?",
                  (terminal_state, first[0]["tz"]), event={"kind": "test_transition"})
    for i in range(3):
        open_triage(store, f"blocked: new reason {i}. until: {CMD}")
    assert triages(store) == [{**first[0], "state": terminal_state}]


def test_pending_triage_is_not_duplicated(store: Store) -> None:
    open_triage(store)
    open_triage(store)
    assert len(triages(store)) == 1


def test_repeated_real_blocks_after_triage_rejection_do_not_loop(store: Store) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,first_seen,last_seen) VALUES('W1','w',?,?)",
                  (now, now), event={"kind": "setup"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('P','TZ-330','task','taken',1,'work','','','[]','[]','[]',?,'',?,?)",
            (json.dumps([CMD]), now, now), event={"kind": "setup"},
        )
    for cycle in range(3):
        with store.tx() as tx:
            tx.change("UPDATE tasks SET state='taken' WHERE project='P' AND tz='TZ-330'",
                      (), event={"kind": "setup"})
            tx.change(
                "INSERT INTO claims(project,tz,worker_id,state,started_at) "
                "VALUES('P','TZ-330','W1','active',?)", (now,), event={"kind": "setup"},
            )
        queue.block(store, "W1", "blocked: until: " + CMD)
        with store.read() as tx:
            assert tx.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-330'")["state"] == "frozen"
        assert len(triages(store)) == 1
        with store.tx() as tx:
            tx.change("UPDATE tasks SET state='rejected' WHERE project='P' AND tz=?",
                      (triages(store)[0]["tz"],), event={"kind": "test_reject"})


def admit(store: Store, digest: str) -> None:
    now = datetime.now(timezone.utc)
    with store.tx() as tx:
        if tx.one("SELECT 1 FROM tasks WHERE project='P' AND tz='producer'") is None:
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES('P','producer','producer','accepted',1,'work','','','[]','[]',"
                "'[]','[]','',?,?)",
                (now.isoformat(), now.isoformat()), event={"kind": "setup"},
            )
        tx.record_event(kind="accept", project="P", tz="producer",
                        payload={"evidence_sha256": digest})
        proof = tx.one("SELECT MAX(id) AS id FROM events WHERE project='P' AND tz='producer' "
                       "AND kind='accept'")
        tx.record_event(
            kind="external_data_ready", project="P", tz="TZ-330",
            payload={"subject": SUBJECT, "change_source": "external_data_ready",
                     "sha256": digest,
                     "window_start_utc": (now - timedelta(minutes=1)).isoformat(),
                     "window_end_utc": now.isoformat(),
                     "admission": {"tz": "producer", "sha256": digest,
                                   "accept_event_id": proof["id"]}},
        )


def test_new_independent_data_reopens_once_even_after_reject(store: Store) -> None:
    open_triage(store)
    original = triages(store)[0]
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='rejected' WHERE project='P' AND tz=?",
                  (original["tz"],), event={"kind": "test_reject"})
    open_triage(store)
    assert len(triages(store)) == 1

    admit(store, "a" * 64)
    open_triage(store)
    assert len(triages(store)) == 2
    assert "self-acceptance:TZ-330" in triages(store)[1]["body"]
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='rejected' WHERE project='P' AND tz=?",
                  (triages(store)[1]["tz"],), event={"kind": "test_reject"})
    open_triage(store)
    assert len(triages(store)) == 2  # the admitted digest has been consumed


def test_unverified_external_data_cannot_reopen(store: Store) -> None:
    open_triage(store)
    item = triages(store)[0]
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='rejected' WHERE project='P' AND tz=?",
                  (item["tz"],), event={"kind": "test_reject"})
        tx.record_event(kind="external_data_ready", project="P", tz="TZ-330",
                        payload={"subject": SUBJECT, "change_source": "external_data_ready",
                                 "sha256": "b" * 64, "admission": {"tz": "producer",
                                                                "sha256": "b" * 64,
                                                                "accept_event_id": 1}})
    open_triage(store)
    assert len(triages(store)) == 1
