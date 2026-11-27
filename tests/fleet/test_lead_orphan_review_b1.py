"""№48 B1/B2 (09.11.2026): ревью-сирота сдачи-прогресса ведущего и чужой хвост заявки оператору.

Живые случаи: deep-research review:TZ-LEAD:211, Nova 165, TSPU 9, lnb 220 — цикл обслуживания
открыл ревью в окне между submit и progress_accept; TSPU заморожен заявкой по fix Beta App.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from dev_mcp.fleet import platform_manual, platform_route, queue, rudiments
from dev_mcp.fleet.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task(tx, project, tz, state, kind="work", body=""):
    tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
              "resources,acceptance,author_worker,body,created_at,updated_at) "
              "VALUES(?,?,?,?,1,?,'','','[]','[]','[]','[]','W1',?,?,?)",
              (project, tz, tz, state, kind, body, _now(), _now()), event={"kind": "test_setup"})


@pytest.fixture()
def store(tmp_path):
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    with s.tx() as tx:
        for name in ("P", "B"):
            root = tmp_path / name
            (root / "ToDo" / "lead").mkdir(parents=True)
            (root / "ToDo" / "lead" / "LEDGER.json").write_text(
                json.dumps({"obligations": [{"id": "O-a", "status": "unknown"}]}), encoding="utf-8")
            tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                      (name, str(root)), event={"kind": "test_setup"})
        for w in ("W1", "W2"):
            tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES(?,?,?)", (w, _now(), _now()),
                      event={"kind": "test_setup"})
        _task(tx, "P", "TZ-LEAD", "taken")
    return s


def _submit(store, note):
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE project='P' AND tz='TZ-LEAD'", (),
                  event={"kind": "submit", "worker_id": "W1", "project": "P", "tz": "TZ-LEAD",
                         "payload": {"note": note}})


def _state(store, project, tz):
    with store.read() as tx:
        row = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
        return row["state"] if row else None


def test_maintenance_routes_lead_progress_to_checkpoint_not_review(store):
    _submit(store, "починил парсер")  # прогресс: без строки «веха:»
    out = queue.reopen_orphan_reviews(store)
    assert out == ["P/TZ-LEAD:server-progress:TZ-LEAD"]
    assert _state(store, "P", "TZ-LEAD") == "ready"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='review'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='review_reopen'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='accept'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='lead_checkpoint'") == 1
    assert queue.reopen_orphan_reviews(store) == []  # повтор не создаёт ничего


def test_maintenance_still_opens_review_for_milestone(store):
    _submit(store, "веха: O-a L1→L2\nзабытое: нет")
    out = queue.reopen_orphan_reviews(store)
    assert out == ["P/TZ-LEAD"]
    assert _state(store, "P", "TZ-LEAD") == "submitted"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='review'") == 1


def _orphan(store, review_state="frozen", claim=False):
    """Живая последовательность 08.11 16:39: review_open → lead-progress accept → reopen."""
    with store.tx() as tx:
        _task(tx, "P", "review:TZ-LEAD:211", review_state, kind="review",
              body='[fleet-review] {"project":"P","tz":"TZ-LEAD"}')
        tx.record_event(kind="review_open", worker_id="W1", project="P", tz="review:TZ-LEAD:211",
                        payload={"review_of": "TZ-LEAD"})
        tx.record_event(kind="accept", worker_id="server-lead-progress", project="P", tz="TZ-LEAD",
                        payload={"producer": "lead-progress", "progress": True})
        tx.change("UPDATE tasks SET state='taken' WHERE project='P' AND tz='TZ-LEAD'", (),
                  event={"kind": "reactivate", "project": "P", "tz": "TZ-LEAD"})
        if claim:
            tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                      "VALUES('P','review:TZ-LEAD:211','W2','active',?)", (_now(),),
                      event={"kind": "test_setup"})


def test_rudiment_retires_lead_orphan_review_only(store, tmp_path):
    _orphan(store)
    with store.tx() as tx:  # законное ревью вехи: источник submitted — не трогать
        _task(tx, "B", "TZ-LEAD", "submitted")
        _task(tx, "B", "review:TZ-LEAD:3", "frozen", kind="review",
              body='[fleet-review] {"project":"B","tz":"TZ-LEAD"}')
        tx.record_event(kind="review_open", project="B", tz="review:TZ-LEAD:3", payload={})
        tx.record_event(kind="accept", project="B", tz="TZ-LEAD",
                        payload={"producer": "lead-progress", "progress": True})
    assert rudiments.retire(store, names=("lead_orphan_review",), dry=True) == {"lead_orphan_review": 1}
    out = rudiments.retire(store, names=("lead_orphan_review",), preimage_dir=tmp_path / "pre")
    assert out["retired"] == 1
    assert _state(store, "P", "review:TZ-LEAD:211") == "rejected"
    assert _state(store, "B", "review:TZ-LEAD:3") == "frozen"
    assert _state(store, "P", "TZ-LEAD") == "taken"


def test_orphan_with_active_reviewer_is_retired_on_detection(store):
    _orphan(store, review_state="taken", claim=True)
    assert rudiments.retire(store, names=("lead_orphan_review",), dry=True) == {}  # активный захват
    assert rudiments.retire_lead_orphan(store, "P", "review:TZ-LEAD:211", "W2") is True
    assert _state(store, "P", "review:TZ-LEAD:211") == "rejected"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE state='active' AND tz='review:TZ-LEAD:211'") == 0
    assert rudiments.retire_lead_orphan(store, "P", "review:TZ-LEAD:211", "W2") is False


def test_manual_platform_route_never_freezes_lead(store, monkeypatch):
    monkeypatch.setattr(platform_manual, "enabled", lambda: True)
    with store.tx() as tx:
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('P','TZ-LEAD','W1','active',?)", (_now(),), event={"kind": "test_setup"})
    routed = platform_route.route_request(
        store, source_project="P", source_tz="TZ-LEAD", worker_id="W1",
        summary="fleet review defect: FLEET_SEALED_REVIEW error", path="src/dev_mcp/fleet/review.py")
    assert routed.status == "created" and routed.task.startswith("manual-platform:")
    assert _state(store, "P", "TZ-LEAD") == "taken"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE tz='TZ-LEAD' AND state='active'") == 1
        assert platform_route.pending_platform(tx, "P", "TZ-LEAD") is None
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1


def test_manual_platform_binds_to_refused_claim_not_current(store, monkeypatch):
    # 08.11 W9087: отказ по fix Beta App (claim 31488) заявлен из захвата TSPU/TZ-LEAD.
    monkeypatch.setattr(platform_manual, "enabled", lambda: True)
    with store.tx() as tx:
        _task(tx, "B", "fix:queue-source:19b1", "ready", kind="fix")
        tx.change("INSERT INTO claims(id,project,tz,worker_id,state,started_at,released_at) "
                  "VALUES(31488,'B','fix:queue-source:19b1','W1','released',?,?)", (_now(), _now()),
                  event={"kind": "test_setup"})
        _task(tx, "B", "TZ-9", "taken")
    routed = platform_route.route_request(
        store, source_project="B", source_tz="TZ-9", worker_id="W1",
        summary="fleet guard defect FLEET_FOREIGN_PROJECT",
        detail="W1, fix:queue-source:19b1 (claim_id=31488). Страж отказал.",
        path="src/dev_mcp/fleet/queue.py")
    assert routed.status == "created"
    assert _state(store, "B", "TZ-9") == "taken"          # текущий захват чата не тронут
    assert _state(store, "B", "fix:queue-source:19b1") == "frozen"  # источник отказа
    with store.read() as tx:
        row = tx.one("SELECT project,tz FROM events WHERE kind='manual_platform_required'")
    assert (row["project"], row["tz"]) == ("B", "fix:queue-source:19b1")


def test_claim_reference_of_another_worker_is_ignored(store, monkeypatch):
    monkeypatch.setattr(platform_manual, "enabled", lambda: True)
    with store.tx() as tx:
        _task(tx, "B", "TZ-9", "taken")
        _task(tx, "B", "TZ-10", "ready")
        tx.change("INSERT INTO claims(id,project,tz,worker_id,state,started_at) "
                  "VALUES(777,'B','TZ-10','W2','released',?)", (_now(),), event={"kind": "test_setup"})
    platform_route.route_request(
        store, source_project="B", source_tz="TZ-9", worker_id="W1",
        summary="fleet take defect", detail="см. claim_id=777", path="src/dev_mcp/fleet/queue.py")
    assert _state(store, "B", "TZ-10") == "ready"
    assert _state(store, "B", "TZ-9") == "frozen"
