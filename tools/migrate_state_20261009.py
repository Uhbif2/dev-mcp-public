"""Repair historical fleet records. Explicit --db; default is read-only --dry-run."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dev_mcp.fleet import judgment, queue  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402

_PATH = re.compile(r"([/\\]todo[/\\]decisions[/\\])([^/\\]+)(\.md)$", re.I)


def _obj(value, fallback):
    try:
        return json.loads(value or "") 
    except (ValueError, TypeError):
        return fallback


def _plan(tx):
    result = []
    for row in tx.query("SELECT project,tz,state,kind,owns,body,acceptance FROM tasks WHERE state!='taken'"):
        p, tz, state = row["project"], row["tz"], row["state"]
        owns = _obj(row["owns"], [])
        changes = []
        for old in owns:
            m = _PATH.search(old.replace("\\", "/"))
            new = old if not m else old[:m.start(2)] + judgment._safe_name(m.group(2)) + old[m.end(2):]
            changes.append(new)
        if owns != changes:
            body = row["body"]
            for old, new in zip(owns, changes):
                if old != new:
                    old_base = old.replace("\\", "/").split("/")[-1]
                    new_base = new.replace("\\", "/").split("/")[-1]
                    body = body.replace("ToDo/decisions/" + old_base, "ToDo/decisions/" + new_base)
            last = tx.one("SELECT kind,payload FROM events WHERE project=? AND tz=? "
                          "AND kind IN ('block','freeze','thaw','reactivate','approve') "
                          "ORDER BY id DESC LIMIT 1", (p, tz))
            blocked = state == "frozen" and last and last["kind"] == "block" and any(
                old != new and old.replace("\\", "/").split("/")[-1] in
                str(_obj(last["payload"], {}).get("reason", ""))
                for old, new in zip(owns, changes)
            )
            result.append({"kind": "decision_path", "project": p, "tz": tz,
                           "owns": changes, "body": body, "state": "ready" if blocked else state,
                           "reason": "Windows-invalid ':' in decision filename"})
        if state == "frozen":
            last = tx.one("SELECT kind,payload FROM events WHERE project=? AND tz=? "
                          "AND kind IN ('block','freeze','thaw','reactivate','approve') "
                          "ORDER BY id DESC LIMIT 1", (p, tz))
            if last and last["kind"] == "block":
                reason = str(_obj(last["payload"], {}).get("reason", ""))
                if queue.self_acceptance_until(reason, row["acceptance"]):
                    result.append({"kind": "self_acceptance", "project": p, "tz": tz,
                                   "reason": reason})
        if row["kind"] == "triage" and state == "accepted":
            old = tx.one("SELECT id,payload FROM events WHERE project=? AND tz=? "
                         "AND kind='triage_applied' ORDER BY id DESC LIMIT 1", (p, tz))
            if old and _obj(old["payload"], {}).get("errors"):
                reopened = tx.one("SELECT id FROM events WHERE project=? AND tz=? "
                                  "AND kind='triage_apply_reopen' AND id>? LIMIT 1",
                                  (p, tz, old["id"]))
                if not reopened:
                    result.append({"kind": "triage_error", "project": p, "tz": tz,
                                   "reason": "legacy triage_applied contains errors"})
    return result


def migrate(store: Store, *, apply: bool = False) -> list[dict]:
    if not apply:
        with store.read() as tx:
            return _plan(tx)
    with store.tx() as tx:
        actions = _plan(tx)
        for action in actions:
            project, tz, kind = action["project"], action["tz"], action["kind"]
            if kind == "decision_path":
                tx.change("UPDATE tasks SET owns=?,body=?,state=?,updated_at=? "
                          "WHERE project=? AND tz=? AND state!='taken'",
                          (json.dumps(action["owns"], ensure_ascii=False), action["body"],
                           action["state"], queue._now(), project, tz),
                          event={"kind": "state_migrated", "project": project, "tz": tz,
                                 "payload": {"reason": action["reason"], "action": kind}})
            elif kind == "self_acceptance":
                tx.record_event(kind="freeze", project=project, tz=tz,
                                payload={"reason": action["reason"], "migration": True})
                queue._self_acceptance_triage(tx, project, tz, action["reason"])
                tx.record_event(kind="state_migrated", project=project, tz=tz,
                                payload={"reason": "self acceptance is not an external dependency"})
            elif kind == "triage_error":
                tx.record_event(kind="triage_apply_reopen", project=project, tz=tz,
                                payload={"reason": action["reason"]})
                tx.record_event(kind="state_migrated", project=project, tz=tz,
                                payload={"reason": action["reason"]})
    return actions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--dry-run", action="store_true")
    choice.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    path = args.db.resolve(strict=True)
    if args.apply:
        actions = migrate(Store.open(str(path)), apply=True)
    else:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        class View:
            def query(self, sql, params=()):
                return conn.execute(sql, params).fetchall()
            def one(self, sql, params=()):
                return conn.execute(sql, params).fetchone()
        try:
            actions = _plan(View())
        finally:
            conn.close()
    print(json.dumps({"mode": "apply" if args.apply else "dry-run",
                      "count": len(actions), "changes": actions}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
