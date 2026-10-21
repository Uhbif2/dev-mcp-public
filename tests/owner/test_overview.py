from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import outcome
from dev_mcp.fleet.interventions import InterventionLedger
from dev_mcp.fleet.store import Store
from dev_mcp.owner.overview import snapshot


@pytest.fixture()
def store(tmp_path):
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    yield s
    s.close()


def add(store, title="Visible", *, parent_id=None, intent="intent-1", project="P",
        subject="v1", public=True):
    return outcome.create_outcome(
        store, project=project, title=title, intent_id=intent, parent_id=parent_id,
        spec={"dashboard_visibility": "public" if public else "private",
              "private_note": "DO-NOT-EXPOSE-OWNER-PRIVATE"},
        evidence_spec={"subject_version": subject, "secret": "SECRET-EVIDENCE-SPEC"},
        authority={"token": "SECRET-AUTHORITY"},
    )


def proof(store, obj, *, kind="run", subject="v1", verdict="pass", ref="SECRET-RAW-PROOF"):
    return outcome.add_evidence(
        store, writer=outcome.server_evidence_writer(store), outcome_id=obj.id,
        kind=kind, ref=ref, subject_version=subject, env_version="env:1",
        verdict=verdict, producer="server",
    )


def task(store, tz="TZ-1", state="accepted"):
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P','C:/Work/P')",
                  event="test.setup")
        tx.change(
            "INSERT INTO tasks(project,tz,state,created_at,updated_at) "
            "VALUES('P',?,?,?,?)",
            (tz, state, "2026-10-09T00:00:00+00:00", "2026-10-09T00:00:00+00:00"),
            event="test.setup",
        )


def test_empty_database_and_filtered_empty_are_not_errors(store):
    assert snapshot(store)["status"] == "empty"
    assert snapshot(store)["interventions"]["available"] is False
    add(store)
    assert snapshot(store, project="Other")["status"] == "empty"
    assert snapshot(store, project="")["status"] == "error"


def test_interventions_projection_uses_ledger_metrics_without_writing_on_read(store, tmp_path):
    add(store, project="P")
    add(store, intent="intent-Q", project="Q")
    with store.tx() as tx:
        for name in ("P", "Q"):
            tx.change("INSERT INTO projects(name,root) VALUES(?,?)", (name, str(tmp_path)),
                      event="test.seed")
    # Schema is created by the writer/producer, never by dashboard snapshot.
    InterventionLedger(store, "P", tmp_path)
    InterventionLedger(store, "Q", tmp_path)
    now = datetime.now(timezone.utc)
    events = [
        ("P", "manual-commit", now, "p1"),
        ("P", "manual-commit", now, "p2"),
        ("P", "manual-recover", now - timedelta(days=1), "p3"),
        ("Q", "manual-recover", now, "q1"),
    ]
    with store.tx() as tx:
        for project, category, at, identifier in events:
            tx.change(
                "INSERT INTO intervention_log(source,identifier,project,class,actor,action,ts,reference) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("test", identifier, project, category, "operator", "manual",
                 at.isoformat(), "test-reference"),
                event="test.seed",
            )
    with store.read() as tx:
        before = tx.scalar("SELECT COUNT(*) FROM events")
    all_projects = snapshot(store)
    filtered = snapshot(store, project="P")
    other = snapshot(store, project="Q")
    assert all_projects["interventions"]["available"] is True
    assert all_projects["interventions"]["goal"] == 0
    assert (all_projects["interventions"]["today"],
            all_projects["interventions"]["yesterday"],
            all_projects["interventions"]["rising"]) == (3, 1, True)
    assert (filtered["interventions"]["today"],
            filtered["interventions"]["yesterday"]) == (2, 1)
    assert filtered["interventions"]["by_class"] == {
        "manual-commit": 2, "manual-recover": 1}
    assert other["interventions"]["by_class"] == {"manual-recover": 1}
    assert filtered["interventions"]["by_day"][now.date().isoformat()] == {
        "manual-commit": 2}
    assert all_projects["revision"] != filtered["revision"]
    assert snapshot(store, project="P")["revision"] == filtered["revision"]
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events") == before


