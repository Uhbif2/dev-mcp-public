"""TZ-339: activity never substitutes for a verified front transition."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet import outcome, progress_watch, watchdog
from dev_mcp.fleet.store import Store


@pytest.fixture
def world(tmp_path: Path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    outcome.install_schema(store)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=4)).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES(?,?)", ("Intent", str(tmp_path)),
                  event="test.project")
        tx.change("INSERT INTO outcomes(id,project,title,intent_id,spec,evidence_spec,"
                  "authority,state,rev,created_ts,parent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  ("root", "Intent", "root", "intent-one", "{}", "{}", "{}", "open", 1,
                   old, None), event="test.root")
        for n in range(9):
            oid, tz = f"stage-{n}", f"root-stage-{n}"
            tx.change("INSERT INTO outcomes(id,project,title,intent_id,spec,evidence_spec,"
                      "authority,state,rev,created_ts,parent_id) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (oid, "Intent", f"stage_{n}", "intent-one", "{}", "{}", "{}",
                       "open", 1, (now-timedelta(hours=4)+timedelta(microseconds=n)).isoformat(),
                       "root"), event="test.stage")
            tx.change("INSERT INTO tasks(project,tz,state,kind,body,created_at,updated_at)"
                      " VALUES(?,?,?,?,?,?,?)",
                      ("Intent", tz, "ready" if n == 0 else "frozen", "triage",
                       f"[intent-template] outcome={oid}", old, old), event="test.task")
            tx.change("INSERT INTO outcome_work(outcome_id,project,tz) VALUES(?,?,?)",
                      (oid, "Intent", tz), event="test.link")
    yield store, now
    store.close()


def _detect(store, now):
    with store.read() as tx:
        return progress_watch.findings(tx, now, watchdog._issue)


def _snapshot(store, now):
    with store.read() as tx:
        return progress_watch.front_snapshot(tx, now)


def test_block_triage_review_reopen_activity_is_not_progress(world):
    store, now = world
    for kind in ("take", "block", "triage", "review", "reopen", "reject", "take"):
        with store.tx() as tx:
            tx.record_event(kind=kind, project="Intent", tz="root-stage-0",
                            payload={"reason": "retry"})
    findings = _detect(store, now)
    assert [f.kind for f in findings] == ["intent_stall"]
    assert "stage_0" in findings[0].detail
    assert _snapshot(store, now)[0]["minutes_without_progress"] >= 239


def test_nine_frozen_stages_produce_one_primary_front_alert(world):
    store, now = world
    fronts = _snapshot(store, now)
    assert len(fronts) == 1
    assert fronts[0]["stage"] == "stage_0"
    assert fronts[0]["tz"] == "root-stage-0"
    assert len(_detect(store, now)) == 1


def test_repeated_same_proof_and_stale_version_do_not_reset_clock(world):
    store, now = world
    old = (now - timedelta(hours=4)).isoformat()
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET evidence_spec=? WHERE id='stage-0'",
                  (json.dumps({'subject_version': 'v2'}),), event='test.version')
        for ident, version, stamp in [('first', 'v2', old), ('repeated', 'v2', now.isoformat()),
                                      ('stale', 'v1', now.isoformat())]:
            tx.change("INSERT INTO evidence(id,outcome_id,kind,ref,subject_version,"
                      "env_version,verdict,producer,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                      (ident,'stage-0','review','artifact',version,'env','pass','trusted',stamp),
                      event='test.proof')
    assert _snapshot(store, now)[0]['minutes_without_progress'] >= 239


def test_high_churn_does_not_create_duplicate_project_and_task_alerts(world):
    store, now = world
    with store.tx() as tx:
        for _ in range(25):
            tx.record_event(kind="take", project="Intent", tz="root-stage-0", payload={})
            tx.record_event(kind="block", project="Intent", tz="root-stage-0", payload={})
    assert [f.kind for f in _detect(store, now)] == ["intent_stall"]


@pytest.mark.parametrize("progress_kind", [
    "outcome_done", "accepted_artifact", "evidence_pass", "obstacle_resolved", "delivery"
])
def test_verified_progress_resets_front_clock(world, progress_kind):
    store, now = world
    recent = (now - timedelta(minutes=10)).isoformat()
    with store.tx() as tx:
        if progress_kind == "outcome_done":
            tx.change("UPDATE outcomes SET state='done',closed_ts=? WHERE id='stage-0'",
                      (recent,), event="outcome.closed")
        elif progress_kind == "accepted_artifact":
            tx.change("UPDATE tasks SET state='accepted',updated_at=? "
                      "WHERE project='Intent' AND tz='root-stage-0'",
                      (recent,), event="test.accepted")
            tx.record_event(kind="accept", project="Intent", tz="root-stage-0",
                            ts=recent, payload={})
        elif progress_kind == "obstacle_resolved":
            tx.change("INSERT INTO obstacles(id,outcome_id,type,cause_code,predicate,state,ts) "
                      "VALUES('obs','stage-0','defect','producer_failed','{}','resolved',?)",
                      (recent,), event="test.obstacle")
            tx.record_event(kind="outcome.obstacle_resolved", ts=recent,
                            payload={"id": "obs"})
        else:
            tx.change("INSERT INTO evidence(id,outcome_id,kind,ref,subject_version,"
                      "env_version,verdict,producer,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                      ("ev", "stage-0", "delivery" if progress_kind == "delivery" else "review",
                       "stage-artifact", "version-1", "env", "pass", "trusted", recent),
                      event="outcome.evidence")
    assert _detect(store, now) == []
    assert _snapshot(store, now)[0]["minutes_without_progress"] < 180


def test_accepted_then_reopened_stage_not_new_progress(world):
    store, now = world
    recent = (now - timedelta(minutes=10)).isoformat()
    with store.tx() as tx:
        tx.record_event(kind="accept", project="Intent", tz="root-stage-0",
                        ts=recent, payload={})
        tx.change("UPDATE tasks SET state='ready' WHERE project='Intent' "
                  "AND tz='root-stage-0'", event="test.reopened")
    assert len(_detect(store, now)) == 1


def test_report_is_meaningful_and_sensor_never_creates_document_ticket(world, tmp_path):
    store, now = world
    rows = _snapshot(store, now)
    path = tmp_path / "data" / "fleet" / "progress-front.json"
    progress_watch.write_front_report(path, now, rows)
    content = json.loads(path.read_text(encoding="utf-8"))
    assert content["schema"] == 1
    assert content["fronts"][0]["intent"] == "intent-one"
    assert content["fronts"][0]["stage"] == "stage_0"
    assert content["fronts"][0]["minutes_without_progress"] >= 239
    assert content["fronts"][0]["reason"]
    assert content["fronts"][0]["stalled"] is True
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'watch:%'") == 0
