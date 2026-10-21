"""TZ-40: isolated take, durable trust binding and hostile paths.

Only temporary Git repositories / temporary queue DBs are used. The actual
file/run tool-layer integration is a separate request and is NOT claimed here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from dev_mcp.fleet import goals, queue
from dev_mcp.fleet import review
from dev_mcp.fleet.claim_review_bite import ClaimReviewError, capture_submission
from dev_mcp.fleet.claim_context import ClaimContext, ClaimRoutingError
from dev_mcp.fleet.claim_take_routing import (
    ClaimTakeRouting, claim_command_environment, prepare_claim_context,
    project_path_to_claim, resolve_claim_context, resolve_active_claim_context,
    sealed_review_projection, validate_claim_command_argv,
)
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.claim_tree import ClaimWorktrees
from dev_mcp.fleet.claim_review_bite import capture_submission


def git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, (args, out.stderr)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "Project with spaces"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "unit@example.invalid")
    git(root, "config", "user.name", "Unit")
    (root / "src").mkdir()
    (root / "src" / "owned.txt").write_text("trunk\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "base")
    return root


def seed(store: Store, root: Path, workers: tuple[str, ...] = ("W-A", "W-B"),
         tasks: tuple[str, ...] = ("TZ-A", "TZ-B"),
         same_owns: bool = False) -> None:
    now = "2026-10-10T00:00:00+00:00"
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Unit", str(root)), event={"kind": "seed"})
        for name in workers:
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES(?,?,?,?,?)", (name, name, "chatgpt", now, now),
                      event={"kind": "seed"})
        for i, tz in enumerate(tasks):
            owns = [str(root / "src" / ("owned.txt" if same_owns or i == 0 else "second.txt"))]
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES('Unit',?,'test','ready',1,'work','','','[]',?,'[]','[]','',?,?)",
                (tz, json.dumps(owns), now, now),
                event={"kind": "seed"},
            )


def ready_review(store: Store, root: Path, author_claim: int) -> None:
    now = "2026-10-10T00:01:00+00:00"
    with store.tx() as tx:
        tx.change("UPDATE claims SET state='released',released_at=? WHERE id=?",
                  (now, author_claim), event={"kind": "seed"})
        tx.change("UPDATE tasks SET state='submitted' WHERE project='Unit' AND tz='TZ-A'",
                  (), event={"kind": "seed"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('Unit','review:TZ-A','Review','ready',1,'review','','','[]',"
            "?,'[]','[]','',?,?)",
            (json.dumps([str(root / "src" / "owned.txt")]), now, now),
            event={"kind": "seed"},
        )


def test_review_checkout_is_exact_sealed_candidate_not_trunk(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store.open(str(tmp_path / "review.sqlite"))
    seed(store, repo, tasks=("TZ-A",))
    monkeypatch.setattr(goals, "project_info",
                        lambda name: {"commit": True, "claim_trees": True})
    assert queue.take_next(store, "W-A", "Unit")
    with store.read() as tx:
        author_claim = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W-A'"))
    author = resolve_claim_context(store, "W-A", author_claim)
    (author.tree_path / "src" / "owned.txt").write_text("candidate\n", encoding="utf-8")
    sealed = capture_submission(store, author_claim, "W-A")
    assert sealed is not None and sealed.revision != author.base_sha
    ready_review(store, repo, author_claim)

    review = queue.take_next(store, "W-B", "Unit")
    assert review is not None and review.tz == "review:TZ-A"
    with store.read() as tx:
        reviewer_claim = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W-B'"))
    context = resolve_claim_context(store, "W-B", reviewer_claim)
    assert context.base_sha == sealed.revision
    assert git(context.tree_path, "rev-parse", "HEAD") == sealed.revision
    assert (context.tree_path / "src" / "owned.txt").read_text() == "candidate\n"
    assert (repo / "src" / "owned.txt").read_text() == "trunk\n"
    (context.tree_path / "src" / "owned.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ClaimRoutingError, match="dirty files"):
        resolve_claim_context(store, "W-B", reviewer_claim)
    git(context.tree_path, "reset", "--hard", author.base_sha)
    with pytest.raises(ClaimRoutingError, match="sealed revision"):
        resolve_claim_context(store, "W-B", reviewer_claim)


def test_review_without_seal_does_not_fall_back_to_trunk(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store.open(str(tmp_path / "unsealed-review.sqlite"))
    seed(store, repo, tasks=("TZ-A",))
    monkeypatch.setattr(goals, "project_info",
                        lambda name: {"commit": True, "claim_trees": True})
    assert queue.take_next(store, "W-A", "Unit")
    with store.read() as tx:
        author_claim = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W-A'"))
    ready_review(store, repo, author_claim)
    assert queue.take_next(store, "W-B", "Unit") is None
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE worker_id='W-B'") == 0


def test_prepare_two_claims_isolated_and_command_environment(repo: Path):
    settings = {"commit": True, "claim_trees": True}
    sha = git(repo, "rev-parse", "HEAD")
    ctx1 = prepare_claim_context(claim_id=1, worker_id="W1", project="Unit", tz="TZ-A",
                                 project_root=repo, settings=settings)
    ctx2 = prepare_claim_context(claim_id=2, worker_id="W2", project="Unit", tz="TZ-B",
                                 project_root=repo, settings=settings)
    assert ctx1.mode == ctx2.mode == "isolated"
    assert ctx1.branch != ctx2.branch
    assert ctx1.tree_path != ctx2.tree_path
    assert ctx1.base_sha == ctx2.base_sha == sha
    p1 = project_path_to_claim(repo / "src" / "owned.txt", ctx1)
    p2 = project_path_to_claim("src/owned.txt", ctx2)
    p1.write_text("first\n", encoding="utf-8")
    p2.write_text("second\n", encoding="utf-8")
    assert (repo / "src" / "owned.txt").read_text(encoding="utf-8") == "trunk\n"
    assert p1.read_text(encoding="utf-8") == "first\n"
    assert p2.read_text(encoding="utf-8") == "second\n"
    assert git(repo, "rev-parse", "HEAD") == sha
    assert git(repo, "status", "--porcelain") == ""
    c = claim_command_environment(ctx1, {"PYTHONPATH": "old", "OTHER": "kept"})
    assert c.cwd == ctx1.tree_path and c.claim_id == 1 and c.tree_revision == sha
    assert str(ctx1.tree_path / "src") == c.environment["PYTHONPATH"].split(os.pathsep)[0]
    assert c.environment["VIRTUAL_ENV"] == str(repo / ".venv")
    assert c.python_executable == repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert prepare_claim_context(claim_id=1, worker_id="W1", project="Unit", tz="TZ-A",
                                 project_root=repo, settings=settings).tree_path == ctx1.tree_path


def test_queue_take_persists_binding_atomically_and_resolves(repo: Path, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    seed(store, repo)
    monkeypatch.setattr(goals, "project_info", lambda name: {"commit": True, "claim_trees": True})
    assert queue.take_next(store, "W-A", "Unit").tz == "TZ-A"
    with store.read() as tx:
        row = tx.one("SELECT c.id,e.payload FROM claims c JOIN events e "
                     "ON e.kind='take' AND e.worker_id=c.worker_id AND e.project=c.project "
                     "AND e.tz=c.tz WHERE c.worker_id='W-A'")
        claim_id = int(row["id"])
        payload = json.loads(row["payload"])
        assert payload["claim_id"] == claim_id
        assert payload["claim_context"]["worker_id"] == "W-A"
    ctx = resolve_claim_context(store, "W-A", claim_id)
    assert ctx.mode == "isolated"
    assert ctx.tree_path.is_dir()
    assert ClaimTakeRouting(store).resolve_claim_context("W-A", claim_id) == ctx
    assert resolve_active_claim_context(store, "W-A") == ctx
    with pytest.raises(ClaimRoutingError, match="active worker"):
        resolve_claim_context(store, "W-B", claim_id)
    with pytest.raises(ClaimRoutingError, match="no active claim"):
        resolve_active_claim_context(store, "W-B")
    with store.tx() as tx:
        tx.change("UPDATE events SET payload=? WHERE kind='take' AND worker_id='W-A'",
                  ('{"claim_id":1,"claim_context":{"mode":"shared"}}',), event={"kind": "test_injection"})
    with pytest.raises(ClaimRoutingError):
        resolve_claim_context(store, "W-A", claim_id)


def test_queue_allows_overlap_only_between_durable_isolated_claims(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store.open(str(tmp_path / "overlap.sqlite"))
    seed(store, repo, same_owns=True)
    monkeypatch.setattr(goals, "project_info", lambda name: {"commit": True, "claim_trees": True})
    first = queue.take_next(store, "W-A", "Unit")
    second = queue.take_next(store, "W-B", "Unit")
    assert first is not None and second is not None and first.tz != second.tz
    with store.read() as tx:
        a_id = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W-A'"))
        b_id = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W-B'"))
    a = resolve_claim_context(store, "W-A", a_id)
    b = resolve_claim_context(store, "W-B", b_id)
    assert a.tree_path != b.tree_path
    project_path_to_claim(repo / "src" / "owned.txt", a).write_text("A")
    project_path_to_claim(repo / "src" / "owned.txt", b).write_text("B")
    assert (repo / "src" / "owned.txt").read_text() == "trunk\n"


def test_shared_claim_keeps_old_overlap_exclusion(repo: Path, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
    store = Store.open(str(tmp_path / "shared-overlap.sqlite"))
    seed(store, repo, same_owns=True)
    monkeypatch.setattr(goals, "project_info", lambda name: {"commit": False, "claim_trees": False})
    assert queue.take_next(store, "W-A", "Unit") is not None
    assert queue.take_next(store, "W-B", "Unit") is None


def test_shared_mode_preserves_legacy_without_git(tmp_path: Path):
    root = tmp_path / "not-a-git-repo"
    root.mkdir()
    for flags in ({}, {"commit": False, "claim_trees": True},
                  {"commit": True, "claim_trees": False}):
        context = prepare_claim_context(claim_id=7, worker_id="W", project="Shared",
                                        tz="TZ", project_root=root, settings=flags)
        assert context.mode == "shared"
        assert context.tree_path == root
        assert project_path_to_claim("new.txt", context) == root / "new.txt"
        env = claim_command_environment(context, {"PYTHONPATH": "unchanged"})
        assert env.cwd == root and env.environment == {"PYTHONPATH": "unchanged"}


def test_dirty_trunk_and_stale_base_fail_closed(repo: Path):
    settings = {"commit": True, "claim_trees": True}
    (repo / "scratch.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(ClaimRoutingError, match="dirty trunk"):
        prepare_claim_context(claim_id=11, worker_id="W", project="Unit", tz="TZ",
                              project_root=repo, settings=settings)
    assert not ClaimWorktrees(repo).path("claim-11").exists()
    (repo / "scratch.txt").unlink()
    previous = prepare_claim_context(claim_id=11, worker_id="W", project="Unit",
                                     tz="TZ", project_root=repo, settings=settings)
    (repo / "head.txt").write_text("later", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "advanced")
    with pytest.raises(ClaimRoutingError, match="stale"):
        prepare_claim_context(claim_id=11, worker_id="W", project="Unit", tz="TZ",
                              project_root=repo, settings=settings)
    assert previous.tree_path.is_dir()


def test_path_boundary_and_traversal_foreign_symlink(repo: Path, tmp_path: Path):
    info = {"commit": True, "claim_trees": True}
    context = prepare_claim_context(claim_id=1, worker_id="W", project="Unit", tz="TZ",
                                    project_root=repo, settings=info)
    foreign = prepare_claim_context(claim_id=2, worker_id="X", project="Unit", tz="TZ2",
                                    project_root=repo, settings=info)
    with pytest.raises(ClaimRoutingError, match="traversal"):
        project_path_to_claim("../outside.txt", context)
    with pytest.raises(ClaimRoutingError, match="foreign claim"):
        project_path_to_claim(foreign.tree_path / "src" / "owned.txt", context)
    outside = tmp_path / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    assert project_path_to_claim(outside, context, read_only=True) == outside
    with pytest.raises(ClaimRoutingError, match="outside"):
        project_path_to_claim(outside, context)
    with pytest.raises(ClaimRoutingError, match="declared owns"):
        project_path_to_claim(repo / "src" / "owned.txt", context, owns=(str(repo / "elsewhere"),))
    symlink = repo / "src" / "escape.txt"
    try:
        symlink.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ClaimRoutingError, match="symlink"):
        project_path_to_claim(symlink, context)


def test_claim_context_rejects_bad_or_forged_event(repo: Path):
    good = prepare_claim_context(claim_id=1, worker_id="W", project="Unit", tz="TZ",
                                 project_root=repo,
                                 settings={"commit": True, "claim_trees": True})
    assert ClaimContext.from_event(good.as_event()) == good
    for edit in (
        {"mode": "untrusted"}, {"claim_id": 0}, {"tree_path": str(repo)},
        {"base_sha": ""}, {"worker_id": ""},
    ):
        data = {**good.as_event(), **edit}
        if edit == {"tree_path": str(repo)}:
            # Structural parse permits an existing directory: resolver still
            # verifies exact branch/path via manager._verified_path.
            assert ClaimContext.from_event(data).tree_path == repo
        else:
            with pytest.raises(ClaimRoutingError):
                ClaimContext.from_event(data)


def test_absolute_argv_traversal_and_inline_code_are_rejected(repo: Path, tmp_path: Path):
    context = prepare_claim_context(claim_id=3, worker_id="W", project="Unit", tz="TZ",
                                    project_root=repo,
                                    settings={"commit": True, "claim_trees": True})
    assert validate_claim_command_argv(["python", "-m", "pytest", "-q"], context) == (
        "python", "-m", "pytest", "-q",
    )
    assert validate_claim_command_argv([str(repo / ".venv" / "Scripts" / "python.exe"),
                                        "-m", "pytest"], context)
    for argv in (
        ["python", str(repo / "src" / "owned.txt")],
        ["python", str(tmp_path / "outside.py")],
        ["python", "--output=" + str(repo / "out.txt")],
        ["python", "../src/owned.txt"],
        ["python", "-c", "print('not confined')"],
        [],
    ):
        with pytest.raises(ClaimRoutingError):
            validate_claim_command_argv(argv, context)


def test_sealed_projection_exposes_author_only_files_not_trunk(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store.open(str(tmp_path / "review.sqlite"))
    seed(store, repo, workers=("W-A", "W-R", "W-X"), tasks=("TZ-A",))
    with store.tx() as tx:
        tx.change("UPDATE tasks SET owns=? WHERE project='Unit' AND tz='TZ-A'",
                  (json.dumps([str(repo / "src")]),), event={"kind": "seed_owns"})
    monkeypatch.setattr(goals, "project_info",
                        lambda _: {"commit": True, "claim_trees": True})
    assert queue.take_next(store, "W-A", "Unit").tz == "TZ-A"
    context = resolve_active_claim_context(store, "W-A")
    relative = Path("src") / "submitted-only.py"
    (context.tree_path / relative).write_text("RESULT = 'immutable'\n", encoding="utf-8")
    submission = capture_submission(store, context.claim_id, "W-A")
    assert submission is not None
    with store.tx() as tx:
        tx.changes([
            ("UPDATE tasks SET state='submitted',author_worker='W-A' "
             "WHERE project='Unit' AND tz='TZ-A'", ()),
            ("UPDATE claims SET state='released' WHERE id=?", (context.claim_id,)),
        ], event={"kind": "submit", "project": "Unit", "tz": "TZ-A",
                  "worker_id": "W-A", "payload": {"claim_id": context.claim_id}})
    review.open_review(store, "Unit", "TZ-A")
    assert queue.take_next(store, "W-R", "Unit").tz.startswith("review:")

    projection = sealed_review_projection(store, "W-R", repo / relative)
    assert projection is not None
    assert projection.root == repo and projection.relative == relative
    assert projection.source_claim_id == context.claim_id
    assert projection.revision == submission.revision
    assert projection.is_owned(relative)
    assert not projection.is_owned("README.md")
    assert git(repo, "show", f"{projection.revision}:{relative.as_posix()}") == "RESULT = 'immutable'"
    assert not (repo / relative).exists()  # base checkout does not contain new author file
    assert sealed_review_projection(store, "W-A", repo / relative) is None
    assert sealed_review_projection(store, "W-X", repo / relative) is None
    assert sealed_review_projection(store, "W-R", tmp_path / "outside.txt") is None
    with pytest.raises(ClaimRoutingError, match="invalid sealed review path"):
        sealed_review_projection(store, "W-R", repo / "src" / ".." / "src" / "submitted-only.py")

    # A read is bound to the private immutable ref, not the mutable author tree
    # nor a forged claim ID. A changed reference must fail closed.
    git(repo, "update-ref", f"refs/fleet/submissions/{submission.submission_id}",
        context.base_sha)
    with pytest.raises(ClaimReviewError, match="reference changed"):
        sealed_review_projection(store, "W-R", repo / relative)
    store.close()
