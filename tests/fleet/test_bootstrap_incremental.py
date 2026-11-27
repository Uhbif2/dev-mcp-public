"""Incremental bootstrap must preserve cold snapshot semantics and workspace isolation."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dev_mcp.fleet import exec_preflight as pre
from dev_mcp.fleet.gate_snapshot import SnapshotError, SnapshotStore


def _versions(root: Path, files: list[str]) -> dict[str, list[int]]:
    return {name: [st.st_size, st.st_mtime_ns, st.st_ctime_ns]
            for name in files if (st := (root / name).stat())}


def test_incremental_manifest_identical_to_full_with_add_edit_delete(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    for number in range(40):
        (source / "nested" / f"{number:03}.py").write_text(f"value = {number}\n")
    names = sorted(path.relative_to(source).as_posix() for path in source.rglob("*.py"))
    store = SnapshotStore(tmp_path / "objects")
    base = store.create(source, include=names)
    versions = _versions(source, names)
    for number in (1, 3, 5, 7, 9):
        (source / "nested" / f"{number:03}.py").write_text(f"edited = {number}\n")
    (source / "nested" / "000.py").unlink()
    (source / "nested" / "new.py").write_text("new = 1\n")
    next_names = sorted(path.relative_to(source).as_posix()
                        for path in source.rglob("*.py"))
    changed = store.create_incremental(
        source, next_names, base_snapshot=base.snapshot_id, base_versions=versions)
    cold = SnapshotStore(tmp_path / "cold").create(source, include=next_names)
    assert changed == cold
    assert store.manifest(changed.snapshot_id) == SnapshotStore(tmp_path / "cold").manifest(cold.snapshot_id)
    assert store.verify(changed.snapshot_id) == changed
    assert store.verify(base.snapshot_id) == base
    assert not (store._object(changed.snapshot_id) / "tree" / "nested" / "000.py").exists()
    assert (store._object(changed.snapshot_id) / "tree" / "nested" / "new.py").read_text() == "new = 1\n"


def test_incremental_workspace_reuses_directory_and_rejects_dirty_files(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    for number in range(15):
        (root / f"{number:02}.py").write_text(f"item = {number}\n")
    monkeypatch.setattr(pre, "_git_visible", lambda _: None)
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    one, first, _ = pre._cached_source(root, SnapshotStore(tmp_path / "unused"), None)
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    work = pre._prepare_cached_work(one, first, None, key)
    (root / "01.py").write_text("item = 101\n")
    (root / "02.py").unlink()
    (root / "new.py").write_text("brand_new = True\n")
    two, second, _ = pre._cached_source(root, SnapshotStore(tmp_path / "unused"), None)
    assert first != second
    updated = pre._prepare_cached_work(two, second, None, key)
    assert updated == work
    assert (updated / "01.py").read_text() == "item = 101\n"
    assert not (updated / "02.py").exists()
    assert (updated / "new.py").is_file()
    # A dirty existing file is not a valid incremental base.
    (updated / "03.py").write_text("item = 999\n")
    (updated / "alien.py").write_text("alien\n")
    again = pre._prepare_cached_work(two, second, None, key)
    assert again == updated
    assert not (again / "alien.py").exists()
    assert (again / "03.py").read_text() == "item = 3\n"
    assert (again / "01.py").read_text() == "item = 101\n"


def test_side_cache_is_incremental_and_deps_not_included(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "test_run.py").write_text("def test_ok(): pass\n")
    (root / "asset.png").write_bytes(b"first")
    (root / "vendor").mkdir()
    (root / "vendor" / "lib.py").write_text("vendor = True\n")
    monkeypatch.setattr(pre, "_git_visible", lambda _: ["test_run.py", "asset.png"])
    monkeypatch.setattr(pre, "_MAX_FILES", 1)
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    snapshots = SnapshotStore(tmp_path / "unused")
    store, snap_a, side_a = pre._cached_source(root, snapshots, None)
    assert side_a is not None and (side_a / "asset.png").read_bytes() == b"first"
    (root / "test_run.py").write_text("def test_ok(): assert 1\n")
    _, snap_b, side_b = pre._cached_source(root, snapshots, None)
    assert snap_a != snap_b and side_b == side_a
    (root / "asset.png").write_bytes(b"second")
    _, snap_c, side_c = pre._cached_source(root, snapshots, None)
    assert snap_c == snap_b and side_c != side_b
    assert (side_c / "asset.png").read_bytes() == b"second"
    assert "vendor/lib.py" not in [row["path"] for row in store.manifest(snap_c)["files"]]
    assert pre.project_deps(root, ("vendor",))["vendor"] == root / "vendor"


def test_side_overlay_resolves_unchanged_bytes_from_prior_generation(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "test_run.py").write_text("def test_ok(): pass\n")
    (root / "asset.png").write_bytes(b"one")
    (root / "notes.md").write_text("important")
    monkeypatch.setattr(pre, "_git_visible", lambda _: ["test_run.py", "asset.png", "notes.md"])
    monkeypatch.setattr(pre, "_MAX_FILES", 1)
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    obj = SnapshotStore(tmp_path / "unused")
    first_store, first_id, first_side = pre._cached_source(root, obj, None)
    assert first_side is not None
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    first_work = pre._prepare_cached_work(first_store, first_id, first_side, key)
    assert (first_work / "notes.md").read_text() == "important"
    (root / "asset.png").write_bytes(b"two")
    next_store, next_id, next_side = pre._cached_source(root, obj, None)
    assert next_side != first_side
    assert not (next_side / "notes.md").exists()
    assert (next_side / ".fleet-side-origins.json").is_file()
    next_work = pre._prepare_cached_work(next_store, next_id, next_side, key)
    assert next_work == first_work
    assert (next_work / "asset.png").read_bytes() == b"two"
    assert (next_work / "notes.md").read_text() == "important"
    # Two overlay generations resolve directly to the original content owner.
    (root / "asset.png").write_bytes(b"three")
    _, snapshot, third_side = pre._cached_source(root, obj, None)
    pre._prepare_cached_work(next_store, snapshot, third_side, key)
    assert (next_work / "asset.png").read_bytes() == b"three"
    assert (next_work / "notes.md").read_text() == "important"


def test_incremental_refuses_symlink_inside_base_snapshot(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "a").mkdir()
    (src / "a" / "safe").write_text("safe")
    store = SnapshotStore(tmp_path / "objects")
    base = store.create(src)
    other = tmp_path / "other"
    other.mkdir()
    (other / "safe").write_text("outside")
    directory = store._object(base.snapshot_id) / "tree" / "a"
    # Inject a symlink in the old content-addressed tree.
    (directory / "safe").chmod(0o666)
    (directory / "safe").unlink()
    directory.rmdir()
    try:
        directory.symlink_to(other, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(SnapshotError, match="linked base"):
        store.create_incremental(
            src, ["a/safe"], base_snapshot=base.snapshot_id,
            base_versions=_versions(src, ["a/safe"]))
