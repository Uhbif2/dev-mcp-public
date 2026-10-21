from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.outcome import (  # noqa: E402
    CAUSE_CODES,
    ServerEvidenceWriter,
    add_evidence,
    check_invariant,
    close_outcome,
    create_outcome,
    get_outcome,
    install_schema,
    link_work,
    next_step,
    open_obstacle,
    resolve_obstacle,
    server_evidence_writer,
)
from dev_mcp.fleet.store import FleetUnavailable, Store  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path):
    registry = Store.open(str(tmp_path / "registry.sqlite"))
    assert registry.healthy()
    yield registry
    registry.close()


def outcome(store: Store, *, parent_id: str | None = None, project: str = "P", **extra):
    return create_outcome(
        store, project=project, title="Check P", intent_id="intent-1",
        spec={"target": "true", "revision": 1},
        evidence_spec={"verifier": "pytest"},
        authority={"paths": ["C:/repo"], "budget": 0},
        parent_id=parent_id, **extra,
    )


def work(store: Store, tz: str, *, state: str = "ready", project: str = "P"):
    now = "2026-10-09T00:00:00+00:00"
    with store.tx() as tx:
        tx.change(
            "INSERT OR IGNORE INTO projects(name, root) VALUES (?,?)",
            (project, "C:/repo"), event="test.project",
        )
        tx.change(
            "INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES (?,?,?,?,?)",
            (project, tz, state, now, now), event="test.task",
        )


def codes(store: Store) -> set[str]:
    return {x.code for x in check_invariant(store)}


def test_migration_additive_idempotent_and_legacy_store_healthy(store: Store):
    with store.read() as tx:
        tasks_before = tuple(r[1] for r in tx.query("PRAGMA table_info(tasks)"))
        version_before = tx.scalar("SELECT MAX(version) FROM schema_version")

    install_schema(store)
    install_schema(store)
    with store.read() as tx:
        assert {r["name"] for r in tx.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('outcomes','evidence','obstacles','decisions','outcome_work')"
        )} == {"outcomes", "evidence", "obstacles", "decisions", "outcome_work"}
        assert tx.scalar("SELECT MAX(version) FROM outcome_schema_version") == 1
        assert tx.scalar("SELECT MAX(version) FROM schema_version") == version_before
        assert tuple(r[1] for r in tx.query("PRAGMA table_info(tasks)")) == tasks_before
    assert store.healthy()


def test_parent_tree_close_and_revision(store: Store):
    root = outcome(store)
    left = outcome(store, parent_id=root.id)
    right = outcome(store, parent_id=root.id)
    assert left.parent_id == root.id
    assert right.project == root.project
    assert root.rev == 1
    with pytest.raises(ValueError, match="open children"):
        close_outcome(store, root.id)
    assert close_outcome(store, left.id).state == "done"
    assert close_outcome(store, right.id, state="dropped").state == "dropped"
    closed = close_outcome(store, root.id)
    assert closed.rev == 2 and closed.closed_ts
    assert next_step(store, closed).kind == "done"
    with pytest.raises(ValueError, match="must exist and be open"):
        close_outcome(store, root.id)


def test_all_four_next_steps_and_parent_routing(store: Store):
    active_work = outcome(store)
    work(store, "TZ-1")
    link_work(store, outcome_id=active_work.id, project="P", tz="TZ-1")
    assert next_step(store, active_work.id).kind == "work"
    assert next_step(store, active_work.id).id == "P/TZ-1"

    waiting = outcome(store)
    observed = open_obstacle(
        store, outcome_id=waiting.id, type="external_fact", cause_code="resource_down",
        predicate={"sensor": "adb-device-present"}, change_source="sensor:adb",
    )
    assert (next_step(store, waiting).kind, next_step(store, waiting).id) == ("wait", observed)
    assert "no_next_step" not in {v.code for v in check_invariant(store) if v.outcome_id == waiting.id}
    resolve_obstacle(store, observed)
    assert next_step(store, waiting).kind == "done"

    manual = outcome(store)
    owner = open_obstacle(
        store, outcome_id=manual.id, type="owner_action", cause_code="owner_physical",
        predicate={}, owner_action="Connect USB cable",
    )
    assert (next_step(store, manual).kind, next_step(store, manual).id) == ("owner", owner)
    assert "prose_control" not in {v.code for v in check_invariant(store) if v.outcome_id == manual.id}

    parent = outcome(store)
    child = outcome(store, parent_id=parent.id)
    link_work(store, outcome_id=child.id, project="P", tz="TZ-1")
    assert next_step(store, parent).kind == "work"
    assert next_step(store, parent).id == "P/TZ-1"
    assert next_step(store, close_outcome(store, active_work.id)).kind == "done"


def test_unready_or_nonexistent_work_is_not_issuable(store: Store):
    item = outcome(store)
    link_work(store, outcome_id=item.id, project="P", tz="TZ-NOT-YET-CREATED")
    assert next_step(store, item.id).kind == "done"
    assert "no_next_step" in codes(store)
    work(store, "TZ-BUSY", state="taken")
    link_work(store, outcome_id=item.id, project="P", tz="TZ-BUSY")
    assert next_step(store, item.id).kind == "done"


