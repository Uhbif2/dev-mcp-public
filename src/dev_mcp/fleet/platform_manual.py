"""Operator repair outbox; it never registers self-editing fleet tasks."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Any

# Rebuild the multi-megabyte operator report immediately on new front/resolution
# events, and otherwise refresh its checked_at at most once per five minutes.
_EXPORT_REFRESH_S = 300.0
_EXPORT_STATE: dict[str, tuple[tuple[int, int], float]] = {}


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
    target = Path(raw).with_name('manual-platform-backlog.json')
    # Indexed MAX(id) probes avoid decoding every front and fsyncing a ~3.6 MB
    # JSON report on every 30s controller tick with no relevant changes.
    with store.read() as tx:
        cursor = (
            int(tx.scalar("SELECT MAX(id) FROM events WHERE kind='manual_platform_required'") or 0),
            int(tx.scalar("SELECT MAX(id) FROM events WHERE kind='manual_platform_resolved'") or 0),
        )
    now = time.monotonic()
    previous = _EXPORT_STATE.get(str(target))
    if (previous is not None and previous[0] == cursor
            and now - previous[1] < _EXPORT_REFRESH_S and target.is_file()):
        return
    with store.read() as tx:
        rows = tx.query("SELECT id,ts,payload FROM events WHERE kind='manual_platform_required' ORDER BY id")
        resolved = {json.loads(r['payload']).get('id'): json.loads(r['payload']) for r in
                    tx.query("SELECT payload FROM events WHERE kind='manual_platform_resolved' ORDER BY id")}
    payload = [{'event_id':r['id'],'ts':r['ts'],**json.loads(r['payload']),
                'resolution':resolved.get(json.loads(r['payload']).get('id'))} for r in rows]
    from .progress_watch import write_front_report
    from datetime import datetime, timezone
    write_front_report(target,datetime.now(timezone.utc),payload)
    # Only cache successful writes. An event arriving during the snapshot is
    # picked up by the next cursor check; failed writes retry next tick.
    _EXPORT_STATE[str(target)] = (cursor, time.monotonic())


RECHECK_EVERY_S = 600
_DRIFT_TITLE = 'не коммитится: файлы изменены вне флота'
_TERMINAL = ('accepted', 'rejected')


def _front_subject(front: dict[str, Any]) -> tuple[str, str] | None:
    """Watchdog fronts of the retired platform project name the task as «project/tz»."""
    project, tz = str(front.get('source_project') or ''), str(front.get('source_tz') or '')
    if project == 'Fleet Platform' and '/' in tz:
        project, tz = tz.split('/', 1)
    return (project, tz) if project and tz else None


def _drift_files(front: dict[str, Any]) -> list[str]:
    owns = front.get('owns') or []
    if owns:
        return [str(p) for p in owns]
    tail = str(front.get('detail') or '').rsplit('Файлы:', 1)
    return [p.strip() for p in tail[1].split(',') if p.strip()] if len(tail) == 2 else []


def _dirty(root: str, files: list[str]) -> set[str] | None:
    """Paths of ``files`` that differ from HEAD; None when git cannot tell."""
    import subprocess
    try:
        out = subprocess.run(['git', '-C', root, 'status', '--porcelain', '-z', '-uall', '--', *files],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    dirty: set[str] = set()
    for entry in out.stdout.decode('utf-8', 'replace').split('\0'):
        if len(entry) > 3:
            dirty.add(entry[3:].replace('\\', '/'))
    return dirty


def recheck(store: Any, *, now: Any = None, force: bool = False) -> dict[str, int]:
    """Retire operator fronts whose cause is gone (27.10: 172 of 367 open were stale).

    An operator queue nobody re-reads is a dead end: a front stays open long after the
    files were committed or the looping task finished. Only conditions the registry and
    git can prove are retired here; everything else stays for the operator. Retirement is
    recorded with producer ``auto-recheck`` and never thaws platform-deploy waiters.
    """
    from datetime import datetime, timezone
    stamp = now or datetime.now(timezone.utc)
    with store.read() as tx:
        last = tx.one("SELECT ts FROM events WHERE kind='manual_platform_recheck' ORDER BY id DESC LIMIT 1")
        if last and not force:
            try:
                if (stamp - datetime.fromisoformat(str(last['ts']))).total_seconds() < RECHECK_EVERY_S:
                    return {'skipped': 1}
            except (TypeError, ValueError):
                pass
        resolved = {json.loads(r['payload']).get('id') for r in
                    tx.query("SELECT payload FROM events WHERE kind='manual_platform_resolved'")}
        fronts = [json.loads(r['payload']) for r in
                  tx.query("SELECT payload FROM events WHERE kind='manual_platform_required' ORDER BY id")]
        fronts = [f for f in fronts if f.get('id') not in resolved]
        roots = {str(r['name']): str(r['root']) for r in tx.query('SELECT name,root FROM projects')}
        states: dict[tuple[str, str], str | None] = {}
        reviews: set[tuple[str, str]] = set()
        for front in fronts:
            subject = _front_subject(front)
            if subject:
                row = tx.one('SELECT state,kind FROM tasks WHERE project=? AND tz=?', subject)
                states[subject] = row['state'] if row else None
                if row and row['kind'] == 'review':
                    reviews.add(subject)
    retire: list[tuple[str, str]] = []
    drift: dict[str, list[tuple[dict[str, Any], list[str]]]] = {}
    for front in fronts:
        title = str(front.get('title', ''))
        subject = _front_subject(front)
        if (subject and states.get(subject) in _TERMINAL
                and (title.startswith('Watchdog: ') or subject in reviews)):
            retire.append((front['id'], f'subject task {subject[0]}/{subject[1]} is {states[subject]}'))
        elif title.endswith(_DRIFT_TITLE) and front.get('source_project') in roots:
            files = _drift_files(front)
            if files:
                drift.setdefault(str(front['source_project']), []).append((front, files))
    for project, items in drift.items():
        dirty = _dirty(roots[project], sorted({p for _, files in items for p in files}))
        if dirty is None:
            continue
        for front, files in items:
            if not any(p.replace('\\', '/') in dirty for p in files):
                retire.append((front['id'], 'all candidate files equal HEAD: ' + ', '.join(files[:10])))
    with store.tx() as tx:
        for ident, evidence in retire:
            tx.record_event(kind='manual_platform_resolved',
                            payload={'id': ident, 'candidate': '', 'evidence': evidence,
                                     'producer': 'auto-recheck'})
        tx.record_event(kind='manual_platform_recheck',
                        payload={'open': len(fronts), 'retired': len(retire)})
    if retire:
        export(store)
    return {'open': len(fronts), 'retired': len(retire)}


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
