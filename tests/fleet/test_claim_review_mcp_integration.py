"""TZ-41 public registered MCP review boundary, not only adapter unit calls."""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from dev_mcp.audit import Audit
from dev_mcp.fleet import claim_review_bite, goals, queue
from dev_mcp.fleet.claim_take_routing import resolve_active_claim_context
from dev_mcp.fleet.store import Store
from dev_mcp.policy import Policy
from dev_mcp.server import build_server


def tool(server, name):
    item = asyncio.run(server.get_tool(name))
    assert item
    if inspect.iscoroutinefunction(item.fn):
        return lambda *args, **kwargs: asyncio.run(item.fn(*args, **kwargs))
    return item.fn


def git(root, *parts):
    proc = subprocess.run(["git", "-C", str(root), *parts], capture_output=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def rig(tmp_path, monkeypatch, request):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "review@example.invalid")
    git(root, "config", "user.name", "Reviewer")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src/state.txt").write_text("bad\n", encoding="utf-8")
    (root / "tests/check.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('src/state.txt').read_text().strip() == 'good' else 9)\n",
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "bad baseline")
    baseline = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(goals, "project_info", lambda _: {
        "commit": True, "claim_trees": True,
    })
    # Keep the complete verified sealed execution path, but point the test
    # adapter at the already installed Python rather than creating a fake venv.
    native = claim_review_bite.reviewer_run
    monkeypatch.setattr(
        claim_review_bite, "reviewer_run",
        lambda store, cid, worker, argv, **kw: native(
            store, cid, worker, argv, python_executable=Path(sys.executable), **kw),
    )
    native_bite = claim_review_bite.check_fix_bite
    monkeypatch.setattr(
        claim_review_bite, "check_fix_bite",
        lambda store, cid, worker, argv, **kw: native_bite(
            store, cid, worker, argv,
            python_executable=Path(sys.executable), **kw),
    )
    def sealed_inspect(store, claim_id, reviewer, operation, path, *,
                       query="", pattern="*", max_results=50):
        # Deterministic production-contract adapter until the adjacent backend
        # claim integrates: sealed revision, active reviewer and owns only.
        submission, _, _ = claim_review_bite._submission(store, claim_id)
        claim_review_bite._review_claim(store, submission, reviewer)
        if operation == "list_dir":
            if path != "src":
                raise claim_review_bite.ClaimReviewError("outside owns")
            return [{"path": "src/state.txt", "is_dir": False}]
        if path not in ("src/state.txt", "src", "."):
            raise claim_review_bite.ClaimReviewError("outside owns")
        value = git(root, "show", submission.revision + ":src/state.txt")
        if operation == "read_file":
            if path != "src/state.txt":
                raise claim_review_bite.ClaimReviewError("not an owned file")
            return value + "\n"
        if operation == "search_files":
            if query.casefold() in value.casefold() and pattern in ("*", "*.txt"):
                return [{"path": "src/state.txt", "line": 1, "text": value}]
            return []
        raise claim_review_bite.ClaimReviewError("unsupported operation")
    monkeypatch.setattr(claim_review_bite, "reviewer_inspect",
                        sealed_inspect, raising=False)
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{tmp_path.as_posix()}']\n"
        f"write_roots = ['{tmp_path.as_posix()}']\n"
        f"exec_roots = ['{tmp_path.as_posix()}']\n"
        "exec_allowed_bin = ['python']\n"
        "allow_write = true\n"
        "deny_patterns = ['.git', '.env']\n"
        "exec_timeout_cap_s = 30\n",
        encoding="utf-8",
    )
    db = tmp_path / "fleet.sqlite"
    store = Store.open(str(db))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('demo',?,1,1)",
                  (str(root),), event={"kind": "seed"})
        for worker in ("WA", "WR", "WX", "WZ"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES(?,?,'chatgpt',?,?)", (worker, worker, now, now),
                      event={"kind": "seed"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('demo','request:test','request','ready',1,'request','','','[]',?,'[]',?,'',?,?)",
            (json.dumps([str(root / "src/state.txt")]),
             json.dumps(["python tests/check.py"]), now, now), event={"kind": "seed"},
        )
    server = build_server(Policy.load(config), Audit(tmp_path / "audit.jsonl"), fleet_db=db)
    assert "demo/request:test" in tool(server, "fleet_take")("WA")
    author_ctx = resolve_active_claim_context(store, "WA")
    assert author_ctx.mode == "isolated"
    initial_attempt = getattr(request, "param", "good\n")
    assert "перезаписан" in tool(server, "write_file")(
        str(root / "src/state.txt"), initial_attempt, True, "WA")
    assert "сдана на приёмку" in tool(server, "fleet_submit")("WA", "Done")
    assert "review:request:test" in tool(server, "fleet_take")("WR")
    with store.read() as tx:
        sealed = tx.one("SELECT payload FROM events WHERE kind='claim_submission'")
    assert sealed and json.loads(sealed["payload"])["author"] == "WA"
    yield server, store, root, author_ctx, baseline
    store.close()


def test_public_review_run_and_accept_only_immutable_submission(rig):
    server, store, root, author, baseline = rig
    run = tool(server, "run")
    assert "Отказано" in run(str(root), ["python", "tests/check.py"],
                            "", 20, False, "WX")
    assert "Отказано" in run(str(root), ["python", "tests/check.py"],
                            "src", 20, False, "WR")
    assert "Отказано" in run(str(root), ["python", "tests/other.py"],
                            "", 20, False, "WR")
    successful = run(str(root), ["python", "tests/check.py"], "", 20, False, "WR")
    assert "verified sealed review" in successful and "код возврата: 0" in successful
    assert "Вердикт accept сохранён" in tool(server, "fleet_review")("WR", "accept", "")
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='request:test'") == "accepted"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='claim_accept'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='claim_review_run' AND rc=0") == 1
        payload = json.loads(tx.one("SELECT payload FROM events WHERE kind='claim_submission'")["payload"])
    from dev_mcp.fleet.tools import FleetContext
    verified = FleetContext(store).verify_accepted_claim(
        payload["claim_id"], payload["revision"],
    )
    assert verified.reviewer == "WR" and verified.submission_revision == payload["revision"]
    assert (root / "src/state.txt").read_text() == "bad\n"
    assert git(root, "rev-parse", "HEAD") == baseline
    assert (author.tree_path / "src/state.txt").read_text() == "good\n"


