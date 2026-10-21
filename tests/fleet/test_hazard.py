from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.hazard import stale_reads
from dev_mcp.fleet.inbox import pending_for
from dev_mcp.fleet.store import Store


def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _event(
    store: Store,
    *,
    kind: str,
    worker_id: str,
    path: str,
    minutes_ago: int,
    payload: dict | None = None,
) -> None:
    with store.tx() as tx:
        tx.record_event(
            kind=kind,
            worker_id=worker_id,
            path_norm=path,
            ts=_ts(minutes_ago),
            payload=payload or {},
        )


def test_own_write_does_not_trigger(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    path = "contracts/trace.md"
    _event(store, kind="read", worker_id="W1", path=path, minutes_ago=120)
    _event(store, kind="write", worker_id="W1", path=path, minutes_ago=40)

    assert stale_reads(store, "W1") == []
    assert pending_for(store, "W1") == []


def test_write_before_read_does_not_trigger(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    path = "contracts/trace.md"
    _event(store, kind="write", worker_id="W2", path=path, minutes_ago=120)
    _event(store, kind="read", worker_id="W1", path=path, minutes_ago=40)

    assert stale_reads(store, "W1") == []


def test_other_worker_write_after_old_read_triggers_once(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    path = "contracts/trace.md"
    _event(store, kind="read", worker_id="W1", path=path, minutes_ago=120)
    _event(store, kind="write", worker_id="W2", path=path, minutes_ago=40)

    first = stale_reads(store, "W1")
    second = stale_reads(store, "W1")

    assert len(first) == 1
    assert second == []
    assert first[0].kind == "stale_read"
    assert first[0].sender.startswith("system")
    assert path in first[0].body
    assert "прочитан тобой 2 ч назад" in first[0].body
    assert "изменён W2 40 мин назад" in first[0].body

    pending = pending_for(store, "W1")
    assert [msg.id for msg in pending] == [first[0].id]


def test_reread_after_foreign_write_clears_staleness(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    path = "contracts/trace.md"
    _event(store, kind="read", worker_id="W1", path=path, minutes_ago=120)
    _event(store, kind="write", worker_id="W2", path=path, minutes_ago=40)
    _event(store, kind="read", worker_id="W1", path=path, minutes_ago=10)

    assert stale_reads(store, "W1") == []


def test_recent_read_is_below_default_stale_threshold(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    path = "contracts/recent.md"
    _event(store, kind="read", worker_id="W1", path=path, minutes_ago=19)
    _event(store, kind="write", worker_id="W2", path=path, minutes_ago=5)

    assert stale_reads(store, "W1") == []


def test_latest_read_before_same_write_is_used(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    path = "contracts/trace.md"
    _event(store, kind="read", worker_id="W1", path=path, minutes_ago=180)
    _event(store, kind="read", worker_id="W1", path=path, minutes_ago=30)
    _event(store, kind="write", worker_id="W2", path=path, minutes_ago=5)

    messages = stale_reads(store, "W1")
    assert len(messages) == 1
    assert "30 мин назад" in messages[0].body


def test_noisy_paths_are_suppressed(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    noisy = [
        ("logs/fleet/audit.jsonl", {}),
        ("src/dev_mcp/__pycache__/server.pyc", {}),
        ("tmp/result.tmp", {}),
        ("repo/root", {"is_dir": True}),
        ("tests/**", {}),
    ]

    for index, (path, payload) in enumerate(noisy):
        # Keep every pair distinct in time so this also exercises multiple candidate events.
        _event(
            store,
            kind="read",
            worker_id="W1",
            path=path,
            minutes_ago=150 + index,
            payload=payload,
        )
        _event(
            store,
            kind="write",
            worker_id="W2",
            path=path,
            minutes_ago=50 + index,
            payload=payload,
        )

    assert stale_reads(store, "W1") == []
    assert pending_for(store, "W1") == []


def test_project_root_traversal_is_suppressed(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = str(tmp_path / "project").replace("\\", "/")
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change(
            """
            INSERT INTO projects(name, root, priority, active)
            VALUES ('P', ?, 5, 1)
            """,
            (root,),
            event={"kind": "join"},
        )

    _event(store, kind="read", worker_id="W1", path=root, minutes_ago=120)
    _event(store, kind="write", worker_id="W2", path=root, minutes_ago=30)

    assert stale_reads(store, "W1") == []


def _claim(store: Store, worker_id: str, tz: str, *, minutes_ago: int, active: bool) -> None:
    with store.tx() as tx:
        sql = tx._conn.execute  # сырые вставки: change() требует события, тут оно не нужно
        sql("INSERT OR IGNORE INTO projects(name,root) VALUES('p','C:/p')")
        sql(
            "INSERT OR IGNORE INTO workers(id,first_seen,last_seen) VALUES(?,?,?)",
            (worker_id, _ts(600), _ts(0)),
        )
        sql(
            "INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('p',?,?,?,?)",
            (tz, "taken" if active else "accepted", _ts(600), _ts(0)),
        )
        sql(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('p',?,?,?,?)",
            (tz, worker_id, "active" if active else "released", _ts(minutes_ago)),
        )


def test_reads_of_previous_task_in_the_same_chat_do_not_trigger(tmp_path) -> None:
    # 22.09.2026: W37 на пятой задаче получил ~30 уведомлений про файлы прошлой TZ-250,
    # пошёл её перепроверять и сдал её работу под TZ-31.
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _claim(store, "W1", "TZ-250", minutes_ago=200, active=False)
    _event(store, kind="read", worker_id="W1", path="old/server.mjs", minutes_ago=180)
    _claim(store, "W1", "TZ-31", minutes_ago=90, active=True)
    _event(store, kind="read", worker_id="W1", path="new/brand.md", minutes_ago=60)
    for _ in range(3):
        _event(store, kind="write", worker_id="W2", path="old/server.mjs", minutes_ago=30)
    _event(store, kind="write", worker_id="W2", path="new/brand.md", minutes_ago=30)

    messages = stale_reads(store, "W1")
    assert [m.body.split(",")[0] for m in messages] == ["new/brand.md"]


def test_many_foreign_writes_after_one_read_give_one_notification(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _event(store, kind="read", worker_id="W1", path="a.md", minutes_ago=120)
    for minutes in (50, 40, 30):
        _event(store, kind="write", worker_id="W2", path="a.md", minutes_ago=minutes)

    messages = stale_reads(store, "W1")
    assert len(messages) == 1
    assert "чужих записей с твоего чтения: 3" in messages[0].body


def test_chat_between_tasks_gets_no_stale_notifications(tmp_path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _claim(store, "W1", "TZ-1", minutes_ago=200, active=False)
    _event(store, kind="read", worker_id="W1", path="a.md", minutes_ago=120)
    _event(store, kind="write", worker_id="W2", path="a.md", minutes_ago=30)

    assert stale_reads(store, "W1") == []
