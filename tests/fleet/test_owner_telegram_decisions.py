"""No48 B4 (09.11.2026): owner-decision markers reach Telegram once; a change re-sends."""
from __future__ import annotations

import json

from dev_mcp.fleet import owner_telegram
from dev_mcp.fleet.store import Store


def _item(ask="Нужно ли X?", cost="стоит O-a", **kw):
    base = {"id": "local-library/O-x", "line": "local-library", "since": "2026-11-06",
            "age_days": 3, "ask": ask, "cost": cost, "source": "C:/x/OWNER.md:3"}
    base.update(kw)
    return base


def test_marker_sent_once_with_provider_ids_and_resent_on_change(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    sent: list[str] = []
    send = lambda text: sent.append(text) or [101]  # noqa: E731
    items = [_item()]
    assert owner_telegram.tick(store, send=send, decisions=lambda: items) == 1
    assert owner_telegram.tick(store, send=send, decisions=lambda: items) == 0  # repeat tick
    assert "Нужно ли X?" in sent[0] and "Цена ожидания: стоит O-a" in sent[0]
    assert "Claude Code" not in sent[0]
    with store.read() as tx:
        payload = json.loads(tx.one("SELECT payload FROM events WHERE kind='owner_tg_sent'")["payload"])
    assert payload["message_ids"] == [101] and payload["kind"] == "owner_decision"
    items[0] = _item(cost="стоит O-a и O-b")  # changed question/cost -> one new message
    assert owner_telegram.tick(store, send=send, decisions=lambda: items) == 1
    assert owner_telegram.tick(store, send=send, decisions=lambda: items) == 0


def test_obstacle_cards_and_standing_decisions_are_not_duplicated(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    sent: list[str] = []
    items = [_item(line="fleet-obstacles", id="fleet-p/ob-1"),
             _item(id="nova/O-old", already_decided={"id": "retire-rudiments"})]
    assert owner_telegram.tick(store, send=lambda t: sent.append(t) or True,
                               decisions=lambda: items) == 0
    assert sent == []


def test_failed_send_records_nothing(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    assert owner_telegram.tick(store, send=lambda t: False, decisions=lambda: [_item()]) == 0
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner_tg_sent'") == 0
