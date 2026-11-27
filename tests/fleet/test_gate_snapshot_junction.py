"""A source junction must never allow gate snapshot capture outside the tree."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from dev_mcp.fleet.gate_snapshot import SnapshotError, SnapshotStore


def _junction(source: Path, target: Path) -> Path:
    link = source / "linked"
    if os.name == "nt":
        from dev_mcp.fleet.exec_preflight import link_dir
        link_dir(target, link)
    else:
        link.symlink_to(target, target_is_directory=True)
    return link


def test_source_junction_excluded_from_walk_and_rejected_explicitly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.py").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.py").write_text("never copy", encoding="utf-8")
    link = _junction(source, outside)
    store = SnapshotStore(tmp_path / "snapshots")
    try:
        ref = store.create(source)
        assert store.manifest(ref.snapshot_id)["files"][0]["path"] == "safe.py"
        assert ref.file_count == 1
        with pytest.raises(SnapshotError, match="link|junction|reparse"):
            store.create(source, include=["linked/private.py"])
    finally:
        from dev_mcp.fleet.exec_preflight import unlink_dir
        unlink_dir(link)
    assert (outside / "private.py").read_text(encoding="utf-8") == "never copy"


def test_junction_component_is_rejected_even_if_it_looks_like_directory(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    (source / "linked").mkdir(parents=True)
    (source / "safe.py").write_text("safe", encoding="utf-8")
    (source / "linked" / "private.py").write_text("secret", encoding="utf-8")
    original = getattr(Path, "is_junction", None)

    def fake_junction(path: Path) -> bool:
        return path == source / "linked" or (original(path) if original else False)

    monkeypatch.setattr(Path, "is_junction", fake_junction, raising=False)
    store = SnapshotStore(tmp_path / "snapshots")
    with pytest.raises(SnapshotError, match="link|junction|reparse"):
        store.create(source, include=["linked/private.py"])
    assert [x["path"] for x in store.manifest(store.create(source).snapshot_id)["files"]] == ["safe.py"]


def test_source_root_must_not_be_junction(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.py").write_text("secret", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    link = _junction(home, outside)
    try:
        with pytest.raises(SnapshotError, match="real directory"):
            SnapshotStore(tmp_path / "snapshots").create(link)
    finally:
        from dev_mcp.fleet.exec_preflight import unlink_dir
        unlink_dir(link)


def test_incremental_capture_refuses_replaced_source_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "linked").mkdir(parents=True)
    (source / "linked" / "file.py").write_text("before", encoding="utf-8")
    store = SnapshotStore(tmp_path / "snapshots")
    base = store.create(source)
    old = source / "linked"
    old.rename(source / "moved")
    link = _junction(source, source / "moved")
    try:
        # Versions matching the prior manifest used to skip _safe_source.
        versions = {"linked/file.py": (6, 1, 1)}
        with pytest.raises(SnapshotError, match="link|junction|reparse"):
            store.create_incremental(
                source, ["linked/file.py"], base_snapshot=base.snapshot_id,
                base_versions=versions, current_versions=versions,
            )
    finally:
        from dev_mcp.fleet.exec_preflight import unlink_dir
        unlink_dir(link)
