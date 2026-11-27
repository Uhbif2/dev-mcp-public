"""08.11 (LEAD-SYSTEM-DESIGN 2.1): дерево стадий проекта с ведущим — история."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from dev_mcp.fleet import instructions, outcome, stage_history
from dev_mcp.fleet.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setup(tmp_path: Path) -> Store:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    outcome.install_schema(store)
    with store.tx() as tx:
        for name in ("P", "Q"):
            tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                      (name, str(tmp_path / name)), event={"kind": "test_setup"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES('P','TZ-LEAD','lead','taken',0,'work','','','[]','[]','[]','[]',NULL,'',?,?)",
            (_now(), _now()), event={"kind": "test_setup"})
        for project in ("P", "Q"):
            root = f"intent-{project.lower()}"
            for oid, parent in ((root, None), (f"{root}:delivered", root)):
                tx.change("INSERT INTO outcomes(id,parent_id,project,title,intent_id,spec,evidence_spec,"
                          "authority,state,rev,created_ts) VALUES(?,?,?,?,?,'{}','{}','{}','open',1,?)",
                          (oid, parent, project, oid.rsplit(":", 1)[-1], root, _now()),
                          event={"kind": "test_setup"})
            tx.change("INSERT INTO obstacles(id,outcome_id,type,cause_code,predicate,change_source,"
                      "resolver_work,owner_action,state,ts) VALUES(?,?,'needs_result','procedure_unknown',"
                      "'{}','',NULL,NULL,'open',?)", (f"wait-{project}", f"{root}:delivered", _now()),
                      event={"kind": "test_setup"})
    return store


def test_lead_project_stage_tree_becomes_history_and_others_stay(tmp_path):
    store = _setup(tmp_path)
    assert stage_history.retire(store) == {"P": {"nodes": 2, "obstacles": 1}}
    assert stage_history.retire(store) == {}  # идемпотентно
    with store.read() as tx:
        states = {r["id"]: r["state"] for r in tx.query("SELECT id,state FROM outcomes")}
        blockers = {r["id"]: (r["state"], r["change_source"]) for r in tx.query("SELECT * FROM obstacles")}
    assert states["intent-p"] == states["intent-p:delivered"] == "dropped"
    assert states["intent-q:delivered"] == "open"  # проект без ведущего — прежняя схема
    assert blockers["wait-P"] == ("resolved", stage_history.DECISION)
    assert blockers["wait-Q"][0] == "open"
    # Контроллер не видит у P листа без работы — не заведёт plan.
    assert not [v for v in outcome.check_invariant(store) if str(v.outcome_id).startswith("intent-p")]


def test_owner_intent_for_lead_project_becomes_one_instruction(tmp_path):
    store = _setup(tmp_path)
    src = "intent:ticktick/abc@3"
    first = stage_history.intent_to_instruction(store, "P", raw_intent="Сделай  экспорт\nв PDF", source=src)
    again = stage_history.intent_to_instruction(store, "P", raw_intent="Сделай экспорт в PDF", source=src)
    assert first == again
    items = [i for i in instructions.items(store, "P") if i["id"] == first]
    assert len(items) == 1 and "экспорт в PDF" in items[0]["text"]
