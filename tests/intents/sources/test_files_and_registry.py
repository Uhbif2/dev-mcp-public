from __future__ import annotations

import sqlite3

import pytest

from dev_mcp.intents.sources import FileIntentAdapter, IntentEnvelope, IntentRegistry, PollResult


@pytest.fixture
def registry():
    with sqlite3.connect(":memory:") as db:
        yield IntentRegistry(db)


def test_verbatim_bytes_revision_and_restart(tmp_path):
    root = tmp_path / "intents"
    root.mkdir()
    text = "Строка 1\r\nстрока 2  \n\n"
    (root / "a.md").write_bytes(text.encode("utf-8"))
    adapter = FileIntentAdapter(root, source_id="file:demo", authority={"budget": 0})
    database = tmp_path / "seen.db"
    with sqlite3.connect(database) as db:
        registry = IntentRegistry(db)
        first = registry.poll(adapter)
        assert first.applied and [c.kind for c in first.changes] == ["created"]
        assert first.changes[0].envelope.raw_intent == text
        assert registry.poll(adapter).changes == ()
    with sqlite3.connect(database) as db:
        registry = IntentRegistry(db)
        assert registry.poll(adapter).changes == ()
        assert registry.get("file:demo", "a.md")[0].raw_intent == text
        (root / "a.md").write_text("now changed", encoding="utf-8")
        updated = registry.poll(adapter)
        assert [c.kind for c in updated.changes] == ["updated"]
        assert updated.changes[0].previous_revision == first.changes[0].envelope.revision
        assert registry.poll(adapter).changes == ()
        (root / "a.md").unlink()
        cancelled = registry.poll(adapter)
        assert [c.kind for c in cancelled.changes] == ["cancelled"]
        assert cancelled.changes[0].envelope.raw_intent == "now changed"
        assert registry.get("file:demo", "a.md")[1] == "cancelled"
        assert registry.poll(adapter).changes == ()
        (root / "a.md").write_text("now changed", encoding="utf-8")
        assert [c.kind for c in registry.poll(adapter).changes] == ["created"]


@pytest.mark.parametrize("payload", [b"", b"  \r\n", b"\xff\xfe", b"x" * 129])
def test_malformed_or_large_file_never_cancels_previous(tmp_path, registry, payload):
    path = tmp_path / "a.txt"
    path.write_text("ok", encoding="utf-8")
    source = FileIntentAdapter(tmp_path, source_id="file:demo", max_bytes=128)
    assert len(registry.poll(source).changes) == 1
    path.write_bytes(payload)
    result = registry.poll(source)
    assert not result.applied and not result.changes and result.errors
    assert registry.get("file:demo", "a.txt")[1] == "active"
    assert registry.get("file:demo", "a.txt")[0].raw_intent == "ok"


def test_missing_directory_not_interpreted_as_deletion(tmp_path, registry):
    root = tmp_path / "intents"
    root.mkdir()
    (root / "a.md").write_text("ok")
    source = FileIntentAdapter(root, source_id="file:demo")
    registry.poll(source)
    (root / "a.md").unlink()
    root.rmdir()
    assert not registry.poll(source).applied
    assert registry.get("file:demo", "a.md")[1] == "active"


def test_symlinks_are_never_read_and_scans_fail_closed(tmp_path, registry):
    root = tmp_path / "intents"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    result = registry.poll(FileIntentAdapter(root, source_id="file:symlink-test"))
    assert not result.applied and not result.changes
    assert any("symlink" in error for error in result.errors)


def test_same_external_id_is_isolated_by_source_id(registry):
    def snapshot(source, value):
        env = IntentEnvelope(source, "same", value, value, {}, "2026-10-09T09:00:00Z")
        return PollResult(source, (env,))
    assert registry.reconcile(snapshot("files", "first")).changes[0].kind == "created"
    assert registry.reconcile(snapshot("obsidian", "second")).changes[0].kind == "created"
    assert registry.reconcile(PollResult("files")).changes[0].kind == "cancelled"
    assert registry.get("obsidian", "same")[1] == "active"


def test_invalid_or_incomplete_snapshots_never_mutate(registry):
    env = IntentEnvelope("s", "id", "1", "one", {}, "2026-10-09T09:00:00Z")
    assert registry.reconcile(PollResult("s", (env,))).applied
    for snapshot in (
        PollResult("s", (), False, ("network",)),
        PollResult("s", (env, env)),
        PollResult("other", (env,)),
    ):
        assert not registry.reconcile(snapshot).applied
    assert registry.get("s", "id")[1] == "active"
    assert registry.get("s", "id")[0].raw_intent == "one"


def test_authority_change_produces_new_revision(tmp_path, registry):
    (tmp_path / "plan.txt").write_text("plan")
    source1 = FileIntentAdapter(tmp_path, source_id="file:demo", authority={"root": "one"})
    source2 = FileIntentAdapter(tmp_path, source_id="file:demo", authority={"root": "two"})
    assert registry.poll(source1).changes[0].kind == "created"
    assert registry.poll(source2).changes[0].kind == "updated"
    assert registry.get("file:demo", "plan.txt")[0].authority == {"root": "two"}


def test_durable_outbox_replays_after_crash_until_ack(tmp_path):
    file = tmp_path / "a.md"
    file.write_text("first", encoding="utf-8")
    adapter = FileIntentAdapter(tmp_path, source_id="file:outbox")
    database = tmp_path / "history.sqlite"
    with sqlite3.connect(database) as db:
        registry = IntentRegistry(db)
        created = registry.poll(adapter).changes[0]
        assert created.event_id is not None
        assert registry.pending_changes() == (created,)
        # Simulate crash before downstream delivery; no ACK is written.
    with sqlite3.connect(database) as db:
        registry = IntentRegistry(db)
        assert registry.poll(adapter).changes == ()  # no duplicate source revision
        assert registry.pending_changes() == (created,)  # but delivery not lost
        file.write_text("second", encoding="utf-8")
        update = registry.poll(adapter).changes[0]
        file.unlink()
        cancel = registry.poll(adapter).changes[0]
        assert [c.kind for c in registry.pending_changes()] == ["created", "updated", "cancelled"]
        assert [c.event_id for c in registry.pending_changes()] == [created.event_id, update.event_id, cancel.event_id]
        assert registry.ack_change(created.event_id)
        assert not registry.ack_change(created.event_id)
        assert [c.kind for c in registry.pending_changes()] == ["updated", "cancelled"]
        assert registry.get("file:outbox", "a.md")[1] == "cancelled"


def test_ack_can_rollback_with_downstream_write(tmp_path):
    adapter = FileIntentAdapter(tmp_path, source_id="file:test")
    (tmp_path / "a.txt").write_text("intent")
    with sqlite3.connect(":memory:") as db:
        registry = IntentRegistry(db)
        change = registry.poll(adapter).changes[0]
        try:
            with db:
                db.execute("CREATE TABLE controller_effects (event_id INTEGER)")
                db.execute("INSERT INTO controller_effects VALUES (?)", (change.event_id,))
                assert registry.ack_change(change.event_id)
                raise RuntimeError("simulated controller failure")
        except RuntimeError:
            pass
        assert registry.pending_changes() == (change,)
        assert db.execute("SELECT count(*) FROM controller_effects").fetchone()[0] == 0
