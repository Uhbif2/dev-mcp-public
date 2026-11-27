"""Cached bootstrap worktrees: restore exactly dirty paths, never the whole tree."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from dev_mcp.fleet import exec_preflight as pre
from dev_mcp.fleet.gate_snapshot import SnapshotStore


def _snapshot_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int):
    cache = tmp_path / "cache"
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", cache)
    store = SnapshotStore(tmp_path / "objects")
    snapshot_id = "a" * 64
    obj = store._object(snapshot_id)
    tree = obj / "tree"
    tree.mkdir(parents=True)
    rows = []
    for i in range(n):
        name = f"package_{i // 100:04d}/module_{i:05d}.py"
        source = tree / name
        source.parent.mkdir(exist_ok=True)
        data = f"v={i}\n".encode()
        source.write_bytes(data)
        rows.append({"path": name, "size": len(data),
                     "sha256": __import__("hashlib").sha256(data).hexdigest()})
    (obj / "manifest.json").write_text(json.dumps({"files": rows}), encoding="utf-8")
    return store, snapshot_id, rows


def test_cached_workspace_repairs_only_dirty_files_and_removes_extra_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    store, snapshot_id, rows = _snapshot_fixture(tmp_path, monkeypatch, 240)
    work = pre._prepare_cached_work(store, snapshot_id, None, "test-work")
    first = work / rows[0]["path"]
    second = work / rows[1]["path"]
    original = first.read_bytes()
    first.write_bytes(b"x" * len(original))
    (work / "extra" / "nested").mkdir(parents=True)
    (work / "extra" / "nested" / "side_effect.txt").write_text("side effect")
    (work / "untracked.tmp").write_text("side effect")
    copied = []
    copy = pre.shutil.copyfile

    def track(src, dst, *args, **kwargs):
        copied.append(Path(dst).relative_to(work).as_posix())
        return copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(pre.shutil, "copyfile", track)
    repaired = pre._prepare_cached_work(store, snapshot_id, None, "test-work")
    assert repaired == work
    assert copied == [rows[0]["path"]]
    assert first.read_bytes() == original
    assert second.read_bytes() == (store._object(snapshot_id) / "tree" / rows[1]["path"]).read_bytes()
    assert not (work / "extra").exists()
    assert not (work / "untracked.tmp").exists()
    # The source snapshot is never writable through the gate's worktree.
    assert (store._object(snapshot_id) / "tree" / rows[0]["path"]).read_bytes() == original


def test_workspace_repairs_link_without_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, snapshot_id, rows = _snapshot_fixture(tmp_path, monkeypatch, 2)
    work = pre._prepare_cached_work(store, snapshot_id, None, "links")
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "keep.txt").write_text("external")
    link = work / "foreign"
    try:
        pre.link_dir(outside, link)
    except (OSError, PermissionError):
        pytest.skip("filesystem does not allow symlink/junction creation")
    pre._prepare_cached_work(store, snapshot_id, None, "links")
    assert not link.exists()
    assert (outside / "keep.txt").read_text() == "external"


def test_cached_workspace_single_walk_and_missing_source_on_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    store, snapshot_id, rows = _snapshot_fixture(tmp_path, monkeypatch, 5)
    work = pre._prepare_cached_work(store, snapshot_id, None, "single-walk")
    (work / "unexpected" / "nested").mkdir(parents=True)
    (work / "unexpected" / "nested" / "temp.txt").write_text("extra")
    original_walk = pre.os.walk
    walks = []

    def tracked_walk(*args, **kwargs):
        walks.append((Path(args[0]), kwargs.get("topdown", True)))
        return original_walk(*args, **kwargs)

    monkeypatch.setattr(pre.os, "walk", tracked_walk)
    pre._prepare_cached_work(store, snapshot_id, None, "single-walk")
    assert walks == [(work, True)]
    assert not (work / "unexpected").exists()
    source = store._object(snapshot_id) / "tree" / rows[0]["path"]
    source.unlink()
    # An intact workspace does not need to reread source snapshot bytes.
    pre._prepare_cached_work(store, snapshot_id, None, "single-walk")
    target = work / rows[0]["path"]
    target.write_text("changed")
    with pytest.raises(pre.SnapshotError, match="source symlink or missing"):
        pre._prepare_cached_work(store, snapshot_id, None, "single-walk")


def test_unchanged_workspace_uses_lstat_without_following_file_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    store, snapshot_id, rows = _snapshot_fixture(tmp_path, monkeypatch, 2)
    work = pre._prepare_cached_work(store, snapshot_id, None, "single-stat")
    target = work / rows[0]["path"]
    original_stat = Path.stat

    def tracked_stat(path, *args, **kwargs):
        if path == target and kwargs.get("follow_symlinks", True):
            raise AssertionError("unchanged cached file must not follow file links")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", tracked_stat)
    pre._prepare_cached_work(store, snapshot_id, None, "single-stat")


def test_parallel_workspace_checks_copy_only_on_main_thread(tmp_path, monkeypatch):
    store, snapshot_id, rows = _snapshot_fixture(tmp_path, monkeypatch, 3)
    work = pre._prepare_cached_work(store, snapshot_id, None, "parallel-work")
    corrupted = work / rows[0]["path"]
    corrupted.write_bytes(b"xxxx")
    monkeypatch.setattr(pre, "_WORKSPACE_PARALLEL_MIN_FILES", 1)
    original_copy = pre.shutil.copyfile
    copy_threads = []

    def track_copy(src, dest, *args, **kwargs):
        copy_threads.append(threading.current_thread().name)
        return original_copy(src, dest, *args, **kwargs)

    monkeypatch.setattr(pre.shutil, "copyfile", track_copy)
    pre._prepare_cached_work(store, snapshot_id, None, "parallel-work")
    assert copy_threads == [threading.main_thread().name]
    assert corrupted.read_bytes() == b"v=0\n"


def test_workspace_20000_file_cached_benchmark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Synthetic, not a live project: initial materialization vs later dirty repair."""
    if os.environ.get("FLEET_WORKSPACE_BENCH") != "1":
        pytest.skip("20k-file benchmark is opt-in (FLEET_WORKSPACE_BENCH=1)")
    store, snapshot_id, rows = _snapshot_fixture(tmp_path, monkeypatch, 20_000)
    t0 = time.perf_counter()
    work = pre._prepare_cached_work(store, snapshot_id, None, "benchmark")
    cold = time.perf_counter() - t0
    (work / "pytest.out").write_text("dirty fixture")
    (work / rows[10]["path"]).write_text("modified")
    t1 = time.perf_counter()
    pre._prepare_cached_work(store, snapshot_id, None, "benchmark")
    cached = time.perf_counter() - t1
    print(f"\nworkspace synthetic files=20000 cold={cold:.3f}s cached_dirty={cached:.3f}s")
    assert not (work / "pytest.out").exists()
    assert (work / rows[10]["path"]).read_bytes() == (
        store._object(snapshot_id) / "tree" / rows[10]["path"]).read_bytes()
    assert cached < cold, (cold, cached)
