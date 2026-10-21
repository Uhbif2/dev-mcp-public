from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from dev_mcp.fleet.inbox import (
    ack,
    broadcast,
    pending_for,
    render_envelope,
    send,
)
from dev_mcp.fleet.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker(store: Store, worker_id: str, *, revoked: bool = False) -> None:
    now = _now()
    revoked_at = now if revoked else None
    with store.tx() as tx:
        tx.change(
            """
            INSERT INTO workers(
                id, label, kind, first_seen, last_seen, revoked_at, revoke_reason
            ) VALUES (?, ?, 'chatgpt', ?, ?, ?, ?)
            """,
            (
                worker_id,
                worker_id,
                now,
                now,
                revoked_at,
                "owner stopped it" if revoked else "",
            ),
            event={"kind": "join", "worker_id": worker_id},
        )


def _row(store: Store, message_id: str):
    with store.tx() as tx:
        return tx.one("SELECT * FROM messages WHERE id = ?", (message_id,))


def test_message_survives_real_store_reopen_until_ack(tmp_path) -> None:
    db_path = tmp_path / "fleet.sqlite"
    store = Store.open(str(db_path))
    _worker(store, "W1")

    message_id = send(
        store,
        to="W1",
        from_kind="worker",
        from_id="W2",
        kind="contract",
        body="ops/probe/recipes.py опубликован",
        dedup_key="contract-recipes-v1",
    )
    first = pending_for(store, "W1")
    assert [msg.id for msg in first] == [message_id]
    store.close()

    reopened = Store.open(str(db_path))
    second = pending_for(reopened, "W1")
    assert [msg.id for msg in second] == [message_id]

    assert ack(reopened, "W1", [message_id]) == 1
    assert pending_for(reopened, "W1") == []
    reopened.close()


def test_ack_is_by_concrete_id_not_cursor(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")
    first = send(
        store,
        to="W1",
        from_kind="system",
        from_id="fleet",
        kind="note",
        body="первый факт",
    )
    second = send(
        store,
        to="W1",
        from_kind="system",
        from_id="fleet",
        kind="note",
        body="второй факт",
    )

    assert ack(store, "W1", [second]) == 1
    assert [msg.id for msg in pending_for(store, "W1")] == [first]


def test_pending_budget_items_bytes_and_remainder_counter(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")

    for index in range(5):
        send(
            store,
            to="W1",
            from_kind="system",
            from_id="fleet",
            kind="note",
            body=f"факт {index}",
        )

    pending = pending_for(store, "W1")
    assert len(pending) == 3
    envelope = render_envelope(pending, "W1")
    assert len(envelope.encode("utf-8")) <= 2048
    assert '--- ещё 2 уведомления: fleet_inbox("W1") ---' in envelope

    # Byte budget is applied to the actual UTF-8 envelope, not only to message bodies.
    tiny = pending_for(store, "W1", max_items=3, max_bytes=260)
    tiny_rendered = render_envelope(tiny, "W1")
    assert len(tiny_rendered.encode("utf-8")) <= 260
    assert tiny.remaining_count == 5 - len(tiny)


def test_dedup_key_returns_same_stable_id(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")

    first = send(
        store,
        to="W1",
        from_kind="worker",
        from_id="W2",
        kind="contract",
        body="контракт опубликован",
        dedup_key="same-fact",
    )
    second = send(
        store,
        to="W1",
        from_kind="worker",
        from_id="W2",
        kind="contract",
        body="контракт опубликован ещё раз",
        dedup_key="same-fact",
    )

    assert second == first
    assert [msg.id for msg in pending_for(store, "W1")] == [first]


def test_importance_beats_chronology(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")

    contract_id = send(
        store,
        to="W1",
        from_kind="worker",
        from_id="W2",
        kind="contract",
        body="контракт опубликован",
    )
    decided_id = send(
        store,
        to="W1",
        from_kind="owner",
        from_id="owner",
        kind="request_decided",
        body="заявка одобрена",
    )
    revoke_id = send(
        store,
        to="W1",
        from_kind="owner",
        from_id="owner",
        kind="revoke",
        body="воркер отозван",
    )

    assert [msg.id for msg in pending_for(store, "W1", max_items=3)] == [
        revoke_id,
        decided_id,
        contract_id,
    ]


def test_pending_can_be_rendered_alongside_error_response(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")
    message_id = send(
        store,
        to="W1",
        from_kind="system",
        from_id="fleet",
        kind="note",
        body="причина уже исправлена",
    )

    application_error = {"ok": False, "error": "FLEET_FOREIGN_PATH"}
    envelope = render_envelope(pending_for(store, "W1"), "W1")

    assert application_error["ok"] is False
    assert message_id in envelope
    assert "причина уже исправлена" in envelope


def test_render_always_exposes_sender_and_owner_is_visibly_distinct(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")

    send(
        store,
        to="W1",
        from_kind="worker",
        from_id="W7",
        kind="contract",
        body="контракт опубликован",
    )
    send(
        store,
        to="W1",
        from_kind="owner",
        from_id="",
        kind="request_decided",
        body="граница задания изменена владельцем",
    )

    rendered = render_envelope(pending_for(store, "W1"), "W1")
    assert "от W7" in rendered
    assert "владелец" in rendered


def test_broadcast_reaches_all_active_workers_but_not_revoked(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")
    _worker(store, "W2")
    _worker(store, "W3", revoked=True)

    ids = broadcast(
        store,
        body="контракт fleet обновлён",
        from_id="owner",
        dedup_key="fleet-contract-v2",
    )

    assert len(ids) == 2
    assert len(pending_for(store, "W1")) == 1
    assert len(pending_for(store, "W2")) == 1
    assert pending_for(store, "W3") == []


def test_only_owner_may_broadcast_and_questions_are_not_messages(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")

    with pytest.raises(PermissionError):
        broadcast(store, body="готово", from_kind="worker", from_id="W2")

    with pytest.raises(PermissionError):
        send(
            store,
            to="W1",
            from_kind="worker",
            from_id="W2",
            kind="request_decided",
            body="заявка одобрена",
        )

    with pytest.raises(ValueError, match="questions"):
        send(
            store,
            to="W1",
            from_kind="worker",
            from_id="W2",
            kind="note",
            body="переделать контракт?",
        )


def test_delivered_at_is_attempt_not_ack_and_unacked_message_repeats(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _worker(store, "W1")
    message_id = send(
        store,
        to="W1",
        from_kind="system",
        from_id="fleet",
        kind="note",
        body="готовый факт",
    )

    assert _row(store, message_id)["delivered_at"] is None
    assert [msg.id for msg in pending_for(store, "W1")] == [message_id]
    row = _row(store, message_id)
    assert row["delivered_at"] is not None
    assert row["acked_at"] is None

    # A failed transport does not imply ack: the same message is eligible again.
    assert [msg.id for msg in pending_for(store, "W1")] == [message_id]
