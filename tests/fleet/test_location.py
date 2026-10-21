"""Переезд реестра флота logs/fleet → data/fleet и резерв (src/dev_mcp/fleet/location.py)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dev_mcp.fleet.location import backup_fleet_db, fleet_db_path
from dev_mcp.fleet.store import Store


@pytest.fixture(autouse=True)
def storage_capacity(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("dev_mcp.fleet.location.shutil.disk_usage",
                        lambda p: SimpleNamespace(free=64 * 1024**3))


def _legacy(home: Path) -> Path:
    db = home / "logs" / "fleet" / "fleet.sqlite"
    Store.open(str(db))
    conn = sqlite3.connect(db)  # не `with`: он коммитит, но не закрывает — база осталась бы занята
    conn.execute("CREATE TABLE marker(x)")
    conn.execute("INSERT INTO marker VALUES ('from-legacy')")
    conn.commit()
    conn.close()
    snap = home / "logs" / "fleet" / "snapshots"
    snap.mkdir()
    (snap / "claim-1.json").write_text("{}", encoding="utf-8")
    return db


def test_fresh_home_uses_data(tmp_path: Path) -> None:
    assert fleet_db_path(tmp_path) == tmp_path / "data" / "fleet" / "fleet.sqlite"
    assert not (tmp_path / "data").exists()  # путь не создаёт ничего сам


def test_legacy_folder_moves_whole(tmp_path: Path) -> None:
    _legacy(tmp_path)
    path = fleet_db_path(tmp_path)
    assert path == tmp_path / "data" / "fleet" / "fleet.sqlite"
    assert not (tmp_path / "logs" / "fleet").exists()
    assert (tmp_path / "data" / "fleet" / "snapshots" / "claim-1.json").exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT x FROM marker").fetchone() == ("from-legacy",)


def test_open_legacy_is_left_in_place(tmp_path: Path) -> None:
    db = _legacy(tmp_path)
    holder = sqlite3.connect(db)  # процесс со старым кодом держит базу
    try:
        holder.execute("SELECT 1 FROM marker").fetchone()
        assert fleet_db_path(tmp_path) == db
        assert not (tmp_path / "data" / "fleet").exists()  # вторая база не появилась
    finally:
        holder.close()
    assert fleet_db_path(tmp_path) == tmp_path / "data" / "fleet" / "fleet.sqlite"


def test_current_wins_over_legacy(tmp_path: Path) -> None:
    current = tmp_path / "data" / "fleet" / "fleet.sqlite"
    Store.open(str(current))
    _legacy(tmp_path)
    assert fleet_db_path(tmp_path) == current
    assert (tmp_path / "logs" / "fleet" / "fleet.sqlite").exists()  # чужое не трогаем


def test_backup_keeps_last_n(tmp_path: Path) -> None:
    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    Store.open(str(db))
    backups = tmp_path / "data" / "backups"
    backups.mkdir(parents=True)
    for i in range(5):
        (backups / f"fleet-20260101-00000{i}.sqlite").write_bytes(b"")
    made = backup_fleet_db(db, keep=3)
    assert made is not None and made.parent == backups
    kept = sorted(p.name for p in backups.glob("fleet-*.sqlite"))
    assert len(kept) == 3 and made.name in kept
    with sqlite3.connect(made) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_skips_outside_data(tmp_path: Path) -> None:
    db = tmp_path / "logs" / "fleet" / "fleet.sqlite"
    Store.open(str(db))
    assert backup_fleet_db(db) is None
    assert backup_fleet_db(tmp_path / "data" / "fleet" / "fleet.sqlite") is None


def test_backup_low_space_never_creates_destination(tmp_path, monkeypatch):
    from types import SimpleNamespace
    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    Store.open(str(db))
    monkeypatch.setattr("dev_mcp.fleet.location.shutil.disk_usage",
                        lambda p: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="insufficient space"):
        backup_fleet_db(db)
    assert not list((tmp_path / "data" / "backups").iterdir())


def test_compressed_backup_counts_toward_rolling_retention(tmp_path):
    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    Store.open(str(db))
    folder = tmp_path / "data" / "backups"
    folder.mkdir(exist_ok=True)
    (folder / "fleet-20260101-000000.sqlite.gz").write_bytes(b"old archive")
    made = backup_fleet_db(db, keep=1)
    assert list(folder.iterdir()) == [made]


def test_only_installed_registry_uses_operator_archive(tmp_path, monkeypatch):
    from dev_mcp.fleet import location, goals
    pretend = tmp_path/'installed/src/dev_mcp/fleet/location.py'
    monkeypatch.setattr(location,'__file__',str(pretend))
    archive = tmp_path/'other-volume/rolling'
    monkeypatch.setattr(goals,'_load_doc',lambda:{'storage':{'backup_root':str(archive)}})
    installed = tmp_path/'installed/data/fleet/fleet.sqlite'
    assert location.backup_directory(installed) == archive
    unrelated = tmp_path/'fixture/data/fleet/fleet.sqlite'
    assert location.backup_directory(unrelated) == unrelated.parent.parent/'backups'


def test_backup_flush_failure_cannot_publish_or_replace_previous_backup(tmp_path, monkeypatch):
    from dev_mcp.fleet import location
    db=tmp_path/'data/fleet/fleet.sqlite'
    Store.open(str(db))
    previous=backup_fleet_db(db)
    content=previous.read_bytes()
    def failed_flush(*a):
        raise OSError('flush failed')
    monkeypatch.setattr(location.os,'fsync',failed_flush)
    with pytest.raises(OSError,match='flush failed'):
        backup_fleet_db(db)
    assert previous.read_bytes()==content
    assert list(previous.parent.glob('fleet-*.sqlite'))==[previous]
    assert not list(previous.parent.glob('*.partial'))


def test_backup_is_one_snapshot_even_when_writer_changes_source_at_copy(tmp_path, monkeypatch):
    from dev_mcp.fleet import location

    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    Store.open(str(db))
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE witness(value TEXT)")
        conn.execute("INSERT INTO witness VALUES('before')")
    connect = sqlite3.connect
    class Source:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def backup(self, dst, **kwargs):
            with connect(db) as writer:
                writer.execute("INSERT INTO witness VALUES('during-copy')")
            self.connection.backup(dst, **kwargs)
    def controlled_connect(path, *args, **kwargs):
        connection = connect(path, *args, **kwargs)
        return Source(connection) if Path(path) == db else connection
    monkeypatch.setattr(location.sqlite3, "connect", controlled_connect)
    made = backup_fleet_db(db)
    with connect(made) as snapshot:
        assert snapshot.execute("SELECT value FROM witness").fetchall() == [('before',)]
    with connect(db) as current:
        assert current.execute("SELECT count(*) FROM witness").fetchone()[0] == 2


def test_interrupted_backup_keeps_previous_snapshot_and_removes_partial(tmp_path, monkeypatch):
    from dev_mcp.fleet import location

    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    Store.open(str(db))
    previous = backup_fleet_db(db)
    content = previous.read_bytes()
    monkeypatch.setattr(location, "BACKUP_BUDGET_S", -1)
    with pytest.raises(TimeoutError):
        backup_fleet_db(db)
    assert previous.read_bytes() == content
    assert list(previous.parent.glob("fleet-*.sqlite")) == [previous]
    assert not list(previous.parent.glob("*.partial"))


def test_real_isolated_backup_contains_committed_witness_and_respects_kernel_lease(tmp_path):
    from dev_mcp.fleet import location
    db=tmp_path/'data/fleet/fleet.sqlite'
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE witness(value TEXT)')
        conn.execute("INSERT INTO witness VALUES('committed')")
    made=location.backup_fleet_db_isolated(db)
    with sqlite3.connect(made) as conn:
        assert conn.execute('SELECT value FROM witness').fetchall()==[('committed',)]
        assert conn.execute('PRAGMA quick_check').fetchone()==('ok',)
    with location._backup_lease(db) as acquired:
        assert acquired
        assert location.backup_fleet_db_isolated(db) is None
    assert list(made.parent.glob('fleet-*.sqlite'))==[made]
