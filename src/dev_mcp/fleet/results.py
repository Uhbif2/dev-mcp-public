"""Publish immutable, gated changes to the owner's existing local result channel.

An available change bundle is not a deployment or an owner approval. All three
states retain separate evidence. No cloud adapter or new agent authority exists.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
import zipfile
from typing import Any

from dev_mcp.process_execution import bounded_run
from .gate_snapshot import _restricted

MAX_BYTES = 32 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(64*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: str, *args: str) -> bytes:
    return bounded_run(['git', '-c', 'core.hooksPath=', '-c', 'core.fsmonitor=false', *args],
                       cwd=root, capture_output=True, check=True, timeout=30).stdout


def available(tx: Any, *, project: str | None = None, tz: str | None = None) -> list[dict]:
    """Latest owner artifacts, rehashed before advertising them as available."""
    clauses = ["kind='owner_result_available'"]
    args: list[Any] = []
    for field, value in (('project', project), ('tz', tz)):
        if value is not None:
            clauses.append(field + '=?')
            args.append(value)
    # Backfilling an older integration must not displace the project's newer
    # result or create another completion notification every thirty seconds.
    rows = tx.query('SELECT id,project,tz,payload FROM events WHERE ' + ' AND '.join(clauses)
                    + " ORDER BY json_extract(payload,'$.accept_id') DESC,id DESC LIMIT 32", args)
    out, seen = [], set()
    for row in rows:
        key = (row['project'], row['tz'])
        if key in seen:
            continue
        seen.add(key)
        data = json.loads(row['payload'])
        path = Path(data.get('artifact', ''))
        try:
            if (path.is_symlink() or path.stat().st_size > MAX_BYTES or
                    _digest(path) != data.get('sha256')):
                continue
        except OSError:
            continue
        out.append({**data, 'event_id': row['id'], 'project': row['project'], 'tz': row['tz']})
    return out


class ResultPublisher:
    def __init__(self, store: Any, project_info: Any):
        self.store, self.project_info = store, project_info
        self.home = Path(store.path).resolve().parent / 'results'

    def _superseded(self, row: Any) -> bool:
        """A newer accept of the same task exists: this integration will never publish."""
        try:
            accept_id = int(json.loads(row['payload']).get('accept_id') or 0)
        except (TypeError, ValueError):
            return False
        with self.store.tx() as tx:
            newer = tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept' AND project=? AND tz=?",
                              (row['project'], row['tz']))
            if not accept_id or newer is None or int(newer) <= accept_id:
                return False
            tx.record_event(kind='owner_result_superseded', project=row['project'], tz=row['tz'],
                            payload={'integration_id': row['id'], 'accept_id': accept_id,
                                     'newer_accept_id': int(newer)})
        return True

    def _publish(self, row: Any) -> bool:
        integration = json.loads(row['payload'])
        commit = str(integration.get('commit') or '')
        if not re.fullmatch('[0-9a-f]{7,40}', commit):
            return False
        with self.store.read() as tx:
            if tx.one("SELECT 1 FROM events WHERE kind='owner_result_available' "
                      "AND json_extract(payload,'$.integration_id')=?", (row['id'],)):
                return False
            task = tx.one('SELECT t.state,t.title,p.root FROM tasks t JOIN projects p ON p.name=t.project '
                          'WHERE t.project=? AND t.tz=? AND p.active=1', (row['project'],row['tz']))
            last_accept=tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept' AND project=? AND tz=?",
                                  (row['project'],row['tz']))
        if not task or task['state'] != 'accepted' or last_accept!=integration['accept_id']:
            return False
        root = str(task['root'])
        revision = _git(root, 'rev-parse', '--verify', commit + '^{commit}').decode().strip()
        names = _git(root, 'diff-tree','--root','--no-commit-id','--name-only','-r','-z',revision)
        paths = [p.decode('utf-8') for p in names.split(b'\0') if p]
        if len(paths) > 400 or any(_restricted(p) or PurePosixPath(p).is_absolute()
                                   or '..' in PurePosixPath(p).parts for p in paths):
            raise ValueError('result paths exceed the safe artifact boundary')
        flags = self.project_info(str(row['project']))
        gate_id = None
        tested_revision = None
        if flags.get('gate'):
            with self.store.read() as tx:
                gates = tx.query("SELECT id,payload FROM events WHERE kind='gate' AND project=? "
                                 "AND rc=0 AND id>? ORDER BY id DESC LIMIT 32", (row['project'], row['id']))
            for gate in gates:
                proof = json.loads(gate['payload'])
                if str(proof.get('command') or '').strip()!=str(flags['gate']).strip():
                    continue
                # A later batch HEAD is valid only when this integrated revision
                # is included in that exact tested history.
                tested = str(proof.get('tested_revision') or '')
                if not re.fullmatch('[0-9a-f]{40}', tested):
                    continue
                relation = bounded_run(['git','merge-base','--is-ancestor',revision,tested],
                                       cwd=root,capture_output=True,timeout=10)
                if relation.returncode == 0:
                    unchanged=bounded_run(['git','diff','--quiet','--no-ext-diff',revision,tested,'--',*paths],
                                          cwd=root,capture_output=True,timeout=10)
                    if unchanged.returncode!=0:
                        continue
                    gate_id = int(gate['id'])
                    tested_revision = tested
                    break
            if gate_id is None:
                return False
        patch = _git(root,'show','--format=fuller','--binary','--no-ext-diff',revision,'--',*paths)
        if len(patch) > MAX_BYTES:
            raise ValueError('result exceeds artifact size limit')
        entries = _git(root,'ls-tree','-rlz',revision,'--',*paths)
        files=[]
        total=len(patch)
        for entry in entries.split(b'\0'):
            if not entry: continue
            metadata,name=entry.split(b'\t',1)
            mode,kind,sha,size=metadata.split()
            if kind!=b'blob' or mode not in (b'100644',b'100755'):
                raise ValueError('result contains unsupported linked files')
            total+=int(size)
            if total>MAX_BYTES:
                raise ValueError('result file contents exceed artifact size limit')
            files.append((name.decode('utf-8'),sha.decode('ascii')))
        # Read blobs directly: archive can apply export-ignore/export-subst,
        # which would change or omit the very bytes the gate has verified.
        contents={name:_git(root,'cat-file','blob',sha) for name,sha in files}
        target = self.home / hashlib.sha256(str(row['project']).encode()).hexdigest()[:16] / str(row['id'])
        # Every path is server-derived. Refuse reparse redirects before writing.
        for part in [self.home, target.parent, target]:
            if part.is_symlink() or getattr(part,'is_junction',lambda:False)():
                raise ValueError('linked result directory')
            part.mkdir(parents=True,exist_ok=True)
        artifact = target / 'result.zip'
        if artifact.is_symlink() or getattr(artifact,'is_junction',lambda:False)():
            raise ValueError('linked result artifact')
        receipt = {'integration_id': int(row['id']), 'accept_id': int(integration['accept_id']),
                   'revision': revision, 'gate_event': gate_id, 'gate_required': bool(flags.get('gate')),
                   'tested_revision':tested_revision,
                   'artifact': str(artifact), 'files': paths, 'created_at': _now(),
                   'status': 'artifact_available', 'deployment': 'not_claimed', 'owner_accepted': False}
        with tempfile.TemporaryFile() as temporary:
            with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as package:
                package.writestr('changes.patch',patch)
                for name,content in contents.items():
                    package.writestr('files/'+name,content)
                package.writestr('receipt.json',json.dumps(receipt,ensure_ascii=False,indent=2))
                package.writestr('RESULT.md',f"# {task['title'] or row['tz']}\n\n"
                                 f"{row['project']}/{row['tz']}\n\nRevision: {revision}\n\n"
                                 "Accepted changes and server gate evidence. This bundle does not claim deployment or owner approval.\n")
            temporary.seek(0)
            data = temporary.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES:
            raise ValueError('result package exceeds artifact size limit')
        receipt['sha256']=hashlib.sha256(data).hexdigest()
        # A crash can leave the already-built bundle. It is reused only if its
        # complete hash matches; no foreign content is silently overwritten.
        if artifact.exists():
            if hashlib.sha256(artifact.read_bytes()).hexdigest()!=receipt['sha256']:
                # Archive metadata timestamps vary across retries. Verify its
                # immutable receipt/patch before adopting the existing bytes.
                with zipfile.ZipFile(artifact) as existing:
                    old=json.loads(existing.read('receipt.json'))
                    expected={'changes.patch','receipt.json','RESULT.md',
                              *('files/'+name for name in contents)}
                    immutable={key:value for key,value in receipt.items() if key not in {'created_at','sha256'}}
                    if (set(existing.namelist())!=expected or len(existing.namelist())!=len(expected) or
                            any(old.get(key)!=value for key,value in immutable.items()) or
                            existing.read('changes.patch')!=patch or
                            any(existing.read('files/'+name)!=content for name,content in contents.items())):
                        raise ValueError('existing result differs from integrated changes')
                receipt['sha256']=hashlib.sha256(artifact.read_bytes()).hexdigest()
        else:
            with artifact.open('xb') as sink:
                sink.write(data)
        if hashlib.sha256(artifact.read_bytes()).hexdigest()!=receipt['sha256']:
            raise ValueError('result readback failed')
        with self.store.tx() as tx:
            current=tx.one('SELECT t.state,p.root FROM tasks t JOIN projects p ON p.name=t.project '
                           'WHERE t.project=? AND t.tz=? AND p.active=1',(row['project'],row['tz']))
            current_accept=tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept' AND project=? AND tz=?",
                                     (row['project'],row['tz']))
            if not current or current['state']!='accepted' or current['root']!=root or current_accept!=integration['accept_id']:
                return False
            if not tx.one("SELECT 1 FROM events WHERE kind='owner_result_available' "
                          "AND json_extract(payload,'$.integration_id')=?", (row['id'],)):
                tx.record_event(kind='owner_result_available', project=row['project'],tz=row['tz'],payload=receipt)
                return True
        return False

    def tick(self) -> int:
        since=(datetime.now(timezone.utc)-timedelta(days=14)).isoformat()
        cutoff=time.time()-600
        # 05.11.2026: one query with three correlated json_extract subqueries over all
        # owner_result_wait events (87k) took 43 s and ran every 30 s — a whole core of
        # the server. Same selection: three index scans by kind, the join in Python.
        # Incremental: the first tick reads the window, later ticks only events after it.
        state=self.__dict__.setdefault('_scan',{'seen':0,'cand':{},'done':set(),'waits':{}})
        with self.store.read() as tx:
            seen=state['seen']
            for r in tx.query("SELECT id,project,tz,ts,payload FROM events WHERE kind='integrate' "
                              "AND id>? AND ts>=? AND json_extract(payload,'$.commit') IS NOT NULL",
                              (seen,since)):
                state['cand'][r['id']]=r
            state['done'].update(r['iid'] for r in tx.query(
                "SELECT json_extract(payload,'$.integration_id') AS iid FROM events "
                "WHERE kind IN ('owner_result_available','owner_result_superseded') AND id>?",(seen,)))
            for r in tx.query("SELECT id,json_extract(payload,'$.integration_id') AS iid,"
                              "json_extract(payload,'$.checked_at') AS checked FROM events "
                              "WHERE kind='owner_result_wait' AND id>? AND ts>=?",(seen,since)):
                last,checked=state['waits'].get(r['iid'],(0,None))
                state['waits'][r['iid']]=(max(last,r['id']),
                                          r['checked'] if checked is None or (r['checked'] or 0)>checked else checked)
            state['seen']=int(tx.scalar("SELECT COALESCE(MAX(id),0) FROM events") or 0)
        for key in [k for k,r in state['cand'].items() if str(r['ts'])<since]:
            del state['cand'][key]
        candidates=list(state['cand'].values())
        done,waits=state['done'],state['waits']
        def fresh_wait(row: Any) -> bool:
            checked=(waits.get(row['id']) or (0,None))[1]
            return checked is not None and float(checked)>cutoff
        rows=[r for r in candidates if r['id'] not in done and not fresh_wait(r)]
        rows.sort(key=lambda r: -int(r['id']))
        rows.sort(key=lambda r: int((waits.get(r['id']) or (0,None))[0] or 0))
        rows=rows[:8]
        count=0
        for row in rows:
            # 07.11.2026: an integration overtaken by a newer accept of the same task can
            # never publish, yet it was re-checked and logged as owner_result_wait every
            # 10 min for 14 days — a lead accepts every few minutes (~730 waits in 2 h).
            if self._superseded(row):
                done.add(row['id'])
                continue
            try:
                published=self._publish(row)
                count+=int(published)
                if not published:
                    with self.store.tx() as tx:
                        tx.record_event(kind='owner_result_wait',project=row['project'],tz=row['tz'],
                                        payload={'integration_id':row['id'],'checked_at':time.time()})
            except Exception as exc:
                with self.store.tx() as tx:
                    tx.record_event(kind='owner_result_wait',project=row['project'],tz=row['tz'],
                                    payload={'integration_id':row['id'],'checked_at':time.time()})
                    old=tx.one("SELECT payload FROM events WHERE kind='owner_result_error' "
                               "AND project=? AND tz=? ORDER BY id DESC LIMIT 1",(row['project'],row['tz']))
                    reason=f'{type(exc).__name__}: {exc}'[:200]
                    if not old or json.loads(old['payload']).get('reason')!=reason:
                        tx.record_event(kind='owner_result_error',project=row['project'],tz=row['tz'],
                                        payload={'integration_id':row['id'],'reason':reason})
        return count


def run_worker(store: Any, stop_event: Any) -> None:
    import logging
    from .goals import project_info
    publisher=ResultPublisher(store,project_info)
    while not stop_event.is_set():
        try:
            publisher.tick()
        except Exception:
            logging.getLogger(__name__).exception('owner result publication deferred')
        stop_event.wait(30)
