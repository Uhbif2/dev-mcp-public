"""Read-only operator evidence for a project-by-project fleet retrospective."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-10-14T18:00:00+00:00")
    parser.add_argument("--output", type=Path, default=Path("logs/retrospective-20261015"))
    args = parser.parse_args()
    db = Path("data/fleet/fleet.sqlite").resolve()
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in conn.execute(sql, params)]

    args.output.mkdir(parents=True, exist_ok=True)
    summaries = []
    projects = rows("SELECT * FROM projects ORDER BY priority,name")
    for index, project in enumerate(projects, 1):
        name = project["name"]
        tasks = rows("SELECT * FROM tasks WHERE project=? AND state!='accepted' ORDER BY updated_at", (name,))
        counts = {r["state"]: r["n"] for r in rows(
            "SELECT state,count(*) n FROM tasks WHERE project=? GROUP BY state", (name,))}
        event_counts = {r["kind"]: r["n"] for r in rows(
            "SELECT kind,count(*) n FROM events WHERE project=? AND ts>=? GROUP BY kind", (name, args.since))}
        stages = rows("SELECT id,title,state,closed_ts,spec,evidence_spec FROM outcomes WHERE project=? ORDER BY created_ts", (name,))
        recent = rows("SELECT * FROM events WHERE project=? AND ts>=? AND kind NOT IN ('read','write','run','probe_run','routing_would_pick','checkpoint','expect','wake_order','cross_review_attempt') ORDER BY id", (name, args.since))
        last_progress = rows("SELECT ts,kind,tz FROM events WHERE project=? AND kind IN ('accept','integrate','outcome.closed') ORDER BY id DESC LIMIT 1", (name,))
        claims = rows("SELECT c.*,w.last_seen FROM claims c JOIN workers w ON w.id=c.worker_id WHERE c.project=? AND c.state IN ('active','preparing')", (name,))
        data = {"project": project, "since": args.since, "captured_at": datetime.now(timezone.utc).isoformat(),
                "counts": counts, "event_counts": event_counts, "tasks": tasks,
                "stages": stages, "events": recent, "claims": claims, "last_progress": last_progress}
        filename = f"{index:02d}-" + "".join(c if c.isalnum() or c in "-_" else "-" for c in name) + ".json"
        (args.output / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {"project": name, "file": filename, "active": project["active"], "counts": counts,
                   "night": {k: event_counts.get(k, 0) for k in ("take", "accept", "reject", "block", "integrate")},
                   "last": last_progress, "open_stages": [s["title"] for s in stages if s["state"] == "open"]}
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False))
    (args.output / "index.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    global_events = rows("SELECT * FROM events WHERE ts>=? AND project IS NULL AND kind LIKE '%error%' ORDER BY id", (args.since,))
    (args.output / "global-errors.json").write_text(json.dumps(global_events, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()


if __name__ == "__main__":
    main()
