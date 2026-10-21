"""TZ-42: real temporary Git histories and independently reviewed claims."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

import pytest

from dev_mcp.fleet import goals, queue, review
from dev_mcp.fleet.claim_accept_flow import (
    ClaimAcceptError, ClaimAcceptFlow, _path, _within_owner, release_ready,
)
from dev_mcp.fleet.claim_review_bite import (
    accept_claim, capture_submission, reviewer_run,
)
from dev_mcp.fleet.claim_take_routing import resolve_claim_context
from dev_mcp.fleet.controller import Controller, DeliveryRuntime
from dev_mcp.fleet.store import Store


def git(root: Path, *argv: str) -> str:
    p = subprocess.run(["git", "-C", str(root), *argv],
                       text=True, encoding="utf-8", errors="replace",
                       capture_output=True)
    assert p.returncode == 0, (argv, p.stdout, p.stderr)
    return p.stdout.strip()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "Проверяемый repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fleet Test")
    git(root, "config", "user.email", "fleet@example.invalid")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "shared.txt").write_text("base\n", encoding="utf-8")
    (root / "src" / "side.txt").write_text("side\n", encoding="utf-8")
    (root / "tests" / "pass.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    git(root, "add", "src", "tests")
    git(root, "commit", "-m", "base")
    store = Store.open(str(tmp_path / "fleet.db"))
    monkeypatch.setattr(goals, "project_info", lambda _: {
        "commit": True, "claim_trees": True})
    now = "2026-10-10T00:00:00+00:00"
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Unit", str(root)), event={"kind": "seed"})
        for worker in ("W-A", "W-B", "W-R", "W-S"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen)"
                      " VALUES(?,?,?,?,?)",
                      (worker, worker, "chatgpt", now, now), event={"kind": "seed"})
        for tz in ("TZ-A", "TZ-B"):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at)"
                " VALUES('Unit',?,'test','ready',1,'work','','','[]',?,'[]',?,'',?,?)",
                (tz, json.dumps([str(root / "src")]),
                 json.dumps(["python tests/pass.py"]), now, now),
                event={"kind": "seed"},
            )
    yield root, store
    store.close()


def take(store: Store, who: str, tz: str):
    assignment = queue.take_next(store, who, "Unit")
    assert assignment and assignment.tz == tz
    with store.read() as tx:
        claim = int(tx.scalar(
            "SELECT id FROM claims WHERE worker_id=? AND state='active' "
            "ORDER BY id DESC LIMIT 1", (who,)))
    return claim, resolve_claim_context(store, who, claim)


def submitted(store: Store, claim: int):
    with store.tx() as tx:
        c = tx.one("SELECT project,tz,worker_id FROM claims WHERE id=?", (claim,))
        tx.changes([
            ("UPDATE tasks SET state='submitted',author_worker=? WHERE project=? AND tz=?",
             (c["worker_id"], c["project"], c["tz"])),
            ("UPDATE claims SET state='released' WHERE id=?", (claim,)),
        ], event={"kind": "submit", "project": c["project"], "tz": c["tz"],
                  "worker_id": c["worker_id"], "payload": {"claim_id": claim}})


def finish_accept(store: Store, claim: int, tz: str, reviewer: str):
    review.open_review(store, "Unit", tz)
    assignment = queue.take_next(store, reviewer, "Unit")
    assert assignment and assignment.tz.startswith("review:")
    result = reviewer_run(store, claim, reviewer, ["python", "tests/pass.py"],
                          python_executable=Path(sys.executable))
    assert result.rc == 0
    proof = accept_claim(store, claim, reviewer)
    assert proof is not None
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Unit' AND tz=?",
                  (tz,), event={"kind": "accept", "project": "Unit",
                                "tz": tz, "worker_id": reviewer})
    return proof


def accepted_one(world, *, content="first\n"):
    root, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "shared.txt").write_text(content, encoding="utf-8")
    submission = capture_submission(store, claim, "W-A")
    assert submission
    submitted(store, claim)
    finish_accept(store, claim, "TZ-A", "W-R")
    return claim, ctx, submission


def flow(store, *, gate=None, settings=None, repair=None):
    return ClaimAcceptFlow(
        store, settings=settings or (lambda _: {"commit": True, "claim_trees": True}),
        gate=gate if gate is not None else lambda _: 0, repair=repair)


def test_accepted_only_ff_gate_evidence_idempotent(world):
    root, store = world
    initial = git(root, "rev-parse", "HEAD")
    claim, ctx, sub = accepted_one(world)
    assert git(root, "rev-parse", "HEAD") == initial
    observed = []
    assert release_ready(store, claim, sub.revision) is False

    def gate(trunk):
        observed.append((trunk.resolve(), git(trunk, "rev-parse", "HEAD"),
                         (trunk / "src" / "shared.txt").read_text()))
        return 0

    result = flow(store, gate=gate).on_accept(claim, sub.revision)
    assert result.status == "integrated" and result.gate_rc == 0
    assert result.accepted_sha == sub.revision
    assert result.integrated_sha != initial
    assert git(root, "rev-parse", "HEAD^") == initial  # no unreviewed ancestors
    assert (root / "src" / "side.txt").read_text() == "side\n"
    assert observed == [(root.resolve(), result.integrated_sha, "first\n")]
    assert release_ready(store, claim, sub.revision)
    assert not release_ready(store, claim, "0" * 40)
    assert git(ctx.tree_path, "status", "--porcelain")
    again = flow(store, gate=gate).on_accept(claim, sub.revision)
    assert again.status == "already_integrated" and again.integrated_sha == result.integrated_sha
    assert len(observed) == 1
    with store.read() as tx:
        kinds = [r["kind"] for r in tx.query(
            "SELECT kind FROM events WHERE kind LIKE 'claim_%'")]
    assert "claim_integration_prepared" in kinds
    assert "claim_integrated" in kinds
    assert "claim_trunk_gate" in kinds


def test_controller_delivery_defers_until_exact_gated_trunk(world, tmp_path, monkeypatch):
    """A server accept alone must never reach the delivery/Operator callback."""
    root, store = world
    claim, _, sub = accepted_one(world)
    with store.tx() as tx:
        reviewer_claim = tx.one(
            "SELECT id,tz FROM claims WHERE worker_id='W-R' ORDER BY id DESC LIMIT 1")
        assert reviewer_claim is not None
        review_tz = reviewer_claim["tz"]
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Unit' AND tz=?",
                  (review_tz,), event={"kind": "test-review-accepted"})
        tx.change("UPDATE claims SET state='released' WHERE id=?",
                  (reviewer_claim["id"],), event={"kind": "test-review-released"})
        tx.record_event(kind="accept", project="Unit", tz="TZ-A", worker_id="W-R",
                        payload={"review_task": review_tz})
    pinned = {"head": ""}
    invocations = []
    monkeypatch.setattr(
        "dev_mcp.fleet.delivery.on_accept",
        lambda event, **kwargs: invocations.append(event) or {"status": "delivered"})
    controller = Controller(
        store, delivery_route=lambda _project, _tz: DeliveryRuntime(
            (root,), tmp_path / "delivery.db", "a" * 64, "b" * 64,
            lambda *_: None, lambda *_: "b" * 64,
            integrated_sha=pinned["head"]))
    controller._schema()
    errors = []
    # The shared fixture also records an unbound accept; only the explicit
    # review-bound event may become retryable claim delivery.
    assert controller._delivery_events(errors) == 2
    assert errors == [] and invocations == []
    with store.read() as tx:
        blocked = tx.one(
            "SELECT receipt FROM controller_delivery_receipts "
            "WHERE receipt LIKE '%claim_trunk_not_ready%' LIMIT 1")
    assert blocked is not None
    assert json.loads(blocked["receipt"])["status"] == "claim_trunk_not_ready"
    # Even after a green gate, a route lacking independently pinned provenance
    # is not permitted to reuse another commit's generic acceptance.
    first = flow(store).on_accept(claim, sub.revision)
    assert first.status == "integrated"
    assert controller._delivery_events(errors) == 1 and not invocations
    pinned["head"] = first.integrated_sha
    assert controller._delivery_events(errors) == 1
    assert len(invocations) == 1 and errors == []
    assert controller._delivery_events(errors) == 0


def test_two_claim_conflict_is_typed_and_trunk_preserved(world):
    root, store = world
    a, actx = take(store, "W-A", "TZ-A")
    b, bctx = take(store, "W-B", "TZ-B")
    (actx.tree_path / "src" / "shared.txt").write_text("one\n", encoding="utf-8")
    (bctx.tree_path / "src" / "shared.txt").write_text("two\n", encoding="utf-8")
    a_sub = capture_submission(store, a, "W-A")
    b_sub = capture_submission(store, b, "W-B")
    submitted(store, a)
    submitted(store, b)
    finish_accept(store, a, "TZ-A", "W-R")
    finish_accept(store, b, "TZ-B", "W-S")
    worker = flow(store)
    assert worker.on_accept(a, a_sub.revision).status == "integrated"
    after_a = git(root, "rev-parse", "HEAD")
    blocked = worker.on_accept(b, b_sub.revision)
    assert blocked.status == "needs_result"
    assert blocked.conflict_paths == ("src/shared.txt",)
    assert blocked.repair_status == "needs_result"
    assert blocked.repair_tz.startswith("repair:TZ-B")
    assert git(root, "rev-parse", "HEAD") == after_a
    assert (root / "src" / "shared.txt").read_text() == "one\n"
    assert git(bctx.tree_path, "status", "--porcelain")
    with store.read() as tx:
        event = tx.one("SELECT payload FROM events "
                       "WHERE kind='claim_integration_obstacle'")
    payload = json.loads(event["payload"])
    assert payload["requires_new_acceptance"] is True
    assert payload["depends_on"]["kind"] == "result"
    assert payload["depends_on"]["tz"] == blocked.repair_tz
    assert payload["resolver_work"] == f"Unit/{blocked.repair_tz}"
    with store.read() as tx:
        repair_task = tx.one("SELECT state,kind,owns,body FROM tasks "
                             "WHERE project='Unit' AND tz=?", (blocked.repair_tz,))
    assert repair_task and repair_task["state"] == "ready"
    assert repair_task["kind"] == "work"
    assert "NEW independent reviewer" in repair_task["body"]
    replay = worker.on_accept(b, b_sub.revision)
    assert replay.status == "needs_result"
    assert replay.repair_status == "requires_new_independent_acceptance"
    assert replay.repair_tz == blocked.repair_tz
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Unit' "
                         "AND tz LIKE 'repair:TZ-B%'") == 1


def test_production_integrator_conflict_registers_queue_repair_and_fresh_review(
        world, tmp_path, monkeypatch):
    """Production path: two Git collisions → durable, claimable resolver → fresh review."""
    from dev_mcp.fleet.integrate import Batch, Integrator
    root, store = world
    a, actx = take(store, "W-A", "TZ-A")
    b, bctx = take(store, "W-B", "TZ-B")
    (actx.tree_path / "src" / "shared.txt").write_text("one\n", encoding="utf-8")
    (bctx.tree_path / "src" / "shared.txt").write_text("two\n", encoding="utf-8")
    a_sub = capture_submission(store, a, "W-A")
    b_sub = capture_submission(store, b, "W-B")
    submitted(store, a)
    submitted(store, b)
    finish_accept(store, a, "TZ-A", "W-R")
    finish_accept(store, b, "TZ-B", "W-S")
    monkeypatch.setattr(Integrator, "_gate", lambda *_args, **_kw: {"rc": 0})
    integrator = Integrator(
        store, tmp_path / "production-state.json",
        project_info=lambda _: {"commit": True, "claim_trees": True,
                                "gate": "python tests/pass.py"})
    with store.read() as tx:
        accepts = [dict(row) for row in tx.query(
            "SELECT id,project,tz,worker_id FROM events WHERE kind='accept' "
            "AND tz IN ('TZ-A','TZ-B') ORDER BY id")]
    assert len(accepts) == 2
    first = integrator._commit(Batch("Unit", str(root)), accepts[0])
    assert first["payload"]["status"] == "integrated"
    protected_head = git(root, "rev-parse", "HEAD")
    second = integrator._commit(Batch("Unit", str(root)), accepts[1])
    assert second["payload"]["status"] == "needs_result"
    assert second["payload"]["conflict_paths"] == ["src/shared.txt"]
    assert git(root, "rev-parse", "HEAD") == protected_head
    resolver = second["payload"]["waiting_for"][0]
    assert second["payload"]["obstacle"]["resolver_work"] == [f"Unit/{resolver}"]
    with store.read() as tx:
        repair = tx.one("SELECT * FROM tasks WHERE project='Unit' AND tz=?", (resolver,))
    assert repair and repair["state"] == "ready" and repair["kind"] == "work"
    assert json.loads(repair["acceptance"]) == ["python tests/pass.py"]
    assert json.loads(repair["owns"]) == [str(root / "src")]
    # A real queue claim is possible: obstacle is not merely text in an event.
    repair_assignment = queue.take_next(store, "W-A", "Unit")
    assert repair_assignment and repair_assignment.tz == resolver
    with store.read() as tx:
        repair_claim = int(tx.scalar(
            "SELECT id FROM claims WHERE worker_id='W-A' AND state='active' "
            "ORDER BY id DESC LIMIT 1"))
    repair_ctx = resolve_claim_context(store, "W-A", repair_claim)
    (repair_ctx.tree_path / "src" / "shared.txt").write_text(
        "reconciled\n", encoding="utf-8")
    resolved = capture_submission(store, repair_claim, "W-A")
    assert resolved and resolved.revision != b_sub.revision
    submitted(store, repair_claim)
    with store.tx() as tx:
        tx.change("UPDATE claims SET state='released' WHERE worker_id='W-S' "
                  "AND state='active'", event={"kind": "test-release-old-review"})
    finish_accept(store, repair_claim, resolver, "W-S")  # independent of W-A
    with store.read() as tx:
        repair_accept = dict(tx.one(
            "SELECT id,project,tz,worker_id FROM events WHERE kind='accept' "
            "AND tz=? ORDER BY id DESC LIMIT 1", (resolver,)))
    integrated_repair = integrator._commit(Batch("Unit", str(root)), repair_accept)
    assert integrated_repair["payload"]["status"] == "integrated"
    assert git(root, "rev-parse", "HEAD") != protected_head
    assert (root / "src" / "shared.txt").read_text() == "reconciled\n"


def test_concurrent_integration_of_conflicting_claims_never_loses_head(world):
    root, store = world
    a, actx = take(store, "W-A", "TZ-A")
    b, bctx = take(store, "W-B", "TZ-B")
    (actx.tree_path / "src" / "shared.txt").write_text("one\n")
    (bctx.tree_path / "src" / "shared.txt").write_text("two\n")
    submissions = [capture_submission(store, a, "W-A"),
                   capture_submission(store, b, "W-B")]
    submitted(store, a)
    submitted(store, b)
    finish_accept(store, a, "TZ-A", "W-R")
    finish_accept(store, b, "TZ-B", "W-S")
    from dev_mcp.fleet.claim_tree import ClaimTreeError
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(flow(store).on_accept, a, submissions[0].revision)
        future_b = pool.submit(flow(store).on_accept, b, submissions[1].revision)
        results = []
        locked = []
        for future, claim, submission in ((future_a, a, submissions[0]),
                                          (future_b, b, submissions[1])):
            try:
                results.append(future.result())
            except ClaimTreeError as exc:
                assert "integration lock held" in str(exc)
                locked.append((claim, submission.revision))
    # Cross-process Git lock deliberately fails closed rather than racing.
    # Once its holder has exited, the denied claim is safely retried.
    for claim, revision in locked:
        results.append(flow(store).on_accept(claim, revision))
    assert sorted(r.status for r in results) == ["integrated", "needs_result"]
    assert len([r for r in results if r.repair_tz]) == 1
    assert git(root, "rev-parse", "HEAD") == next(
        r.integrated_sha for r in results if r.status == "integrated")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='work' "
                         "AND project='Unit' AND tz LIKE 'repair:%'") == 1


def test_two_distinct_claims_rebase_without_loss_and_gate_both(world):
    root, store = world
    a, actx = take(store, "W-A", "TZ-A")
    b, bctx = take(store, "W-B", "TZ-B")
    (actx.tree_path / "src" / "first.txt").write_text("accepted first\n")
    (bctx.tree_path / "src" / "second.txt").write_text("accepted second\n")
    a_sub = capture_submission(store, a, "W-A")
    b_sub = capture_submission(store, b, "W-B")
    submitted(store, a)
    submitted(store, b)
    finish_accept(store, a, "TZ-A", "W-R")
    finish_accept(store, b, "TZ-B", "W-S")
    gates = []
    worker = flow(store, gate=lambda trunk: gates.append(
        (git(trunk, "rev-parse", "HEAD"), sorted(p.name for p in (trunk / "src").iterdir()))
    ) or 0)
    first = worker.on_accept(a, a_sub.revision)
    second = worker.on_accept(b, b_sub.revision)
    assert first.status == second.status == "integrated"
    assert second.integrated_sha != first.integrated_sha
    assert git(root, "rev-parse", "HEAD^") == first.integrated_sha
    assert (root / "src" / "first.txt").read_text() == "accepted first\n"
    assert (root / "src" / "second.txt").read_text() == "accepted second\n"
    assert [x[0] for x in gates] == [first.integrated_sha, second.integrated_sha]
    assert release_ready(store, b, b_sub.revision)
    assert not release_ready(store, a, a_sub.revision)  # stale green HEAD


def test_foreign_staged_untracked_claim_and_dirty_trunk_fail_closed(world):
    root, store = world
    claim, ctx, sub = accepted_one(world)
    base = git(root, "rev-parse", "HEAD")
    # Урок 55: блокирует только незакоммиченное в путях принятой правки.
    shared = root / "src" / "shared.txt"
    original_shared = shared.read_text(encoding="utf-8")
    shared.write_text("not authorized", encoding="utf-8")
    with pytest.raises(ClaimAcceptError, match="dirty trunk"):
        flow(store).on_accept(claim, sub.revision)
    assert git(root, "rev-parse", "HEAD") == base
    shared.write_text(original_shared, encoding="utf-8")
    # The submitted claim workspace cannot be mutated after independent review.
    (ctx.tree_path / "side-new.txt").write_text("untracked")
    with pytest.raises(ClaimAcceptError, match="changed"):
        flow(store).on_accept(claim, sub.revision)
    assert git(root, "rev-parse", "HEAD") == base


def test_forged_author_or_unaccepted_revision_cannot_promote(world):
    root, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "shared.txt").write_text("unaccepted\n")
    sub = capture_submission(store, claim, "W-A")
    assert sub
    original = git(root, "rev-parse", "HEAD")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Unit' AND tz='TZ-A'",
                  event={"kind": "author_claim", "worker_id": "W-A",
                         "payload": {"accepted": True}})
    with pytest.raises(ClaimAcceptError, match="acceptance unverified"):
        flow(store).on_accept(claim, sub.revision)
    assert git(root, "rev-parse", "HEAD") == original


def test_nonzero_gate_no_release_and_no_silent_rollback(world):
    root, store = world
    claim, ctx, sub = accepted_one(world)
    received = []
    result = flow(store, gate=lambda trunk: received.append(
        git(trunk, "rev-parse", "HEAD")) or 17).on_accept(claim, sub.revision)
    assert result.status == "gate_failed" and result.gate_rc == 17
    assert received == [result.integrated_sha]
    assert git(root, "rev-parse", "HEAD") == result.integrated_sha
    assert result.repair_status == "styk_required"
    assert not release_ready(store, claim, sub.revision)
    replay = flow(store).on_accept(claim, sub.revision)
    assert replay.status == "gate_failed"
    assert replay.gate_rc == 17


def test_disabled_flag_uses_legacy_and_does_not_touch_head(world):
    root, store = world
    claim, ctx, sub = accepted_one(world)
    base = git(root, "rev-parse", "HEAD")
    result = flow(store, settings=lambda _: {"commit": True, "claim_trees": False}
                  ).on_accept(claim, sub.revision)
    assert result.status == "legacy"
    assert git(root, "rev-parse", "HEAD") == base
    assert not (root / "src" / "shared.txt").read_text() == "first\n"
    legacy_off = flow(store, settings=lambda _: {"commit": False, "claim_trees": True}
                      ).on_accept(claim, sub.revision)
    assert legacy_off.status == "legacy"
    assert git(root, "rev-parse", "HEAD") == base


def test_path_policy_unicode_and_traversal(world):
    root, _ = world
    assert _within_owner(root, "src/кириллица.txt", (str(root / "src"),))
    assert not _within_owner(root, "src2/file.txt", (str(root / "src"),))
    assert _path("src/кириллица.txt".encode()) == "src/кириллица.txt"
    for hostile in (b"../outside.txt", b"/abs.txt", b".git/config",
                    b"src/../escape", b"src\\escape", b"C:evil.txt", b"\xff"):
        with pytest.raises(ClaimAcceptError):
            _path(hostile)



def test_integrator_hook_uses_only_bound_claim_accept(world, tmp_path, monkeypatch):
    """No legacy cherry-pick, reviewer mismatch or unattended stage."""
    from dev_mcp.fleet.integrate import Batch, Integrator
    root, store = world
    claim, ctx, sub = accepted_one(world)
    accepted_head = git(root, "rev-parse", "HEAD")
    seen = []

    def fake_gate(self, batch, *, force=False):
        seen.append((batch.root, git(Path(batch.root), "rev-parse", "HEAD"),
                     force, batch.accepts[0]["tz"]))
        return {"kind": "gate", "rc": 0, "project": batch.project}

    monkeypatch.setattr(Integrator, "_gate", fake_gate)
    integrator = Integrator(
        store, tmp_path / "integrate-state.json",
        project_info=lambda _: {"commit": True, "claim_trees": True,
                                "gate": "python tests/pass.py"},
    )
    with store.read() as tx:
        original = tx.one("SELECT id,project,tz,worker_id FROM events "
                          "WHERE kind='accept' AND tz='TZ-A' ORDER BY id DESC LIMIT 1")
    batch = Batch("Unit", str(root))
    event = integrator._commit(batch, dict(original))
    assert isinstance(event["payload"], dict)
    assert event["payload"]["commit"] == git(root, "rev-parse", "HEAD")
    assert git(root, "rev-parse", "HEAD") != accepted_head
    assert seen and seen[0] == (str(root), git(root, "rev-parse", "HEAD"),
                               True, "TZ-A")
    assert git(root, "status", "--porcelain") == ""

    # A forged accept with another worker has no matching TZ-41 reviewer event.
    changed = git(root, "rev-parse", "HEAD")
    forged = dict(original)
    forged["id"] = int(original["id"]) + 10000
    forged["worker_id"] = "W-A"
    again = integrator._commit(Batch("Unit", str(root)), forged)
    assert git(root, "rev-parse", "HEAD") == changed
    assert "missing independent" in again["payload"]["reason"]


def test_prepared_receipt_recovers_crash_after_fast_forward(world, monkeypatch):
    root, store = world
    claim, ctx, sub = accepted_one(world)
    worker = flow(store)
    original = worker._record
    seen = []

    def crash_once(kind, proof, **values):
        if kind == "claim_integrated" and not seen:
            seen.append(kind)
            raise RuntimeError("simulated process death after fast-forward")
        return original(kind, proof, **values)

    monkeypatch.setattr(worker, "_record", crash_once)
    with pytest.raises(RuntimeError, match="simulated process death"):
        worker.on_accept(claim, sub.revision)
    promoted = git(root, "rev-parse", "HEAD")
    assert (root / "src" / "shared.txt").read_text() == "first\n"
    monkeypatch.setattr(worker, "_record", original)
    replay = worker.on_accept(claim, sub.revision)
    assert replay.status == "already_integrated"
    assert replay.integrated_sha == promoted
    assert replay.gate_rc == 0
    with store.read() as tx:
        recovered = [json.loads(e["payload"]) for e in tx.query(
            "SELECT payload FROM events WHERE kind='claim_integrated'")]
    assert any(e.get("recovered") for e in recovered)


def test_unowned_files_never_part_of_reviewed_snapshot(world):
    root, store = world
    claim, ctx = take(store, "W-A", "TZ-A")
    (ctx.tree_path / "src" / "shared.txt").write_text("wanted\n")
    (ctx.tree_path / "other.txt").write_text("foreign\n")
    git(ctx.tree_path, "add", "other.txt")
    from dev_mcp.fleet.claim_review_bite import ClaimReviewError
    with pytest.raises(ClaimReviewError, match="outside accepted owns"):
        capture_submission(store, claim, "W-A")


def test_enabled_claim_gate_exception_fails_closed_after_ff(world):
    root, store = world
    claim, ctx, sub = accepted_one(world)

    def unavailable(_):
        raise OSError("gate executable missing")

    result = flow(store, gate=unavailable).on_accept(claim, sub.revision)
    assert result.status == "gate_failed" and result.gate_rc == -1
    assert result.integrated_sha == git(root, "rev-parse", "HEAD")
    assert result.repair_status == "styk_required"


def test_rejected_diff_binary_or_symlink(world):
    from dev_mcp.fleet.claim_accept_flow import _paths_and_tree
    root, store = world
    base = git(root, "rev-parse", "HEAD")
    (root / "src" / "shared.txt").write_bytes(b"some\x00binary\x00blob")
    git(root, "add", "src/shared.txt")
    git(root, "commit", "-m", "binary fixture")
    revision = git(root, "rev-parse", "HEAD")
    with pytest.raises(ClaimAcceptError, match="binary"):
        _paths_and_tree(root, base, revision, (str(root / "src"),))

def test_missing_gate_fails_before_mutating_trunk(world):
    root, store = world
    claim, ctx, sub = accepted_one(world)
    base = git(root, "rev-parse", "HEAD")
    worker = ClaimAcceptFlow(
        store, settings=lambda _: {"commit": True, "claim_trees": True})
    with pytest.raises(ClaimAcceptError, match="full trunk gate"):
        worker.on_accept(claim, sub.revision)
    assert git(root, "rev-parse", "HEAD") == base


def test_integrated_commit_carries_reviewer_trailer_for_guard(world):
    """Урок 56: страж выкатывает только коммиты с Fleet-Reviewer (или одобренные)."""
    root, store = world
    claim, ctx, sub = accepted_one(world)
    result = flow(store).on_accept(claim, sub.revision)
    message = git(root, "log", "--format=%B", "-n", "20", result.integrated_sha)
    assert "Fleet-Reviewer:" in message and "Fleet-Task:" in message
