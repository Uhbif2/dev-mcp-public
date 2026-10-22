from pathlib import Path
from types import SimpleNamespace
import gzip
import os
import threading
import time

import pytest

from dev_mcp.fleet import storage_guard, world


def backup(home, name, content=b"SQLite format 3\x00" + b"x" * 65536):
    p = home / "data" / "backups" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    os.utime(p, (0, 0))
    return p


def test_compaction_preserves_every_byte_and_latest_raw_copy(tmp_path):
    old = backup(tmp_path, "fleet-20260101-000000.sqlite")
    recent = backup(tmp_path, "fleet-20260102-000000.sqlite")
    original = old.read_bytes()
    result = storage_guard.compact_one(tmp_path)
    assert result["saved_bytes"] > 0
    assert not old.exists() and recent.exists()
    assert gzip.decompress((old.parent / result["archive"]).read_bytes()) == original
    assert storage_guard.compact_one(tmp_path) is None


def test_compaction_interruption_keeps_original_and_removes_partial(tmp_path):
    old = backup(tmp_path, "fleet-20260101-000000.sqlite")
    backup(tmp_path, "fleet-20260102-000000.sqlite")
    original = old.read_bytes()
    with pytest.raises(TimeoutError):
        storage_guard.compact_one(tmp_path, budget_s=-1)
    assert old.read_bytes() == original
    assert not list(old.parent.glob("*.partial"))


def test_live_wal_or_symlink_cannot_be_compacted(tmp_path):
    old = backup(tmp_path, "fleet-20260101-000000.sqlite")
    backup(tmp_path, "fleet-20260102-000000.sqlite")
    Path(str(old) + "-wal").write_bytes(b"not checkpointed")
    assert storage_guard.compact_one(tmp_path) is None
    assert old.exists()


def test_disk_sensor_refuses_build_from_fresh_observation_and_recovers(monkeypatch):
    cfg = {"disk": {"scope": "run", "global": True, "sensor": "disk",
                    "path": ".", "min_free_bytes": 100, "run_match": "build"}}
    monkeypatch.setattr(world, "config", lambda: cfg)
    monkeypatch.setattr(world, "claim_resources", lambda *a: [])
    monkeypatch.setattr("shutil.disk_usage", lambda p: SimpleNamespace(free=99))
    release, detail = world.hold_for_run(None, "", ["python", "build.py"], "test", wait_s=0)
    assert release is None and "НЕ запущена" in detail
    light, _ = world.hold_for_run(None, "", ["git", "diff"], "test", wait_s=0)
    assert callable(light)
    light()
    monkeypatch.setattr("shutil.disk_usage", lambda p: SimpleNamespace(free=101))
    release, _ = world.hold_for_run(None, "", ["python", "build.py"], "test", wait_s=0)
    assert callable(release)
    release()


def test_storage_heartbeat_is_published_while_compression_is_still_running(tmp_path, monkeypatch):
    from dev_mcp.fleet.store import Store
    store = Store.open(str(tmp_path / "data" / "fleet" / "fleet.sqlite"))
    started, finish, stop = threading.Event(), threading.Event(), threading.Event()

    def slow_compaction(*a, **k):
        started.set()
        assert finish.wait(5)
        return None

    monkeypatch.setattr(storage_guard, "compact_one", slow_compaction)
    monkeypatch.setattr(storage_guard.shutil, "disk_usage", lambda p: SimpleNamespace(free=500 * 1024**2))
    worker = threading.Thread(target=storage_guard.run_worker, args=(store, stop),
                              kwargs={"interval_s": 0.05})
    worker.start()
    try:
        assert started.wait(2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with store.read() as tx:
                if tx.scalar("SELECT COUNT(*) FROM events WHERE kind='storage_tick'"):
                    break
            time.sleep(0.01)
        else:
            pytest.fail("compression blocked the observation heartbeat")
        assert "мало места" in storage_guard.health(store)
    finally:
        stop.set()
        finish.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_new_archive_allows_compacting_former_latest_local_backup(tmp_path, monkeypatch):
    from dev_mcp.fleet import location
    old = backup(tmp_path, "fleet-20260101-000000.sqlite")
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(location, "backup_directory", lambda db: archive)
    partial = archive / "fleet-20260102-000000.partial"
    partial.write_bytes(old.read_bytes())
    assert storage_guard.compact_one(tmp_path) is None
    final = partial.with_suffix(".sqlite")
    partial.rename(final)
    result = storage_guard.compact_one(tmp_path)
    assert result["source"] == old.name
    assert not old.exists() and final.exists()
    assert gzip.decompress(Path(str(old) + ".gz").read_bytes()) == final.read_bytes()


@pytest.mark.production
def test_installed_disk_reserve_does_not_block_small_build_tests(monkeypatch):
    import tomllib
    cfg = tomllib.loads((Path(__file__).parents[2] / "fleet-projects.toml").read_text(encoding="utf-8"))
    monkeypatch.setattr(world, "config", lambda: cfg["resources"])
    monkeypatch.setattr("shutil.disk_usage", lambda p: SimpleNamespace(free=0))
    small = ["py", "-3", "-m", "pytest", "test/test_build_app.py", "-q"]
    heavy = ["py", "-3", "packaging/build_app.py"]
    assert world.disk_admission([world.DISK_ADMISSION, *small], ".") == 0
    assert world.disk_admission([world.DISK_ADMISSION, *heavy], ".") == 1
    assert world.disk_admission([world.DISK_ADMISSION, "py", "-3", "check_all.py"], ".") == 1
    monkeypatch.setattr("shutil.disk_usage", lambda p: SimpleNamespace(free=9 * 1024**3))
    assert world.disk_admission([world.DISK_ADMISSION, *heavy], ".") == 0
