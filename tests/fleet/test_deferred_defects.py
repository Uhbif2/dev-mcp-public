"""Regressions from 2026-10-09 autonomy review, isolated from the live fleet DB."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet import evidence, judgment, mill, probe, queue
from dev_mcp.fleet.claim_context import ClaimContext
from dev_mcp.fleet.claim_take_routing import ClaimTakeRouting
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store
from tools import migrate_state_20261009 as migration


def stamp(minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    db = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "P"
    (root / "ToDo" / "decisions").mkdir(parents=True)
    with db.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("P", str(root)), event={"kind": "test_setup"})
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Mill Works", str(tmp_path / "millroot")), event={"kind": "test_setup"})
        tx.change("INSERT INTO workers(id,label,first_seen,last_seen) VALUES('W1','w',?,?)",
                  (stamp(), stamp()), event={"kind": "test_setup"})
    return db


def task(store: Store, tz: str, *, state: str = "frozen", kind: str = "work",
         needs: tuple[str, ...] = (), acceptance: tuple[str, ...] = (),
         owns: tuple[str, ...] = (), body: str = "", project: str = "P") -> None:
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,"
            "owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES(?,?,?, ?,1,?,'','',?,?,'[]',?,?,?,?)",
            (project, tz, tz, state, kind, json.dumps(needs), json.dumps(owns),
             json.dumps(acceptance), body, stamp(), stamp()),
            event={"kind": "test_setup"},
        )


def event(store: Store, kind: str, tz: str, *, project: str = "P", **payload) -> None:
    with store.tx() as tx:
        tx.record_event(kind=kind, project=project, tz=tz, payload=payload)


def test_triage5_filename_in_prose_is_not_dependency(store: Store) -> None:
    task(store, "fix:request:713eff07:2", state="accepted")
    task(store, "triage:5")
    event(store, "accept", "fix:request:713eff07:2")
    event(store, "block", "triage:5",
          reason="blocked: cannot write ToDo/decisions/fix:request:713eff07:2.md")
    assert queue.thaw_frozen(store, after_min=999) == []
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='triage:5'")["state"] == "frozen"


def test_structured_dependency_requires_accept_after_block(store: Store) -> None:
    task(store, "TZ-123", state="accepted")
    task(store, "TZ-BLOCK", needs=("TZ-123",))
    event(store, "accept", "TZ-123")
    event(store, "block", "TZ-BLOCK", reason="blocked: awaiting old TZ-123")
    assert queue.thaw_frozen(store, after_min=999) == []
    event(store, "accept", "TZ-123")
    assert "TZ-BLOCK" in str(queue.thaw_frozen(store, after_min=999))


def test_tz186_self_acceptance_becomes_triage_not_probe(store: Store) -> None:
    cmd = "py -3 ops/agents/fleet_plan.py result TZ-186 --require-verified"
    task(store, "TZ-186", state="taken", acceptance=(cmd,))
    with store.tx() as tx:
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('P','TZ-186','W1','active',?)", (stamp(),),
                  event={"kind": "test_setup"})
    queue.block(store, "W1", "blocked: remaining result is not verified. until: " + cmd)
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='TZ-186'")["state"] == "frozen"
        triage = tx.one("SELECT body FROM tasks WHERE project='P' AND kind='triage' AND state='ready'")
        assert triage and "TZ-186" in triage["body"]
        assert probe.current(tx, "P", "TZ-186") is None


def test_triage10_schema_error_not_applied_then_single_success(store: Store, monkeypatch) -> None:
    monkeypatch.setattr('dev_mcp.fleet.platform_manual.enabled',lambda:True)
    task(store, "triage:10", state="accepted", kind="triage", body="[fleet-triage] subject=rejects:TZ-10")
    with store.read() as tx:
        root = Path(str(tx.one("SELECT root FROM projects WHERE name='P'")["root"]))
    filename = root / "ToDo" / "lessons" / "bad.md"
    filename.parent.mkdir(exist_ok=True)
    filename.write_text("bad", encoding="utf-8")
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="triage:10",
                        path_norm=str(filename).replace("\\", "/").lower())
    calls = []
    def register(*args):
        calls.append("register")
        return ([], ["schema: required field missing"]) if len(calls) == 1 else (["ok"], [])
    monkeypatch.setattr("dev_mcp.fleet.lessons.register_files", register)
    first = judgment.apply_accepted(store)
    assert first and first[0].get("errors")
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='triage:10'")["state"] == "accepted"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied'") == 0
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='triage:10'",
                  (), event={"kind": "test_setup"})
    assert judgment.apply_accepted(store) == []
    from dev_mcp.fleet import platform_manual
    platform_manual.resolve(store,first[0]['repair'],candidate='a'*40,evidence='operator fixed lesson registrar')
    assert judgment.apply_accepted(store)[0]["lessons"] == ["ok"]
    assert judgment.apply_accepted(store) == []
    assert calls == ["register", "register"]


def test_mill_same_error_creates_one_repair(store: Store) -> None:
    for _ in range(63):
        mill.record_failure(store, "case document over budget")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='Mill Works' AND kind='request'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='mill_quarantine'") == 1


def test_mill_error_streak_resets_after_healthy_tick(store: Store) -> None:
    mill.record_failure(store, "case document over budget")
    mill.record_failure(store, "case document over budget")
    event(store, "mill_tick", "", project="Mill Works", completed=[])
    assert mill.record_failure(store, "case document over budget") is None
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='mill_repair_open'") == 0


def test_mill_failed_tick_still_bootstraps_prepared_pairs(store: Store, tmp_path: Path,
                                                          monkeypatch) -> None:
    from subprocess import CompletedProcess
    root = tmp_path / "millroot"
    (root / "mill").mkdir(parents=True)
    (root / "mill" / "__main__.py").write_text("", encoding="utf-8")
    tzdir = root / "ToDo" / "tz"
    tzdir.mkdir(parents=True)
    (tzdir / "TZ-MA-old.md").write_text("prepared", encoding="utf-8")
    seen = []
    monkeypatch.setattr("dev_mcp.fleet.spec.sync_selected",
                        lambda _s, _p, _r, tzs, **kwargs: seen.extend(tzs) or {})
    def runner(argv, _cwd, _timeout):
        if argv[4:] == ["tick"]:
            return CompletedProcess(argv, 2, "", "case document over budget")
        return CompletedProcess(argv, 0, "{}", "")
    report = mill.tick(store, run=runner)
    assert report["tick"].endswith("case document over budget")
    assert report["new_tz"] == ["TZ-MA-old"]
    assert seen == ["TZ-MA-old"]


def test_producer_quarantined_case_is_addressable_while_healthy_pair_dispatches(
        store: Store, tmp_path: Path, monkeypatch) -> None:
    """A producer's durable Case quarantine must reach the fleet, not just disappear."""
    from subprocess import CompletedProcess
    root = tmp_path / "millroot"
    (root / "mill").mkdir(parents=True)
    (root / "mill" / "__main__.py").write_text("", encoding="utf-8")
    selected: list[str] = []
    monkeypatch.setattr("dev_mcp.fleet.spec.sync_selected",
                        lambda _s, _p, _r, tzs, **kwargs: selected.extend(tzs) or {})
    producer = {
        "new_tz": ["TZ-MA-healthy"], "completed": [], "renewed": 0,
        "open_pairs": 1, "pool": 0,
        "quarantine": [{"case_id": "case:unreadable-one",
                        "reason": "case document over budget: 2000 > 1000"}],
    }

    def runner(argv, _cwd, _timeout):
        if argv[3:] == ["mill", "tick"]:
            return CompletedProcess(argv, 0, json.dumps(producer), "")
        return CompletedProcess(argv, 0, "{}", "")

    for _ in range(3):
        result = mill.tick(store, run=runner)
        assert result["quarantine"] == producer["quarantine"]
        assert result["new_tz"] == ["TZ-MA-healthy"]
    assert selected == ["TZ-MA-healthy"] * 3
    with store.read() as tx:
        issues = tx.query(
            "SELECT tz,body FROM tasks WHERE project='Mill Works' AND kind='request' "
            "AND body LIKE '%case:unreadable-one%'")
        assert len(issues) == 1
        assert "case document over budget" in issues[0]["body"]
        events = tx.query(
            "SELECT payload FROM events WHERE kind='mill_quarantine' AND project='Mill Works'")
        case_events = [json.loads(row["payload"]) for row in events
                       if json.loads(row["payload"]).get("case_id") == "case:unreadable-one"]
        assert len(case_events) == 1


