from __future__ import annotations
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dev_mcp.fleet.gate_snapshot import SnapshotStore, SnapshotError


def test_deterministic_immutable_and_workspace_isolated(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    file = source / "nested" / "a.txt"
    file.write_text("one", encoding="utf-8")
    store = SnapshotStore(tmp_path / "objects")
    a = store.create(source)
    assert a.file_count == 1 and a.byte_count == 3
    assert a.snapshot_id == a.subject_version == store.create(source).snapshot_id
    work = store.materialize(a.snapshot_id, tmp_path / "workspace")
    (work / "nested" / "a.txt").write_text("modified")
    file.write_text("two")
    assert store.verify(a.snapshot_id) == a
    assert (store.root / a.snapshot_id / "tree" / "nested" / "a.txt").read_text() == "one"
    assert store.create(source).snapshot_id != a.snapshot_id


def test_reject_empty_traversal_duplicate_and_missing(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    store = SnapshotStore(tmp_path / "objects")
    with pytest.raises(SnapshotError, match="empty"):
        store.create(source)
    (source / "a").write_text("x")
    for name in ("../a", "a/../a", "/a", "C:/a", "nested\\a", "missing"):
        with pytest.raises(SnapshotError):
            store.create(source, [name])
    with pytest.raises(SnapshotError):
        store.create(source, ["a", "a"])
    for bad_id in ("../../bad", "123", "f" * 65):
        with pytest.raises(SnapshotError):
            store.verify(bad_id)


def test_detect_modified_snapshot_and_refuse_overwrite(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"original")
    store = SnapshotStore(tmp_path / "objects")
    snapshot = store.create(source)
    target = store.root / snapshot.snapshot_id / "tree" / "a"
    target.chmod(0o644)
    target.write_bytes(b"tampered")
    with pytest.raises(SnapshotError, match="digest"):
        store.verify(snapshot.snapshot_id)
    with pytest.raises(SnapshotError):
        store.create(source)


def test_symlink_refused(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    link = source / "shortcut"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(SnapshotError, match="symlink"):
        SnapshotStore(tmp_path / "objects").create(source, ["shortcut"])


def test_snapshot_rejects_injected_directory_symlink_without_copying_secrets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("safe")
    store = SnapshotStore(tmp_path / "objects")
    ref = store.create(source)
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("must never be copied")
    injected = store.root / ref.snapshot_id / "tree" / "foreign"
    try:
        injected.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(SnapshotError, match="symlink"):
        store.verify(ref.snapshot_id)
    workspace = tmp_path / "workspace"
    with pytest.raises(SnapshotError):
        store.materialize(ref.snapshot_id, workspace)
    assert not workspace.exists()


def test_snapshot_rejects_unmanifested_empty_directory(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "safe.txt").write_text("safe")
    store = SnapshotStore(tmp_path / "objects")
    ref = store.create(source)
    (store.root / ref.snapshot_id / "tree" / "foreign").mkdir()
    with pytest.raises(SnapshotError, match="unexpected snapshot directory"):
        store.verify(ref.snapshot_id)


def test_secrets_excluded_by_default_and_rejected_explicitly(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "module.py").write_text("print(1)")
    (source / ".env").write_text("API_KEY=never-store-this")
    (source / "client.pem").write_text("certificate")
    store = SnapshotStore(tmp_path / "objects")
    result = store.create(source)
    assert result.file_count == 1
    for restricted in (".env", "client.pem"):
        with pytest.raises(SnapshotError, match="secret"):
            store.create(source, [restricted])
