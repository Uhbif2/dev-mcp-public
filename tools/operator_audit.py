"""Read-only operator evidence for integration incidents and lesson outcomes."""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dev_mcp.fleet.model import task_root
from dev_mcp.json_book import write_json


def integrations(conn, project, tasks):
    result = []
    recorded = list(conn.execute(
        "SELECT id,ts,kind,tz,rc,payload FROM events WHERE project=? "
        "AND kind IN ('submit','submission_file_seal','accept','integrate') ORDER BY id", (project,)))
    for tz in tasks:
        task = conn.execute("SELECT state,updated_at FROM tasks WHERE project=? AND tz=?",
                            (project, tz)).fetchone()
        events = []
        for row in recorded:
            if task_root(str(row['tz'] or '')) != tz:
                continue
            payload = json.loads(row['payload'])
            events.append({**{k: row[k] for k in ('id', 'ts', 'kind', 'tz', 'rc')},
                           'details': {k: payload[k] for k in (
                               'accept_id', 'commit', 'commits', 'reason', 'reason_code',
                               'deferred', 'waiting_for', 'skipped', 'skip', 'already_in_head',
                               'superseded', 'source', 'submission_id', 'files') if k in payload}})
        result.append({'tz': tz, 'task': dict(task) if task else None, 'events': events})
    return {'project': project, 'tasks': result}


def lessons(conn):
    catalog = {}
    for row in conn.execute("SELECT id,ts,kind,project,tz,payload FROM events "
                            "WHERE kind IN ('lesson_add','lesson_retire') ORDER BY id"):
        data = json.loads(row['payload'])
        key = (row['project'], data.get('id'))
        if row['kind'] == 'lesson_retire':
            catalog.pop(key, None)
        elif data.get('id'):
            catalog[key] = {'event_id': row['id'], 'registered_at': row['ts'],
                            'source_tz': row['tz'], **data}
    items = []
    for (project, ident), item in catalog.items():
        refs = item.get('evidence') or []
        verified = []
        for ref in refs:
            row = conn.execute("SELECT id,kind,project,tz FROM events WHERE id=?", (ref,)).fetchone()
            if row:
                verified.append(dict(row))
        # A class marked 'code' is a proposal to the architect, not proof of an implemented guard.
        implementations = [dict(row) for row in conn.execute(
            "SELECT id,ts,tz FROM events WHERE kind='lesson_implemented' AND project=? "
            "AND json_extract(payload,'$.lesson_id')=? AND id>? ORDER BY id",
            (project, ident, item['event_id']))]
        items.append({'project': project, 'id': ident, 'tier': item.get('tier'),
                      'class': item.get('class'), 'check': item.get('check'),
                      'registered_at': item['registered_at'], 'source_tz': item['source_tz'],
                      'evidence_count': len(refs), 'existing_evidence': verified,
                      'foreign_evidence_count': sum(r['project'] != project for r in verified),
                      'implementation_receipts': implementations,
                      'effect': 'unproven' if not implementations else 'requires_post_implementation_comparison'})
    return {'unique_lessons': len(items),
            'tiers': dict(collections.Counter(x['tier'] for x in items)),
            'event_counts': {r[0]: r[1] for r in conn.execute(
                "SELECT kind,count(*) FROM events WHERE kind IN "
                "('lesson_scan','lesson_add','lesson_shown','lesson_implemented') GROUP BY kind")},
            'lessons': items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('integrations', 'lessons'))
    parser.add_argument('--db', type=Path, default=Path(__file__).resolve().parents[1] / 'data/fleet/fleet.sqlite')
    parser.add_argument('--project', default='Beta App')
    parser.add_argument('--tasks', nargs='+', default=['TZ-544','TZ-545','TZ-549','TZ-550','TZ-551','TZ-552','TZ-553','TZ-554'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        body = integrations(conn, args.project, args.tasks) if args.kind == 'integrations' else lessons(conn)
    payload = {'observed_at': datetime.now(timezone.utc).isoformat(), 'read_only': True, **body}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, payload, indent=2)
    print(json.dumps({'kind': args.kind, 'output': str(args.output),
                      'items': len(body.get('tasks', body.get('lessons', [])))}, ensure_ascii=False))


if __name__ == '__main__':
    main()