def test_transient_triage_apply_stays_accepted_with_one_operator_repair(store: Store, monkeypatch) -> None:
    monkeypatch.setattr('dev_mcp.fleet.platform_manual.enabled',lambda:True)
    task(store, "triage:11", state="accepted", kind="triage",
         body="[fleet-triage] subject=rejects:TZ-11")
    with store.read() as tx:
        root = Path(str(tx.one("SELECT root FROM projects WHERE name='P'")["root"]))
    filename = root / "ToDo" / "lessons" / "temporary.md"
    filename.parent.mkdir(exist_ok=True)
    filename.write_text("retry", encoding="utf-8")
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="triage:11",
                        path_norm=str(filename).replace("\\", "/").lower())
    monkeypatch.setattr("dev_mcp.fleet.lessons.register_files",
                        lambda *_args: ([], ["temporary IO timeout"]))
    assert judgment.apply_accepted(store)[0]["status"] == "repair"
    assert judgment.apply_accepted(store) == []
    assert judgment.apply_accepted(store) == []
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='triage:11'")["state"] == "accepted"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1


def test_migration_legacy_decision_filename_and_taken_skip(store: Store) -> None:
    with store.read() as tx:
        root = str(tx.one("SELECT root FROM projects WHERE name='P'")["root"]).replace("\\", "/")
    unsafe = root + "/ToDo/decisions/fix:request:713eff07:2.md"
    safe = root + "/ToDo/decisions/fix__request__713eff07__2.md"
    for tz, state in (("triage:5", "frozen"), ("triage:6", "taken")):
        task(store, tz, kind="triage", state=state, owns=(unsafe,),
             body=f"See ToDo/decisions/fix:request:713eff07:2.md\ntz: fix:request:713eff07:2")
        event(store, "block", tz, reason="blocked: invalid filename " + unsafe)
    actions = migration.migrate(store)
    assert [(a["tz"], a["kind"]) for a in actions] == [("triage:5", "decision_path")]
    with store.read() as tx:
        assert unsafe in tx.one("SELECT owns FROM tasks WHERE tz='triage:5'")["owns"]
    assert len(migration.migrate(store, apply=True)) == 1
    assert migration.migrate(store, apply=True) == []
    with store.read() as tx:
        row = tx.one("SELECT owns,body,state FROM tasks WHERE tz='triage:5'")
        assert json.loads(row["owns"]) == [safe]
        assert "ToDo/decisions/fix__request__713eff07__2.md" in row["body"]
        assert "tz: fix:request:713eff07:2" in row["body"]
        assert row["state"] == "ready"
        assert unsafe in tx.one("SELECT owns FROM tasks WHERE tz='triage:6'")["owns"]
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='state_migrated'") == 1