def test_reviewer_reads_same_sealed_revision_as_acceptance(rig):
    server, store, root, author, _ = rig
    read = tool(server, "read_file")
    search = tool(server, "search_files")
    # Shared trunk and review checkout were both based on the *bad* commit.
    assert (root / "src/state.txt").read_text(encoding="utf-8") == "bad\n"
    own = str(root / "src/state.txt")
    reviewed = read(own, 1, 50, "WR")
    assert "good" in reviewed and "bad" not in reviewed
    assert "good" in search("good", own, "*.txt", False, 10, "WR")
    assert "Совпадений нет" in search("bad", own, "*.txt", False, 10, "WR")
    assert "good" in search("good", str(root), "*.txt", False, 10, "WR")
    assert "Совпадений нет" in search("raise SystemExit", str(root), "*.py", False, 10, "WR")
    projected = reviewed.split("  (строк всего:", 1)[0]
    # No temp snapshot path is exposed; copying the public source path cannot
    # grant another worker access to the sealed bytes.
    assert "good" not in read(projected, 1, 50, "WX")
    assert "good" in read(projected, 1, 50, "WR")
    assert "good" not in search("good", projected, "*.txt", False, 10, "WX")
    # Another worker without the trusted review claim sees no sealed source.
    assert "good" not in read(own, 1, 50, "WX")
    assert "Отказано" in read(str(author.tree_path / "src/state.txt"), 1, 50, "WR")


def test_reviewer_source_projection_denies_changed_author_and_unowned_paths(rig):
    server, store, root, author, _ = rig
    read = tool(server, "read_file")
    # Tests/check.py was not owned by the author claim, and must not be
    # presented as part of the author's sealed work.
    assert "Отказано" in read(str(root / "tests/check.py"), 1, 50, "WR")
    (author.tree_path / "src/state.txt").write_text("changed after submit\n", encoding="utf-8")
    assert "Отказано" in read(str(root / "src/state.txt"), 1, 50, "WR")


def test_registered_sealed_directory_contract_and_strict_type_validation(rig, monkeypatch):
    server, _, root, _, _ = rig
    listing = tool(server, "list_dir")
    source = str(root / "src")
    assert "state.txt" in listing(source, "*.txt", False, "WR")
    assert "state.txt" not in listing(source, "*.py", False, "WR")
    assert "Отказано" in listing(source, "*", True, "WR")

    # Contradictory backend variants must be refused rather than guessing.
    monkeypatch.setattr(claim_review_bite, "reviewer_inspect",
                        lambda *a, **kw: [
                            {"path": "src/state.txt", "type": "dir", "is_dir": False}])
    assert "Отказано" in listing(source, "*", False, "WR")


def test_public_review_refuses_missing_or_stale_acceptance(rig):
    server, store, root, author, _ = rig
    review = tool(server, "fleet_review")
    assert "Отказано" in review("WR", "accept", "")
    run = tool(server, "run")
    assert "код возврата: 0" in run(str(root), ["python", "tests/check.py"],
                                   "", 20, False, "WR")
    # A byte change after the accepted run cannot reuse the sealed proof.
    (author.tree_path / "src/state.txt").write_text("edited after review\n")
    assert "Отказано" in review("WR", "accept", "")
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='request:test'") == "submitted"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='claim_accept'") == 0


