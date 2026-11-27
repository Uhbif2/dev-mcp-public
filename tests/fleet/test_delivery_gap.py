"""L2→L4 handoff remains manual; conversion is grounded in exact proof chain."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_mcp.fleet import delivery_gap, goals, integrate, obstacles
from dev_mcp.fleet import generators, judgment
from tests.fleet.test_lead_admin import _task, env  # noqa: F401


def _event(store, kind, tz="TZ-2", **payload):
    with store.tx() as tx:
        tx.record_event(kind=kind, project="P", tz=tz, payload=payload)
        return int(tx.scalar("SELECT MAX(id) FROM events"))


def test_integration_hook_creates_idempotent_operator_front_not_release(env, monkeypatch):
    store, _root = env
    _task(store, "P", "TZ-2", "accepted")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET body=? WHERE project='P' AND tz='TZ-2'",
                  ("O-android-update: verified source code on the integrated main branch",),
                  event={"kind": "test_setup"})
    monkeypatch.setattr(goals, "project_info", lambda _: {"commit": True, "lead_continuous": True})
    sha = "a" * 40
    monkeypatch.setattr(delivery_gap, "_full_sha", lambda root, commit: sha)
    accept_id = _event(store, "accept", review_task="review:TZ-2")

    integrator = object.__new__(integrate.Integrator)
    integrator.store = store
    integrator._record(kind="integrate", project="P", tz="TZ-2",
                       payload={"accept_id": accept_id, "commit": sha[:10]})
    integrator._record(kind="integrate", project="P", tz="TZ-2",
                       payload={"accept_id": accept_id, "commit": sha[:10]})
    with store.read() as tx:
        fronts = tx.query("SELECT payload FROM events WHERE kind='manual_platform_required'")
        assert len(fronts) == 1
        front = json.loads(fronts[0]["payload"])
        assert sha in front["detail"] and "O-android-update" in front["detail"]
        assert "operator" in front["detail"] and "native" in front["detail"]
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'delivery:%'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='delivery_gate'") == 0
    with store.read() as tx:
        summary = delivery_gap.conversion(tx, "P")
    assert summary["P"]["counts"] == [1, 1, 0, 0, 0]


def test_gap_ignores_deferred_or_unrelated_accepts(env, monkeypatch):
    store, _ = env
    _task(store, "P", "TZ-2", "accepted")
    aid = _event(store, "accept")
    monkeypatch.setattr(goals, "project_info", lambda _: {"commit": True, "lead_continuous": True})
    assert not delivery_gap.on_integration(store, "P", "TZ-2", {"accept_id": aid, "deferred": ["x"]})
    assert not delivery_gap.on_integration(store, "P", "TZ-2", {"accept_id": aid + 1, "commit": "f" * 40})
    with store.read() as tx:
        assert not tx.query("SELECT id FROM events WHERE kind='manual_platform_required'")


def test_linked_obligation_uses_existing_operator_obstacle_and_wakeup(env, monkeypatch):
    store, root = env
    _task(store, "P", "TZ-2", "accepted")
    (root / "ToDo/lead/LEDGER.json").write_text(
        json.dumps({"obligations": [{"id": "O-android-update", "status": "unknown"}]}),
        encoding="utf-8")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET body=? WHERE project='P' AND tz='TZ-2'",
                  ("Release proof for O-android-update",), event={"kind": "test_setup"})
    monkeypatch.setattr(goals, "project_info", lambda _: {"commit": True, "lead_continuous": True})
    sha = "a" * 40
    aid = _event(store, "accept")
    result = delivery_gap.on_integration(store, "P", "TZ-2",
                                         {"accept_id": aid, "commit": sha})
    assert "ob-" in result
    items = obstacles.open_items(store, "P")
    assert len(items) == 1 and items[0]["actor"] == "operator"
    assert items[0]["predicate"] == {"kind": "owner_answer"}
    assert items[0]["obligations"] == ["O-android-update"]
    obstacles.sync_operator_backlog(store)
    with store.read() as tx:
        required = tx.query("SELECT payload FROM events WHERE kind='manual_platform_required'")
        assert len(required) == 1
        front = json.loads(required[0]["payload"])
        assert "signed" in front["detail"] and sha in front["detail"]
    assert obstacles.close(store, "P", items[0]["id"], by="operator",
                           answer="installed/native receipts verified") is True
    assert not obstacles.open_items(store, "P")


def test_stage_conversion_requires_same_sha_device_and_negative_native(env):
    store, _ = env
    _task(store, "P", "TZ-2", "accepted")
    sha = "a" * 40
    artifact = "b" * 64
    aid = _event(store, "accept", review_task="review:TZ-2")
    _event(store, "integrate", accept_id=aid, commit=sha[:10])
    bad = dict(stage="signed", accept_id=aid, commit="c" * 40,
               artifact_sha=artifact, url="https://example.invalid/release",
               evidence="signed checksum verified")
    _event(store, "delivery_gate", **bad)
    with store.read() as tx:
        assert delivery_gap.conversion(tx, "P")["P"]["counts"] == [1, 1, 0, 0, 0]
    _event(store, "delivery_gate", **{**bad, "commit": sha})
    _event(store, "delivery_gate", stage="installed", accept_id=aid,
           commit=sha, artifact_sha="c" * 64,
           device="android-1", version="1.2.3", evidence="app version screen")
    with store.read() as tx:
        assert delivery_gap.conversion(tx, "P")["P"]["counts"] == [1, 1, 1, 0, 0]
    _event(store, "delivery_gate", stage="installed", accept_id=aid,
           commit=sha, artifact_sha=artifact, device="android-1",
           version="1.2.3", evidence="app version screen")
    _event(store, "delivery_gate", stage="works_live", accept_id=aid,
           commit=sha, artifact_sha=artifact,
           verdict="works_live", evidence="native positive")
    with store.read() as tx:
        assert delivery_gap.conversion(tx, "P")["P"]["counts"] == [1, 1, 1, 1, 0]
    _event(store, "delivery_gate", stage="works_live", accept_id=aid,
           commit=sha, artifact_sha=artifact, verdict="works_live",
           evidence="native positive", negative_evidence="blocked tunnel negative control")
    with store.read() as tx:
        assert delivery_gap.conversion(tx, "P")["P"]["counts"] == [1, 1, 1, 1, 1]
        text = "\n".join(delivery_gap.summary_lines(tx, "P"))
        assert "installed 1" in text and "works_live 1" in text


def test_pending_ages_only_after_proven_gate(env):
    store, _ = env
    _task(store, "P", "TZ-2", "accepted")
    aid = _event(store, "accept")
    with store.read() as tx:
        summary = delivery_gap.summary_lines(tx, "P", now=datetime.now(timezone.utc) + timedelta(hours=4))
        assert any("accepted→integrated: 1 ждут" in line for line in summary)
        assert any("старейший 4.0 ч" in line for line in summary)


def test_conversion_excludes_review_accept_and_prior_integrate_receipts(env):
    store, _ = env
    _task(store, "P", "TZ-2", "accepted")
    _task(store, "P", "review:TZ-2:1", "accepted")
    _event(store, "integrate", accept_id=4, commit="b" * 40)
    aid = _event(store, "accept")
    _event(store, "accept", tz="review:TZ-2:1")
    _event(store, "integrate", accept_id=aid, commit="not a sha")
    with store.read() as tx:
        assert delivery_gap.conversion(tx, "P")["P"]["counts"] == [1, 0, 0, 0, 0]


def test_triage_task_generator_is_still_disabled(env, monkeypatch):
    # P1-4: judgment consults the global gate; new L2→L4 actions are not
    # triage task inserts and do not reenable automatic generators.
    store, _ = env
    doc = goals._load_doc()
    if "platform" not in doc:
        import pytest
        pytest.skip("owner's live fleet-projects.toml is absent (public installation)")
    assert doc["platform"]["task_generators"]["enabled"] is False
    monkeypatch.setattr(generators, "enabled", lambda: False)
    assert judgment.open_triages(store) == []
