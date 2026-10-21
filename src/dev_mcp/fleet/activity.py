"""Short-lived browser observations, separate from model MCP progress."""
from __future__ import annotations
from datetime import datetime,timezone
from typing import Any


def record(store: Any, observations: list[dict[str,Any]]) -> int:
    if not isinstance(observations,list) or len(observations)>24:
        raise ValueError('invalid activity batch')
    for row in observations:
        if (not isinstance(row,dict) or set(row)!={'label','task','state','stall_s'} or
            not all(isinstance(row[k],str) for k in ('label','task','state')) or
            len(row['label'])>80 or len(row['task'])>600 or
            row['state'] not in {'generating','idle','error','unresponsive'} or
            type(row['stall_s']) is not int or not 0<=row['stall_s']<=86400):
            raise ValueError('invalid activity observation')
    stamp=datetime.now(timezone.utc).isoformat()
    count=0
    with store.tx() as tx:
        if not tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_activity'"):
            tx.change('CREATE TABLE worker_activity(worker_id TEXT PRIMARY KEY,claim_id INTEGER NOT NULL,'
                      'state TEXT NOT NULL,stall_s INTEGER NOT NULL,seen_at TEXT NOT NULL)',event='activity.schema')
        for row in observations:
            held=tx.one("SELECT w.id,c.id claim_id,c.project,c.tz FROM workers w JOIN claims c ON c.worker_id=w.id "
                        "WHERE w.label=? AND w.revoked_at IS NULL AND c.state='active' ORDER BY c.id DESC LIMIT 1",
                        (row['label'],))
            if not held or row['task']!=f"{held['project']}/{held['tz']}":
                continue
            tx.change('INSERT INTO worker_activity VALUES(?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET '
                      'claim_id=excluded.claim_id,state=excluded.state,stall_s=excluded.stall_s,seen_at=excluded.seen_at',
                      (held['id'],held['claim_id'],row['state'],row['stall_s'],stamp),event='activity.observed')
            count+=1
    return count
