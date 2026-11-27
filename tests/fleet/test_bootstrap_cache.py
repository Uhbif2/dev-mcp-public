"""Bootstrap source-cache regression: no mutable-tree reads during execution."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from dev_mcp.fleet import exec_preflight as pre
from dev_mcp.fleet.gate_snapshot import SnapshotStore


def _source(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_future.py").write_text("def test_future(): assert False\n")
    (root / "asset.png").write_bytes(b"first")
    monkeypatch.setattr(pre, "_git_visible", lambda _: [
        "tests/test_future.py", "asset.png"])
    monkeypatch.setattr(pre, "_MAX_FILES", 1)
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    return root


def test_non_git_project_reuses_snapshots(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.py").write_text("value = 1\n")
    monkeypatch.setattr(pre, "_git_visible", lambda _: None)
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    one = pre._cached_source(root, SnapshotStore(tmp_path / "legacy"), None)
    two = pre._cached_source(root, SnapshotStore(tmp_path / "legacy"), None)
    assert one[1] == two[1]
    (root / "a.py").write_text("value = 2\n")
    three = pre._cached_source(root, SnapshotStore(tmp_path / "legacy"), None)
    assert one[1] != three[1]


def test_snapshot_reused_until_any_source_file_changes(tmp_path, monkeypatch):
    root = _source(tmp_path, monkeypatch)
    store = SnapshotStore(tmp_path / "transient")
    one = pre._cached_source(root, store, None)
    two = pre._cached_source(root, store, None)
    assert one[1] == two[1]
    assert one[2] == two[2]
    assert (one[2] / "asset.png").read_bytes() == b"first"
    (root / "asset.png").write_bytes(b"second")
    three = pre._cached_source(root, store, None)
    assert three[2] != one[2]
    assert (three[2] / "asset.png").read_bytes() == b"second"
    (root / "tests" / "test_future.py").write_text("def test_future(): assert True\n")
    four = pre._cached_source(root, store, None)
    assert four[1] != three[1]


def test_prepared_workspace_reused_and_restored(tmp_path, monkeypatch):
    root = _source(tmp_path, monkeypatch)
    store = SnapshotStore(tmp_path / "transient")
    cached, snapshot_id, side = pre._cached_source(root, store, None)
    work1 = pre._prepare_cached_work(cached, snapshot_id, side, side.name)
    assert (work1 / "asset.png").read_bytes() == b"first"
    (work1 / "asset.png").write_bytes(b"modified by acceptance")
    (work1 / "tests" / "test_future.py").unlink()
    (work1 / "tests" / "test_leak.py").write_text("leak")
    work2 = pre._prepare_cached_work(cached, snapshot_id, side, side.name)
    assert work1 == work2
    assert (work2 / "asset.png").read_bytes() == b"first"
    assert (work2 / "tests" / "test_future.py").is_file()
    assert not (work2 / "tests" / "test_leak.py").exists()
    # Restore mtime after a same-size mutation: on NTFS the USN still changes;
    # on unsupported filesystems the fallback content hash detects it.
    import os
    target = work2 / "asset.png"
    before = target.stat()
    usn_before = pre._file_usn(target)
    target.write_bytes(b"other")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    usn_after = pre._file_usn(target)
    if usn_before is not None and usn_after is not None:
        assert usn_before != usn_after
    pre._prepare_cached_work(cached, snapshot_id, side, side.name)
    assert target.read_bytes() == b"first"


def test_red_is_allowed_only_when_explicitly_requested(tmp_path, monkeypatch):
    root = _source(tmp_path, monkeypatch)
    monkeypatch.setattr(pre, "_MAX_FILES", 4000)
    def runner(argv, **kwargs):
        if "--collect-only" in argv:
            return SimpleNamespace(returncode=0, stdout="collected", stderr="")
        return SimpleNamespace(returncode=1, stdout="FAILED test_future", stderr="")
    store = SnapshotStore(tmp_path / "objects")
    red = pre.preflight_gate(
        "python -m pytest tests/test_future.py", source_root=root,
        snapshots=store, runner=runner, acceptance_red=True)
    strict = pre.preflight_gate(
        "python -m pytest tests/test_future.py", source_root=root,
        snapshots=store, runner=runner)
    assert red.ok and red.reason == "baseline_red" and red.launch.returncode == 1
    assert not strict.ok and strict.reason == "gate_launch_failed"


def test_red_rejects_interpreter_import_and_cli_errors(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for output, code in [("ImportError: broken", 1),
                         ("SyntaxError: invalid syntax", 1),
                         ("unrecognized arguments: --x", 4)]:
        def runner(argv, **kwargs):
            return SimpleNamespace(returncode=code, stdout="", stderr=output)
        result = pre._probe(
            "python -m pytest test_new.py", work, work, None, runner,
            sys.executable, 3, None, "a" * 64, False, acceptance_red=True)
        assert not result.ok, output


def test_collection_cached_only_for_same_immutable_version(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        return pre.ExecutionResult(tuple(argv) if not isinstance(argv, str) else (argv,),
                                   0, "collected", "")
    monkeypatch.setattr(pre, "run_command", command)
    kwargs = dict(
        command="python -m pytest", work=work, root=work, binary_root=None,
        runner=subprocess.run, python=sys.executable, timeout_s=10, windows=False,
        snapshot_id="a" * 64, tests_present=True,
        collection_cache=tmp_path / "objects",
    )
    pre._probe(**kwargs)
    pre._probe(**kwargs)
    assert len(calls) == 3  # 2 acceptance probes, 1 collection
    pre._probe(**(kwargs | {"snapshot_id": "b" * 64}))
    assert len(calls) == 5


def test_failed_collection_reused_without_masking_failure(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if isinstance(argv, list) and "--collect-only" in argv:
            return pre.ExecutionResult(tuple(argv), 2, "", "ImportError: bad module")
        return pre.ExecutionResult(("python",), 0, "", "")
    monkeypatch.setattr(pre, "run_command", command)
    kwargs = dict(
        command="python -m pytest", work=work, root=work, binary_root=None,
        runner=subprocess.run, python=sys.executable, timeout_s=10, windows=False,
        snapshot_id="a" * 64, tests_present=True,
        collection_cache=tmp_path / "objects",
    )
    assert not pre._probe(**kwargs).ok
    assert not pre._probe(**kwargs).ok
    assert len(calls) == 3  # still runs each gate, but collect is once


def test_red_does_not_turn_submit_green(tmp_path):
    from dev_mcp.fleet.exec_preflight import ExecutionResult
    result = ExecutionResult(("python", "-m", "pytest"), 1, "", "failed")
    assert not result.ok


def test_unrelated_edit_during_snapshot_does_not_fail_or_poison_index(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "owned.py").write_text("owned = 1\n")
    (root / "unrelated.py").write_text("other = 1\n")
    monkeypatch.setattr(pre, "_git_visible", lambda _: ["owned.py", "unrelated.py"])
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    original = SnapshotStore.create

    def race(self, source, include=None, **kwargs):
        snap = original(self, source, include=include, **kwargs)
        (root / "unrelated.py").write_text("other = 2\n")
        return snap

    with monkeypatch.context() as patch:
        patch.setattr(SnapshotStore, "create", race)
        store, snap_id, side = pre._cached_source(
            root, SnapshotStore(tmp_path / "throwaway"), None, ("owned.py",))
    assert side is None
    assert store.verify(snap_id).file_count == 2
    assert not list((tmp_path / "cache" / "index").glob("*")) if (
        tmp_path / "cache" / "index").is_dir() else True
    second = pre._cached_source(root, SnapshotStore(tmp_path / "throwaway"), None,
                                ("owned.py",))
    assert second[1] != snap_id


def test_owned_edit_during_snapshot_remains_strict(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "owned.py").write_text("before\n")
    (root / "other.py").write_text("fixed\n")
    monkeypatch.setattr(pre, "_git_visible", lambda _: ["owned.py", "other.py"])
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    original = SnapshotStore.create

    def race(self, source, include=None, **kwargs):
        result = original(self, source, include=include, **kwargs)
        (root / "owned.py").write_text("after\n")
        return result

    import pytest
    from dev_mcp.fleet.gate_snapshot import SnapshotError
    with monkeypatch.context() as patch:
        patch.setattr(SnapshotStore, "create", race)
        with pytest.raises(SnapshotError, match="owned/acceptance"):
            pre._cached_source(root, SnapshotStore(tmp_path / "throwaway"), None,
                               ("owned.py",))


def test_side_capture_does_not_fail_when_unowned_media_changes(tmp_path, monkeypatch):
    root = _source(tmp_path, monkeypatch)
    original = pre._copy_side_media

    def race(source, work, names):
        original(source, work, names)
        (root / "asset.png").write_bytes(b"changed")
    with monkeypatch.context() as patch:
        patch.setattr(pre, "_copy_side_media", race)
        store, snapshot, side = pre._cached_source(
            root, SnapshotStore(tmp_path / "throwaway"), None,
            ("tests/test_future.py",))
    assert store.verify(snapshot)
    assert side is not None and (side / "asset.png").read_bytes() == b"first"
    after = pre._cached_source(root, SnapshotStore(tmp_path / "throwaway"), None,
                               ("tests/test_future.py",))
    assert after[2] != side
    assert (after[2] / "asset.png").read_bytes() == b"changed"