def test_snapshot_versioned_applied_proven_delivered_and_work(store):
    root = add(store, "Parent")
    child = add(store, "Child", parent_id=root.id)
    task(store)
    outcome.link_work(store, outcome_id=child.id, project="P", tz="TZ-1")
    proof(store, child, subject="old", kind="delivery")
    proof(store, child, kind="review")
    before = snapshot(store)
    assert before["status"] == "ready"
    assert len(before["intents"]) == 1
    item = before["intents"][0]["results"][0]["children"][0]
    assert item["id"] == child.id
    assert item["stage"] == {"applied": True, "proven": True, "delivered": False}
    assert item["work"] == [{"tz": "TZ-1", "state": "accepted"}]
    assert {e["subject_version"] for e in item["evidence"]} == {"v1"}
    assert before["revision"] == snapshot(store)["revision"]
    proof(store, child, kind="delivery")
    after = snapshot(store)
    item = after["intents"][0]["results"][0]["children"][0]
    assert item["stage"]["delivered"]
    assert after["revision"] != before["revision"]


def test_accepted_work_without_evidence_is_not_proof(store):
    obj = add(store)
    task(store)
    outcome.link_work(store, outcome_id=obj.id, project="P", tz="TZ-1")
    node = snapshot(store)["intents"][0]["results"][0]
    assert node["stage"] == {"applied": True, "proven": False, "delivered": False}
    assert node["waiting"] == []


def test_private_fields_and_fake_markup_are_not_exposed(store):
    add(store, "<img src=x onerror=alert(1)>")
    add(store, "PRIVATE-OWNER-TITLE", public=False)
    proof(store, outcome.create_outcome(
        store, project="P", title="Hidden", intent_id="intent-1",
        spec={"dashboard_visibility": "private"}, evidence_spec={"subject_version": "v1"},
        authority={"token": "not-client"},
    ))
    payload = snapshot(store)
    import json
    raw = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("SECRET-RAW-PROOF", "DO-NOT-EXPOSE-OWNER-PRIVATE",
                      "SECRET-AUTHORITY", "SECRET-EVIDENCE-SPEC", "PRIVATE-OWNER-TITLE"):
        assert forbidden not in raw
    assert "<img src=x onerror=alert(1)>" in raw  # inert JSON data, textContent on client


def test_obstacle_reason_is_typed_not_raw_predicate(store):
    obj = add(store)
    outcome.open_obstacle(
        store, outcome_id=obj.id, type="owner_action", cause_code="owner_taste",
        predicate={"secret": "REDACT-THIS"}, owner_action="PRIVATE-CREDENTIAL",
    )
    node = snapshot(store)["intents"][0]["results"][0]
    assert node["waiting"][0] == {
        "type": "owner_action", "cause_code": "owner_taste",
        "reason": "Ожидается выбор визуального варианта",
    }
    assert "REDACT-THIS" not in str(node)
    assert "PRIVATE-CREDENTIAL" not in str(node)


def test_cyclic_parent_graph_stays_bounded_and_reports_error(store):
    first = add(store)
    second = add(store, parent_id=first.id)
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET parent_id=? WHERE id=?", (second.id, first.id),
                  event="test.cycle")
    snap = snapshot(store)
    assert snap["status"] == "error"
    assert any("цикл" in issue for issue in snap["issues"])
    assert len(snap["intents"][0]["results"]) == 2


def test_corrupt_parent_and_missing_step_are_explicit(store):
    parent = add(store)
    node = snapshot(store)["intents"][0]["results"][0]
    assert node["waiting"][0]["cause_code"] == "no_next_step"
    unrelated = add(store, intent="another-intent")
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET parent_id=? WHERE id=?", (unrelated.id, parent.id),
                  event="test.cross-intent")
    result = snapshot(store)
    assert result["status"] == "error"
    assert any("интенции" in issue for issue in result["issues"])
