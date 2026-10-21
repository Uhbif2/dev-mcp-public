"""TZ-39: guard receipts, durable cursor, replay, rollback, and targeted thaw."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from dev_mcp.fleet.deploy_events import tick
from dev_mcp.fleet.store import Store


SHA_A = "a" * 40
SHA_B = "b" * 40
T0 = "2026-10-10T07:58:00+00:00"
T1 = "2026-10-10T07:58:01+00:00"


@pytest.fixture
def world(tmp_path: Path):
    root = tmp_path / "fake-live"
    (root / "logs").mkdir(parents=True)
    log = root / "logs" / "platform-guard.jsonl"
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    now = "2026-10-10T07:20:00+00:00"
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('Unit',?,1,1)",
                  (str(root),), event={"kind": "seed"})
        for tz, reason in (
            ("exact", f"blocked: change_source=platform_deploy commit={SHA_A}"),
            ("other_revision", f"until: platform_deploy commit={SHA_B}"),
            ("after", "until: platform_deploy after=2026-10-10T07:57:00+00:00"),
            ("future", "change_source=platform_deploy after=2026-10-10T09:00:00+00:00"),
            ("other_source", f"change_source=some_other_system commit={SHA_A}"),
            ("manual", "штатная выкатка не доказана"),
            ("no_target", "change_source=platform_deploy"),
        ):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
                "spec_sha,needs,owns,resources,acceptance,body,created_at,updated_at)"
                " VALUES('Unit',?,'blocked','frozen',1,'work','','','[]','[]','[]','[]','',?,?)",
                (tz, now, now), event={"kind": "seed"},
            )
            tx.record_event(kind="block", project="Unit", tz=tz, ts=now,
                            payload={"reason": reason, "previous_state": "taken"})
    yield log, store
    store.close()


def logline(code: str, ts: str = T0, **fields) -> str:
    return json.dumps({"code": code, "ts": ts, **fields}, separators=(",", ":")) + "\n"


def append(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.writelines(lines)


def events(store: Store, kind: str):
    with store.read() as tx:
        return tx.query("SELECT id,payload,project,tz FROM events WHERE kind=? ORDER BY id",
                        (kind,))


def state(store: Store, tz: str) -> str:
    with store.read() as tx:
        return tx.one("SELECT state FROM tasks WHERE project='Unit' AND tz=?", (tz,))["state"]


def success(log: Path, candidate: str = SHA_A):
    append(log, logline("known_good", stage="before", candidate=candidate),
           logline("known_good", stage="after", candidate=candidate),
           logline("deployed", ts=T1))


def test_guard_receipt_one_event_matching_thaw_and_repeat_no_duplicates(world):
    log, store = world
    success(log)
    result = tick(store, log)
    assert result == {"events": 1, "thawed": 2, "warnings": 0}
    deployed = events(store, "platform_deployed")
    assert len(deployed) == 1
    proof = json.loads(deployed[0]["payload"])
    assert proof["candidate"] == proof["known_good"] == SHA_A and proof["ts"] == T1
    assert state(store, "exact") == state(store, "after") == "ready"
    assert all(state(store, tz) == "frozen" for tz in (
        "other_revision", "future", "other_source", "manual", "no_target"))
    assert len(events(store, "thaw")) == 2
    assert tick(store, log) == {"events": 0, "thawed": 0, "warnings": 0}
    assert len(events(store, "platform_deployed")) == 1
    assert len(events(store, "thaw")) == 2


def test_rollback_durable_but_never_thaws(world):
    log, store = world
    append(log, logline("rollback", stage="before"),
           logline("rollback_ok", ts=T1),
           logline("rollback", ts=T1, stage="after", success=1))
    result = tick(store, log)
    assert result["events"] == 1 and result["thawed"] == 0
    assert len(events(store, "platform_rolled_back")) == 1
    assert events(store, "platform_deployed") == []
    assert state(store, "exact") == state(store, "after") == "frozen"
    assert tick(store, log)["events"] == 0


def test_bad_lines_report_warning_but_do_not_break_later_good_receipt(world):
    log, store = world
    append(log, '{"ts":\n', logline("unknown"),
           '"not-object"\n',
           logline("known_good", stage="after", candidate=SHA_A),
           logline("deployed", ts=T1))
    result = tick(store, log)
    assert result == {"events": 1, "thawed": 2, "warnings": 2}
    assert len(events(store, "platform_guard_warning")) == 2
    assert len(events(store, "platform_deployed")) == 1
    assert tick(store, log)["warnings"] == 0


def test_incomplete_line_stays_unread_until_append(world):
    log, store = world
    after = logline("known_good", stage="after", candidate=SHA_A)
    log.write_bytes(after[:-1].encode("utf-8"))
    assert tick(store, log) == {"events": 0, "thawed": 0, "warnings": 0}
    with log.open("ab") as out:
        out.write(b"\n" + logline("deployed", ts=T1).encode("utf-8"))
    assert tick(store, log)["thawed"] == 2


def test_rotation_same_path_new_inode_or_changed_prefix_restarts(world):
    log, store = world
    success(log)
    assert tick(store, log)["events"] == 1
    # Replacement with a different prefix but possibly the same file size
    # must not inherit the previous consumed cursor.
    replacement = log.with_suffix(".new")
    append(replacement, logline("known_good", stage="after", candidate=SHA_B),
           logline("deployed", ts=T1))
    replacement.replace(log)
    result = tick(store, log)
    assert result["events"] == 1
    assert state(store, "other_revision") == "ready"
    assert len(events(store, "platform_deployed")) == 2


def test_receipt_across_ticks_and_stale_or_unverified_receipt_refused(world):
    log, store = world
    append(log, logline("known_good", stage="after", candidate=SHA_A))
    assert tick(store, log)["events"] == 0
    append(log, logline("deployed", ts=T1))
    assert tick(store, log)["thawed"] == 2

    path = log.with_name("different-guard.jsonl")
    append(path, logline("known_good", ts="2026-10-10T05:00:00+00:00",
                         stage="after", candidate=SHA_B),
           logline("deployed", ts=T1),
           logline("known_good", stage="before", candidate=SHA_B),
           logline("deployed", ts=T1),
           logline("known_good", stage="after", candidate="wrong"),
           logline("deployed", ts=T1))
    assert tick(store, path)["events"] == 0
    assert state(store, "other_revision") == "frozen"


def test_atomic_cursor_failure_does_not_consume_journal(world, monkeypatch):
    log, store = world
    success(log)
    from contextlib import contextmanager
    original = store.tx

    @contextmanager
    def fail_before_commit():
        with original() as tx:
            record = tx.record_event

            def broken(**data):
                if data.get("kind") == "platform_guard_cursor":
                    raise RuntimeError("disk error while saving cursor")
                return record(**data)
            tx.record_event = broken
            yield tx
    monkeypatch.setattr(store, "tx", fail_before_commit)
    with pytest.raises(RuntimeError, match="cursor"):
        tick(store, log)
    assert len(events(store, "platform_deployed")) == 0
    assert state(store, "exact") == "frozen"
    monkeypatch.setattr(store, "tx", original)
    assert tick(store, log)["thawed"] == 2


def test_repeated_deploy_no_duplicate_task_thaw(world):
    log, store = world
    success(log)
    assert tick(store, log)["thawed"] == 2
    append(log, logline("known_good", ts="2026-10-10T08:10:00+00:00",
                        stage="after", candidate=SHA_A),
           logline("deployed", ts="2026-10-10T08:10:01+00:00"))
    assert tick(store, log)["events"] == 1
    assert tick(store, log)["thawed"] == 0
    assert len(events(store, "platform_deployed")) == 2
    assert len(events(store, "thaw")) == 2


def test_public_tool_maintenance_hook_calls_consumer(world, monkeypatch):
    """Production maintenance step, not only unit helper, calls deploy_events.tick."""
    log, store = world
    success(log)
    from dev_mcp.fleet import tools
    # The production hook calls tick(store) with the path derived from the DB.
    # Our test Store isn't in a live root, so route log through the implementation
    # but exercise the actual tools hook and transaction.
    from dev_mcp.fleet import deploy_events
    original = deploy_events.tick
    monkeypatch.setattr(deploy_events, "tick", lambda s: original(s, log))
    tools._tick_platform_deploy_events(store)
    assert len(events(store, "platform_deployed")) == 1
    assert state(store, "exact") == "ready"


def test_historical_deploy_cannot_satisfy_newly_blocked_task(world):
    log, store = world
    success(log)
    with store.tx() as tx:
        tx.record_event(kind="block", project="Unit", tz="exact",
                        ts="2026-10-10T08:10:00+00:00",
                        payload={"reason": f"until: platform_deploy commit={SHA_A}"})
    assert tick(store, log)["events"] == 1
    assert state(store, "exact") == "frozen"
    assert state(store, "after") == "ready"
    append(log, logline("known_good", ts="2026-10-10T08:11:00+00:00",
                        stage="after", candidate=SHA_A),
           logline("deployed", ts="2026-10-10T08:11:01+00:00"))
    assert tick(store, log)["thawed"] == 1
    assert state(store, "exact") == "ready"


def test_default_live_log_path_is_derived_from_store(world):
    log, store = world
    success(log)
    root = log.parent.parent
    db = root / "data" / "fleet" / "fleet.sqlite"
    db.parent.mkdir(parents=True)
    store.backup(str(db))
    copy = Store.open(str(db))
    try:
        assert tick(copy)["thawed"] == 2
        assert tick(copy)["events"] == 0
    finally:
        copy.close()


def test_typed_event_criterion_and_invalid_timestamp_fail_closed(world):
    log, store = world
    with store.tx() as tx:
        tx.record_event(kind="block", ts="2026-10-10T07:20:00+00:00",
                        project="Unit", tz="future",
                        payload={"change_source": "platform_deploy",
                                 "after": "not-a-date"})
        tx.record_event(kind="block", ts="2026-10-10T07:20:00+00:00",
                        project="Unit", tz="other_revision",
                        payload={"change_source": "platform_deploy", "commit": SHA_A})
    success(log)
    assert tick(store, log)["thawed"] == 3
    assert state(store, "other_revision") == "ready"
    assert state(store, "future") == "frozen"


def test_rotation_replaying_same_receipt_is_idempotent(world):
    log, store = world
    success(log)
    assert tick(store, log)["events"] == 1
    replacement = log.with_name("rotated.jsonl")
    replacement.write_bytes(log.read_bytes())
    replacement.replace(log)
    assert tick(store, log)["events"] == 0
    assert len(events(store, "platform_deployed")) == 1
    assert len(events(store, "thaw")) == 2


def test_corrupt_utf8_and_oversized_line_warn_and_resume(world):
    log, store = world
    with log.open("wb") as out:
        out.write(b"\xff\xfe\xfd\n")
        out.write(b"x" * 100_000 + b"\n")
        out.write(logline("known_good", stage="after", candidate=SHA_A).encode())
        out.write(logline("deployed", ts=T1).encode())
    observed = tick(store, log)
    assert observed["warnings"] == 2
    assert observed["events"] == 1
    assert state(store, "exact") == "ready"
