from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import store as store_mod
from dev_mcp.fleet.store import FleetUnavailable, Store


def test_stuck_holder_fails_waiters_honestly_and_logs_its_stack(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "LOCK_WAIT_S", 0.3)
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    inside, release = threading.Event(), threading.Event()

    def holder_sleeps_inside_tx() -> None:
        with s.tx():
            inside.set()
            release.wait(5)

    t = threading.Thread(target=holder_sleeps_inside_tx, name="fleet-holder")
    t.start()
    inside.wait(5)
    with pytest.raises(FleetUnavailable, match="занят"):
        with s.tx():
            pass
    release.set()
    t.join(5)
    log = (tmp_path / "slow-tx.log").read_text(encoding="utf-8")
    assert "ЗАТОР" in log and "fleet-holder" in log
    assert "holder_sleeps_inside_tx" in log  # стек держателя в момент затора
    with s.tx():  # замок освобождён
        pass


def test_slow_transaction_is_logged_fast_one_is_not(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "SLOW_TX_S", 0.05)
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    with s.tx():
        pass
    assert not (tmp_path / "slow-tx.log").exists()
    with s.tx():
        time.sleep(0.1)
    assert "долгая транзакция" in (tmp_path / "slow-tx.log").read_text(encoding="utf-8")


def test_two_stores_on_one_file_share_the_write_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "LOCK_WAIT_S", 0.3)
    a = Store.open(str(tmp_path / "fleet.sqlite"))
    b = Store.open(str(tmp_path / "fleet.sqlite"))  # как интегратор рядом с сервером
    inside, release = threading.Event(), threading.Event()

    def hold() -> None:
        with a.tx():
            inside.set()
            release.wait(5)

    t = threading.Thread(target=hold, name="server-tx")
    t.start()
    inside.wait(5)
    with pytest.raises(FleetUnavailable, match="server-tx"):  # честный отказ с именем держателя
        with b.tx():
            pass
    release.set()
    t.join(5)


def test_read_does_not_wait_for_writer_and_cannot_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "LOCK_WAIT_S", 0.3)
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    inside, release = threading.Event(), threading.Event()

    def hold() -> None:
        with s.tx():
            inside.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    inside.wait(5)
    with s.read() as tx:  # писатель держит замок — читатель всё равно проходит
        assert tx.scalar("SELECT COUNT(*) FROM events") is not None
        with pytest.raises(RuntimeError, match="только для чтения"):
            tx.record_event(kind="x")
    release.set()
    t.join(5)
    with s.tx() as tx:
        with s.read() as inner:  # внутри своей транзакции read() — та же транзакция
            assert inner is tx


def test_perf_indexes_exist(tmp_path: Path) -> None:
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    with s.read() as tx:
        names = {r["name"] for r in tx.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"events_kind_ts", "events_task_kind", "events_tool"} <= names
