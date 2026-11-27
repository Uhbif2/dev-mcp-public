"""Роль владельца: 48 ч, восстановление номера после ночи, роль не теряется (20.11.2026).

Регрессия: роль выдавалась на 12 ч; ночью автовосстановление молчащих захватов
(queue.recover_stale_with_work, wake.give_up) отзывало номер вкладки владельца, а
`fleet.cmd owner-grant` отказывал отозванному номеру — агента регистрировали заново.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import chat_handoff, queue, wake  # noqa: E402
from dev_mcp.fleet.owner import carry_grant, grant, grants_path, owner_ids_for_store, read_grants, secret_path  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402

_FLEET = Path(__file__).resolve().parents[2] / "tools" / "fleet.py"
_spec = importlib.util.spec_from_file_location("fleet_console_owner48_test", _FLEET)
fleet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def _repo_store(tmp_path: Path) -> tuple[Path, Store]:
    repo = tmp_path / "repo"
    db = repo / "data" / "fleet" / "fleet.sqlite"
    db.parent.mkdir(parents=True)
    return repo, Store.open(str(db))


def _grants(repo: Path) -> Path:
    return grants_path(secret_path(repo))


def _silent_claim(store: Store, worker: str, tz: str, *, silent_h: float, wrote: bool) -> None:
    now = datetime.now(timezone.utc)
    started, seen = _iso(now - timedelta(hours=silent_h + 0.5)), _iso(now - timedelta(hours=silent_h))
    with store.tx() as tx:
        tx.change("INSERT OR IGNORE INTO projects(name,root,priority,active) VALUES('P','C:/P',1,1)",
                  (), event={"kind": "test_setup"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES('P',?,?,'taken',5,'work','','','[]','[]','[]','[]',NULL,'b',?,?)",
            (tz, tz, started, started), event={"kind": "test_setup"})
        tx.change("INSERT OR IGNORE INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES(?,?,'chatgpt',?,?)", (worker, "owner-" + worker, started, seen),
                  event={"kind": "test_setup"})
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('P',?,?,'active',?)",
                  (tz, worker, started), event={"kind": "test_setup"})
        if wrote:
            tx.change("INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload) "
                      "VALUES (?,'write',?,'P',?,'edit_file','a.py',NULL,'{}')",
                      (_iso(now - timedelta(hours=silent_h + 0.2)), worker, tz),
                      event={"kind": "test_setup"})


def _worker(store: Store, worker: str) -> sqlite3.Row:
    with store.read() as tx:
        return tx.one("SELECT revoked_at,revoke_reason FROM workers WHERE id=?", (worker,))


def test_owner_grant_default_is_48_hours() -> None:
    args = fleet.build_parser().parse_args(["owner-grant", "W1"])
    assert args.hours == 48


def test_owner_grant_restores_revoked_number(tmp_path: Path, monkeypatch, capsys) -> None:
    repo, store = _repo_store(tmp_path)
    monkeypatch.setattr(fleet, "REPO", repo)
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason) "
                  "VALUES('W7','owner','chatgpt','2026-11-19T20:00:00+00:00','2026-11-19T22:00:00+00:00',"
                  "'2026-11-20T03:00:00+00:00','automatic recovery P/TZ-1: worker silent >= 180 min')",
                  (), event={"kind": "test_setup"})
    before = time.time()
    assert fleet._grant_ids(Path(store.path), ["W7"], 48) == 0
    out = capsys.readouterr().out
    assert "восстановлен" in out and "automatic recovery" in out
    assert _worker(store, "W7")["revoked_at"] is None
    until = read_grants(_grants(repo))["W7"]
    assert before + 48 * 3600 - 5 <= until <= time.time() + 48 * 3600 + 5
    # Неизвестный номер по-прежнему не выдаётся.
    assert fleet._grant_ids(Path(store.path), ["W404"], 48) == 2


def test_recover_stale_with_work_keeps_owner_number(tmp_path: Path) -> None:
    repo, store = _repo_store(tmp_path)
    _silent_claim(store, "W1", "TZ-1", silent_h=5, wrote=True)  # вкладка владельца
    _silent_claim(store, "W2", "TZ-2", silent_h=5, wrote=True)  # обычный агент
    grant(_grants(repo), "W1", 3600)
    returned = queue.recover_stale_with_work(store, silence_min=180)
    assert sorted(t.tz for t in returned) == ["TZ-1", "TZ-2"]  # захваты возвращены оба
    assert _worker(store, "W1")["revoked_at"] is None
    assert _worker(store, "W2")["revoked_at"]
    with store.read() as tx:
        payloads = [json.loads(r["payload"]) for r in tx.query(
            "SELECT payload FROM events WHERE kind='requeue' ORDER BY id")]
    assert sorted(p.get("owner_kept") for p in payloads) == [False, True]


def test_give_up_keeps_owner_number(tmp_path: Path) -> None:
    repo, store = _repo_store(tmp_path)
    _silent_claim(store, "W1", "TZ-1", silent_h=6, wrote=False)
    _silent_claim(store, "W2", "TZ-2", silent_h=6, wrote=False)
    grant(_grants(repo), "W1", 3600)
    book = wake.ChatBook(tmp_path / "chats.json")  # чатов нет → ветка «чат неизвестен»
    released = wake.give_up(store, book)
    assert sorted(released) == ["P/TZ-1", "P/TZ-2"]
    assert _worker(store, "W1")["revoked_at"] is None
    assert _worker(store, "W2")["revoked_at"]


def test_expired_or_missing_grant_does_not_protect(tmp_path: Path) -> None:
    repo, store = _repo_store(tmp_path)
    _silent_claim(store, "W1", "TZ-1", silent_h=5, wrote=True)
    path = _grants(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"W1": time.time() - 10}), encoding="utf-8")
    queue.recover_stale_with_work(store, silence_min=180)
    assert _worker(store, "W1")["revoked_at"]


def test_owner_ids_outside_data_fleet_layout_is_empty(tmp_path: Path) -> None:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    grant(grants_path(secret_path(tmp_path.parent)), "W1", 3600)
    assert owner_ids_for_store(store) == set()
    assert carry_grant(store, "W1", "W2") is False


def test_carry_grant_moves_live_role_without_extending(tmp_path: Path) -> None:
    repo, store = _repo_store(tmp_path)
    until = grant(_grants(repo), "W1", 3600)
    assert carry_grant(store, "W1", "W1.2") is True
    grants = read_grants(_grants(repo))
    assert grants["W1.2"] == until and grants["W1"] == until
    assert carry_grant(store, "W1", "W1.2") is False  # повтор ничего не меняет
    assert carry_grant(store, "W9", "W10") is False   # у источника роли нет


def test_handoff_transfer_carries_owner_role(tmp_path: Path) -> None:
    repo, store = _repo_store(tmp_path)
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES('W5','owner','chatgpt','2026-11-20T00:00:00+00:00','2026-11-20T00:00:00+00:00')",
                  (), event={"kind": "test_setup"})
    grant(_grants(repo), "W5", 3600)
    successor = chat_handoff.rotated_tag("W5")
    chat_handoff.transfer_rights(store, "W5", successor)
    assert successor in owner_ids_for_store(store)