def test_invariant_no_next_step_and_prose_control(store: Store):
    orphan = outcome(store)
    bare = open_obstacle(
        store, outcome_id=orphan.id, type="unknown_method", cause_code="procedure_unknown",
        predicate={"text": "someone should resolve"}, change_source="timer",
    )
    issues = check_invariant(store)
    assert {(v.code, v.outcome_id) for v in issues} >= {
        ("no_next_step", orphan.id), ("prose_control", orphan.id),
    }
    assert any(bare in v.detail for v in issues)


def test_invariant_self_wait_own_acceptance_case(store: Store):
    item = outcome(store)
    open_obstacle(
        store, outcome_id=item.id, type="needs_result", cause_code="acceptance_impossible",
        predicate={"requires_outcome": item.id, "kind": "acceptance"},
        change_source=item.id,
    )
    assert any(v.code == "self_wait" and v.outcome_id == item.id for v in check_invariant(store))


@pytest.mark.parametrize("size", [2, 3])
def test_wait_cycles_including_three_nodes(store: Store, size: int):
    nodes = [outcome(store) for _ in range(size)]
    for index, source in enumerate(nodes):
        target = nodes[(index + 1) % size]
        open_obstacle(
            store, outcome_id=source.id, type="needs_result",
            cause_code="gate_red_foreign", predicate={"requires_outcome": target.id},
            change_source=target.id,
        )
    affected = {v.outcome_id for v in check_invariant(store) if v.code == "wait_cycle"}
    assert affected == {n.id for n in nodes}


def test_evidence_requires_server_capability_and_versions(store: Store):
    item = outcome(store)
    with pytest.raises(PermissionError):
        ServerEvidenceWriter(store)
    server = server_evidence_writer(store)
    with pytest.raises(ValueError, match="subject_version"):
        add_evidence(
            store, writer=server, outcome_id=item.id, kind="run", ref="job-id",
            subject_version="", env_version="win11/py3", verdict="pass", producer="runner",
        )
    with pytest.raises(ValueError, match="kind"):
        add_evidence(
            store, writer=server, outcome_id=item.id, kind="agent_report", ref="job-id",
            subject_version="commit-1", env_version="win11/py3", verdict="pass", producer="agent",
        )
    another = Store.open(":memory:")
    try:
        with pytest.raises(PermissionError):
            add_evidence(
                store, writer=server_evidence_writer(another), outcome_id=item.id,
                kind="run", ref="job-id", subject_version="commit-1",
                env_version="win11/py3", verdict="pass", producer="runner",
            )
    finally:
        another.close()
    evidence_id = add_evidence(
        store, writer=server, outcome_id=item.id,
        kind="run", ref="job-id", subject_version="commit-1",
        env_version="win11/py3", verdict="pass", producer="server-runner",
    )
    with store.read() as tx:
        recorded = tx.one("SELECT * FROM evidence WHERE id=?", (evidence_id,))
        assert recorded["outcome_id"] == item.id
        assert recorded["subject_version"] == "commit-1"
        assert recorded["producer"] == "server-runner"


@pytest.mark.parametrize("bad", [
    {}, {"blob": "a" * 66000}, {"nan": float("nan")},
    {"bad": {1, 2, 3}}, {"deep": [1] * 70000},
])
def test_reject_empty_huge_and_non_json_spec(store: Store, bad):
    with pytest.raises(ValueError):
        create_outcome(
            store, project="P", title="Bad", intent_id="intent",
            spec=bad, evidence_spec={"verify": "run"}, authority={"paths": []},
        )
    with store.read() as tx:
        install_count = tx.scalar("SELECT COUNT(*) FROM events WHERE kind='outcome.created'")
    assert install_count == 0


def test_unknown_cause_and_parent_validation(store: Store):
    parent = outcome(store)
    assert CAUSE_CODES
    with pytest.raises(ValueError, match="cause_code"):
        open_obstacle(
            store, outcome_id=parent.id, type="external_fact", cause_code="invented",
            predicate={"sensor": "ok"}, change_source="sensor",
        )
    with pytest.raises(ValueError, match="parent"):
        outcome(store, parent_id="deleted-or-missing-id")
    with pytest.raises(ValueError, match="parent"):
        outcome(store, parent_id=parent.id, project="Other")
    with pytest.raises(ValueError, match="project"):
        link_work(store, outcome_id=parent.id, project="Wrong", tz="TZ-1")
    close_outcome(store, parent.id)
    with pytest.raises(ValueError, match="parent"):
        outcome(store, parent_id=parent.id)


def test_parent_fk_restrict_and_no_duplicate_work_link(store: Store):
    parent = outcome(store)
    child = outcome(store, parent_id=parent.id)
    link_work(store, outcome_id=child.id, project="P", tz="TZ-1")
    link_work(store, outcome_id=child.id, project="P", tz="TZ-1")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM outcome_work WHERE outcome_id=?", (child.id,)) == 1
    # Foreign-key semantics are checked with the actual Store connection.
    with pytest.raises(FleetUnavailable, match="FOREIGN KEY"):
        with store.tx() as tx:
            tx.change("DELETE FROM outcomes WHERE id=?", (parent.id,), event="test.remove-parent")
    assert get_outcome(store, child.id).parent_id == parent.id


def test_failed_obstacle_insert_does_not_leak_partial_rows(store: Store):
    outcome(store)
    with pytest.raises(ValueError, match="outcome does not exist"):
        open_obstacle(
            store, outcome_id="missing", type="external_fact",
            cause_code="resource_down", predicate={"sensor": "adb"},
        )
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM obstacles") == 0
        assert tx.scalar("SELECT COUNT(*) FROM outcomes") == 1
