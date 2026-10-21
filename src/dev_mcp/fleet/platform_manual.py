"""Operator repair outbox; it never registers self-editing fleet tasks."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any

def enabled() -> bool:
    from . import goals
    return goals._load_doc().get('platform', {}).get('maintenance') == 'manual'

def enqueue(tx: Any, fingerprint: str, *, title: str, detail: str,
            source_project: str = '', source_tz: str = '', owns: list[str] | None = None,
            acceptance: list[str] | None = None) -> str:
    ident = 'manual-platform:' + fingerprint
    if not tx.one("SELECT 1 FROM events WHERE kind='manual_platform_required' "
                  "AND json_extract(payload,'$.id')=? LIMIT 1", (ident,)):
        tx.record_event(kind='manual_platform_required',project=source_project or None,tz=source_tz or None,
                        payload={'id':ident,'title':title,'detail':detail[:16000],
                                 'source_project':source_project,'source_tz':source_tz,
                                 'owns':owns or [],'acceptance':acceptance or []})
    return ident

def export(store: Any) -> None:
    raw = str(getattr(store,'path',''))
    if not raw or raw == ':memory:': return
    with store.read() as tx:
        rows = tx.query("SELECT id,ts,payload FROM events WHERE kind='manual_platform_required' ORDER BY id")
        resolved = {json.loads(r['payload']).get('id'): json.loads(r['payload']) for r in
                    tx.query("SELECT payload FROM events WHERE kind='manual_platform_resolved' ORDER BY id")}
    payload = [{'event_id':r['id'],'ts':r['ts'],**json.loads(r['payload']),
                'resolution':resolved.get(json.loads(r['payload']).get('id'))} for r in rows]
    target = Path(raw).with_name('manual-platform-backlog.json')
    from .progress_watch import write_front_report
    from datetime import datetime, timezone
    write_front_report(target,datetime.now(timezone.utc),payload)


def resolve(store: Any, ident: str, *, candidate: str, evidence: str) -> int:
    """Trusted operator entry point, deliberately absent from the agent MCP API.

    Invoke only after verifying the named repair on the deployed main commit.
    The evidence and commit remain in the registry even after project removal.
    Legacy Fleet Platform identities may refer to the archived execution ledger.
    """
    if not ident.startswith(('manual-platform:', 'Fleet Platform/')):
        raise ValueError('expected a manual or archived platform repair identity')
    if not re.fullmatch('[a-f0-9]{40}',candidate) or not evidence.strip():
        raise ValueError('deployed commit and verification evidence are required')
    with store.tx() as tx:
        prior = tx.one("SELECT id FROM events WHERE kind='manual_platform_resolved' "
                       "AND json_extract(payload,'$.id')=? "
                       "AND json_extract(payload,'$.candidate')=? ORDER BY id DESC LIMIT 1",(ident,candidate))
        if prior: return int(prior['id'])
        tx.record_event(kind='manual_platform_resolved',payload={'id':ident,'candidate':candidate,
                        'evidence':evidence,'producer':'operator-verified-deployment'})
        receipt = int(tx.scalar("SELECT MAX(id) FROM events"))
        required = tx.one("SELECT payload FROM events WHERE kind='manual_platform_required' "
                          "AND json_extract(payload,'$.id')=? ORDER BY id DESC LIMIT 1",(ident,))
        if required:
            source = json.loads(required['payload'])
            project,tz = source.get('source_project'),source.get('source_tz')
            task = tx.one("SELECT state,kind FROM tasks WHERE project=? AND tz=?",(project,tz))
            error = tx.one("SELECT payload FROM events WHERE kind='triage_apply_error' AND project=? AND tz=? "
                           "ORDER BY id DESC LIMIT 1",(project,tz))
            if (task and task['state']=='accepted' and task['kind']=='triage' and error
                    and json.loads(error['payload']).get('repair') == ident):
                tx.record_event(kind='triage_apply_reopen',project=project,tz=tz,
                                payload={'producer':'operator-verified-deployment','receipt_event_id':receipt})
        return receipt
