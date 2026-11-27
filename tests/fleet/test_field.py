"""Сигналы поля -> поручения ведущему (fleet/field.py, 08.11.2026)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import field  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402


def _ts(days: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _err(msg: str, days: float = 0) -> dict:
    return {"ts": _ts(days), "level": "error", "error": msg}


@pytest.fixture()
def env(tmp_path: Path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "Proj"
    root.mkdir()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)", ("P", str(root)),
                  event={"kind": "test_setup"})
    cfg = {"sources": [{"path": "err.jsonl", "kind": "jsonl", "since_days": 3}]}
    return store, root, cfg


def _instructions(store: Store) -> list[dict]:
    with store.read() as tx:
        rows = tx.query("SELECT payload FROM events WHERE kind='instruction' AND project='P'")
    return [json.loads(r["payload"]) for r in rows]


def test_new_fingerprint_creates_field_instruction_once(env):
    store, root, cfg = env
    _write(root / "err.jsonl", [_err("timeout after 31 s on /a/b/1"), _err("timeout after 45 s on /a/b/2")])
    made = field.scan(store, "P", root, cfg)
    assert len(made) == 1 and made[0]["count"] == 2
    ins = _instructions(store)
    assert len(ins) == 1 and ins[0]["source"] == "field" and ins[0]["author"] == "field"
    assert ins[0]["fingerprint"] == made[0]["fingerprint"] and ins[0]["done_when"]
    with store.read() as tx:
        msg = tx.one("SELECT to_task,body FROM messages")
    assert msg["to_task"] == "P/TZ-LEAD" and ins[0]["id"] in msg["body"]
    # второй прогон: тот же отпечаток -- дубля нет
    assert field.scan(store, "P", root, cfg) == []
    assert len(_instructions(store)) == 1


def test_new_fingerprint_after_first_run_creates_second(env):
    store, root, cfg = env
    _write(root / "err.jsonl", [_err("disk full 1"), _err("disk full 2")])
    assert len(field.scan(store, "P", root, cfg)) == 1
    _write(root / "err.jsonl", [_err("disk full 3"), _err("disk full 4"),
                                _err("bind failed port 8"), _err("bind failed port 9")])
    made = field.scan(store, "P", root, cfg)
    assert len(made) == 1 and "bind failed" in made[0]["first"]
    assert len(_instructions(store)) == 2


def test_limit_three_per_run_and_rest_next_run(env):
    store, root, cfg = env
    rows = []
    for name in ("alpha", "bravo", "charlie", "delta", "echo"):
        rows += [_err(f"{name} broke"), _err(f"{name} broke")]
    _write(root / "err.jsonl", rows)
    assert len(field.scan(store, "P", root, cfg)) == 3
    assert len(field.scan(store, "P", root, cfg)) == 2
    assert field.scan(store, "P", root, cfg) == []


def test_single_occurrence_old_and_dry_run_do_not_register(env):
    store, root, cfg = env
    _write(root / "err.jsonl", [_err("rare one"), _err("stale err", days=10), _err("stale err", days=11),
                                {"ts": _ts(), "level": "info", "message": "fine"}])
    assert field.scan(store, "P", root, cfg) == []
    _write(root / "err.jsonl", [_err("again x"), _err("again x")])
    assert len(field.scan(store, "P", root, cfg, dry=True)) == 1
    assert _instructions(store) == []


def test_log_blocks_and_events_source(env):
    store, root, _ = env
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    block = (f"[E {now},1.1 mcp.server] Error handling POST\nTraceback (most recent call last):\n"
             "  File \"C:/x/y.py\", line 12, in f\nstarlette.requests.ClientDisconnect\n")
    (root / "p.log").write_text(block + f"[I {now},2.2 mcp] ok\n" + block.replace("12", "99"), encoding="utf-8")
    with store.tx() as tx:
        for _ in range(2):
            tx.record_event(kind="live_deployed", project="P",
                            payload={"ok": False, "instance": "lib", "detail": "initialize timeout"})
        tx.record_event(kind="live_deployed", project="P", payload={"ok": True, "instance": "lib"})
    cfg = {"sources": [{"path": "p.log", "kind": "log"},
                       {"kind": "events", "event_kind": "live_deployed", "bad_field": "ok", "bad_values": [False, 0]}]}
    made = field.scan(store, "P", root, cfg)
    assert sorted(m["count"] for m in made) == [2, 2]


def test_tick_uses_project_info_and_throttles(env):
    store, root, cfg = env
    _write(root / "err.jsonl", [_err("boom 1"), _err("boom 2")])
    field._LAST_RUN.clear()
    info = lambda name: {"field": cfg} if name == "P" else {}  # noqa: E731
    assert len(field.tick(store, info)) == 1
    _write(root / "err.jsonl", [_err("other 1"), _err("other 2")])
    assert field.tick(store, info) == []   # every_min=60: ещё рано
