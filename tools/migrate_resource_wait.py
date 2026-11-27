"""One-time, opt-in migration of legacy phone-only fleet blocks.

Default and --dry are strictly read-only. Never migrate owner/guard freezes,
operator policies, or blocks that require human Gallery/field acceptance.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

PHONE_ONLY = (
    "physical phone resource became unavailable",
    "ресурс «телефон» недоступен",
    "нет ресурса «телефон»",
    "нет ресурса \"телефон\"",
)


def candidates(conn: sqlite3.Connection) -> list[tuple[int, str, str, str, str]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT t.project,t.tz,t.resources,e.id,e.kind,e.payload
        FROM tasks t JOIN events e ON e.id=(
          SELECT MAX(id) FROM events
          WHERE project=t.project AND tz=t.tz AND
          kind IN ('block','freeze','thaw','reactivate','approve','auto_reactivate'))
        WHERE t.state='frozen' ORDER BY t.project,t.tz"""
    )
    found = []
    for row in rows:
        if row["kind"] != "block":
            continue
        try:
            resources = json.loads(row["resources"])
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or "телефон" not in resources:
            continue
        if (payload.get("reactivate_policy") or payload.get("resource_wait") or
                payload.get("origin") in {"owner", "manual", "guard"} or
                payload.get("gate") in {"manual", "security"}):
            continue
        reason = str(payload.get("reason") or "").casefold()
        # A block requiring Gallery, signed APK or human consent is NOT merely
        # a missing USB resource; these are durable independent conditions.
        if not any(phrase in reason for phrase in PHONE_ONLY):
            continue
        if any(word in reason for word in ("gallery", "consent", "signed apk", "source-frozen")):
            continue
        found.append((row["id"], row["project"], row["tz"],
                      row["payload"], json.dumps({**payload, "origin": "fleet_auto",
                      "gate": "resource", "resource_wait": ["телефон"]}, ensure_ascii=False,
                      separators=(",", ":"))))
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/fleet/fleet.sqlite")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = Path(args.db).resolve()
    # Dry mode cannot acquire a write handle, even accidentally.
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode={'rw' if args.apply else 'ro'}", uri=True)
    try:
        entries = candidates(conn)
        for event_id, project, tz, _, _ in entries:
            print(f"{project}/{tz} event={event_id} resource_wait=телефон")
        print(f"TOTAL={len(entries)} mode={'APPLY' if args.apply else 'DRY'}")
        if args.apply:
            with conn:
                for event_id, _, _, old, new in entries:
                    cur = conn.execute(
                        "UPDATE events SET payload=? WHERE id=? AND kind='block' AND payload=?",
                        (new, event_id, old))
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Concurrent event modification: {event_id}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
