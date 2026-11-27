"""Сверка тени «снимок сдачи / печать» по реестру (FLEET-SERVICE-WORK 03.11 §7 шаг 2).

    .venv\\Scripts\\python.exe tools\\snapshot_shadow.py [--since 2026-11-04] [--project P] [--json]

Для каждой сдачи со снимком (`submission_snapshot`) берёт её приёмку и первую последующую
интеграцию и спрашивает: что сделал интегратор по печатям, и что было бы по снимку.
- `committed`: интегратор закоммитил — совпадают ли байты коммита с байтами снимка;
- `seal_lost` / `drift`: интегратор отложил (нет печати / байты сдвинулись / чужие записи) —
  снимок эти байты держит, интеграция по нему была бы возможна без нового захвата;
- `no_commit`: проект сам не коммитит — сравнивать не с чем.
Только чтение: реестр — `mode=ro`, git — `rev-parse`.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "fleet" / "fleet.sqlite"
SEAL = ("missing_trusted_submission_file_seal", "not sealed")
DRIFT = ("accepted_candidate_bytes_changed", "unfinished foreign writes", "shared worktree diff",
         "candidate bytes changed")


def classify(payload: dict) -> str:
    if payload.get("commit"):
        return "committed"
    text = " ".join(str(payload.get(k) or "") for k in ("reason_code", "reason", "skipped")).casefold()
    if "коммит не разрешён" in text:
        return "no_commit"
    if any(s.casefold() in text for s in SEAL):
        return "seal_lost"
    if any(s.casefold() in text for s in DRIFT):
        return "drift"
    return "other"


def _blob(root: str, commit: str, path: str) -> str:
    done = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "--quiet", f"{commit}:{path}"],
                          capture_output=True, text=True, encoding="utf-8")
    return done.stdout.strip()


def report(db: Path = DB, *, since: str = "", project: str = "") -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    roots = {r["name"]: r["root"] for r in con.execute("SELECT name,root FROM projects")}
    query = "SELECT id,ts,project,tz,payload FROM events WHERE kind='submission_snapshot'"
    args: list = []
    if since:
        query += " AND ts>=?"
        args.append(since)
    if project:
        query += " AND project=?"
        args.append(project)
    counts: collections.Counter = collections.Counter()
    rows = []
    for snap in con.execute(query + " ORDER BY id", args):
        data = json.loads(snap["payload"] or "{}")
        state = ("snapshot" if data.get("commit") else "unchanged" if data.get("unchanged")
                 else "error" if data.get("error") else "skipped")
        accept = con.execute("SELECT id FROM events WHERE kind='accept' AND project=? AND tz=? AND id>? "
                             "ORDER BY id LIMIT 1", (snap["project"], snap["tz"], snap["id"])).fetchone()
        outcome, match = "pending", None
        if accept is not None:
            integ = con.execute("SELECT payload FROM events WHERE kind='integrate' AND project=? AND tz=? "
                                "AND id>? ORDER BY id LIMIT 1",
                                (snap["project"], snap["tz"], accept["id"])).fetchone()
            if integ is not None:
                ip = json.loads(integ["payload"] or "{}")
                outcome = classify(ip)
                if outcome == "committed" and state == "snapshot":
                    root = str(roots.get(snap["project"]) or "")
                    match = all(_blob(root, str(ip["commit"]), p) == b
                                for p, b in (data.get("blobs") or {}).items())
        else:
            reject = con.execute("SELECT 1 FROM events WHERE kind='reject' AND project=? AND tz=? AND id>? "
                                 "LIMIT 1", (snap["project"], snap["tz"], snap["id"])).fetchone()
            outcome = "rejected" if reject else "pending"
        counts[(state, outcome, match)] += 1
        rows.append({"project": snap["project"], "tz": snap["tz"], "ts": snap["ts"], "snapshot": state,
                     "integration": outcome, "bytes_match": match})
    return {"total": len(rows), "by_class": {" / ".join(str(x) for x in k): v
                                            for k, v in counts.most_common()}, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", default=str(DB))
    a = parser.parse_args(argv)
    out = report(Path(a.db), since=a.since, project=a.project)
    sys.stdout.reconfigure(encoding="utf-8")
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    print(f"сдач со снимком: {out['total']}  (снимок / интеграция по печатям / байты коммита = снимку)")
    for key, value in out["by_class"].items():
        print(f"  {value:5}  {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
