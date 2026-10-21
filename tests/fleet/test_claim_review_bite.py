"""TZ-41 contract and hostile public reviewer boundary, in temporary Git repos."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dev_mcp.fleet import goals, queue, review
from dev_mcp.fleet.claim_review_bite import (
    ClaimReviewBite, ClaimReviewError, accept_claim, capture_submission,
    check_fix_bite, reviewer_inspect, reviewer_run, verify_accepted_claim,
)
from dev_mcp.fleet.claim_take_routing import resolve_claim_context
from dev_mcp.fleet.store import Store


def git(repo: Path, *argv: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *argv], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, (argv, p.stderr)
    return p.stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo with spaces"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "state.txt").write_text("bad\n", encoding="utf-8")
    (root / "tests" / "check.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('src/state.txt').read_text().strip() == 'good' else 6)\n",
        encoding="utf-8",
    )
    (root / "tests" / "regression.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "base")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    now = "2026-10-10T00:00:00+00:00"
    monkeypatch.setattr(goals, "project_info", lambda _: {"claim_trees": True, "commit": True})
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('Unit',?,1,1)",
                  (str(root),), event={"kind": "seed"})
        for worker in ("W-A", "W-B", "W-R", "W-S"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen)"
                      " VALUES(?,?,?,?,?)", (worker, worker, "chatgpt", now, now),
                      event={"kind": "seed"})
        for tz in ("TZ-A", "TZ-B"):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES('Unit',?,'test','ready',1,'work','','','[]',?,'[]',?,'',?,?)",
                (tz, json.dumps([str(root / "src" / "state.txt")]),
                 json.dumps(["python tests/check.py"]), now, now), event={"kind": "seed"},
            )
    yield root, store
    store.close()


def take(store: Store, author: str, tz: str):
    assert queue.take_next(store, author, "Unit").tz == tz
    with store.read() as tx:
        claim_id = int(tx.scalar("SELECT id FROM claims WHERE worker_id=? "
                                 "AND state='active' ORDER BY id DESC LIMIT 1", (author,)))
    return claim_id, resolve_claim_context(store, author, claim_id)


def mark_submitted(store: Store, claim_id: int):
    with store.tx() as tx:
        c = tx.one("SELECT project,tz,worker_id FROM claims WHERE id=?", (claim_id,))
        tx.changes([
            ("UPDATE tasks SET state='submitted',author_worker=? WHERE project=? AND tz=?",
             (c["worker_id"], c["project"], c["tz"])),
            ("UPDATE claims SET state='released' WHERE id=?", (claim_id,)),
        ], event={"kind": "submit", "worker_id": c["worker_id"],
                  "project": c["project"], "tz": c["tz"], "payload": {"claim_id": claim_id}})


def take_review(store: Store, tz: str, reviewer: str):
    review.open_review(store, "Unit", tz)
    assigned = queue.take_next(store, reviewer, "Unit")
    assert assigned.tz.startswith("review:"), assigned.tz


def run_good(store, claim_id, reviewer):
    return reviewer_run(store, claim_id, reviewer, ["python", "tests/check.py"],
                        python_executable=Path(sys.executable))


def test_reviewer_inspect_w3249_reads_pinned_snapshot_not_trunk(world):
    root, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src/state.txt").write_text("good\n", encoding="utf-8")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    assert (root / "src/state.txt").read_text() == "bad\n"
    assert reviewer_inspect(store, claim, "W-R", "read_file", "src/state.txt") == "good\n"
    assert "src/state.txt" in reviewer_inspect(store, claim, "W-R", "list_dir", "src")
    assert "src/state.txt:1: good" in reviewer_inspect(
        store, claim, "W-R", "search_files", str(root), query="good", pattern="*.txt")
    assert reviewer_inspect(
        store, claim, "W-R", "search_files", "src", query="bad") == "Совпадений нет."


def test_reviewer_inspect_w3249_denies_nonreviewer_and_traversal(world):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src/state.txt").write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    with pytest.raises(ClaimReviewError, match="reviewer"):
        reviewer_inspect(store, claim, "W-A", "read_file", "src/state.txt")
    with pytest.raises(ClaimReviewError, match="reviewer"):
        reviewer_inspect(store, claim, "W-S", "list_dir", "src")
    with pytest.raises(ClaimReviewError, match="outside"):
        reviewer_inspect(store, claim, "W-R", "read_file", "../secret.txt")
    with pytest.raises(ClaimReviewError, match="protected"):
        reviewer_inspect(store, claim, "W-R", "read_file", ".git/config")


def test_reviewer_inspect_w3249_refuses_changed_source_after_seal(world):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    source = ctx.tree_path / "src/state.txt"
    source.write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    assert "good" in reviewer_inspect(store, claim, "W-R", "read_file", "src/state.txt")
    source.write_text("changed after seal\n")
    with pytest.raises(ClaimReviewError, match="changed"):
        reviewer_inspect(store, claim, "W-R", "read_file", "src/state.txt")


def test_two_claims_independent_reviewer_and_trunk_unchanged(world):
    root, store = world
    a, ctx_a = take(store, "W-A", "TZ-A")
    b, ctx_b = take(store, "W-B", "TZ-B")
    assert ctx_a.tree_path != ctx_b.tree_path
    (ctx_a.tree_path / "src" / "state.txt").write_text("good\n")
    (ctx_b.tree_path / "src" / "state.txt").write_text("bad B\n")
    before = git(root, "rev-parse", "HEAD")
    sub_a = capture_submission(store, a, "W-A")
    sub_b = capture_submission(store, b, "W-B")
    assert sub_a and sub_b and sub_a.revision != sub_b.revision
    mark_submitted(store, a)
    mark_submitted(store, b)
    take_review(store, "TZ-A", "W-R")
    assert run_good(store, a, "W-R").rc == 0
    with pytest.raises(ClaimReviewError, match="reviewer"):
        reviewer_run(store, a, "W-A", ["python", "tests/check.py"],
                     python_executable=Path(sys.executable))
    proof = accept_claim(store, a, "W-R")
    assert proof and proof.submission_revision == sub_a.revision
    with pytest.raises(ClaimReviewError, match="independent active"):
        accept_claim(store, b, "W-R")
    with pytest.raises(ClaimReviewError, match="accepted"):
        verify_accepted_claim(store, a, sub_a.revision)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Unit' AND tz='TZ-A'",
                  event={"kind": "accept"})
    verified = verify_accepted_claim(store, a, sub_a.revision)
    assert verified == ClaimReviewBite(store).verify_accepted_claim(a, sub_a.revision)
    assert verified.reviewer == "W-R" and verified.run_ids
    with pytest.raises(ClaimReviewError, match="wrong submission"):
        verify_accepted_claim(store, a, "0" * 40)
    assert (root / "src" / "state.txt").read_text().strip() == "bad"
    assert git(root, "rev-parse", "HEAD") == before


def test_tracked_and_untracked_mutations_invalidate_success(world):
    root, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    source = ctx.tree_path / "src" / "state.txt"
    source.write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    assert run_good(store, claim, "W-R").rc == 0
    untracked = ctx.tree_path / "untracked.py"
    untracked.write_text("print('new source')\n")
    with pytest.raises(ClaimReviewError, match="changed"):
        accept_claim(store, claim, "W-R")
    untracked.unlink()
    source.write_text("bad\n")
    with pytest.raises(ClaimReviewError, match="changed"):
        accept_claim(store, claim, "W-R")
    source.write_text("good\n")
    # Same bytes no longer renew proof after a modification.
    with pytest.raises(ClaimReviewError, match="changed"):
        run_good(store, claim, "W-R")


def test_changed_during_run_never_writes_positive_event(world, monkeypatch):
    root, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    source = ctx.tree_path / "src" / "state.txt"
    source.write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    from dev_mcp.fleet import claim_review_bite as bridge
    original = bridge._execute

    def mutate(*args, **kwargs):
        rc, output = original(*args, **kwargs)
        source.write_text("mutation after green\n")
        return rc, output

    monkeypatch.setattr(bridge, "_execute", mutate)
    with pytest.raises(ClaimReviewError, match="changed"):
        run_good(store, claim, "W-R")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='claim_review_run'") == 0


def test_rejected_revision_bite_old_fails_new_passes(world):
    root, store = world
    rejected, ctx = take(store, "W-A", "TZ-A")
    old = capture_submission(store, rejected, "W-A")
    assert old
    mark_submitted(store, rejected)
    with store.tx() as tx:
        tx.record_event(kind="reject", worker_id="W-R", project="Unit",
                        tz="TZ-A", payload={"note": "old code is broken",
                                            "fix_task": "fix:TZ-A"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at)"
            " SELECT project,'fix:TZ-A','fix','ready',priority,'fix',spec_path,spec_sha,"
            "needs,owns,resources,acceptance,'[fleet-fix] source=Unit/TZ-A',created_at,updated_at"
            " FROM tasks WHERE project='Unit' AND tz='TZ-A'",
            event={"kind": "seed_fix"},
        )
    fix_id, fix_ctx = take(store, "W-B", "fix:TZ-A")
    (fix_ctx.tree_path / "src" / "state.txt").write_text("good\n")
    new = capture_submission(store, fix_id, "W-B")
    mark_submitted(store, fix_id)
    take_review(store, "fix:TZ-A", "W-R")
    assert run_good(store, fix_id, "W-R").rc == 0
    bite = check_fix_bite(store, fix_id, "W-R", ["python", "tests/check.py"],
                          regression_commands=[["python", "tests/regression.py"]],
                          python_executable=Path(sys.executable))
    assert bite.verdict == "passed" and bite.old_rc == 6 and bite.new_rc == 0
    assert bite.previous_revision == old.revision and bite.revision == new.revision
    assert accept_claim(store, fix_id, "W-R").bite_id is not None
    assert (root / "src" / "state.txt").read_text().strip() == "bad"


@pytest.mark.parametrize("venv_python", [
    ".venv/Scripts/python.exe",
    ".venv\\Scripts\\python.exe",
    ".venv/bin/python",
    "./.venv/Scripts/python.exe",
    ".\\.venv\\Scripts\\python.exe",
])
def test_bite_replays_project_python_in_regressions(world, venv_python):
    root, store = world
    rejected, _ = take(store, "W-A", "TZ-A")
    capture_submission(store, rejected, "W-A")
    mark_submitted(store, rejected)
    with store.tx() as tx:
        tx.record_event(kind="reject", worker_id="W-R", project="Unit", tz="TZ-A",
                        payload={"fix_task": "fix:TZ-A"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at)"
            " SELECT project,'fix:TZ-A','fix','ready',priority,'fix',spec_path,spec_sha,"
            "needs,owns,resources,acceptance,'[fleet-fix] source=Unit/TZ-A',created_at,updated_at"
            " FROM tasks WHERE project='Unit' AND tz='TZ-A'", event={"kind": "seed_fix"})
    fix_id, ctx = take(store, "W-B", "fix:TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    capture_submission(store, fix_id, "W-B")
    mark_submitted(store, fix_id)
    take_review(store, "fix:TZ-A", "W-R")
    bite = check_fix_bite(
        store, fix_id, "W-R", [venv_python, "tests/check.py"],
        regression_commands=[[venv_python, "tests/regression.py"]],
        python_executable=Path(sys.executable),
    )
    assert (bite.verdict, bite.old_rc, bite.new_rc) == ("passed", 6, 0)


def test_bad_bite_and_structured_visual_na_does_not_replace_acceptance(world):
    root, store = world
    rejected, _ = take(store, "W-A", "TZ-A")
    capture_submission(store, rejected, "W-A")
    mark_submitted(store, rejected)
    with store.tx() as tx:
        tx.record_event(kind="reject", worker_id="W-R", project="Unit",
                        tz="TZ-A", payload={"fix_task": "fix:TZ-A"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at)"
            " SELECT project,'fix:TZ-A','fix','ready',priority,'fix',spec_path,spec_sha,"
            "needs,owns,resources,acceptance,'[fleet-fix] source=Unit/TZ-A',created_at,updated_at"
            " FROM tasks WHERE project='Unit' AND tz='TZ-A'",
            event={"kind": "seed_fix"},
        )
    fix_id, ctx = take(store, "W-B", "fix:TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    capture_submission(store, fix_id, "W-B")
    mark_submitted(store, fix_id)
    take_review(store, "fix:TZ-A", "W-R")
    with pytest.raises(ClaimReviewError, match="structured reason"):
        check_fix_bite(store, fix_id, "W-R", None)
    assert check_fix_bite(store, fix_id, "W-R", None,
                          na_reason="visual fixture is evaluated manually").verdict == "n/a"
    with pytest.raises(ClaimReviewError, match="missing fresh"):
        accept_claim(store, fix_id, "W-R")
    assert run_good(store, fix_id, "W-R").rc == 0
    assert accept_claim(store, fix_id, "W-R").bite_id is not None


def test_missing_or_forged_evidence_cannot_authorize_accept(world):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    sub = capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    with pytest.raises(ClaimReviewError, match="missing fresh"):
        accept_claim(store, claim, "W-R")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Unit' AND tz='TZ-A'",
                  event={"kind": "accept", "payload": {"accepted": True}})
    with pytest.raises(ClaimReviewError, match="trusted accept"):
        verify_accepted_claim(store, claim, sub.revision)


def test_symlink_outside_snapshot_is_rejected(world, tmp_path):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    source = ctx.tree_path / "src" / "state.txt"
    source.unlink()
    outside = tmp_path / "secret.txt"
    outside.write_text("good")
    try:
        source.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks require Windows developer mode")
    with pytest.raises(ClaimReviewError, match="symlink"):
        capture_submission(store, claim, "W-A")


def test_corrupt_submission_replay_fails_closed(world):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    with store.tx() as tx:
        row = tx.one("SELECT id,payload FROM events WHERE kind='claim_submission'")
        body = json.loads(row["payload"])
        body["revision"] = "0" * 40
        tx.change("UPDATE events SET payload=? WHERE id=?",
                  (json.dumps(body), row["id"]), event={"kind": "test_corruption"})
    with pytest.raises(ClaimReviewError, match="rev-parse failed|commit missing"):
        run_good(store, claim, "W-R")


def test_failed_bite_blocks_accept_even_with_green_acceptance(world):
    _, store = world
    source_id, _ = take(store, "W-A", "TZ-A")
    capture_submission(store, source_id, "W-A")
    mark_submitted(store, source_id)
    with store.tx() as tx:
        tx.record_event(kind="reject", worker_id="W-R", project="Unit", tz="TZ-A",
                        payload={"fix_task": "fix:TZ-A"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at)"
            " SELECT project,'fix:TZ-A','fix','ready',priority,'fix',spec_path,spec_sha,"
            "needs,owns,resources,acceptance,'[fleet-fix] source=Unit/TZ-A',created_at,updated_at"
            " FROM tasks WHERE project='Unit' AND tz='TZ-A'", event={"kind": "seed_fix"})
    claim, ctx = take(store, "W-B", "fix:TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    capture_submission(store, claim, "W-B")
    mark_submitted(store, claim)
    take_review(store, "fix:TZ-A", "W-R")
    assert run_good(store, claim, "W-R").rc == 0
    # The test which was already passing in old code cannot count as a bite.
    bad = check_fix_bite(store, claim, "W-R", ["python", "tests/regression.py"],
                         python_executable=Path(sys.executable))
    assert bad.verdict == "failed" and bad.old_rc == bad.new_rc == 0
    with pytest.raises(ClaimReviewError, match="verified successful bite"):
        accept_claim(store, claim, "W-R")


def test_reviewer_claim_retake_invalidates_replayed_run(world):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    take(store, "W-B", "TZ-B")  # Leave only the review ready for retake.
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    assert run_good(store, claim, "W-R").rc == 0
    with store.tx() as tx:
        previous = tx.one("SELECT id FROM claims WHERE worker_id='W-R' AND state='active'")
        tx.change("UPDATE claims SET state='released' WHERE id=?", (previous["id"],),
                  event={"kind": "manual_release"})
        # The production queue intentionally refuses a second take by the same
        # prior reviewer. Simulate a replayed/malformed controller claim directly:
        # the public accept boundary must STILL refuse old run evidence.
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('Unit','review:TZ-A','W-R','active',?)",
                  ("2026-10-10T00:01:00+00:00",), event={"kind": "test_retry_claim"})
    with pytest.raises(ClaimReviewError, match="missing fresh"):
        accept_claim(store, claim, "W-R")
    assert run_good(store, claim, "W-R").rc == 0
    assert accept_claim(store, claim, "W-R").reviewer_claim_id != previous["id"]


def test_large_output_is_bounded_and_snapshot_revision_survives_cleanup(world):
    root, store = world
    (root / "tests" / "loud.py").write_text("print('x'*250000)\n")
    git(root, "add", "tests/loud.py")
    git(root, "commit", "-m", "add loud acceptance")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET acceptance=? WHERE project='Unit' AND tz='TZ-A'",
                  (json.dumps(["python tests/loud.py"]),), event={"kind": "test_acceptance"})
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    sub = capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    proof = reviewer_run(store, claim, "W-R", ["python", "tests/loud.py"],
                         python_executable=Path(sys.executable))
    assert proof.rc == 0 and len(proof.output) <= 4096
    accept_claim(store, claim, "W-R")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Unit' AND tz='TZ-A'",
                  event={"kind": "accept"})
    from dev_mcp.fleet.claim_tree import ClaimWorktrees
    ClaimWorktrees(root).close(f"claim-{claim}", abandon=True)
    assert not ctx.tree_path.exists()
    assert verify_accepted_claim(store, claim, sub.revision).tree_sha == sub.tree_sha


def test_unsafe_source_argv_and_legacy_shared_route(world, monkeypatch):
    root, store = world
    with store.tx() as tx:
        tx.change("UPDATE tasks SET acceptance=? WHERE project='Unit' AND tz='TZ-A'",
                  (json.dumps(["python ../outside.py"]),), event={"kind": "test_acceptance"})
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    with pytest.raises(ClaimReviewError, match="source argument"):
        reviewer_run(store, claim, "W-R", ["python", "../outside.py"],
                     python_executable=Path(sys.executable))
    with store.read() as tx:
        assert not tx.query("SELECT id FROM events WHERE kind='claim_review_run'")

    # The old shared route must not be blocked by new isolated proof requirements.
    monkeypatch.setattr(goals, "project_info",
                        lambda _: {"claim_trees": False, "commit": False})
    # Legacy take events are not required to expose TZ-40 context at all.
    assert queue.take_next(store, "W-B", "Unit").tz == "TZ-B"
    with store.read() as tx:
        legacy = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W-B' AND state='active'"))
    assert capture_submission(store, legacy, "W-B") is None
    assert accept_claim(store, legacy, "W-B") is None


def test_invalid_command_encoding_and_snapshot_disappears(world, monkeypatch):
    _, store = world
    with store.tx() as tx:
        tx.change("UPDATE tasks SET acceptance=? WHERE project='Unit' AND tz='TZ-A'",
                  (json.dumps(["python tests/\ud800.py"]),), event={"kind": "malformed_command"})
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    with pytest.raises(ClaimReviewError, match="encoding"):
        capture_submission(store, claim, "W-A")

    with store.tx() as tx:
        tx.change("UPDATE tasks SET acceptance=? WHERE project='Unit' AND tz='TZ-A'",
                  (json.dumps(["python tests/check.py"]),), event={"kind": "fixed_command"})
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    from dev_mcp.fleet import claim_review_bite as bridge
    original = bridge._execute

    def vanished(*args, **kwargs):
        rc, output = original(*args, **kwargs)
        from shutil import rmtree
        rmtree(ctx.tree_path)
        return rc, output

    monkeypatch.setattr(bridge, "_execute", vanished)
    with pytest.raises(ClaimReviewError):
        run_good(store, claim, "W-R")
    with store.read() as tx:
        assert not tx.query("SELECT id FROM events WHERE kind='claim_review_run'")


def test_crash_before_proof_transaction_has_no_success_event(world, monkeypatch):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    from contextlib import contextmanager
    original_tx = store.tx

    @contextmanager
    def crashed_tx():
        with original_tx() as tx:
            record = tx.record_event

            def reject_run(**data):
                if data.get("kind") == "claim_review_run":
                    raise OSError("simulated failure between rc and durable event")
                return record(**data)
            tx.record_event = reject_run
            yield tx

    monkeypatch.setattr(store, "tx", crashed_tx)
    with pytest.raises(OSError, match="simulated failure"):
        run_good(store, claim, "W-R")
    with store.read() as tx:
        assert not tx.query("SELECT id FROM events WHERE kind='claim_review_run'")


def test_same_bytes_touched_after_green_run_invalidates_evidence(world):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    source = ctx.tree_path / "src" / "state.txt"
    source.write_text("good\n")
    capture_submission(store, claim, "W-A")
    mark_submitted(store, claim)
    take_review(store, "TZ-A", "W-R")
    assert run_good(store, claim, "W-R").rc == 0
    old = source.stat().st_mtime_ns
    os.utime(source, ns=(old + 2_000_000_000, old + 2_000_000_000))
    assert source.read_text() == "good\n"
    with pytest.raises(ClaimReviewError, match="changed"):
        accept_claim(store, claim, "W-R")


def test_out_of_owns_dirty_source_cannot_enter_sealed_revision(world):
    _, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "state.txt").write_text("good\n")
    (ctx.tree_path / "tests" / "check.py").write_text("raise SystemExit(0)\n")
    with pytest.raises(ClaimReviewError, match="outside accepted owns"):
        capture_submission(store, claim, "W-A")
    assert git(ctx.tree_path, "diff", "--cached", "--name-only") == ""


def test_fix_bite_refuses_foreign_rejected_source_even_inside_project(world):
    _, store = world
    first, _ = take(store, "W-A", "TZ-A")
    capture_submission(store, first, "W-A")
    mark_submitted(store, first)
    second, _ = take(store, "W-B", "TZ-B")
    capture_submission(store, second, "W-B")
    mark_submitted(store, second)
    # A reject of TZ-B does not provide an old revision for fix:TZ-A.
    with store.tx() as tx:
        tx.record_event(kind="reject", worker_id="W-R", project="Unit", tz="TZ-B",
                        payload={"fix_task": "fix:TZ-B"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at)"
            " SELECT project,'fix:TZ-A','fix','ready',priority,'fix',spec_path,spec_sha,"
            "needs,owns,resources,acceptance,'[fleet-fix] source=Unit/TZ-B',created_at,updated_at"
            " FROM tasks WHERE project='Unit' AND tz='TZ-A'",
            event={"kind": "test_foreign_fix"},
        )
    fix_id, _ = take(store, "W-S", "fix:TZ-A")
    capture_submission(store, fix_id, "W-S")
    mark_submitted(store, fix_id)
    take_review(store, "fix:TZ-A", "W-R")
    with pytest.raises(ClaimReviewError, match="source|rejection"):
        check_fix_bite(store, fix_id, "W-R", None,
                       na_reason="document fixture cannot be exercised")


def test_second_fix_bites_immediately_rejected_fix_not_original_root(world):
    _, store = world
    original_id, _ = take(store, "W-A", "TZ-A")
    original = capture_submission(store, original_id, "W-A")
    mark_submitted(store, original_id)
    with store.tx() as tx:
        tx.record_event(kind="reject", worker_id="W-R", project="Unit", tz="TZ-A",
                        payload={"fix_task": "fix:TZ-A"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at)"
            " SELECT project,'fix:TZ-A','fix','ready',priority,'fix',spec_path,spec_sha,"
            "needs,owns,resources,acceptance,'[fleet-fix] source=Unit/TZ-A',created_at,updated_at"
            " FROM tasks WHERE project='Unit' AND tz='TZ-A'",
            event={"kind": "first_fix"},
        )
    first_id, first_ctx = take(store, "W-B", "fix:TZ-A")
    (first_ctx.tree_path / "src" / "state.txt").write_text("still bad\n")
    previous_fix = capture_submission(store, first_id, "W-B")
    mark_submitted(store, first_id)
    with store.tx() as tx:
        tx.record_event(kind="reject", worker_id="W-R", project="Unit", tz="fix:TZ-A",
                        payload={"fix_task": "fix:TZ-A:2"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at)"
            " SELECT project,'fix:TZ-A:2','fix','ready',priority,'fix',spec_path,spec_sha,"
            "needs,owns,resources,acceptance,'[fleet-fix] source=Unit/TZ-A',created_at,updated_at"
            " FROM tasks WHERE project='Unit' AND tz='fix:TZ-A'",
            event={"kind": "second_fix"},
        )
    second_id, second_ctx = take(store, "W-S", "fix:TZ-A:2")
    (second_ctx.tree_path / "src" / "state.txt").write_text("good\n")
    capture_submission(store, second_id, "W-S")
    mark_submitted(store, second_id)
    take_review(store, "fix:TZ-A:2", "W-R")
    bite = check_fix_bite(store, second_id, "W-R", ["python", "tests/check.py"],
                          python_executable=Path(sys.executable))
    assert bite.verdict == "passed"
    assert bite.previous_revision == previous_fix.revision
    assert bite.previous_revision != original.revision
