"""TZ-147: automatic production integration must respect independent acceptance.

All Git effects are confined to pytest's temporary repository. In particular,
the accepted-only TZ-63 helper is *not* called directly: the oracle exercises
the production Integrator.tick entry-point after real queue/review transitions.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import goals, queue, review  # noqa: E402
from dev_mcp.fleet.integrate import Integrator  # noqa: E402
from dev_mcp.fleet.model import norm_path  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402


OWNED = "ToDo/TZ-operator.md"
STAGED = "staged_other.txt"
UNSTAGED = "unstaged_other.txt"
APPROVED = "accepted by independent review\n"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", check=True, timeout=30,
    )
    return result.stdout.strip()


@dataclass
class Scenario:
    store: Store
    root: Path
    integrator: Integrator
    baseline: str
    staged_index: str
    unstaged_index: str


@pytest.fixture()
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    root = tmp_path / "operator-repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fleet test")
    git(root, "config", "user.email", "fleet@example.invalid")
    (root / "ToDo").mkdir()
    for name in (OWNED, STAGED, UNSTAGED):
        path = root / name
        path.write_text("base\n", encoding="utf-8")
    git(root, "add", "--", OWNED, STAGED, UNSTAGED)
    git(root, "commit", "-q", "-m", "baseline")
    base = git(root, "rev-parse", "HEAD")

    store = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            ("Operator", str(root)), event={"kind": "test_setup"},
        )
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
            "spec_sha,needs,owns,resources,acceptance,author_worker,body,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("Operator", "TZ-operator", "operator regression", "ready", 5,
             "work", "", "", "[]", json.dumps([norm_path(str(root / OWNED))]),
             "[]", "[]", None, "test task", now, now),
            event={"kind": "test_setup"},
        )
        for worker in ("W-author", "W-independent"):
            tx.change(
                "INSERT INTO workers(id,label,kind,first_seen,last_seen)"
                " VALUES (?,?,'chatgpt',?,?)",
                (worker, worker, now, now), event={"kind": "test_setup"},
            )

    # The submit handler consults goals, the actual integrator uses its own
    # project policy injection; both point at the same commit-enabled project.
    monkeypatch.setattr(
        goals, "project_info",
        lambda _project: {"commit": True, "claim_trees": False},
    )
    integrator = Integrator(
        store, tmp_path / "integrate-state.json",
        project_info=lambda _project: {"commit": True, "claim_trees": False},
    )
    assert integrator.tick() == []  # initialize cursor *before* acceptance

    assert queue.take_next(store, "W-author", "Operator").tz == "TZ-operator"
    (root / OWNED).write_text(APPROVED, encoding="utf-8")
    (root / STAGED).write_text("other worker's staged bytes\n", encoding="utf-8")
    git(root, "add", "--", STAGED)
    (root / UNSTAGED).write_text("other worker's unstaged bytes\n", encoding="utf-8")

    return Scenario(
        store, root, integrator, base,
        git(root, "ls-files", "--stage", "--", STAGED),
        git(root, "ls-files", "--stage", "--", UNSTAGED),
    )


def independent_accept(s: Scenario) -> None:
    """Produce a genuine server-side submit seal and independent review accept."""
    queue.submit(s.store, "W-author", "ready for independent acceptance")
    with s.store.read() as tx:
        submit = tx.one(
            "SELECT id FROM events WHERE project='Operator' AND tz='TZ-operator'"
            " AND kind='submit' ORDER BY id DESC LIMIT 1"
        )
        receipt = tx.one(
            "SELECT id,tool,worker_id,payload FROM events WHERE"
            " project='Operator' AND tz='TZ-operator' AND"
            " kind='submission_file_seal' ORDER BY id DESC LIMIT 1"
        )
    assert submit is not None and receipt is not None
    assert receipt["id"] > submit["id"]
    assert receipt["tool"] == "fleet" and receipt["worker_id"] is None
    assert json.loads(receipt["payload"]) == {
        "submit_id": submit["id"],
        "files": {OWNED: hashlib.sha256((s.root / OWNED).read_bytes()).hexdigest()},
    }

    review.open_review(s.store, "Operator", "TZ-operator")
    assert queue.take_next(s.store, "W-independent", "Operator").tz == "review:TZ-operator"
    review.decide(s.store, "W-independent", "accept", "separate reviewer")
    with s.store.read() as tx:
        accepted = tx.one(
            "SELECT id,worker_id FROM events WHERE kind='accept' AND"
            " project='Operator' AND tz='TZ-operator' ORDER BY id DESC LIMIT 1"
        )
    assert accepted is not None
    assert accepted["id"] > receipt["id"]
    assert accepted["worker_id"] == "W-independent"


def assert_only_accepted_committed(s: Scenario) -> None:
    """Single oracle shared by production test and explicit old-path mutants."""
    head = git(s.root, "rev-parse", "HEAD")
    assert head != s.baseline, "integrator produced no automatic commit"
    assert git(s.root, "rev-parse", "HEAD^") == s.baseline
    paths = git(s.root, "show", "--format=", "--name-only", "HEAD").splitlines()
    assert paths == [OWNED], "integrator captured unrelated changes"
    assert git(s.root, "show", f"HEAD:{OWNED}") == APPROVED.strip()
    assert git(s.root, "show", f"HEAD:{STAGED}") == "base"
    assert git(s.root, "show", f"HEAD:{UNSTAGED}") == "base"
    assert git(s.root, "ls-files", "--stage", "--", STAGED) == s.staged_index
    assert git(s.root, "ls-files", "--stage", "--", UNSTAGED) == s.unstaged_index
    assert (s.root / STAGED).read_text(encoding="utf-8") == "other worker's staged bytes\n"
    assert (s.root / UNSTAGED).read_text(encoding="utf-8") == "other worker's unstaged bytes\n"
    assert git(s.root, "rev-list", "--count", "HEAD") == "2"


def test_real_entrypoint_commits_only_server_accepted_bytes_once(scenario: Scenario):
    s = scenario
    independent_accept(s)
    events = s.integrator.tick()  # actual production entry-point, no manual Git action
    assert any(e.get("payload", {}).get("commit") for e in events), events
    assert_only_accepted_committed(s)

    first_head = git(s.root, "rev-parse", "HEAD")
    assert s.integrator.tick() == []
    assert git(s.root, "rev-parse", "HEAD") == first_head
    assert_only_accepted_committed(s)


def test_late_foreign_worktree_bytes_are_deferred_without_commit(scenario: Scenario):
    s = scenario
    independent_accept(s)
    (s.root / OWNED).write_text("unattributed late edit\n", encoding="utf-8")
    records = s.integrator.tick()
    deferred = [e["payload"] for e in records
                if e.get("payload", {}).get("reason_code") == "accepted_candidate_bytes_changed"]
    assert deferred and OWNED in deferred[-1]["deferred_paths"], records
    assert git(s.root, "rev-parse", "HEAD") == s.baseline
    assert (s.root / OWNED).read_text(encoding="utf-8") == "unattributed late edit\n"
    assert git(s.root, "ls-files", "--stage", "--", STAGED) == s.staged_index


def test_submission_without_independent_review_never_commits(scenario: Scenario):
    s = scenario
    queue.submit(s.store, "W-author", "submitted but not accepted")
    assert s.integrator.tick() == []
    assert git(s.root, "rev-parse", "HEAD") == s.baseline


@pytest.mark.parametrize("legacy_fault", ["git-add-all", "wait-for-manual-commit"])
def test_oracle_bites_old_insecure_or_manual_paths(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, legacy_fault: str,
):
    s = scenario
    independent_accept(s)

    def legacy_commit(batch, _accept):
        if legacy_fault == "git-add-all":
            # Simulates pre-fix broad staging from the shared dirty worktree.
            git(s.root, "add", "-A")
            git(s.root, "commit", "-q", "-m", "unsafe legacy integration")
        # The other historical failure leaves commit to an operator.
        return {"skipped": legacy_fault}

    monkeypatch.setattr(s.integrator, "_commit", legacy_commit)
    s.integrator.tick()  # still the public production entry-point

    expected = (
        "integrator captured unrelated changes"
        if legacy_fault == "git-add-all"
        else "integrator produced no automatic commit"
    )
    with pytest.raises(AssertionError, match=expected):
        assert_only_accepted_committed(s)
