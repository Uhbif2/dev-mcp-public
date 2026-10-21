"""Private remote delivery: fake gh/git network, real Git history and durable events."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet.store import Store
from dev_mcp.fleet import outcome
from dev_mcp.intents import remote_repo as rr


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "component.json").write_text(json.dumps({"name": "demo", "kind": "intent-outcome-tree"}))
    (root / "README.md").write_text("hello", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost",
         "commit", "-qm", "first")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    kwargs = {"project": "demo", "intent_id": "intent-demo", "spec": {"kind": "stage"},
              "evidence_spec": {"kind": "run", "subject_version": "rev:1"},
              "authority": {"level": "A1"}}
    outcome.create_outcome(store, title="root", outcome_id="root", **kwargs)
    outcome.create_outcome(store, title="understood", parent_id="root", outcome_id="stage", **kwargs)
    outcome.close_outcome(store, "stage")
    monkeypatch.setattr(rr, "_is_live_project", lambda *_: True)
    monkeypatch.setattr(rr, "_has_proof", lambda *_: True)
    monkeypatch.setattr(rr, "OWNER", "example-owner")
    policy = tmp_path / 'scan-policy.toml'
    policy.write_text("deny_patterns = ['*.private']\n", encoding='utf-8')
    monkeypatch.setattr(rr, 'POLICY_PATH', policy)
    yield store, root
    store.close()


def _fake_network(monkeypatch: pytest.MonkeyPatch, *, public: bool = False,
                  fail_push: bool = False) -> dict[str, object]:
    real = rr._exec
    state: dict[str, object] = {"exists": False, "create": 0, "push": 0,
                                "fail_push": fail_push, "calls": []}

    def fake(args: list[str], root: Path) -> subprocess.CompletedProcess[bytes]:
        state["calls"].append(tuple(args))
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0 if state["exists"] else 1,
                                               b"PUBLIC\n" if public else b"PRIVATE\n")
        if args[:3] == ["gh", "repo", "create"]:
            state["create"] += 1
            state["exists"] = True
            return subprocess.CompletedProcess(args, 0, b"")
        if args[:2] == ["git", "push"]:
            state["push"] += 1
            rc = 1 if state["fail_push"] else 0
            state["fail_push"] = False
            return subprocess.CompletedProcess(args, rc, b"")
        return real(args, root)

    monkeypatch.setattr(rr, "_exec", fake)
    return state


def test_creates_private_repo_once_and_reuses_commit(project, monkeypatch) -> None:
    store, root = project
    network = _fake_network(monkeypatch)
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "pushed"
    assert network["create"] == 1 and network["push"] == 1
    assert ("gh", "repo", "create", "example-owner/demo", "--private") in network["calls"]
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "unchanged"
    assert network["create"] == 1 and network["push"] == 1
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='intent.remote_pushed'") == 1


def test_content_secret_in_tree_blocks_every_external_call(project, monkeypatch) -> None:
    store, root = project
    network = _fake_network(monkeypatch)
    (root / "notes.txt").write_text("sk-" + "A" * 28, encoding="ascii")
    result = rr.push_after_proven_stage(store, "demo", root)
    assert result["status"] == "blocked"
    assert not any(args[0] == "gh" or args[:2] == ("git", "push")
                   for args in network["calls"])
    with store.read() as tx:
        row = tx.one("SELECT payload FROM events WHERE kind='intent.remote_owner_action'")
    assert row and "Resolve unsafe" in row["payload"]
    assert "A" * 28 not in row["payload"]
    with store.read() as tx:
        obstacle = tx.one("SELECT type,owner_action,state FROM obstacles WHERE id=?",
                          (rr._obstacle_id("demo"),))
    assert obstacle and obstacle["type"] == "owner_action" and obstacle["state"] == "open"
    assert "GitHub" in obstacle["owner_action"]


def test_owner_action_resolves_when_tree_becomes_safe(project, monkeypatch) -> None:
    store, root = project
    network = _fake_network(monkeypatch)
    secret = root / "private.key"
    secret.write_text("not a real key", encoding="ascii")
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "blocked"
    secret.unlink()
    with store.tx() as tx:
        tx.change("UPDATE events SET ts=? WHERE kind='intent.remote_attempt'",
                  ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),),
                  event="test.elapsed")
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "pushed"
    with store.read() as tx:
        row = tx.one("SELECT state FROM obstacles WHERE id=?", (rr._obstacle_id("demo"),))
    assert row["state"] == "resolved"
    assert network["push"] == 1


def test_policy_denied_path_blocks_even_when_gitignored(project, monkeypatch) -> None:
    store, root = project
    network = _fake_network(monkeypatch)
    (root / ".env").write_text("no live secret content", encoding="utf-8")
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "blocked"
    assert not any(args[0] == "gh" for args in network["calls"])


def test_deleted_historical_secret_still_blocks(project, monkeypatch) -> None:
    store, root = project
    network = _fake_network(monkeypatch)
    path = root / "settings.txt"
    path.write_text("ghp_" + "B" * 30)
    _git(root, "add", "settings.txt")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost",
         "commit", "-qm", "bad history")
    path.unlink()
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost",
         "commit", "-qm", "remove secret")
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "blocked"
    assert not any(args[0] == "gh" for args in network["calls"])


def test_retry_after_failure_respects_persistent_throttle(project, monkeypatch) -> None:
    store, root = project
    network = _fake_network(monkeypatch, fail_push=True)
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "retry"
    assert network["create"] == 1 and network["push"] == 1
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "throttled"
    with store.tx() as tx:
        tx.change("UPDATE events SET ts=? WHERE kind='intent.remote_attempt'",
                  ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),),
                  event="test.elapsed")
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "pushed"
    assert network["create"] == 1 and network["push"] == 2


def test_public_remote_is_blocked_without_push(project, monkeypatch) -> None:
    store, root = project
    network = _fake_network(monkeypatch, public=True)
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "blocked"
    assert network["push"] == 0


def test_test_store_never_executes_gh_or_git_network(tmp_path, monkeypatch) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    monkeypatch.setattr(rr, "_exec", lambda *_: pytest.fail("external command attempted"))
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "skipped"
    store.close()


def test_unproven_stage_never_pushes(project, monkeypatch) -> None:
    store, root = project
    monkeypatch.setattr(rr, "_has_proof", lambda *_: False)
    monkeypatch.setattr(rr, "_exec", lambda *_: pytest.fail("git called before proof"))
    assert rr.push_after_proven_stage(store, "demo", root)["status"] == "skipped"


def test_missing_security_policy_blocks_every_network_write(project, monkeypatch, tmp_path):
    store, root = project
    network = _fake_network(monkeypatch)
    monkeypatch.setattr(rr, 'POLICY_PATH', tmp_path / 'absent-policy.toml')
    assert rr.push_after_proven_stage(store, 'demo', root)['status'] == 'blocked'
    assert not any(args[0] == 'gh' or args[:2] == ('git', 'push') for args in network['calls'])
