"""TZ-57: compare an actual candidate worktree against immutable registration."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import subprocess
from pathlib import Path

import pytest

import dev_mcp.fleet.acceptance_guard as acceptance_guard_module
from dev_mcp.fleet.acceptance_guard import RegisteredProject, blocking_findings, check
from dev_mcp.fleet import goals, queue
from dev_mcp.fleet.claim_take_routing import ClaimTakeRouting
from dev_mcp.fleet.shadow import HistoryEvent, ShadowRules
from dev_mcp.fleet.store import Store


def test_acceptance_guard_import_comes_from_checked_out_source() -> None:
    """An installed/editable package cannot mask a missing module in gate HEAD."""
    root = Path(__file__).resolve().parents[2]
    expected = root / "src" / "dev_mcp" / "fleet" / "acceptance_guard.py"
    assert Path(acceptance_guard_module.__file__).resolve() == expected.resolve()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args],
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


COMMAND = "python -m pytest -q tests/test_contract.py"
SPEC = ("'''yaml\n"  # replaced with a YAML code-fence below
        "'''")
BASE_TEST = (
    "def test_one():\n"
    "    assert 2 == 2\n\n"
    "def test_two():\n"
    "    assert 'x'.upper() == 'X'\n"
)


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[RegisteredProject, str]:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Fleet Test")
    git(tmp_path, "config", "user.email", "fleet@example.invalid")
    put(tmp_path, "ToDo/tz/TZ-1.md",
        "\x60\x60\x60yaml\n"
        "tz: TZ-1\n"
        "owns: [ToDo/tz/TZ-1.md, src/allowed.py, tests/test_contract.py]\n"
        "acceptance:\n"
        "  - " + COMMAND + "\n"
        "\x60\x60\x60\n\n# Example\n")
    put(tmp_path, "tests/test_contract.py", BASE_TEST)
    put(tmp_path, "src/allowed.py", "VALUE = 1\n")
    put(tmp_path, "src/foreign.py", "VALUE = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    context = RegisteredProject(
        root=tmp_path, checkout=tmp_path,
        acceptance=(COMMAND,),
        owns=("ToDo/tz/TZ-1.md", "src/allowed.py", "tests/test_contract.py"),
        spec_path="ToDo/tz/TZ-1.md",
    )
    return context, base


def findings(candidate: tuple[RegisteredProject, str], *paths: str) -> list[str]:
    ctx, sha = candidate
    return check(ctx, "TZ-1", sha, paths)


def test_unchanged_candidate_passes(candidate):
    assert findings(candidate) == []


def test_less_assert_rejected(candidate):
    ctx, _ = candidate
    put(ctx.checkout, "tests/test_contract.py", BASE_TEST.replace("    assert 2 == 2\n", "    pass\n"))
    assert any("assert removed" in e for e in findings(candidate))


def test_weak_assert_same_count_rejected(candidate):
    ctx, _ = candidate
    put(ctx.checkout, "tests/test_contract.py",
        BASE_TEST.replace("assert 2 == 2", "assert True"))
    assert any("assert removed or weakened" in e for e in findings(candidate))


@pytest.mark.parametrize("added", [
    "@pytest.mark.skip(reason='off')\n",
    "@pytest.mark.xfail(reason='off')\n",
    "pytest.skip('off')\n",
])
def test_new_skip_or_xfail_rejected(candidate, added):
    ctx, _ = candidate
    put(ctx.checkout, "tests/test_contract.py",
        "import pytest\n\n" + added + BASE_TEST)
    assert any("skip/xfail introduced" in e for e in findings(candidate))


def test_deleted_test_function_rejected(candidate):
    ctx, _ = candidate
    put(ctx.checkout, "tests/test_contract.py",
        BASE_TEST.split("\ndef test_two():")[0] + "\n")
    assert any("test functions removed" in e for e in findings(candidate))


def test_changed_command_rejected(candidate):
    ctx, _ = candidate
    path = ctx.checkout / ctx.spec_path
    path.write_text(path.read_text().replace(COMMAND, "python -m pytest -q"),
                    encoding="utf-8")
    assert any("acceptance changed" in e for e in findings(candidate))


def test_owner_registered_new_command_can_match(candidate):
    ctx, _ = candidate
    new = "python -m pytest -q tests"
    path = ctx.checkout / ctx.spec_path
    path.write_text(path.read_text().replace(COMMAND, new), encoding="utf-8")
    assert check(replace(ctx, acceptance=(new,)), "TZ-1", candidate[1], []) == []


def test_foreign_file_rejected_even_if_candidate_list_lies(candidate):
    ctx, _ = candidate
    put(ctx.checkout, "src/foreign.py", "VALUE = 2\n")
    assert any("outside owns: src/foreign.py" in e for e in findings(candidate, "src/allowed.py"))


def test_untracked_foreign_file_rejected(candidate):
    ctx, _ = candidate
    put(ctx.checkout, "src/secret_new.py", "VALUE = 2\n")
    assert any("outside owns: src/secret_new.py" in e for e in findings(candidate))


def test_normal_change_plus_new_test_passes(candidate):
    ctx, _ = candidate
    put(ctx.checkout, "src/allowed.py", "VALUE = 2\n")
    put(ctx.checkout, "tests/test_contract.py",
        BASE_TEST + "\ndef test_three():\n    assert (2 + 2) == 4\n")
    assert findings(candidate) == []


def test_shadow_produces_findings_but_not_submit_veto(candidate):
    ctx, _ = candidate
    put(ctx.checkout, "src/foreign.py", "VALUE = 9\n")
    errors = findings(candidate)
    assert errors
    assert blocking_findings(errors, shadow=True) == []
    assert blocking_findings(errors, shadow=False) == errors


def test_invalid_base_ref_fails_closed(candidate):
    ctx, _ = candidate
    assert any("base_sha" in e for e in check(ctx, "TZ-1", "HEAD", []))


@pytest.fixture
def submitting_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real registered queue + two private Git candidates, with a clean trunk."""
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    put(root, "src/allowed.py", "VALUE = 1\n")
    put(root, "src/foreign.py", "VALUE = 1\n")
    put(root, "tests/test_contract.py", BASE_TEST)
    for tz in ("TZ-1", "TZ-2"):
        put(root, f"ToDo/tz/{tz}.md",
            "\x60\x60\x60yaml\n"
            f"tz: {tz}\n"
            f"owns: [ToDo/tz/{tz}.md, src/allowed.py, tests/test_contract.py]\n"
            "acceptance:\n"
            "  - " + COMMAND + "\n"
            "\x60\x60\x60\n\n# Submission integration\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "registered candidate baseline")
    base_sha = git(root, "rev-parse", "HEAD")
    store = Store.open(str(tmp_path / "queue.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("GuardDemo", str(root)), event="seed")
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES(?,?,?,?,?)", ("W-guard", "guard", "chatgpt", now, now),
                  event="seed")
        for tz in ("TZ-1", "TZ-2"):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
                "spec_sha,needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("GuardDemo", tz, tz, "ready", 1, "request",
                 f"ToDo/tz/{tz}.md", base_sha, "[]",
                 json.dumps([str(root / f"ToDo/tz/{tz}.md"),
                             str(root / "src/allowed.py"),
                             str(root / "tests/test_contract.py")]),
                 "[]", json.dumps([COMMAND]), "check real diff", now, now),
                event="seed",
            )
    monkeypatch.setattr(goals, "project_info",
                        lambda name: {"commit": True, "claim_trees": True})
    return store, root, base_sha


