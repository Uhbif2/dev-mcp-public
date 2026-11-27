"""Exact operator-authorized relocation of an existing intent project.

The source envelope, accepted artifacts and historical evidence stay immutable.
One versioned Store event grants only the verified destination, never its parent.
No policy mutation, restart, source polling, task reactivation or Git operation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .model import norm_path, paths_overlap

EVENT = 'project.relocated'
PROTOCOL = 'exact-project-relocation-v1'
OPEN_TASKS = ('ready', 'frozen', 'submitted')


class RelocationError(ValueError):
    pass


def _rows(db: Any, sql: str, args: tuple = ()) -> list:
    return db.query(sql, args) if hasattr(db, 'query') else db.execute(sql, args).fetchall()


def _canonical(raw: str, *, must_exist: bool = True) -> Path:
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts or path.is_symlink():
        raise RelocationError('noncanonical relocation root')
    if must_exist and not path.is_dir():
        raise RelocationError('destination directory unavailable')
    if path.resolve(strict=must_exist) != path.absolute():
        raise RelocationError('relocation root contains an alias')
    return path.absolute()


def _manifest(root: Path, project: str, origin: dict) -> tuple[dict, str]:
    path = root / 'component.json'
    if path.is_symlink() or path.resolve() != path.absolute():
        raise RelocationError('component manifest alias')
    raw = path.read_bytes()
    data = json.loads(raw)
    if (data.get('kind') != 'intent-outcome-tree' or data.get('name') != project
            or data.get('origin') != origin or not data.get('component_id')):
        raise RelocationError('destination origin/identity mismatch')
    return data, hashlib.sha256(raw).hexdigest()


def authorized_target(db: Any, project: str, root: Path, spec: dict) -> Path | None:
    """Return the exact current target only for a valid server-owned receipt.

    Invalid/stale records fail closed. No event means ordinary authority applies.
    """
    rows = _rows(db, 'SELECT payload FROM events WHERE kind=? AND project=? ORDER BY id DESC LIMIT 1',
                 (EVENT, project))
    if not rows:
        return None
    record = json.loads(rows[0]['payload'])
    if (record.get('protocol') != PROTOCOL or record.get('operator_authorized') is not True
            or record.get('project') != project):
        raise RelocationError('invalid relocation authority')
    destination = _canonical(record['to_root'])
    if norm_path(str(root)) != norm_path(str(destination)):
        raise RelocationError('relocation is not a grant for this directory')
    if norm_path(str(spec.get('target', ''))) not in (
            norm_path(record['from_root']), norm_path(str(destination))):
        raise RelocationError('relocation source binding mismatch')
    origin = {'source_id': spec.get('source_id'), 'external_id': spec.get('external_id')}
    if record.get('origin') != origin or record.get('source_revision') != spec.get('revision'):
        raise RelocationError('relocation belongs to another source revision')
    manifest, digest = _manifest(destination, project, origin)
    if digest != record.get('manifest_sha256') or manifest['component_id'] != record.get('component_id'):
        raise RelocationError('relocated identity changed')
    if hashlib.sha256((destination / 'intent/understood.md').read_bytes()).hexdigest() != record.get('understood_sha256'):
        raise RelocationError('proved route artifact changed')
    return destination


def current_target(store: Any, project: str, root: Path, spec: dict) -> Path | None:
    with store.read() as tx:
        return authorized_target(tx, project, root, spec)


def _state(tx: Any, project: str) -> dict:
    projects = _rows(tx, 'SELECT * FROM projects WHERE name=?', (project,))
    if len(projects) != 1 or not projects[0]['active']:
        raise RelocationError('existing active project required')
    return {
        'project': dict(projects[0]),
        'tasks': [dict(r) for r in _rows(tx, "SELECT * FROM tasks WHERE project=? AND state IN ('ready','frozen','submitted') ORDER BY tz", (project,))],
        'outcomes': [dict(r) for r in _rows(tx, "SELECT * FROM outcomes WHERE project=? AND state='open' ORDER BY id", (project,))],
        'queue_sources': [dict(r) for r in _rows(tx, 'SELECT * FROM queue_sources WHERE project=?', (project,))],
    }


def _no_writers(tx: Any, project: str, old: str, new: str) -> None:
    if _rows(tx, "SELECT id FROM claims WHERE project=? AND state='active'", (project,)):
        raise RelocationError('active project claim')
    for row in _rows(tx, "SELECT cp.path_norm FROM claims c JOIN claim_paths cp ON cp.claim_id=c.id WHERE c.state='active'"):
        if paths_overlap(str(row['path_norm']), old) or paths_overlap(str(row['path_norm']), new):
            raise RelocationError('active writer overlaps relocation')
    for row in _rows(tx, 'SELECT name,root FROM projects WHERE name<>?', (project,)):
        if norm_path(str(row['root'])) == norm_path(new):
            raise RelocationError('destination registered to another project')


def prepare(store: Any, project: str, destination: str) -> dict:
    """Read-only plan. Caller persists the exact plan before calling apply()."""
    new = _canonical(destination)
    with store.read() as tx:
        state = _state(tx, project)
        old = str(state['project']['root'])
        if Path(old).exists():
            raise RelocationError('old root still exists; move is not established')
        if norm_path(old) == norm_path(str(new)):
            raise RelocationError('same relocation root')
        _no_writers(tx, project, old, str(new))
        roots = [r for r in state['outcomes'] if r['parent_id'] is None]
        if len(roots) != 1:
            raise RelocationError('one open intent root required')
        spec = json.loads(roots[0]['spec'])
        if norm_path(spec.get('target', '')) != norm_path(old):
            raise RelocationError('root target differs from registry')
        grants = json.loads(roots[0]['authority']).get('project_roots', ())
        old_path = Path(old).absolute()
        if not any(old_path != Path(p).absolute() and old_path.is_relative_to(Path(p).absolute()) for p in grants):
            raise RelocationError('original root lacks authority')
        origin = {'source_id': spec.get('source_id'), 'external_id': spec.get('external_id')}
        manifest, digest = _manifest(new, project, origin)
        understood = new / 'intent/understood.md'
        if understood.is_symlink() or understood.resolve() != understood.absolute():
            raise RelocationError('proved route artifact alias')
        understood_sha = hashlib.sha256(understood.read_bytes()).hexdigest()
        # Source authority is immutable historical provenance, not a mutable grant.
        source = _rows(tx, 'SELECT * FROM intent_source_revisions WHERE source_id=? AND external_id=?',
                       (origin['source_id'], origin['external_id']))
        if len(source) != 1 or source[0]['state'] != 'active' or source[0]['revision'] != spec['revision']:
            raise RelocationError('active source revision mismatch')
        state['source_revision'] = dict(source[0])
    return {'protocol': PROTOCOL, 'operator_authorized': True, 'project': project,
            'from_root': old, 'to_root': str(new), 'origin': origin,
            'source_revision': spec['revision'], 'manifest_sha256': digest,
            'component_id': manifest['component_id'], 'understood_sha256': understood_sha,
            'preimage': state}


def _rebase(raw: str, old: str, new: str) -> str:
    normalized = norm_path(raw)
    base = norm_path(old)
    if normalized == base or normalized.startswith(base + '/'):
        return norm_path(new) + normalized[len(base):]
    return raw


def apply(store: Any, plan: dict, preimage_path: Path) -> int:
    """CAS current operational bindings and append one durable exact-root grant.

    The operator must install/validate exact policy roots separately before this.
    No accepted/rejected row, envelope, revision hash or evidence is rewritten.
    """
    expected = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if preimage_path.is_symlink() or preimage_path.read_bytes() != expected:
        raise RelocationError('exact persisted preimage plan required')
    project, old, new = plan['project'], plan['from_root'], plan['to_root']
    # Rebuild the entire plan before acquiring writer lock, then compare again in tx.
    if prepare(store, project, new) != plan:
        raise RelocationError('relocation plan changed')
    with store.tx() as tx:
        _no_writers(tx, project, old, new)
        current = _state(tx, project)
        source = _rows(tx, 'SELECT * FROM intent_source_revisions WHERE source_id=? AND external_id=?',
                       (plan['origin']['source_id'], plan['origin']['external_id']))
        current['source_revision'] = dict(source[0]) if len(source) == 1 else None
        if current != plan['preimage']:
            raise RelocationError('CAS state mismatch')
        manifest, digest = _manifest(_canonical(new), project, plan['origin'])
        if digest != plan['manifest_sha256'] or manifest['component_id'] != plan['component_id']:
            raise RelocationError('destination changed before apply')
        changes = [('UPDATE projects SET root=? WHERE name=?', (new, project))]
        for row in current['tasks']:
            owns = json.dumps([_rebase(p, old, new) for p in json.loads(row['owns'])], ensure_ascii=False)
            spec_path = _rebase(row['spec_path'], old, new) if Path(row['spec_path']).is_absolute() else row['spec_path']
            changes.append(('UPDATE tasks SET owns=?,spec_path=? WHERE project=? AND tz=?',
                            (owns, spec_path, project, row['tz'])))
        for row in current['outcomes']:
            spec = json.loads(row['spec'])
            if 'target' in spec and norm_path(str(spec['target'])) == norm_path(old):
                spec['target'] = new
                changes.append(('UPDATE outcomes SET spec=?,rev=rev+1 WHERE id=?',
                                (json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(',', ':')), row['id'])))
        for row in current['queue_sources']:
            changes.append(('UPDATE queue_sources SET root_key=? WHERE project=?', (norm_path(new), project)))
        payload = {k: v for k, v in plan.items() if k != 'preimage'}
        payload.update(preimage_path=str(preimage_path), preimage_sha256=hashlib.sha256(expected).hexdigest())
        tx.changes(changes, event={'kind': EVENT, 'project': project, 'payload': payload})
        event_id = int(tx.scalar('SELECT last_insert_rowid()'))
        if authorized_target(tx, project, Path(new), json.loads(current['outcomes'][0]['spec'])) != Path(new):
            raise RelocationError('transaction readback failed')
    return event_id