def test_flag_change_after_isolated_take_does_not_reopen_shared_run(rig, monkeypatch):
    server, store, root, author, _ = rig
    monkeypatch.setattr(goals, "project_info", lambda _: {
        "claim_trees": False, "commit": False,
    })
    run = tool(server, "run")
    result = run(str(root), ["python", "tests/check.py"], "", 20, False, "WR")
    assert "verified sealed review" in result and "код возврата: 0" in result
    assert "Вердикт accept сохранён" in tool(server, "fleet_review")("WR", "accept", "")
    assert (root / "src/state.txt").read_text() == "bad\n"
    assert (author.tree_path / "src/state.txt").read_text() == "good\n"


@pytest.mark.parametrize("rig", ["bad\n"], indirect=True)
def test_registered_fix_requires_real_red_to_green_bite(rig):
    server, store, root, author, baseline = rig
    review = tool(server, "fleet_review")
    assert "Вердикт reject сохранён" in review("WR", "reject", "The old implementation fails")
    with store.read() as tx:
        fixes = tx.query("SELECT tz,state FROM tasks WHERE kind='fix'")
    assert len(fixes) == 1
    fix_tz = fixes[0]["tz"]
    assert fix_tz == "fix:request:test"
    # The legacy tool auto-hands the next available work to the previous
    # reviewer. They must release it before a DIFFERENT fix author can take.
    if fixes[0]["state"] == "taken":
        assert queue.finish(store, "WR", "handoff: use a different fix author") == "ready"
    take = tool(server, "fleet_take")
    assert fix_tz in take("WX")
    fix_ctx = resolve_active_claim_context(store, "WX")
    assert "перезаписан" in tool(server, "write_file")(
        str(root / "src/state.txt"), "good\n", True, "WX")
    result = tool(server, "fleet_submit")(
        "WX", "A real fix.\nbite: python tests/check.py")
    assert "сдана на приёмку" in result, result
    assert "review:fix:request:test" in take("WZ")
    run = tool(server, "run")
    assert "код возврата: 0" in run(
        str(root), ["python", "tests/check.py"], "", 20, False, "WZ")
    assert "Вердикт accept сохранён" in review("WZ", "accept", "")
    with store.read() as tx:
        result = tx.one("SELECT payload FROM events WHERE kind='claim_bite'")
        assert result is not None
        verdict = json.loads(result["payload"])
        assert verdict["verdict"] == "passed"
        assert verdict["old_rc"] == 9 and verdict["new_rc"] == 0
        assert tx.scalar("SELECT state FROM tasks WHERE tz=?", (fix_tz,)) == "accepted"
    assert (root / "src/state.txt").read_text() == "bad\n"
    assert git(root, "rev-parse", "HEAD") == baseline


def test_final_fix_implicit_accept_must_still_have_valid_bite(rig):
    # The old source already passes, so a 'fix' repeating the same green
    # behavior cannot be accepted as a red-to-green bite.
    server, store, root, _, _ = rig
    review = tool(server, "fleet_review")
    assert "Вердикт reject сохранён" in review("WR", "reject", "Change is insufficient")
    with store.read() as tx:
        fix = tx.one("SELECT tz,state FROM tasks WHERE kind='fix'")
    if fix["state"] == "taken":
        assert queue.finish(store, "WR", "handoff: independent fix author") == "ready"
    assert fix["tz"] in tool(server, "fleet_take")("WX")
    from dev_mcp.fleet.review import FINAL_MARK
    with store.tx() as tx:
        tx.change("UPDATE tasks SET body=body || ? WHERE project='demo' AND tz=?",
                  ("\n" + FINAL_MARK, fix["tz"]), event={"kind": "seed_final_review"})
    assert "перезаписан" in tool(server, "write_file")(
        str(root / "src/state.txt"), "good\n", True, "WX")
    assert "сдана на приёмку" in tool(server, "fleet_submit")(
        "WX", "No meaningful regression improvement.\nbite: python tests/check.py")
    assert "review:fix:request:test" in tool(server, "fleet_take")("WZ")
    assert "код возврата: 0" in tool(server, "run")(
        str(root), ["python", "tests/check.py"], "", 20, False, "WZ")
    # Legacy final review silently rewrites a green reject to accept. Typed
    # proof must stop it because old_rc == new_rc == 0, not a real bite.
    refusal = review("WZ", "reject", "The alleged fix changes nothing")
    assert refusal.startswith("Отказано"), refusal
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz=?", (fix["tz"],)) == "submitted"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='claim_accept'") == 0
