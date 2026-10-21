"""TZ-146: transition regressions, using temporary Git and SQLite only."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest

from dev_mcp.fleet import goals, queue
from dev_mcp.fleet.claim_maintenance import (
    Audit, ClaimMaintenance, Finding, MAX_REPAIRS_PER_AUDIT,
)
from dev_mcp.fleet.claim_take_routing import resolve_claim_context
from dev_mcp.fleet.integrate import Batch, Integrator
from dev_mcp.fleet.store import Store


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "isolated project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Unit")
    git(root, "config", "user.email", "unit@example.invalid")
    (root / "src").mkdir()
    (root / "ToDo").mkdir()
    (root / "src" / "first.txt").write_text("first\n", encoding="utf-8")
    (root / "src" / "second.txt").write_text("second\n", encoding="utf-8")
    (root / "ToDo" / "legacy.md").write_text("base\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "base")
    store = Store.open(str(tmp_path / "registry.sqlite"))
    flags = {"commit": True, "claim_trees": True}
    monkeypatch.setattr(goals, "project_info", lambda _: flags)
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Fleet Platform", str(root)), event={"kind": "seed"})
        for worker in ("W-A", "W-B", "W-C", "W-R"):
            tx.change(
                "INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,?,?,?)",
                (worker, worker, "chatgpt", stamp, stamp), event={"kind": "seed"})

    def task(tz: str, rel: str, priority: int = 1):
        with store.tx() as tx:
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES('Fleet Platform',?,'test','ready',?,'work','','','[]',?,'[]','[]','',?,?)",
                (tz, priority, json.dumps([str(root / rel)]), stamp, stamp),
                event={"kind": "seed"})

    yield root, store, flags, task
    store.close()


def active_id(store: Store, worker: str) -> int:
    with store.read() as tx:
        return int(tx.scalar(
            "SELECT id FROM claims WHERE worker_id=? AND state='active'", (worker,)))


def test_dirty_todo_and_unrelated_source_allow_isolated_take(world):
    root, store, flags, task = world
    task("TZ-A", "src/first.txt")
    (root / "ToDo" / "legacy.md").write_text("controller changes\n")
    (root / "ToDo" / "new.md").write_text("generated draft\n")
    (root / "src" / "second.txt").write_text("unrelated dirty\n")
    taken = queue.take_next(store, "W-A", "Fleet Platform")
    assert taken and taken.tz == "TZ-A"
    ctx = resolve_claim_context(store, "W-A", active_id(store, "W-A"))
    assert ctx.mode == "isolated" and ctx.tree_path.is_dir()
    assert (ctx.tree_path / "src/first.txt").read_text() == "first\n"
    assert (root / "ToDo/new.md").exists()


def test_bad_checkout_skips_one_task_and_persists_refusal(world):
    root, store, flags, task = world
    task("TZ-A", "src/first.txt", priority=1)
    task("TZ-B", "src/second.txt", priority=2)
    (root / "src/first.txt").write_text("dirty own file\n")
    taken = queue.take_next(store, "W-A", "Fleet Platform")
    assert taken and taken.tz == "TZ-B"
    with store.read() as tx:
        refused = tx.query(
            "SELECT tz,payload FROM events WHERE kind='claim_take_refused'")
        assert len(refused) == 1 and refused[0]["tz"] == "TZ-A"
        assert "dirty trunk" in json.loads(refused[0]["payload"])["reason"]
        assert tx.scalar("SELECT state FROM tasks WHERE project='Fleet Platform' AND tz='TZ-A'") == "ready"


def test_existing_shared_claim_remains_shared_after_flag_and_integrates(world, tmp_path):
    root, store, flags, task = world
    task("TZ-OLD", "ToDo/legacy.md")
    flags["claim_trees"] = False
    assert queue.take_next(store, "W-A", "Fleet Platform").tz == "TZ-OLD"
    cid = active_id(store, "W-A")
    (root / "ToDo/legacy.md").write_text("old epoch accepted\n")
    with store.tx() as tx:
        tx.changes([
            ("UPDATE claims SET state='released',released_at=? WHERE id=?",
             (datetime.now(timezone.utc).isoformat(), cid)),
            ("UPDATE tasks SET state='accepted',author_worker='W-A' "
             "WHERE project='Fleet Platform' AND tz='TZ-OLD'", ()),
        ], event={"kind": "submit", "project": "Fleet Platform", "tz": "TZ-OLD", "worker_id": "W-A"})
        tx.record_event(kind="accept", project="Fleet Platform", tz="TZ-OLD", worker_id="W-R")
        acceptance = tx.one(
            "SELECT id FROM events WHERE kind='accept' AND tz='TZ-OLD'")
    flags["claim_trees"] = True
    # A fresh copy of the historical registry is used for integration, never
    # the live fleet registry. Copy the test fixture DB via SQLite backup.
    import sqlite3
    with sqlite3.connect(tmp_path / "copied-registry.sqlite") as replica:
        with sqlite3.connect(tmp_path / "registry.sqlite") as source:
            source.backup(replica)
    copy = Store.open(str(tmp_path / "copied-registry.sqlite"))
    try:
        # Active context is read from the same durable take receipt after
        # flipping the flag (temporarily restore the claim's active status).
        with store.tx() as tx:
            tx.change("UPDATE claims SET state='active' WHERE id=?", (cid,),
                      event={"kind": "fixture_probe"})
        assert resolve_claim_context(store, "W-A", cid).mode == "shared"
        before = git(root, "rev-parse", "HEAD")
        integrator = Integrator(copy, tmp_path / "cursor.json",
                                project_info=lambda _: flags)
        batch = Batch("Fleet Platform", str(root))
        output = integrator._commit(
            batch, {"id": int(acceptance["id"]), "tz": "TZ-OLD", "worker_id": "W-R"})
        assert output["payload"].get("commit"), output
        assert batch.legacy_accepts
        assert git(root, "rev-parse", "HEAD") != before
        assert git(root, "status", "--porcelain", "--", "ToDo/legacy.md") == ""
    finally:
        copy.close()


def test_maintenance_ignores_shared_epoch_and_bounded_repairs(world):
    root, store, flags, task = world
    task("TZ-OLD", "ToDo/legacy.md", priority=1)
    flags["claim_trees"] = False
    assert queue.take_next(store, "W-A", "Fleet Platform").tz == "TZ-OLD"
    old_id = active_id(store, "W-A")
    task("TZ-NEW", "src/first.txt")
    flags["claim_trees"] = True
    assert queue.take_next(store, "W-B", "Fleet Platform").tz == "TZ-NEW"
    new_id = active_id(store, "W-B")
    maint = ClaimMaintenance(store, settings=lambda _: flags)
    audit = maint.audit("Fleet Platform")
    assert audit.enabled, audit.error
    assert new_id in {x.claim_id for x in audit.findings}
    assert old_id not in {x.claim_id for x in audit.findings}
    assert not maint.repair_findings(audit)
    synthetic = Audit("Fleet Platform", True, findings=tuple(
        Finding("missing_tree", i + 1000, "Fleet Platform", str(root / f"missing-{i}"))
        for i in range(MAX_REPAIRS_PER_AUDIT + 9)))
    opened = maint.repair_findings(synthetic)
    assert len(opened) == MAX_REPAIRS_PER_AUDIT
    # New ticks cannot flood the queue while existing repairs are pending.
    assert maint.repair_findings(synthetic) == ()
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET state='accepted' WHERE project='Fleet Platform' "
            "AND tz IN (SELECT tz FROM tasks WHERE project='Fleet Platform' "
            "AND tz LIKE 'repair:claim-maintenance:%' LIMIT 9)",
            event={"kind": "fixture_repair_progress"})
    assert len(maint.repair_findings(synthetic)) == 9
    assert maint.repair_findings(synthetic) == ()
