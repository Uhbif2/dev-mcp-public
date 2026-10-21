"""TZ-335: effectful fleet actions must remain pinned across automatic handoff."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from dev_mcp.fleet.store import Store
from dev_mcp.fleet.tools import FleetContext, register


class _MCP:
    def tool(self, **_kwargs):
        return lambda func: func


def _setup(tmp_path, *, review: bool = True):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES (?,?,?,1)",
                  ("demo", str(tmp_path), 1), event={"kind": "seed"})
        for worker in ("W1", "W2"):
            tx.change(
                "INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES (?,?,?,?,?)",
                (worker, worker, "chatgpt", now, now), event={"kind": "seed"},
            )
        names = ("source-A", "source-B", "review:source-A", "review:source-B") if review else ("A", "B")
        for name in names:
            source = name.removeprefix("review:")
            kind = "review" if name.startswith("review:") else "work"
            state = ("submitted" if kind == "work" else
                     "taken" if name.endswith("source-A") else "ready") if review else (
                     "taken" if name == "A" else "ready")
            body = ("[fleet-review] " + json.dumps({"project": "demo", "tz": source})
                    if kind == "review" else "test task")
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("demo", name, name, state, 1 if name.endswith("B") else 8, kind,
                 "", "", "[]", "[]", "[]", "[]", "W2" if kind == "work" else None,
                 body, now, now), event={"kind": "seed"},
            )
        held_tz = "review:source-A" if review else "A"
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES (?,?,?,?,?)",
            ("demo", held_tz, "W1", "active", now), event={"kind": "seed"},
        )
        claim = int(tx.scalar("SELECT MAX(id) FROM claims"))
    api = register(_MCP(), FleetContext(store))
    return store, api, claim


def _current(store):
    with store.read() as tx:
        claim = tx.one(
            "SELECT id,tz FROM claims WHERE worker_id='W1' AND state='active' "
            "ORDER BY id DESC LIMIT 1"
        )
        return (int(claim["id"]), str(claim["tz"])) if claim else None


def test_review_replay_after_handoff_cannot_reject_next_review(tmp_path):
    store, api, old_id = _setup(tmp_path)
    first = api["fleet_review"]("W1", "reject", "unrelated", claim_id=old_id)
    assert "Вердикт reject сохранён" in first
    new = _current(store)
    assert new is not None and new[0] != old_id
    assert new[1] == "review:source-B"
    assert api["fleet_review"]("W1", "reject", "unrelated", claim_id=old_id) == first
    refused = api["fleet_review"]("W1", "reject", "unrelated")
    assert "похоже на повтор действия по прошлой задаче" in refused
    assert _current(store) == new
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='source-B'")["state"] == "submitted"
        assert int(tx.scalar("SELECT COUNT(*) FROM events WHERE kind='reject'")) == 1


def test_mismatched_claim_id_rejects_all_effectors_without_mutating(tmp_path):
    store, api, held = _setup(tmp_path, review=False)
    actions = (
        ("fleet_submit", ("W1", "ready")),
        ("fleet_block", ("W1", "blocked: outside dependency")),
        ("fleet_finish", ("W1", "handoff: done")),
    )
    for name, args in actions:
        assert "FLEET_STALE_CLAIM" in api[name](*args, claim_id=held + 100)
        assert _current(store) == (held, "A")
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='A'")["state"] == "taken"


def test_block_replay_after_handoff_is_idempotent(tmp_path):
    store, api, old_id = _setup(tmp_path, review=False)
    first = api["fleet_block"]("W1", "blocked: missing fixture", claim_id=old_id)
    assert "задача frozen" in first
    new = _current(store)
    assert new is not None and new[0] != old_id and new[1] == "B"
    assert api["fleet_block"]("W1", "blocked: missing fixture", claim_id=old_id) == first
    assert _current(store) == new


def test_submit_replay_after_handoff_does_not_submit_new_task(tmp_path):
    store, api, old_id = _setup(tmp_path, review=False)
    result = api["fleet_submit"]("W1", "ready", claim_id=old_id)
    assert "Работа сдана на приёмку" in result
    new = _current(store)
    assert new is not None and new[0] != old_id and new[1] == "B"
    assert api["fleet_submit"]("W1", "ready", claim_id=old_id) == result
    assert _current(store) == new
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='B'")["state"] == "taken"
        assert int(tx.scalar("SELECT COUNT(*) FROM events WHERE kind='submit'")) == 1


def test_finish_replay_after_next_take_does_not_release_new_task(tmp_path):
    store, api, old_id = _setup(tmp_path, review=False)
    result = api["fleet_finish"]("W1", "handoff: done", claim_id=old_id)
    assert "Claim и ресурсы освобождены" in result
    asyncio.run(api["fleet_take_async"]("W1"))
    new = _current(store)
    assert new is not None and new[0] != old_id and new[1] == "B"
    assert api["fleet_finish"]("W1", "handoff: done", claim_id=old_id) == result
    assert _current(store) == new


def test_review_different_note_for_old_claim_fails_closed(tmp_path):
    store, api, old_id = _setup(tmp_path)
    assert "Вердикт reject" in api["fleet_review"]("W1", "reject", "original", claim_id=old_id)
    new = _current(store)
    assert "FLEET_STALE_CLAIM" in api["fleet_review"](
        "W1", "reject", "different", claim_id=old_id
    )
    assert _current(store) == new


def test_revision_binding_fails_closed_before_review_side_effects(tmp_path):
    store, api, held = _setup(tmp_path)
    denied = api["fleet_review"]("W1", "accept", "tested",
                                  claim_id=held, revision="a" * 40)
    assert "FLEET_STALE_CLAIM" in denied
    assert _current(store) == (held, "review:source-A")


def test_take_and_handoff_expose_new_claim_id(tmp_path):
    store, api, old_id = _setup(tmp_path, review=False)
    finished = api["fleet_finish"]("W1", "handoff: unchanged", claim_id=old_id)
    assert "Claim и ресурсы освобождены" in finished
    taken = asyncio.run(api["fleet_take_async"]("W1"))
    assert "claim_id:" in taken


def test_lost_response_after_handoff_keeps_atomic_receipt(tmp_path, monkeypatch):
    from dev_mcp.fleet import tools
    store, api, old_id = _setup(tmp_path)
    def lost_response(*args, **kwargs):
        raise RuntimeError("transport dropped after handoff")
    monkeypatch.setattr(tools, '_addressed_after', lost_response)
    api['fleet_review']('W1', 'reject', 'old defect', claim_id=old_id)
    new = _current(store)
    assert new is not None and new[1] == 'review:source-B'
    replay = api['fleet_review']('W1', 'reject', 'old defect', claim_id=old_id)
    assert 'Действие уже сохранено' in replay
    legacy = api['fleet_review']('W1', 'reject', 'old defect')
    assert 'FLEET_STALE_CLAIM' in legacy
    assert _current(store) == new
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='reject'") == 1


def test_old_schema_can_address_next_review_in_note_without_extra_take(tmp_path):
    store, api, old_id = _setup(tmp_path)
    api['fleet_review']('W1', 'reject', 'A defect', claim_id=old_id)
    new_id, tz = _current(store)
    assert 'FLEET_STALE_CLAIM' in api['fleet_review']('W1', 'reject', 'different old defect')
    result = api['fleet_review']('W1', 'reject', f'claim_id: {new_id}\nB defect')
    assert 'Вердикт reject сохранён' in result
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='reject'") == 2
