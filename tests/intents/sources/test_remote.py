from __future__ import annotations

import sqlite3

from dev_mcp.intents.sources import (
    IntentRegistry, MarkedItem, ObsidianIntentAdapter, TickTickIntentAdapter,
)


def header(identifier, tags=("owner-intent",), revision="r1"):
    return MarkedItem(identifier, revision, "2026-10-09T09:00:00Z", tuple(tags))


def test_only_structurally_tagged_notes_get_read():
    read_ids = []
    seen_tags = []
    def listed(tag):
        seen_tags.append(tag)
        return [header("real", ("#owner-intent",)),
                header("fake", ("owner-intent-fake",)),
                header("private", ()), header("lookalike", ("my-owner-intent",))]
    def read(identifier):
        read_ids.append(identifier)
        return "Text preserved\n#owner-intent"
    adapter = ObsidianIntentAdapter("vault", list_marked=listed, read_marked=read)
    result = adapter.poll()
    assert result.complete and seen_tags == ["owner-intent"]
    assert read_ids == ["real"]
    assert [(item.source_id, item.external_id) for item in result.envelopes] == [("obsidian:vault", "real")]


def test_provider_failure_cannot_cancel_previous_remote_state():
    items = [header("n1")]
    def listed(tag):
        if items is None:
            raise ConnectionError("offline")
        return items
    adapter = ObsidianIntentAdapter("vault", list_marked=listed, read_marked=lambda _: "intent")
    with sqlite3.connect(":memory:") as db:
        registry = IntentRegistry(db)
        assert registry.poll(adapter).changes[0].kind == "created"
        assert not registry.poll(adapter).changes
        # Replace the API callback with simulated network failure.
        adapter.list_marked = lambda tag: (_ for _ in ()).throw(ConnectionError("offline"))
        failed = registry.poll(adapter)
        assert not failed.applied and not failed.changes
        assert registry.get("obsidian:vault", "n1")[1] == "active"
        adapter.list_marked = lambda tag: []
        assert registry.poll(adapter).changes[0].kind == "cancelled"


def test_remote_revision_text_and_tag_removal_signal():
    metadata = [header("n1")]
    raw = ["hello\r\n"]
    adapter = ObsidianIntentAdapter("vault", list_marked=lambda _: metadata,
                                    read_marked=lambda _: raw[0])
    with sqlite3.connect(":memory:") as db:
        registry = IntentRegistry(db)
        a = registry.poll(adapter).changes[0]
        assert a.envelope.raw_intent == "hello\r\n"
        raw[0] = "hello (modified)\r\n"
        b = registry.poll(adapter).changes[0]
        assert b.kind == "updated" and b.previous_revision == a.envelope.revision
        metadata[:] = [header("n1", ("#not-an-intent",))]
        c = registry.poll(adapter).changes[0]
        assert c.kind == "cancelled" and c.envelope.raw_intent == raw[0]


def test_ticktick_is_disabled_without_owner_grant_even_when_flag_enabled():
    called = []
    def listed(tag):
        called.append("list")
        return [header("task")]
    def read(identifier):
        called.append("read")
        return "task text"
    for options in ({}, {"enabled": True}, {"owner_granted": True}):
        result = TickTickIntentAdapter("account", list_marked=listed,
                                       read_marked=read, **options).poll()
        assert not result.complete and result.errors == ("owner_access_not_granted",)
        assert not called
    result = TickTickIntentAdapter("account", list_marked=listed, read_marked=read,
                                   enabled=True, owner_granted=True).poll()
    assert result.complete and called == ["list", "read"]
    assert result.envelopes[0].source_id == "ticktick:account"


def test_remote_body_failure_and_fake_metadata_are_fail_closed():
    reads = []
    adapter = ObsidianIntentAdapter(
        "vault", list_marked=lambda _: [header("id", ("not-owner-intent",)), header("good")],
        read_marked=lambda identifier: reads.append(identifier) or "",  # empty body
    )
    result = adapter.poll()
    assert not result.complete and result.errors
    assert reads == ["good"]


def test_duplicate_remote_metadata_blocks_reconciliation():
    adapter = ObsidianIntentAdapter("vault", list_marked=lambda _: [header("id"), header("id")],
                                    read_marked=lambda _: "ok")
    with sqlite3.connect(":memory:") as db:
        result = IntentRegistry(db).poll(adapter)
    assert not result.applied and not result.changes

