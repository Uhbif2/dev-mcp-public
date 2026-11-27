"""18.11: Nova delivery stages and repeat-checkpoint regression."""
from __future__ import annotations

import json

from dev_mcp.fleet import lead, lead_stages
from dev_mcp.fleet.store import Store

Stage = lead_stages.LeadStage
Receipt = lead_stages.StageReceipt


def _chain():
    return [
        Receipt(Stage.L1, "O-a", sha="main123", reference="test:1", test_passed=True),
        Receipt(Stage.L2, "O-a", sha="main123", reference="review+integrate:2",
                review_accepted=True, integrated=True),
        Receipt(Stage.L3, "O-a", sha="main123", reference="release:3",
                signed=True, published=True, authorised=True),
    ]


def test_l3_signed_published_without_works_live_and_l4_requires_native_negative():
    evidence = _chain()
    assert lead_stages.validate_transition("L2", "L3", evidence).ok
    bad = lead_stages.validate_transition("L3", "L4", evidence)
    assert not bad.ok and "L4" in bad.reason
    evidence.append(Receipt(Stage.L4, "O-a", sha="main123", reference="device:4",
                            installed_version="gen41", native_action="first API",
                            works_live=True, negative_control=True))
    assert lead_stages.validate_transition("L0", "L4", evidence).ok
    changed = list(evidence)
    changed[2] = Receipt(Stage.L3, "O-a", sha="other", reference="release:3",
                         signed=True, published=True, authorised=True)
    assert not lead_stages.validate_transition("L2", "L3", changed).ok
    different_o = list(evidence)
    different_o[2] = Receipt(Stage.L3, "O-b", sha="main123", reference="release:3",
                             signed=True, published=True, authorised=True)
    assert "разным O-ID" in lead_stages.validate_transition("L2", "L3", different_o).reason
    merged = list(evidence)
    merged[1] = Receipt(Stage.L2, "O-a", sha="merge456", source_sha="main123",
                        reference="review+integrate:2", review_accepted=True, integrated=True)
    merged[2] = Receipt(Stage.L3, "O-a", sha="release789", source_sha="merge456",
                        reference="release:3", signed=True, published=True, authorised=True)
    merged[3] = Receipt(Stage.L4, "O-a", sha="installed0", source_sha="release789",
                        reference="device:4", installed_version="gen41", native_action="first API",
                        works_live=True, negative_control=True)
    assert lead_stages.validate_transition("L0", "L4", merged).ok


def test_stage_milestone_rejects_incomplete_evidence(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "project"
    (root / "ToDo/lead").mkdir(parents=True)
    (root / "ToDo/lead/LEDGER.json").write_text(
        json.dumps({"obligations": [{"id": "O-a"}]}), encoding="utf-8")
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)",
                  (str(root),), event="test_setup")
        for r in _chain():
            tx.record_event(kind="lead_stage_evidence", project="P", payload={
                "obligation": r.obligation, "stage": r.stage.name, "sha": r.sha,
                "receipt": r.reference, "test_passed": r.test_passed,
                "review_accepted": r.review_accepted, "integrated": r.integrated,
                "signed": r.signed, "published": r.published, "authorised": r.authorised})
    with store.read() as tx:
        claim = {"project": "P", "tz": "TZ-LEAD"}
        assert lead.submit_refusal(tx, claim, "веха: O-a L2→L3\nзабытое: нет") == ""
        assert "L4" in lead.submit_refusal(tx, claim, "веха: O-a L3→L4\nзабытое: нет")


def test_progress_repetition_does_not_emit_accept_or_duplicate_checkpoint(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P','.',1,1)",
                  event="test_setup")
        tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,body,created_at,updated_at) "
                  "VALUES('P','TZ-LEAD','lead','submitted',1,'work','','now','now')",
                  event="test_setup")
        tx.record_event(kind="submit", project="P", tz="TZ-LEAD", payload={"note": "O-a факт x"})
    assert lead.progress_accept(store, "P", "TZ-LEAD") == "server-progress:TZ-LEAD"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE project='P' AND tz='TZ-LEAD'",
                  event={"kind": "submit", "project": "P", "tz": "TZ-LEAD",
                         "payload": {"note": "  O-a   факт   x  "}})
    assert lead.progress_accept(store, "P", "TZ-LEAD") == "server-progress:TZ-LEAD"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='lead_checkpoint'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='lead_checkpoint_repeat'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='accept'") == 0
        assert tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz='TZ-LEAD'") == "ready"
