"""Scoped collection and observable timings for selected TZ bootstrap."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

from dev_mcp.fleet import exec_preflight as pre
from dev_mcp.fleet.gate_snapshot import SnapshotStore
from dev_mcp.fleet.spec import _pytest_collection_targets


def test_selected_pytest_paths_only_and_missing_future_tests(tmp_path):
    path = tmp_path / "test" / "test_ready.py"
    path.parent.mkdir()
    path.write_text("def test_ready(): pass\n", encoding="utf-8")
    command = "py -3 -m pytest -q test/test_ready.py::test_ready test/test_future.py"
    assert _pytest_collection_targets(command, tmp_path) == ("test/test_ready.py",)
    assert _pytest_collection_targets(
        "py -3 -m pytest -q test/test_future.py", tmp_path) == ()
    assert _pytest_collection_targets("py -3 -m pytest -q", tmp_path) is None
    assert _pytest_collection_targets("py -3 script.py", tmp_path) is None


def test_timed_preflight_tracks_red_rc_and_narrow_collection(tmp_path):
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_one.py").write_text("def test_one(): assert False\n", encoding="utf-8")
    calls = []
    events = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "--collect-only" in argv:
            return SimpleNamespace(returncode=0, stdout="collected", stderr="")
        return SimpleNamespace(returncode=1, stdout="FAILED tests/test_one.py", stderr="")

    result = pre.preflight_gate(
        "python -m pytest tests/test_one.py", source_root=root,
        snapshots=SnapshotStore(tmp_path / "snap"), runner=runner,
        python=sys.executable, acceptance_red=True,
        collection_targets=("tests/test_one.py",),
        timing=lambda stage, seconds, details: events.append((stage, seconds, details)),
    )
    assert result.ok and result.reason == "baseline_red"
    assert result.launch.returncode == 1
    assert "tests/test_one.py" in calls[-1]
    assert {"snapshot", "workspace", "acceptance", "collection"} <= {
        stage for stage, _, _ in events}
    assert next(data for stage, _, data in events if stage == "acceptance")["rc"] == 1
    assert all(seconds >= 0 for _, seconds, _ in events)


def test_missing_test_skips_collection_but_keeps_baseline(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="FAILED test_future", stderr="")

    events = []
    result = pre.preflight_gate(
        "python -m pytest tests/test_future.py", source_root=root,
        snapshots=SnapshotStore(tmp_path / "snap"), runner=runner,
        acceptance_red=True, collection_targets=(),
        timing=lambda stage, seconds, details: events.append((stage, details)),
    )
    assert result.reason == "baseline_red"
    assert len(calls) == 1
    assert ("collection", {"skipped": True}) in events


def test_source_timing_distinguishes_cold_and_cached(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("value = 1\n")
    monkeypatch.setattr(pre, "_git_visible", lambda _: None)
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    state = {}
    pre._cached_source(root, SnapshotStore(tmp_path / "legacy"), None,
                       cache_status=state)
    assert state["source"] == "cold"
    pre._cached_source(root, SnapshotStore(tmp_path / "legacy"), None,
                       cache_status=state)
    assert state["source"] == "cache"


def test_side_timing_excludes_snapshot_creation(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "test.py").write_text("value = 1\n")
    (root / "asset.png").write_bytes(b"asset")
    monkeypatch.setattr(pre, "_git_visible", lambda _: ["test.py", "asset.png"])
    monkeypatch.setattr(pre, "_MAX_FILES", 1)
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    original_create = SnapshotStore.create

    def delayed_create(self, *args, **kwargs):
        time.sleep(0.15)
        return original_create(self, *args, **kwargs)

    monkeypatch.setattr(SnapshotStore, "create", delayed_create)
    status = {}
    _, _, side = pre._cached_source(root, SnapshotStore(tmp_path / "legacy"), None,
                                    cache_status=status)
    assert side is not None
    assert status["create_s"] >= 0.14
    assert 0 <= status["side_s"] < status["create_s"]


def test_batch_snapshot_reused_only_while_owned_bytes_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    owned = root / "owned.py"
    owned.write_text("original = True\n")
    monkeypatch.setattr(pre, "_git_visible", lambda _: None)
    monkeypatch.setattr(pre, "_PREFLIGHT_CACHE_ROOT", tmp_path / "cache")
    source_session = {}

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def probe():
        entries = []
        result = pre.preflight_gate(
            "python -m py_compile owned.py", source_root=root,
            snapshots=SnapshotStore(tmp_path / "unused"),
            runner=runner, cache_source=True,
            critical_paths=("owned.py",), source_session=source_session,
            collection_targets=(),
            timing=lambda stage, seconds, details: entries.append((stage, details)),
        )
        assert result.ok
        return next(details["source"] for stage, details in entries
                    if stage == "snapshot")

    assert probe() == "cold"
    assert probe() == "session"
    owned.write_text("modified = True\n")
    assert probe() == "incremental"
