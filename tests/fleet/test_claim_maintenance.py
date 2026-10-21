"""TZ-43: no operation uses a live repo; all Git trees and Store DB are temporary."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from dev_mcp.fleet import goals, queue, review
from dev_mcp.fleet.claim_accept_flow import ClaimAcceptFlow
from dev_mcp.fleet.claim_maintenance import ClaimMaintenance
from dev_mcp.fleet.claim_review_bite import (
    accept_claim, capture_submission, reviewer_run,
)
from dev_mcp.fleet.claim_take_routing import resolve_claim_context
from dev_mcp.fleet.claim_tree import ClaimWorktrees
from dev_mcp.fleet.store import Store


def git(root: Path, *args: str, check: bool = True) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30)
    if check:
        assert process.returncode == 0, (args, process.stdout, process.stderr)
    return process.stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "Проект с пробелом"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Maintainer")
    git(root, "config", "user.email", "maintainer@example.invalid")
    (root / "file.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "file.txt")
    git(root, "commit", "-m", "base")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    monkeypatch.setattr(goals, "project_info",
                        lambda _: {"commit": True, "claim_trees": True})
    now = "2026-10-01T00:00:00+00:00"
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Unit", str(root)), event={"kind": "seed"})
        for w in ("W-A", "W-B", "W-C", "W-D", "W-R", "W-S", "W-T"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen)"
                      " VALUES(?,?,?,?,?)", (w, w, "chatgpt", now, now),
                      event={"kind": "seed"})
        for tz in ("TZ-A", "TZ-B", "TZ-C", "TZ-D"):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES('Unit',?,'test','ready',1,'work','','','[]',?,'[]',?,'',?,?)",
                (tz, json.dumps([str(root / "file.txt")]),
                 json.dumps(["python tests/pass.py"]), now, now),
                event={"kind": "seed"})
    future = datetime.now(timezone.utc) + timedelta(days=10)
    maint = ClaimMaintenance(
        store, settings=lambda _: {"commit": True, "claim_trees": True},
        clock=lambda: future)
    yield root, store, maint
    store.close()


def taken(store, who: str, tz: str):
    assignment = queue.take_next(store, who, "Unit")
    assert assignment and assignment.tz == tz
    with store.read() as tx:
        claim_id = int(tx.scalar(
            "SELECT id FROM claims WHERE worker_id=? AND state='active' "
            "ORDER BY id DESC LIMIT 1", (who,)))
    return claim_id, resolve_claim_context(store, who, claim_id)


def submit(store, claim_id):
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        c = tx.one("SELECT project,tz,worker_id FROM claims WHERE id=?", (claim_id,))
        tx.changes([
            ("UPDATE tasks SET state='submitted',author_worker=? "
             "WHERE project=? AND tz=?", (c["worker_id"], c["project"], c["tz"])),
            ("UPDATE claims SET state='released',released_at=? WHERE id=?",
             (now, claim_id)),
        ], event={"kind": "submit", "project": c["project"], "tz": c["tz"],
                  "worker_id": c["worker_id"], "payload": {"claim_id": claim_id}})


def accepted(store, cid, tz, reviewer):
    review.open_review(store, "Unit", tz)
    assignment = queue.take_next(store, reviewer, "Unit")
    assert assignment and assignment.tz.startswith("review:")
    result = reviewer_run(store, cid, reviewer, ["python", "tests/pass.py"],
                          python_executable=Path(sys.executable))
    assert result.rc == 0
    assert accept_claim(store, cid, reviewer) is not None
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Unit' AND tz=?",
                  (tz,), event={"kind": "accept", "project": "Unit",
                                "tz": tz, "worker_id": reviewer})


def green_claim(env):
    root, store, maint = env
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "pass.py").write_text("raise SystemExit(0)\n")
    git(root, "add", "tests/pass.py")
    git(root, "commit", "-m", "test machinery")
    cid, ctx = taken(store, "W-A", "TZ-A")
    (ctx.tree_path / "file.txt").write_text("approved\n", encoding="utf-8")
    sub = capture_submission(store, cid, "W-A")
    submit(store, cid)
    accepted(store, cid, "TZ-A", "W-R")
    outcome = ClaimAcceptFlow(store,
                              settings=lambda _: {"commit": True, "claim_trees": True},
                              gate=lambda _: 0).on_accept(cid, sub.revision)
    assert outcome.status == "integrated" and outcome.gate_rc == 0
    return cid, ctx, sub


def cutoff(maint):
    return maint.clock() - timedelta(days=2)


def by_id(audit, cid):
    return next(item for item in audit.findings if item.claim_id == cid)


def test_green_accepted_dryrun_then_core_close_preserves_trunk(env):
    root, store, maint = env
    cid, ctx, _ = green_claim(env)
    trunk = git(root, "rev-parse", "HEAD")
    # A clean claim is required. Restore source checkout to its uncommitted
    # original version; the accepted content remains in the integrated trunk.
    (ctx.tree_path / "file.txt").write_text("base\n", encoding="utf-8")
    assert git(ctx.tree_path, "status", "--porcelain") == ""
    report = maint.audit("Unit", cutoff=cutoff(maint))
    finding = by_id(report, cid)
    assert finding.kind == "terminal_ready_to_cleanup" and finding.eligible
    assert finding.integrated_sha == trunk
    assert finding.accepted_sha and finding.finished_at
    plan = maint.plan("Unit", older_than=cutoff(maint))
    assert tuple(c.claim_id for c in plan.candidates) == (cid,)
    assert ctx.tree_path.exists()
    result = maint.execute(plan)
    assert result.removed == (cid,) and not result.held and not result.errors
    assert not ctx.tree_path.exists()
    assert git(root, "rev-parse", "HEAD") == trunk
    assert (root / "file.txt").read_text() == "approved\n"
    assert maint.plan("Unit", older_than=cutoff(maint)).candidates == ()
    assert maint.execute(plan).removed == ()


def test_dirty_terminal_and_fresh_terminal_never_removed(env):
    root, store, maint = env
    cid, ctx, _ = green_claim(env)
    report = maint.audit("Unit", cutoff=cutoff(maint))
    f = by_id(report, cid)
    assert f.kind != "terminal_ready_to_cleanup" and f.dirty is True
    assert not maint.plan("Unit", older_than=cutoff(maint)).candidates
    (ctx.tree_path / "file.txt").write_text("base\n")
    finished_at = by_id(maint.audit("Unit", cutoff=cutoff(maint)), cid).finished_at
    before_finish = datetime.fromisoformat(finished_at) - timedelta(seconds=1)
    assert by_id(maint.audit("Unit", cutoff=before_finish), cid).kind != "terminal_ready_to_cleanup"
    assert ctx.tree_path.exists()


def test_active_taken_submitted_and_untrusted_abandoned_survive(env):
    root, store, maint = env
    a, actx = taken(store, "W-A", "TZ-A")
    first = by_id(maint.audit("Unit", cutoff=cutoff(maint)), a)
    assert first.kind == "active" and first.lease_state == "active"
    submit(store, a)
    # No independent reviewer acceptance; no cleanup even with old timestamps.
    assert not by_id(maint.audit("Unit", cutoff=cutoff(maint)), a).eligible
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='abandoned' WHERE project='Unit' AND tz='TZ-A'",
                  event={"kind": "agent-note", "project": "Unit", "tz": "TZ-A",
                         "worker_id": "W-A", "payload": {"abandoned": True}})
    assert not by_id(maint.audit("Unit", cutoff=cutoff(maint)), a).eligible
    assert actx.tree_path.exists()
    assert not maint.plan("Unit", older_than=cutoff(maint)).candidates


def test_authorized_abandoned_only_clean_merged_branch(env):
    root, store, maint = env
    cid, ctx = taken(store, "W-A", "TZ-A")
    submit(store, cid)
    finish = (maint.clock() - timedelta(days=3)).isoformat()
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='abandoned' WHERE project='Unit' AND tz='TZ-A'",
                  event={"kind": "claim_abandon_authorized", "project": "Unit", "tz": "TZ-A",
                         "payload": {"claim_id": cid, "authorized": True,
                                     "authority": "fleet-controller",
                                     "finished_at": finish}})
    assert by_id(maint.audit("Unit", cutoff=cutoff(maint)), cid).eligible
    (ctx.tree_path / "dirty").write_text("retain!")
    assert not by_id(maint.audit("Unit", cutoff=cutoff(maint)), cid).eligible
    (ctx.tree_path / "dirty").unlink()
    # As no accepted integration exists, the manager's close still refuses
    # non-ancestor branch tips; here the checkout is exact trunk base.
    plan = maint.plan("Unit", older_than=cutoff(maint))
    assert plan.candidates
    out = maint.execute(plan)
    assert out.removed == (cid,)
    assert not ctx.tree_path.exists()


def test_lock_blocks_and_is_never_removed_by_age_or_pid_hint(env):
    root, store, maint = env
    cid, ctx, _ = green_claim(env)
    (ctx.tree_path / "file.txt").write_text("base\n")
    manager = ClaimWorktrees(root)
    lock = manager.workspace / ".integration.lock"
    lock.write_text("pid=999999\ncreated=2000-01-01T00:00:00Z", encoding="utf-8")
    result = maint.audit("Unit", cutoff=cutoff(maint))
    assert result.lock_present
    assert by_id(result, cid).kind == "stale_lock"
    assert maint.plan("Unit", older_than=cutoff(maint)).candidates == ()
    assert not maint.execute(maint.plan("Unit", older_than=cutoff(maint))).removed
    repair = maint.repair_findings(result)
    assert len(repair) == 1 and maint.repair_findings(result) == ()
    assert lock.exists() and ctx.tree_path.exists()
    lock.unlink()


def test_missing_tree_and_orphan_are_typed_repair_not_prune(env):
    root, store, maint = env
    cid, ctx = taken(store, "W-A", "TZ-A")
    # Simulate interrupted create/cleanup in disposable temporary repo only.
    git(root, "worktree", "remove", "--force", str(ctx.tree_path))
    orphan = ClaimWorktrees(root).open("unknown-orphan")
    report = maint.audit("Unit", cutoff=cutoff(maint))
    assert by_id(report, cid).kind == "missing_tree"
    assert any(f.kind == "orphan" and f.path == str(orphan) for f in report.findings)
    repair_tasks = maint.repair_findings(report)
    assert len(repair_tasks) == 2
    assert maint.repair_findings(report) == ()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Unit' "
                         "AND tz LIKE 'repair:claim-maintenance:%'") == 2
    assert orphan.exists() and git(root, "branch", "--list", "claim/unknown-orphan")


def test_changed_after_plan_blocks_cleanup_without_leaking_data(env):
    root, store, maint = env
    cid, ctx, _ = green_claim(env)
    (ctx.tree_path / "file.txt").write_text("base\n")
    plan = maint.plan("Unit", older_than=cutoff(maint))
    assert plan.candidates
    (ctx.tree_path / "SECRET-UNTRACKED").write_text("do not remove")
    out = maint.execute(plan)
    assert out.removed == () and out.held == (cid,)
    assert (ctx.tree_path / "SECRET-UNTRACKED").read_text() == "do not remove"
    assert git(root, "rev-parse", "HEAD") == plan.trunk_sha


def test_flagoff_invalid_cutoff_and_untrusted_root_fail_closed(env, tmp_path):
    root, store, maint = env
    disabled = ClaimMaintenance(store, settings=lambda _: {
        "claim_trees": False, "commit": True})
    assert not disabled.audit("Unit").enabled
    assert disabled.plan("Unit", older_than=datetime.now(timezone.utc)).candidates == ()
    assert not maint.audit("Unit", cutoff=datetime.now(timezone.utc)+timedelta(days=30)).enabled
    assert not maint.audit("Unit", cutoff=datetime(2026, 1, 1)).enabled
    with store.tx() as tx:
        tx.change("UPDATE projects SET root=? WHERE name='Unit'",
                  (str(tmp_path),), event={"kind": "corrupt-root"})
    assert not maint.audit("Unit", cutoff=cutoff(maint)).enabled




def test_real_controller_maintenance_trigger_audits_without_cleanup(env, monkeypatch):
    from dev_mcp.fleet.controller import run_claim_maintenance_worker
    root, store, maint = env
    cid, ctx = taken(store, "W-A", "TZ-A")

    class Once:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    stop = Once()
    run_claim_maintenance_worker(store, stop, interval_s=0.01)
    assert stop.stopped and ctx.tree_path.exists()
    with store.read() as tx:
        row = tx.one("SELECT payload FROM events WHERE kind='claim_maintenance_tick' "
                     "AND project='Unit' ORDER BY id DESC LIMIT 1")
    assert row is not None and json.loads(row["payload"])["enabled"] is True

    # Flag-off has no periodic Git worktree audit, even if tasks exist.
    monkeypatch.setattr(goals, "project_info",
                        lambda _: {"commit": True, "claim_trees": False})
    with store.read() as tx:
        before = tx.scalar("SELECT COUNT(*) FROM events "
                           "WHERE kind='claim_maintenance_tick' AND project='Unit'")
    run_claim_maintenance_worker(store, Once(), interval_s=0.01)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events "
                         "WHERE kind='claim_maintenance_tick' AND project='Unit'") == before
    assert ctx.tree_path.exists()



def test_authorized_controller_retention_persists_dryrun_before_cleanup(env, monkeypatch):
    from dev_mcp.fleet.controller import run_claim_maintenance_worker
    root, store, maint = env
    cid, ctx, _ = green_claim(env)
    (ctx.tree_path / "file.txt").write_text("base\n")
    monkeypatch.setattr(goals, "project_info", lambda _: {
        "claim_trees": True, "commit": True, "claim_tree_retention_days": 2})
    # A sealed green trunk gate has a concrete UTC finished_at, synthesized
    # older here to exercise retention without sleeping or touching live DB.
    with store.tx() as tx:
        row = tx.one("SELECT id,payload FROM events WHERE kind='claim_trunk_gate' "
                     "AND project='Unit' ORDER BY id DESC LIMIT 1")
        payload = json.loads(row["payload"])
        payload["finished_at"] = (datetime.now(timezone.utc) -
                                  timedelta(days=4)).isoformat()
        tx.change("UPDATE events SET payload=? WHERE id=?",
                  (json.dumps(payload), row["id"]), event={"kind": "fixture-aged-gate"})
    class Once:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    run_claim_maintenance_worker(store, Once(), interval_s=0.01)
    assert not ctx.tree_path.exists()
    with store.read() as tx:
        proof = tx.query("SELECT id,kind,payload FROM events "
                         "WHERE kind IN ('claim_maintenance_dry_run',"
                         "'claim_maintenance_closed','claim_maintenance_tick') "
                         "AND project='Unit' ORDER BY id DESC LIMIT 3")
    assert [row["kind"] for row in proof] == [
        "claim_maintenance_tick", "claim_maintenance_closed", "claim_maintenance_dry_run"]
    assert json.loads(proof[-1]["payload"])["candidates"][0]["claim_id"] == cid
    assert json.loads(proof[0]["payload"])["removed"] == [cid]



def test_production_run_controller_wires_real_maintenance_thread(env, monkeypatch):
    from dev_mcp.fleet import controller
    import threading

    root, _, _ = env
    targets = []

    class RecordingThread:
        def __init__(self, *, target, args=(), daemon=False, name="", **kwargs):
            targets.append((target, args, daemon, name))

        def start(self):
            pass

    monkeypatch.setattr(controller.threading, "Thread", RecordingThread)
    already_stopped = threading.Event()
    already_stopped.set()
    # The real entrypoint opens a disposable Store, constructs controller
    # workers and registers this background target. Threads are not run.
    controller.run_controller(root.parent / "fleet.sqlite", already_stopped,
                              policy=None)
    assert any(target is controller.run_claim_maintenance_worker
               and daemon and name == "dev-mcp-claim-maintenance-audit"
               for target, _, daemon, name in targets)

def test_needs_result_and_review_state_never_authorize_cleanup(env):
    root, store, maint = env
    cid, ctx = taken(store, "W-A", "TZ-A")
    submit(store, cid)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='review' WHERE project='Unit' AND tz='TZ-A'",
                  event={"kind": "review-state"})
        tx.record_event(kind="claim_integration_obstacle", project="Unit", tz="TZ-A",
                        payload={"claim_id": cid, "depends_on": {"kind": "result"}})
    assert not by_id(maint.audit("Unit", cutoff=cutoff(maint)), cid).eligible
    assert ctx.tree_path.exists()
