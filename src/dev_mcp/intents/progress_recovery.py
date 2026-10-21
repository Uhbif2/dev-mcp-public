"""Resume a legacy circular stage wait only after independent plan progress."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def retire_routed_placeholders(store: Any) -> list[str]:
    """A proved transfer supersedes its empty staging queue, never a live project."""
    from pathlib import Path
    retired = []
    with store.read() as tx:
        routes = tx.query(
            "SELECT DISTINCT old.id,old.project,old.spec,p.root,new.project AS replacement "
            "FROM outcomes old JOIN projects p ON p.name=old.project "
            "JOIN evidence e ON e.ref='intent-route://'||old.id||':understood' "
            "JOIN outcomes understood ON understood.id=e.outcome_id "
            "JOIN outcomes new ON new.id=understood.parent_id "
            "JOIN projects np ON np.name=new.project "
            "WHERE old.parent_id IS NULL AND old.state='dropped' AND p.active=1 "
            "AND e.producer='intent_route_prover' AND e.verdict='pass' "
            "AND understood.state='done' AND new.project!=old.project "
            "AND new.state IN ('open','done') AND np.active=1")
    for route in routes:
        with store.tx() as tx:
            project = route['project']
            if (tx.one("SELECT 1 FROM claims WHERE project=? AND state IN ('active','preparing')", (project,))
                    or tx.one("SELECT 1 FROM outcomes WHERE project=? AND parent_id IS NULL "
                              "AND state IN ('open','done')", (project,))):
                continue
            tasks = tx.query("SELECT owns FROM tasks WHERE project=? AND state NOT IN ('accepted','rejected')",
                             (project,))
            root = Path(route['root']).resolve()
            safe = True
            for task in tasks:
                for raw in json.loads(task['owns']):
                    path = Path(raw)
                    path = (path if path.is_absolute() else root/path).resolve()
                    if not any(path.is_relative_to(root/folder) for folder in ('intent','.intent','ToDo','.clean-start','spikes')):
                        safe = False
            if not safe:
                continue
            tx.change('UPDATE projects SET active=0 WHERE name=? AND active=1', (project,),
                      event={'kind':'intent.placeholder_retired','project':project,
                             'payload':{'replacement':route['replacement'], 'route_root':route['id']}})
            retired.append(project)
    return retired


def resume_implementation_plans(store: Any) -> list[str]:
    """One retry on new accepted prerequisites; never accept a stage or run its gate.

    Applies only to a registered implementation outcome whose model-authored
    historical freeze used its own acceptance as until. Owner/manual freezes,
    cancellations, active claims and unchanged plans retain their authority.
    """
    from dev_mcp.fleet.queue import self_acceptance_until

    resumed = []
    with store.read() as tx:
        outcomes = tx.query(
            "SELECT o.id,o.project,o.spec FROM outcomes o JOIN projects p ON p.name=o.project "
            "WHERE o.state='open' AND o.title='implemented' AND p.active=1")
    for outcome in outcomes:
        project = str(outcome["project"])
        tz = str(outcome["id"]).rsplit(":", 1)[0] + "-implemented"
        with store.tx() as tx:
            task = tx.one("SELECT state,body,acceptance FROM tasks WHERE project=? AND tz=?", (project, tz))
            if (task is None or task["state"] != "frozen"
                    or not str(task["body"]).startswith(f'[intent-template] outcome={outcome["id"]}\n')
                    or tx.one("SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active'", (project, tz))):
                continue
            blocked = tx.one(
                "SELECT id,kind,worker_id,payload FROM events WHERE project=? AND tz=? "
                "AND kind IN ('block','freeze','thaw','reactivate','auto_reactivate') ORDER BY id DESC LIMIT 1",
                (project, tz))
            if blocked is None or blocked["kind"] not in ("freeze", "block") or not blocked["worker_id"]:
                continue
            payload = json.loads(blocked["payload"] or "{}")
            if payload.get("migration") or not self_acceptance_until(str(payload.get("reason") or ""), task["acceptance"]):
                continue
            spec = json.loads(outcome["spec"])
            dependencies = spec.get("depends_on", [])
            if any(not tx.one("SELECT 1 FROM outcomes WHERE id=? AND state='done'", (dep,)) for dep in dependencies):
                continue
            plan = tx.query(
                "SELECT tz,state,author_worker FROM tasks WHERE project=? AND kind='work' "
                "AND tz LIKE 'TZ-%' AND spec_path LIKE 'ToDo/tz/%'", (project,))
            if not plan or any(p["state"] != "accepted" for p in plan):
                continue
            receipts = []
            for item in plan:
                accepted = tx.one("SELECT id,worker_id,payload FROM events WHERE project=? AND tz=? "
                                  "AND kind='accept' ORDER BY id DESC LIMIT 1", (project, item["tz"]))
                if (accepted is None or not accepted["worker_id"]
                        or accepted["worker_id"] == item["author_worker"]
                        or not json.loads(accepted["payload"] or "{}").get("review_task")):
                    break
                receipts.append(int(accepted["id"]))
            if len(receipts) != len(plan) or max(receipts) <= int(blocked["id"]):
                continue
            tx.change("UPDATE tasks SET state='ready',body=body||?,updated_at=? WHERE project=? AND tz=? AND state='frozen'",
                      ("\n\nСервер: весь зарегистрированный план независимо принят после прежнего блокера; "
                       "выполни исходную приёмку заново. Это не accept стадии и не production PASS.\n",
                       datetime.now(timezone.utc).isoformat(), project, tz), event={"kind": "intent.implementation_dependencies_ready", "project": project,
                                           "tz": tz, "payload": {"blocked_event_id": blocked["id"],
                                                                   "accept_event_ids": receipts,
                                                                   "source": "independently_accepted_plan"}})
            resumed.append(f"{project}/{tz}")
    return resumed
