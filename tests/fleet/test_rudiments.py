"""08.11: задачи выведенных генераторов снимает платформа (решение владельца — не спрашивать)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from dev_mcp.fleet import rudiments
from dev_mcp.fleet.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task(tx, tz: str, state: str) -> None:
    tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
              "resources,acceptance,author_worker,body,created_at,updated_at) "
              "VALUES('P',?,?,?,1,'work','','','[]','[]','[]','[]',NULL,'',?,?)",
              (tz, tz, state, _now(), _now()), event={"kind": "test_setup"})


def test_retires_rudiments_keeps_taken_and_returns_lead_charter(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)", (str(tmp_path),),
                  event={"kind": "test_setup"})
        tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W',?,?)", (_now(), _now()),
                  event={"kind": "test_setup"})
        _task(tx, "revalidate:aa", "frozen")
        _task(tx, "review:revalidate:aa:2", "submitted")
        _task(tx, "revalidate:bb", "taken")
        _task(tx, "review:fix:revalidate:cc:2", "frozen")
        _task(tx, "fix:TZ-LEAD:42", "frozen")
        _task(tx, "TZ-LEAD", "submitted")
        _task(tx, "TZ-7", "frozen")
        tx.change("INSERT INTO claims(worker_id,project,tz,state,started_at) VALUES('W','P','revalidate:bb','active',?)",
                  (_now(),), event={"kind": "test_setup"})
    assert rudiments.retire(store, dry=True) == {"revalidate": 3, "lead_fix_round": 1}
    out = rudiments.retire(store, preimage_dir=tmp_path / "pre")
    assert out["retired"] == 4 and len(json.loads(open(out["preimage"], encoding="utf-8").read())) == 4
    with store.read() as tx:
        state = {r["tz"]: r["state"] for r in tx.query("SELECT tz,state FROM tasks")}
        kinds = [r["kind"] for r in tx.query("SELECT kind FROM events WHERE kind IN ('task_retired','reject')")]
    assert state["revalidate:aa"] == state["fix:TZ-LEAD:42"] == "rejected"
    assert state["revalidate:bb"] == "taken" and state["TZ-7"] == "frozen"
    assert state["TZ-LEAD"] == "ready"  # устав не висит на снятом раунде
    assert kinds == ["task_retired"] * 4  # не reject: уроки по отказам их не видят
    assert rudiments.retire(store) == {}
