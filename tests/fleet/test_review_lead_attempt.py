"""Reject ведущего возвращает существующий устав даже при отключённых генераторах задач."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from dev_mcp.fleet import goals, queue, review
from dev_mcp.fleet.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_rejected_lead_milestone_returns_charter_instead_of_fix_round(tmp_path, monkeypatch):
    monkeypatch.setattr(goals, "project_info", lambda _p: {"commit": False, "claim_trees": False})
    from dev_mcp.fleet import generators
    generation_enabled = [True]
    monkeypatch.setattr(generators, "enabled", lambda: generation_enabled[0])
    root = tmp_path / "Proj"
    (root / "ToDo" / "lead").mkdir(parents=True)
    (root / "ToDo" / "lead" / "LEDGER.json").write_text(
        json.dumps({"obligations": [{"id": "O-a", "status": "unknown"}]}), encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)", (str(root),),
                  event={"kind": "test_setup"})
        for w in ("W-L1", "W-L2", "W-R1", "W-R2"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,'chatgpt',?,?)",
                      (w, w, _now(), _now()), event={"kind": "test_setup"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES('P','TZ-LEAD','lead','ready',0,'work','','','[]',?,'[]','[]',NULL,'b',?,?)",
            (json.dumps([str(root / "client").replace("\\", "/").casefold()]), _now(), _now()),
            event={"kind": "test_setup"})
    generation_enabled[0] = False
    for attempt in (1, 2):
        # The lead charter is existing work, not an auto-generated fix round.
        got = queue.take_next(store, "W-L1", "P")
        assert got is not None and got.tz == "TZ-LEAD", attempt
        (root / "client").mkdir(exist_ok=True)
        (root / "client" / "a.py").write_text(f"v{attempt}\n", encoding="utf-8")
        with store.tx() as tx:  # запись идёт через страж — здесь её след в журнале
            tx.record_event(kind="write", worker_id="W-L1", project="P", tz="TZ-LEAD",
                            path_norm=str(root / "client" / "a.py").replace("\\", "/").casefold())
        queue.submit(store, "W-L1", "веха: O-a L1→L2\nзабытое: ничего")
        review.open_review(store, "P", "TZ-LEAD")
        assert queue.take_next(store, f"W-R{attempt}", "P").tz.startswith("review:TZ-LEAD")
        review.decide(store, f"W-R{attempt}", "reject", "переход не виден")
        with store.read() as tx:
            assert tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz='TZ-LEAD'") == "ready"
            assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'fix:TZ-LEAD%'") == 0
            payload = json.loads(tx.scalar("SELECT payload FROM events WHERE kind='reject' "
                                           "ORDER BY id DESC LIMIT 1"))
        assert payload["lead_attempt"] == 1 and payload["attempt"] == attempt
        assert payload["fix_task"] is None
