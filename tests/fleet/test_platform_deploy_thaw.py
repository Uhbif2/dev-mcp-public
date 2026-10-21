"""End-to-end platform-deploy thaw through the real registry ingestion path.

These tests use only temporary SQLite and guard-journal fixtures. No live
deployment, process, network call or guard state is modified.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_queue import StubStore, add_project, add_task
from dev_mcp.fleet import deploy_events, queue


SHA_A = "a" * 40
SHA_B = "b" * 40


def _line(code: str, timestamp: datetime, **fields: object) -> str:
    return json.dumps({"ts": timestamp.isoformat(), "code": code, **fields}) + "\n"


def _success(log: Path, when: datetime, sha: str) -> None:
    with log.open("a", encoding="utf-8") as output:
        output.write(_line("known_good", when, candidate=sha, stage="after"))
        output.write(_line("deployed", when + timedelta(seconds=1)))


def _lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
          reason: str, source: str | None = None) -> dict:
    store = StubStore.open(tmp_path / "fleet.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-BLOCK", state="frozen")
    blocked = datetime.now(timezone.utc) - timedelta(hours=2)
    payload = {"reason": reason}
    if source is not None:
        payload["change_source"] = source
    with store.tx() as tx:
        tx.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            (blocked.isoformat(), "block", "P", "TZ-BLOCK", json.dumps(payload)),
        )
    log = tmp_path / "guard" / "platform-guard.jsonl"
    log.parent.mkdir(parents=True)
    original_tick = deploy_events.tick
    # Exercise queue.thaw_frozen -> deployment registry.tick with a fake log.
    monkeypatch.setattr(deploy_events, "tick", lambda s: original_tick(s, log))
    return {"store": store, "log": log, "blocked": blocked}


def _state(store: StubStore) -> str:
    with store.read() as tx:
        return str(tx.one("SELECT state FROM tasks WHERE tz='TZ-BLOCK'")["state"])


def test_thaw_frozen_direct_caller_consumes_exact_sha_receipt_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = _lane(tmp_path, monkeypatch,
                 reason=f"blocked: change_source=platform_deploy commit={SHA_B}")
    store, log, blocked = lane["store"], lane["log"], lane["blocked"]
    assert queue.thaw_frozen(store, after_min=0) == []
    assert _state(store) == "frozen"  # Timer is not permission to retry.

    _success(log, blocked - timedelta(minutes=1), SHA_B)
    queue.thaw_frozen(store, after_min=0)
    assert _state(store) == "frozen"  # Already-deployed SHA is not new evidence.

    _success(log, blocked + timedelta(minutes=1), SHA_A)
    queue.thaw_frozen(store, after_min=0)
    assert _state(store) == "frozen"  # Fresh deployment of the wrong commit.

    _success(log, blocked + timedelta(minutes=2), SHA_B)
    queue.thaw_frozen(store, after_min=0)
    assert _state(store) == "ready"
    with store.read() as tx:
        rows = tx.query("SELECT payload FROM events WHERE kind='thaw' AND tz='TZ-BLOCK'")
        assert len(rows) == 1
        receipt = json.loads(rows[0]["payload"])
        assert receipt["candidate"] == SHA_B
        assert receipt["change_source"] == "platform_deploy"
        assert receipt["blocked_event_id"] > 0
    assert queue.thaw_frozen(store, after_min=0) == []
    with store.read() as tx:
        assert len(tx.query("SELECT id FROM events WHERE kind='thaw' AND tz='TZ-BLOCK'")) == 1


@pytest.mark.parametrize("reason,source", [
    ("blocked: change_source=platform_deploy", None),
    ("blocked: until: platform_deploy", None),
    ("blocked: waiting for deployed commit", "platform_deploy"),
    ("blocked: change_source=platform_deploy commit=not-a-sha", None),
])
def test_ambiguous_platform_deploy_gate_does_not_auto_thaw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, source: str | None,
) -> None:
    lane = _lane(tmp_path, monkeypatch, reason=reason, source=source)
    _success(lane["log"], lane["blocked"] + timedelta(minutes=1), SHA_A)
    queue.thaw_frozen(lane["store"], after_min=0)
    assert _state(lane["store"]) == "frozen"


def test_timestamp_gated_platform_deploy_requires_fresh_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = datetime.now(timezone.utc) - timedelta(minutes=15)
    lane = _lane(tmp_path, monkeypatch,
                 reason=f"until: platform_deploy after={target.isoformat()}")
    _success(lane["log"], lane["blocked"] + timedelta(minutes=1), SHA_A)
    queue.thaw_frozen(lane["store"], after_min=0)
    assert _state(lane["store"]) == "frozen"
    _success(lane["log"], target + timedelta(minutes=1), SHA_A)
    queue.thaw_frozen(lane["store"], after_min=0)
    assert _state(lane["store"]) == "ready"


def test_rollback_is_not_a_platform_deployment_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = _lane(tmp_path, monkeypatch,
                 reason=f"until: platform_deploy commit={SHA_B}")
    moment = lane["blocked"] + timedelta(minutes=2)
    lane["log"].write_text(
        _line("known_good", moment, stage="after", candidate=SHA_B)
        + _line("rollback", moment + timedelta(seconds=1), stage="before")
        + _line("rollback_ok", moment + timedelta(seconds=2)),
        encoding="utf-8",
    )
    queue.thaw_frozen(lane["store"], after_min=0)
    assert _state(lane["store"]) == "frozen"
