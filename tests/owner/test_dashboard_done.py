"""Regression checks for both owner_dashboard_done contracts without FastMCP.

Extract and execute the actual nested handlers: a local FastMCP installation is
not needed, but the production function bodies and real SQLite Store are used.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from dev_mcp.fleet import outcome  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402


@pytest.fixture()
def harness(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    source = ROOT / "src" / "dev_mcp" / "server.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    build = next(node for node in module.body
                 if isinstance(node, ast.FunctionDef) and node.name == "build_server")
    names = {"_owner_done_action", "owner_dashboard_done"}
    handlers = [node for node in build.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in handlers} == names
    for handler in handlers:
        handler.decorator_list = []

    overview = SimpleNamespace(snapshot=lambda *_args, **_kwargs: {"status": "empty"})
    env = {
        "json": json,
        "re": re,
        "datetime": datetime,
        "timezone": timezone,
        "fleet_store": store,
        "owner_overview": overview,
        "_dashboard_owner": lambda token: token == "owner-token",
        "_dashboard_denied": lambda: json.dumps({"status": "error", "reason": "owner_denied"}),
    }
    handlers_ast = ast.fix_missing_locations(ast.Module(body=handlers, type_ignores=[]))
    exec(compile(handlers_ast, str(source), "exec"), env)
    yield SimpleNamespace(store=store, call=env["owner_dashboard_done"], overview=overview)
    store.close()


def result(harness, **kwargs):
    return json.loads(harness.call("owner-token", **kwargs))


def action_events(store, project="P"):
    with store.read() as tx:
        return tx.query("SELECT payload FROM events WHERE kind='owner.done' AND project=?",
                        (project,))


def test_auth_missing_mixed_and_invalid_selector_are_fail_closed(harness):
    assert json.loads(harness.call(
        "not-an-owner", project="P", action_id="b" * 20, expected_revision="a" * 64,
    ))["reason"] == "owner_denied"
    assert result(harness)["reason"] == "invalid_action_reference"
    assert result(harness, project="P", action_id="b" * 20,
                  expected_revision="a" * 64,
                  obstacle_id="obstacle")["reason"] == "mixed_action_reference"
    assert result(harness, project=" P ", action_id="b" * 20,
                  expected_revision="a" * 64)["reason"] == "invalid_owner_action"
    assert result(harness, project="P", action_id="wrong",
                  expected_revision="a" * 64)["reason"] == "invalid_owner_action"
    assert result(harness, project="P", action_id="b" * 20,
                  expected_revision="wrong")["reason"] == "invalid_owner_action"
    assert action_events(harness.store) == []


def test_action_receipt_atomic_revision_checked_and_idempotent(harness):
    revision, action_id = "a" * 64, "b" * 20
    checks = []

    def snapshot(current_store, *, project=None):
        assert current_store is harness.store and project is None
        # Stronger than a before/after snapshot test: the *actual* callback
        # executes within the same SQLite write transaction as event insertion.
        assert getattr(current_store._local, "active_tx", None) is not None
        checks.append(True)
        return {"status": "ready", "revision": revision, "human": {
            "actions": [{"action_id": action_id, "project": "P", "done": False}],
        }}

    harness.overview.snapshot = snapshot
    accepted = result(harness, project="P", action_id=action_id,
                      expected_revision=revision)
    assert accepted == {"status": "ready", "project": "P",
                        "action_id": action_id, "already_done": False}
    assert checks
    rows = action_events(harness.store)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"]) == {
        "action_id": action_id, "revision": revision, "source": "owner_dashboard",
    }

    # Do not accept a different expected revision just because this action ID
    # already has a receipt; retries of the same revision remain idempotent.
    assert result(harness, project="P", action_id=action_id,
                  expected_revision="c" * 64)["reason"] == "stale_revision"
    harness.overview.snapshot = lambda *_a, **_kw: pytest.fail(
        "idempotent replay must not require the action to remain visible"
    )
    repeated = result(harness, project="P", action_id=action_id,
                      expected_revision=revision)
    assert repeated["already_done"] is True
    assert len(action_events(harness.store)) == 1


def test_action_revision_mismatch_missing_actions_and_done_flag(harness):
    revision, action_id = "a" * 64, "b" * 20
    state = {"status": "ready", "revision": "c" * 64, "human": {
        "actions": [{"action_id": action_id, "project": "P", "done": False}],
    }}
    harness.overview.snapshot = lambda *_a, **_kw: state
    assert result(harness, project="P", action_id=action_id,
                  expected_revision=revision)["reason"] == "stale_revision"
    state["revision"] = revision
    state.pop("human")
    assert result(harness, project="P", action_id=action_id,
                  expected_revision=revision)["reason"] == "action_unavailable"
    state["human"] = {"actions": [
        {"action_id": action_id, "project": "P", "done": True},
    ]}
    assert result(harness, project="P", action_id=action_id,
                  expected_revision=revision)["reason"] == "action_unavailable"
    state["status"] = "error"
    assert result(harness, project="P", action_id=action_id,
                  expected_revision=revision)["reason"] == "action_unavailable"
    assert action_events(harness.store) == []


def test_legacy_obstacle_confirmation_and_stale_subject_version(harness):
    obj = outcome.create_outcome(
        harness.store, project="P", title="Synthetic", intent_id="intent-P",
        spec={"dashboard_visibility": "public"},
        evidence_spec={"subject_version": "v1"}, authority={"token": "synthetic"},
    )
    obstacle_id = outcome.open_obstacle(
        harness.store, outcome_id=obj.id, type="owner_action",
        cause_code="owner_taste", predicate={},
        owner_action="Synthetic owner decision",
    )
    kwargs = {"obstacle_id": obstacle_id, "outcome_id": obj.id,
              "expected_subject_version": "v1"}
    assert result(harness, **kwargs) == {
        "status": "recorded_pending_verification",
        "obstacle_id": obstacle_id, "outcome_id": obj.id,
        "subject_version": "v1", "replayed": False,
    }
    assert result(harness, **kwargs)["replayed"] is True
    stale = dict(kwargs, expected_subject_version="v2")
    assert result(harness, **stale)["reason"] == "stale_action_reference"
    with harness.store.read() as tx:
        receipt = tx.one(
            "SELECT outcome_id,subject_version FROM owner_dashboard_done_receipts WHERE obstacle_id=?",
            (obstacle_id,),
        )
        obstacle = tx.one("SELECT state FROM obstacles WHERE id=?", (obstacle_id,))
        obj_row = tx.one("SELECT state FROM outcomes WHERE id=?", (obj.id,))
        assert receipt["outcome_id"] == obj.id
        assert receipt["subject_version"] == "v1"
        assert obstacle["state"] == obj_row["state"] == "open"
    assert action_events(harness.store) == []
