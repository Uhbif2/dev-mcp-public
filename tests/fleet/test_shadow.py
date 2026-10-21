"""TZ-49: a new blocker cannot silently stop the fleet before evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import hashlib
import subprocess

import pytest

from dev_mcp.fleet.shadow import (
    HistoryEvent, ShadowRules, history_event, replay,
)
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.integrate import Batch, Integrator
from dev_mcp.fleet import queue
from dev_mcp.fleet.model import norm_path

T0 = datetime(2026, 10, 11, tzinfo=timezone.utc)


def when(hours=0, minutes=0):
    return T0 + timedelta(hours=hours, minutes=minutes)


@pytest.fixture
def setup(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            ("Fleet Platform", str(tmp_path)),
            event={"kind": "seed"},
        )
    yield store, tmp_path
    store.close()


def event(kind, t, *, subject="Fleet Platform/TZ-1", revision="a1",
          rule="gate:shared", fingerprint="v1", reason="", unchanged=False):
    return HistoryEvent(t, kind, subject, revision, rule, fingerprint, reason, unchanged)


def test_shadow_not_blocking_and_records_what_and_why(setup):
    store, _ = setup
    engine = ShadowRules(store)
    rule = engine.register("gate:shared", "project_gate", "v1", now=T0)
    assert rule.mode == "shadow" and rule.enabled_at is None
    result = engine.check(
        "gate:shared", would_block=True, subject="Fleet Platform/TZ-1",
        reason="project gate red", revision="a1", now=when(minutes=1),
    )
    assert result.blocked is False and result.mode == "shadow"
    with store.read() as tx:
        row = tx.one("SELECT payload FROM events WHERE kind='would_block'")
        data = json.loads(row["payload"])
    assert data["subject"] == "Fleet Platform/TZ-1"
    assert data["reason"] == "project gate red"
    assert data["fingerprint"] == "v1"


def test_every_family_supported_and_modified_rule_returns_to_shadow(setup):
    store, _ = setup
    engine = ShadowRules(store, min_shadow=timedelta())
    for family in ("project_gate", "guard", "freeze", "zone", "watchdog"):
        rule = engine.register(family, family, "hash-1", now=T0)
        assert rule.family == family and rule.mode == "shadow"
    first = engine.register("guard", "guard", "hash-1", now=when(hours=1))
    assert first.changed_at == T0
    engine.advance("guard", now=when(hours=1), history=[
        event("would_block", when(minutes=1), rule="guard", fingerprint="hash-1"),
    ])
    assert engine.get("guard").mode == "enforce"
    changed = engine.register("guard", "guard", "hash-2", now=when(hours=2))
    assert changed.mode == "shadow"
    assert changed.changed_at == when(hours=2)
    assert engine.get("guard").enabled_at is None
    with pytest.raises(ValueError):
        engine.register("not-valid", "unsupported", "h1", now=T0)


def test_replay_counts_false_blocks_only_when_later_success_has_no_edit(setup):
    store, _ = setup
    engine = ShadowRules(store)
    rule = engine.register("gate:shared", "project_gate", "v1", now=T0)
    evidence = [
        event("would_block", when(minutes=10), subject="P/A", reason="gate red"),
        event("accept", when(minutes=11), subject="P/A"),
        event("would_block", when(minutes=20), subject="P/B", revision="b1"),
        event("write", when(minutes=21), subject="P/B", revision="b2"),
        event("accept", when(minutes=22), subject="P/B", revision="b2"),
        event("would_block", when(minutes=30), subject="P/C"),
        event("deployed", when(minutes=31), subject="P/D"),
        event("would_block", when(minutes=40), subject="P/E", revision="e1"),
        event("accept", when(minutes=41), subject="P/E", revision="e2"),
    ]
    result = replay(rule, evidence, now=when(hours=2))
    assert result.attempts == 4
    assert result.false_blocks == 1
    assert result.false_rate == 0.25
    assert result.examples[0].startswith("P/A: would_block")


def test_replay_uses_exact_subject_and_24h_window(setup):
    store, _ = setup
    rule = ShadowRules(store).register("gate:shared", "project_gate", "v1", now=T0)
    history = [
        event("would_block", when(hours=-26), subject="P/old"),
        event("accept", when(hours=-25), subject="P/old"),
        event("would_block", when(hours=1), subject="P/new"),
        event("accept", when(hours=2), subject="P/other"),
    ]
    assert replay(rule, history, now=when(hours=3)).attempts == 1
    assert replay(rule, history, now=when(hours=3)).false_blocks == 0


def test_history_reads_registry_events_and_guard_journal(setup):
    store, temp = setup
    journal = temp / "guard.jsonl"
    journal.write_text(
        json.dumps({"ts": when(minutes=15).isoformat(), "code": "deployed",
                    "candidate": "sha1", "stage": "after"}) + "\n" +
        '{"bad": ' + "\n" +
        json.dumps({"ts": when(minutes=16).isoformat(), "code": "protected_path",
                    "candidate": "sha2", "stage": "before"}) + "\n",
        encoding="utf-8",
    )
    with store.tx() as tx:
        tx.record_event(
            ts=when(minutes=10).isoformat(), kind="would_block",
            project="Fleet Platform", tz="TZ-1",
            payload={"subject": "deploy:sha1", "rule_id": "guard:file",
                     "fingerprint": "v1", "reason": "protected"},
        )
    engine = ShadowRules(store, guard_journal=journal)
    events = engine.history(now=when(hours=1))
    assert len(events) == 3
    assert {x.kind for x in events} == {"would_block", "deployed", "guard:protected_path"}
    assert next(x for x in events if x.kind == "deployed").subject == "deploy:sha1"
    rule = engine.register("guard:file", "guard", "v1", now=T0)
    assert replay(rule, events, now=when(hours=1)).false_blocks == 1
    # Existing legacy guard entries have no rule_id: replay predicate can
    # reconstruct candidates for the new rule without rewriting the journal.
    guard_block = history_event({"ts": when(minutes=5).isoformat(), "code": "protected_path",
                                "candidate": "sha1"})
    report = engine.advance(
        "guard:file", now=when(hours=5),
        history=[guard_block, *events],
        predicate=lambda source: [
            event("would_block", x.at, rule="guard:file", subject=x.subject)
            for x in source if x.kind == "guard:protected_path"
        ],
    )
    assert report.false_blocks >= 1


def test_auto_promote_only_after_time_and_replay_threshold(setup):
    store, _ = setup
    engine = ShadowRules(store, min_shadow=timedelta(hours=4))
    engine.register("gate:shared", "project_gate", "v1", now=T0)
    samples = [event("would_block", when(minutes=5))]
    engine.advance("gate:shared", now=when(hours=3), history=samples)
    assert engine.get("gate:shared").mode == "shadow"
    engine.advance("gate:shared", now=when(hours=5), history=[])
    assert engine.get("gate:shared").mode == "shadow"
    result = engine.advance("gate:shared", now=when(hours=5), history=samples)
    assert result.attempts == 1 and result.false_blocks == 0
    promoted = engine.get("gate:shared")
    assert promoted.mode == "enforce" and promoted.enabled_at == when(hours=5)
    assert engine.check("gate:shared", would_block=True, subject="P/TZ-1",
                        reason="real defect", defect_found=True, now=when(hours=5, minutes=1)).blocked


def test_false_positive_creates_single_actionable_task_and_prevents_enforce(setup):
    store, _ = setup
    engine = ShadowRules(store, min_shadow=timedelta())
    engine.register("gate:shared", "project_gate", "v1", now=T0)
    evidence = [
        event("would_block", when(minutes=1), reason="red"),
        event("accept", when(minutes=2)),
    ]
    assert engine.advance("gate:shared", now=when(hours=1), history=evidence).false_rate == 1
    engine.advance("gate:shared", now=when(hours=1), history=evidence)
    assert engine.get("gate:shared").mode == "shadow"
    with store.read() as tx:
        rows = tx.query("SELECT tz,body FROM tasks WHERE tz LIKE 'triage:shadow:%'")
    assert len(rows) == 1
    assert "without edits" in rows[0]["body"]
    assert "gate:shared" in rows[0]["body"]


def test_enforced_long_same_code_without_defect_demotes_and_unblocks(setup):
    store, _ = setup
    engine = ShadowRules(store, min_shadow=timedelta())
    engine.register("guard:file", "guard", "v1", now=T0)
    engine.advance("guard:file", now=when(minutes=1), history=[
        event("would_block", when(minutes=1), rule="guard:file"),
    ])
    assert engine.get("guard:file").mode == "enforce"
    assert engine.check("guard:file", would_block=True, subject="deploy:sha",
                        code="protected_path", reason="protected", now=when(minutes=2)).blocked
    second = engine.check("guard:file", would_block=True, subject="deploy:sha",
                          code="protected_path", reason="protected", now=when(hours=1, minutes=3))
    assert not second.blocked and second.mode == "shadow"
    assert engine.get("guard:file").mode == "shadow"
    with store.read() as tx:
        rows = tx.query("SELECT body FROM tasks WHERE tz LIKE 'triage:shadow:%'")
    assert len(rows) == 1 and "stalled_block" in rows[0]["body"]


def test_clear_and_known_defect_prevent_demote(setup):
    store, _ = setup
    engine = ShadowRules(store, min_shadow=timedelta())
    engine.register("guard:file", "guard", "v1", now=T0)
    engine.advance("guard:file", now=when(minutes=1), history=[
        event("would_block", when(minutes=1), rule="guard:file"),
    ])
    engine.check("guard:file", would_block=True, subject="P/TZ", reason="stop",
                 code="A", now=when(minutes=2))
    engine.check("guard:file", would_block=False, subject="P/TZ", reason="clear",
                 now=when(hours=1, minutes=10))
    assert not engine.unstick("guard:file", now=when(hours=2))
    engine.check("guard:file", would_block=True, subject="P/TZ", reason="defect",
                 code="A", defect_found=True, now=when(hours=2))
    assert not engine.unstick("guard:file", now=when(hours=4))
    assert engine.get("guard:file").mode == "enforce"


def test_registry_persists_across_instances_and_no_extra_schema(setup):
    store, _ = setup
    original = ShadowRules(store, min_shadow=timedelta())
    original.register("freeze:stale", "freeze", "v1", now=T0)
    original.advance("freeze:stale", now=when(hours=1), history=[
        event("would_block", when(minutes=2), rule="freeze:stale"),
    ])
    other = ShadowRules(store)
    assert other.get("freeze:stale").mode == "enforce"
    with store.read() as tx:
        tables = {x["name"] for x in tx.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "shadow_rules" not in tables


def test_guard_without_historical_journal_cannot_go_live(setup):
    store, _ = setup
    engine = ShadowRules(store, min_shadow=timedelta())
    engine.register("guard:file", "guard", "v1", now=T0)
    # History is present in the DB but the guard's independent journal was
    # never supplied; promotion must not interpret that as a clean replay.
    with store.tx() as tx:
        tx.record_event(kind="would_block", ts=when(minutes=1).isoformat(),
                        payload={"rule_id": "guard:file", "fingerprint": "v1",
                                 "subject": "deploy:sha1"})
    engine.advance("guard:file", now=when(hours=5))
    assert engine.get("guard:file").mode == "shadow"


def test_real_integrator_gate_red_is_nonblocking_then_enforced(setup):
    store, root = setup
    name = "Fleet Platform"
    batch = Batch(name, str(root), wrote={norm_path(str(root / "broken.py"))})
    integrator = Integrator(
        store, root / "cursor.json",
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "red gate", ""),
        project_info=lambda project: {"gate": "check"},
    )
    result = integrator._gate(batch)
    assert result is not None and result["rc"] == 1
    engine = ShadowRules(store, min_shadow=timedelta())
    rid = f"project_gate:{name}"
    rule = engine.get(rid)
    assert rule.mode == "shadow"
    assert rule.fingerprint == hashlib.sha256(b"check").hexdigest()
    with store.read() as tx:
        zone, _ = queue.red_zone(tx, name)
        recorded = tx.query("SELECT payload FROM events WHERE kind='would_block'")
    assert not zone
    assert len(recorded) == 1 and json.loads(recorded[0]["payload"])["reason"] == "rc=1"
    current = datetime.now(timezone.utc)
    engine.advance(rid, now=current + timedelta(seconds=5), history=[
        event("would_block", current, subject=name + "/gate", rule=rid,
              fingerprint=rule.fingerprint),
    ])
    assert engine.get(rid).mode == "enforce"
    with store.read() as tx:
        zone, _ = queue.red_zone(tx, name)
    assert zone == [norm_path(str(root / "broken.py"))]


def test_legacy_red_styk_stays_blocked_when_rule_first_registered(setup):
    store, root = setup
    with store.tx() as tx:
        tx.record_event(kind="gate", project="Fleet Platform", rc=1,
                        payload={"failed": ["broken.py"]})
        tx.record_event(kind="styk_open", project="Fleet Platform",
                        tz="styk:1", payload={})
    engine = ShadowRules(store)
    engine.register("project_gate:Fleet Platform", "project_gate", "v1")
    with store.read() as tx:
        zone, _ = queue.red_zone(tx, "Fleet Platform")
    assert zone == [norm_path(str(root / "broken.py"))]


def test_legacy_styk_keeps_old_failure_not_new_shadow_gate_failure(setup):
    store, root = setup
    project = "Fleet Platform"
    with store.tx() as tx:
        tx.record_event(kind="gate", project=project, rc=1,
                        payload={"failed": ["old.py"]})
        tx.record_event(kind="styk_open", project=project, tz="styk:1",
                        payload={})

    ShadowRules(store).register(f"project_gate:{project}", "project_gate", "v2")
    with store.tx() as tx:
        tx.record_event(kind="gate", project=project, rc=1,
                        payload={"failed": ["new.py"]})

    with store.read() as tx:
        zone, _ = queue.red_zone(tx, project)
        assert zone == [norm_path(str(root / "old.py"))]
        assert queue.red_overlap(tx, project, "TZ-old",
                                 (norm_path(str(root / "old.py")),))
        assert not queue.red_overlap(tx, project, "TZ-new",
                                     (norm_path(str(root / "new.py")),))


@pytest.mark.parametrize("green_kind", ["regate_result", "gate"])
def test_resolved_legacy_styk_does_not_reblock_on_later_red_shadow_gate(setup, green_kind):
    store, root = setup
    project = "Fleet Platform"
    with store.tx() as tx:
        tx.record_event(kind="gate", project=project, rc=1,
                        payload={"failed": ["old.py"]})
        tx.record_event(kind="styk_open", project=project, tz="styk:1",
                        payload={})
    ShadowRules(store).register(f"project_gate:{project}", "project_gate", "v2")
    with store.tx() as tx:
        tx.record_event(kind=green_kind, project=project, rc=0,
                        payload={"failed": []})
        tx.record_event(kind="gate", project=project, rc=1,
                        payload={"failed": ["new.py"]})
    with store.read() as tx:
        assert queue.red_zone(tx, project) == ([], "")
        assert not queue.red_overlap(tx, project, "TZ-old",
                                     (norm_path(str(root / "old.py")),))
        assert not queue.red_overlap(tx, project, "TZ-new",
                                     (norm_path(str(root / "new.py")),))


def test_repromotion_starts_new_block_episode_not_old_stale_streak(setup):
    store, _ = setup
    engine = ShadowRules(store, min_shadow=timedelta())
    engine.register("guard:file", "guard", "v1", now=T0)
    engine.advance("guard:file", now=when(minutes=1), history=[
        event("would_block", when(minutes=1), rule="guard:file"),
    ])
    engine.check("guard:file", would_block=True, subject="P/TZ", code="A",
                 reason="stop", now=when(minutes=2))
    assert engine.unstick("guard:file", now=when(hours=2))
    assert engine.get("guard:file").mode == "shadow"
    engine.advance("guard:file", now=when(hours=3), history=[
        event("would_block", when(hours=2, minutes=1), rule="guard:file"),
    ])
    assert engine.get("guard:file").mode == "enforce"
    decision = engine.check("guard:file", would_block=True, subject="P/TZ",
                            code="A", reason="stop", now=when(hours=3, minutes=1))
    assert decision.blocked and engine.get("guard:file").mode == "enforce"
