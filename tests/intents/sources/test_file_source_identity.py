"""Regression: different file directories must not share a registry identity."""

from __future__ import annotations

import sqlite3

import pytest

from dev_mcp.intents.sources import FileIntentAdapter, IntentRegistry
from dev_mcp.intents.sources.files import DEFAULT_SOURCE_ID


def test_custom_root_without_unique_id_is_rejected(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "same.md").write_text("first", encoding="utf-8")
    (second / "same.md").write_text("second", encoding="utf-8")

    for root in (first, second):
        with pytest.raises(ValueError, match="source_id"):
            FileIntentAdapter(root)
        # The default directory's reserved ID must not be assigned to another root.
        with pytest.raises(ValueError, match="source_id"):
            FileIntentAdapter(root, source_id=DEFAULT_SOURCE_ID)


def test_explicit_file_source_ids_isolate_revisions_and_cancellations(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "same.md").write_text("first", encoding="utf-8")
    (second / "same.md").write_text("second", encoding="utf-8")
    a = FileIntentAdapter(first, source_id="file:first")
    b = FileIntentAdapter(second, source_id="file:second")
    with sqlite3.connect(":memory:") as db:
        registry = IntentRegistry(db)
        assert [c.kind for c in registry.poll(a).changes] == ["created"]
        assert [c.kind for c in registry.poll(b).changes] == ["created"]
        assert registry.get("file:first", "same.md")[0].raw_intent == "first"
        assert registry.get("file:second", "same.md")[0].raw_intent == "second"
        (second / "same.md").write_text("second revised", encoding="utf-8")
        assert [c.kind for c in registry.poll(b).changes] == ["updated"]
        assert registry.get("file:first", "same.md")[0].raw_intent == "first"
        (first / "same.md").unlink()
        assert [c.kind for c in registry.poll(a).changes] == ["cancelled"]
        assert registry.get("file:second", "same.md")[1] == "active"


def test_default_root_retains_reserved_source_id():
    source = FileIntentAdapter()
    assert source.source_id == DEFAULT_SOURCE_ID