def test_migration_changes_only_legacy_decision_paths_not_triage_verdicts(store: Store) -> None:
    # A historical verdict is evidence; rewriting a Windows-invalid filename
    # must never re-decide an accepted/rejected triage or unfreeze another blocker.
    with store.read() as tx:
        root = str(tx.one("SELECT root FROM projects WHERE name='P'")["root"])
    bad = root + "/ToDo/decisions/fix:TZ-441.md"
    good = root + "/ToDo/decisions/fix__TZ-441.md"
    unrelated = root + "/docs/other:label.md"
    for tz, state in (("triage:10", "accepted"), ("triage:11", "rejected"),
                      ("triage:12", "frozen")):
        task(store, tz, state=state, kind="triage", owns=(bad, unrelated),
             body="Decision: ToDo/decisions/fix:TZ-441.md\ntz: fix:TZ-441")
    event(store, "block", "triage:12", reason="blocked: independent owner_action")

    planned = migration.migrate(store)
    assert {(action["tz"], action["state"]) for action in planned} == {
        ("triage:10", "accepted"), ("triage:11", "rejected"),
        ("triage:12", "frozen"),
    }
    assert len(migration.migrate(store, apply=True)) == 3
    assert migration.migrate(store, apply=True) == []
    with store.read() as tx:
        for tz, expected in (("triage:10", "accepted"), ("triage:11", "rejected"),
                             ("triage:12", "frozen")):
            row = tx.one("SELECT state,owns,body FROM tasks WHERE tz=?", (tz,))
            assert row["state"] == expected
            assert json.loads(row["owns"]) == [good, unrelated]
            assert "ToDo/decisions/fix__TZ-441.md" in row["body"]
            assert "tz: fix:TZ-441" in row["body"]
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='state_migrated'") == 3


def test_migration_legacy_self_acceptance_until(store: Store) -> None:
    cmd = "py -3 ops/agents/fleet_plan.py result TZ-186 --require-verified"
    task(store, "TZ-186", acceptance=(cmd,))
    event(store, "block", "TZ-186", reason="blocked: pending acceptance. until: " + cmd)
    event(store, "block_probe", "TZ-186", argv=["py", "-3", "ops/agents/fleet_plan.py"])
    assert migration.migrate(store)[0]["kind"] == "self_acceptance"
    migration.migrate(store, apply=True)
    assert migration.migrate(store, apply=True) == []
    with store.read() as tx:
        triages = tx.query("SELECT tz,body FROM tasks WHERE kind='triage' AND state='ready'")
        assert len(triages) == 1
        assert "TZ-186" in triages[0]["body"]
        assert probe.current(tx, "P", "TZ-186") is None
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='state_migrated'") == 1


