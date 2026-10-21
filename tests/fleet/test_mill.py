from __future__ import annotations

import json
import subprocess
from pathlib import Path

from dev_mcp.fleet import mill


class FakeTx:
    def __init__(self, root, queued=()):
        self.root, self.queued = root, queued

    def one(self, sql, params):
        return {"root": self.root} if self.root else None

    def query(self, sql, params):
        return [{"tz": tz} for tz in self.queued]


class FakeStore:
    def __init__(self, root, queued=()):
        self.root, self.queued = root, queued

    def read(self):
        store = self

        class Ctx:
            def __enter__(self_inner):
                return FakeTx(store.root, store.queued)

            def __exit__(self_inner, *exc):
                return False
        return Ctx()


def mill_tree(tmp_path: Path) -> Path:
    (tmp_path / "mill").mkdir()
    (tmp_path / "mill" / "__main__.py").write_text("", encoding="utf-8")
    return tmp_path


def recorder(tick_rc=0, tick_out=None, dry_rc=0):
    calls = []

    def run(argv, cwd, timeout_s):
        calls.append(argv[4:])
        if argv[4:] == ["tick", "--dry"]:
            return subprocess.CompletedProcess(argv, dry_rc, "", "")
        if argv[4] == "tick":
            return subprocess.CompletedProcess(argv, tick_rc, json.dumps(tick_out or {}), "boom")
        return subprocess.CompletedProcess(argv, 0, "{}", "")
    return run, calls


def test_no_project_is_skipped():
    run, calls = recorder()
    assert mill.tick(FakeStore(None), run=run) == {"skipped": "no_mill_project"}
    assert calls == []


def test_without_dispatcher_nothing_is_collected(tmp_path):
    run, calls = recorder(dry_rc=2)
    assert mill.tick(FakeStore(str(mill_tree(tmp_path))), run=run) == {"skipped": "no_tick"}
    assert calls == [["tick", "--dry"]]


def test_runs_sources_then_tick_and_bootstraps_new_pairs(tmp_path, monkeypatch):
    seen = {}

    def fake_sync(store, project, root, tzs, *, policy=None):
        seen.update(project=project, root=root, tzs=list(tzs))
        return {"inserted": len(tzs), "errors": {}}
    monkeypatch.setattr("dev_mcp.fleet.spec.sync_selected", fake_sync)
    run, calls = recorder(tick_out={"new_tz": ["TZ-MA-b1", "TZ-MR-b1"], "open_pairs": 1})
    root = mill_tree(tmp_path)
    report = mill.tick(FakeStore(str(root)), run=run)
    assert calls == [["tick", "--dry"], ["run", "--sources", "fleet"],
                     ["run", "--sources", "claude_code"], ["tick"]]
    assert seen == {"project": "Mill Works", "root": str(root), "tzs": ["TZ-MA-b1", "TZ-MR-b1"]}
    assert report["new_tz"] == ["TZ-MA-b1", "TZ-MR-b1"]


def test_failed_tick_is_reported_not_bootstrapped(tmp_path):
    run, _ = recorder(tick_rc=1)
    report = mill.tick(FakeStore(str(mill_tree(tmp_path))), run=run)
    assert report["tick"].startswith("rc=1") and "bootstrap" not in report


def test_orphan_pair_files_are_enqueued(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr("dev_mcp.fleet.spec.sync_selected",
                        lambda store, project, root, tzs, *, policy=None: seen.setdefault("tzs", list(tzs)))
    root = mill_tree(tmp_path)
    tzdir = root / "ToDo" / "tz"
    tzdir.mkdir(parents=True)
    for name in ("TZ-MA-b0", "TZ-MR-b0", "TZ-MA-b9"):
        (tzdir / f"{name}.md").write_text("x", encoding="utf-8")
    run, _ = recorder(tick_out={"new_tz": []})
    report = mill.tick(FakeStore(str(root), queued=("TZ-MA-b9",)), run=run)
    assert seen["tzs"] == ["TZ-MA-b0", "TZ-MR-b0"]
    assert report["new_tz"] == ["TZ-MA-b0", "TZ-MR-b0"]
