"""TZ-338: invalid decisions never reach review; accepted failures open one repair."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import judgment
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Store, Path]:
    db = Store.open(str(tmp_path / "fleet.sqlite"))
    p = tmp_path / "P"
    (p / "ToDo" / "decisions").mkdir(parents=True)
    platform = tmp_path / "platform"
    platform.mkdir()
    with db.tx() as tx:
        for name, root in (("P", p), ("Fleet Platform", platform)):
            tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                      (name, str(root)), event={"kind": "test_setup"})
        for tz, state, kind, acceptance in (
            ("TZ-1", "ready", "work", '["py -3 -m pytest -q tests/one.py"]'),
            ("triage:1", "taken", "triage", "[]"),
        ):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES('P',?,?,?,1,?,'','','[]','[]','[]',?,'',?,?)",
                (tz, tz, state, kind, acceptance, _now(), _now()),
                event={"kind": "test_setup"},
            )
    return db, p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decision(store: Store, root: Path, action: str, **fields: str) -> None:
    path = root / "ToDo" / "decisions" / "TZ-1.md"
    path.write_text(
        "gitignored prose\n```yaml\ntz: TZ-1\naction: " + action + "\n"
        + "".join(f"{k}: {v}\n" for k, v in fields.items()) + "```\n",
        encoding="utf-8",
    )
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="triage:1",
                        worker_id="W1", path_norm=norm_path(str(path)))


def preflight(store: Store, root: Path) -> str:
    return judgment.triage_preflight(
        store, "P", "triage:1", "W1", "2000-01-01T00:00:00+00:00",
        checkout_root=root,
    )


@pytest.mark.parametrize("action", ["", "unknown"])
def test_empty_and_unknown_action_cannot_be_submitted(setup, action: str) -> None:
    store, root = setup
    decision(store, root, action)
    error = preflight(store, root)
    assert "action" in error and ("пуст" in error or "не из" in error)
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz='triage:1'") == "taken"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='proposal'") == 0


def test_self_until_on_target_acceptance_is_rejected(setup) -> None:
    store, root = setup
    decision(store, root, "park", until="py -3 -m pytest -q tests/one.py", why="waiting")
    assert "until" in preflight(store, root)


def test_empty_park_is_not_an_executable_decision(setup) -> None:
    store, root = setup
    decision(store, root, "park")
    assert "park требует" in preflight(store, root)


def test_isolated_preflight_checks_claim_tree_not_live_decision(setup, tmp_path: Path) -> None:
    store, live = setup
    decision(store, live, "")  # source root deliberately invalid
    tree = tmp_path / "isolated"
    (tree / "ToDo" / "decisions").mkdir(parents=True)
    (tree / "ToDo" / "decisions" / "TZ-1.md").write_text(
        "tz: TZ-1\naction: park\nwhy: external condition\n", encoding="utf-8",
    )
    assert "action" in preflight(store, live)
    assert judgment.triage_preflight(
        store, "P", "triage:1", "W1", "2000-01-01T00:00:00+00:00",
        checkout_root=tree,
    ) == ""


def test_valid_park_dry_run_then_actually_freezes_target(setup) -> None:
    store, root = setup
    decision(store, root, "park", until="external dependency", why="blocked")
    assert preflight(store, root) == ""
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'") == "ready"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz='triage:1'",
                  (), event={"kind": "accept", "project": "P", "tz": "triage:1"})
    assert judgment.apply_accepted(store)[0]["proposals"] == ["TZ-1: park"]
    assert judgment.apply_defaults(store) == ["P/TZ-1: park"]
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'") == "frozen"


def test_accepted_bad_decision_remains_accepted_and_opens_one_platform_repair(setup, monkeypatch) -> None:
    from dev_mcp.fleet import platform_manual
    monkeypatch.setattr(platform_manual,'enabled',lambda:True)
    store, root = setup
    decision(store, root, "")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz='triage:1'",
                  (), event={"kind": "accept", "project": "P", "tz": "triage:1"})
    first = judgment.apply_accepted(store)
    assert first[0]["status"] == "repair"
    assert judgment.apply_accepted(store) == []  # no replay/reopen after failure
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz='triage:1'") == "accepted"
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Fleet Platform' "
                         "AND tz LIKE 'repair:%'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_apply_error' "
                         "AND project='P' AND tz='triage:1'") == 1
        payload = json.loads(tx.one("SELECT payload FROM events WHERE kind='triage_apply_error' "
                                    "AND project='P' AND tz='triage:1'")["payload"])
        assert payload["fingerprint"].startswith("apply:")
    # Even when a controller explicitly schedules a replay of this accepted
    # version, the repair fingerprint is stable and cannot multiply tasks.
    with store.tx() as tx:
        tx.record_event(kind="triage_apply_reopen", project="P", tz="triage:1",
                        payload={"why": "test forced replay"})
    assert judgment.apply_accepted(store)[0]["status"] == "repair"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Fleet Platform' "
                         "AND tz LIKE 'repair:%'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1