def _take_private_candidate(store: Store, tz: str):
    task = queue.take_next(store, "W-guard", "GuardDemo")
    assert task is not None and task.tz == tz
    context = ClaimTakeRouting(store).resolve_active_claim_context("W-guard")
    assert context.mode == "isolated"
    assert context.base_sha and context.tree_path != context.project_root
    return context


def _state(store: Store, tz: str) -> tuple[str, str]:
    with store.read() as tx:
        task = tx.one("SELECT state FROM tasks WHERE project='GuardDemo' AND tz=?", (tz,))
        claim = tx.one("SELECT state FROM claims WHERE project='GuardDemo' AND tz=? "
                       "ORDER BY id DESC LIMIT 1", (tz,))
    return str(task["state"]), str(claim["state"])


def test_queue_submit_sealed_candidate_shadow_logs_weakened_assert_and_never_vetoes(
    submitting_claims,
):
    from dev_mcp.fleet.claim_review_bite import capture_submission

    store, root, base_sha = submitting_claims
    first = _take_private_candidate(store, "TZ-1")
    put(first.tree_path, "tests/test_contract.py",
        BASE_TEST.replace("assert 2 == 2", "assert True"))
    registration = RegisteredProject.from_store(
        store, "GuardDemo", "TZ-1", checkout=first.tree_path)
    errors = check(registration, "TZ-1", first.base_sha, [])
    assert any("assert removed or weakened" in item for item in errors)
    with store.read() as tx:
        first_id = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W-guard' AND state='active'"))
    assert capture_submission(store, first_id, "W-guard") is not None

    queue.submit(store, "W-guard", "submitted in shadow")
    assert _state(store, "TZ-1") == ("submitted", "released")
    with store.read() as tx:
        observed = tx.query("SELECT kind,payload FROM events WHERE kind='would_block'")
    assert observed, "queue.submit must journal TZ-49 would_block in shadow mode"
    payload = "\n".join(str(row["payload"]) for row in observed)
    assert "assert removed or weakened" in payload
    assert all(json.loads(row["payload"])["code"] == "acceptance_guard"
               for row in observed), "shadow evidence must come from the sealed acceptance guard"
    assert git(root, "rev-parse", "HEAD") == base_sha
    assert git(root, "status", "--porcelain") == ""

    # TZ-49's generic promotion default is four hours. Even if a replay tries
    # to promote at hour five, TZ-57 explicitly requires 24 hours of shadow
    # and a separate enforcement decision, so the submission cannot be vetoed.
    rules = ShadowRules(store).all()
    assert rules, "submit must use the registered TZ-49 rule"
    candidate_rules = [r for r in rules.values() if r.family in {"zone", "project_gate"}]
    assert len(candidate_rules) == 1
    rule = candidate_rules[0]
    future = datetime.now(timezone.utc) + timedelta(hours=5)
    history = [HistoryEvent(
        at=future - timedelta(hours=1), kind="would_block",
        subject="calibration/unfixed-case", revision="calibration-base",
        rule_id=rule.rule_id, fingerprint=rule.fingerprint,
        reason="unauthorized change",
    )]
    ShadowRules(store).advance(rule.rule_id, history=history, now=future)
    assert ShadowRules(store).get(rule.rule_id).mode == "enforce"

    second = _take_private_candidate(store, "TZ-2")
    put(second.tree_path, "tests/test_contract.py",
        BASE_TEST.replace("assert 2 == 2", "assert True"))
    with store.read() as tx:
        second_id = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W-guard' AND state='active'"))
    assert capture_submission(store, second_id, "W-guard") is not None
    queue.submit(store, "W-guard", "second candidate remains in shadow")
    assert _state(store, "TZ-2") == ("submitted", "released")
    with store.read() as tx:
        observed = tx.query("SELECT payload FROM events WHERE kind='acceptance_guard_shadow'")
        demoted = tx.query("SELECT payload FROM events WHERE kind='shadow_rule_demoted'")
    assert len(observed) == 2
    assert demoted, "premature generic TZ-49 promotion must be returned to TZ-57 shadow"
    assert ShadowRules(store).get(rule.rule_id).mode == "shadow"
    assert git(root, "rev-parse", "HEAD") == base_sha
    assert git(root, "status", "--porcelain") == ""