def test_migration_legacy_applied_error_and_readonly_cli(store: Store, tmp_path: Path) -> None:
    task(store, "triage:10", state="accepted", kind="triage")
    event(store, "triage_applied", "triage:10",
          subject="rejects:TZ-10", errors=["lesson schema required fields missing"])
    with store.read() as tx:
        db = str(tx.one("PRAGMA database_list")["file"]) if False else str(store.path)
        before = tx.scalar("SELECT COUNT(*) FROM events")
    assert migration.main(["--db", db]) == 0
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events") == before
    assert len(migration.migrate(store, apply=True)) == 1
    assert migration.migrate(store, apply=True) == []
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_apply_reopen'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied'") == 1
    assert len(judgment.apply_accepted(store)) == 1
    assert judgment.apply_accepted(store) == []


def test_isolated_modified_claim_green_acceptance_then_edit_is_stale(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed run SHA must be compared to claim bytes, never trunk bytes."""
    root = tmp_path / "P"
    source = root / "src" / "change.py"
    source.parent.mkdir(parents=True)
    source.write_text("trunk\n", encoding="utf-8")
    tree = tmp_path / "isolated" / "claim-42"
    edited = tree / "src" / "change.py"
    edited.parent.mkdir(parents=True)
    edited.write_text("isolated change\n", encoding="utf-8")
    cmd = "py -3 -m pytest tests/fleet/test_deferred_defects.py -q"
    tz = "TZ-ISOLATED-SUBMIT"
    task(store, tz, state="taken", acceptance=(cmd,), owns=(str(source),))
    with store.tx() as tx:
        tx.change("UPDATE workers SET kind='chatgpt' WHERE id='W1'",
                  event={"kind": "test_setup"})
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P',?,'W1','active',?)", (tz, stamp(-2)),
            event={"kind": "test_setup"},
        )
        claim_id = int(tx.scalar(
            "SELECT id FROM claims WHERE project='P' AND tz=? "
            "AND worker_id='W1' AND state='active'", (tz,)
        ))
        tx.change(
            "INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
            (claim_id, norm_path(str(source))),
            event={"kind": "test_setup"},
        )
        tx.record_event(
            kind="take", worker_id="W1", project="P", tz=tz,
            payload={"claim_id": claim_id, "claim_context": {"mode": "isolated"}},
        )
        tx.record_event(
            kind="run", worker_id="W1", project="P", tz=tz, rc=0,
            payload={"command": cmd, "tree": evidence.tree_hash([str(edited)])},
        )
        tx.record_event(
            kind="claim_submission", worker_id="W1", project="P", tz=tz,
            payload={"claim_id": claim_id, "submission_id": "test", "tree_sha": "sha"},
        )
        tx.record_event(
            kind="claim_submission_delta", worker_id="W1", project="P", tz=tz,
            payload={"claim_id": claim_id, "submission_id": "test",
                     "tree_sha": "sha", "changed": True},
        )
    ctx = ClaimContext(claim_id, "W1", "P", tz, root, tree,
                       "a" * 40, "test-branch", "isolated")
    # The normal unit-test harness disables the production submit guard.
    monkeypatch.setattr(queue, "REQUIRE_GREEN_SUBMIT", True)
    monkeypatch.setattr(
        ClaimTakeRouting, "resolve_claim_context",
        lambda self, agent, requested: ctx if (agent, requested) == ("W1", claim_id)
        else (_ for _ in ()).throw(ValueError("untrusted claim")),
    )
    with store.tx() as tx:
        held = dict(tx.one("SELECT * FROM claims WHERE id=?", (claim_id,)))
        assert queue._unproven_submit_reason(store, tx, "W1", held, "") == ""
        # The trunk has a different digest; the old implementation fails here.
        assert evidence.tree_hash([str(source)]) != evidence.tree_hash([str(edited)])

    # Modifying the isolated checkout after green run must revoke freshness.
    edited.write_text("changed after green run\n", encoding="utf-8")
    with store.tx() as tx:
        held = dict(tx.one("SELECT * FROM claims WHERE id=?", (claim_id,)))
        assert "файлы задачи менялись" in queue._unproven_submit_reason(
            store, tx, "W1", held, ""
        )
    with pytest.raises(ValueError, match="файлы задачи менялись"):
        queue.submit(store, "W1", "server-test submission")

    # A fresh test of these exact bytes is sufficient for the actual submit
    # path; restoring tested bytes here is equivalent to an unchanged run.
    edited.write_text("isolated change\n", encoding="utf-8")
    queue.submit(store, "W1", "server-test submission")
    with store.read() as tx:
        assert tx.one(
            "SELECT state FROM tasks WHERE project='P' AND tz=?", (tz,)
        )["state"] == "submitted"
