"""Read-only end-to-end yield and root dependencies from the existing registry."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any


def metrics(conn: Any, since: str) -> dict:
    accepts={int(r[0]) for r in conn.execute("SELECT id FROM events WHERE kind='accept' AND ts>=?",(since,))}
    integrated=set()
    for (raw,) in conn.execute("SELECT payload FROM events WHERE kind='integrate' AND ts>=?",(since,)):
        data=json.loads(raw)
        if data.get('commit') and data.get('accept_id') in accepts:
            integrated.add(data['accept_id'])
    available=set()
    for (raw,) in conn.execute("SELECT payload FROM events WHERE kind='owner_result_available' AND ts>=?",(since,)):
        data=json.loads(raw)
        if data.get('accept_id') in accepts:
            from .results import _digest,MAX_BYTES
            path=Path(data.get('artifact',''))
            try:
                if (not path.is_symlink() and path.stat().st_size<=MAX_BYTES and
                        _digest(path)==data.get('sha256')):
                    available.add(data['accept_id'])
            except OSError:
                pass
    tasks={}
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns={r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
    task_query=('SELECT t.project,t.tz,t.state,t.needs FROM tasks t '
                'JOIN projects p ON p.name=t.project WHERE p.active=1'
                if 'projects' in tables else 'SELECT project,tz,state,needs FROM tasks')
    rows=(conn.execute(task_query) if {'project','tz','state','needs'}<=columns else [])
    for p,tz,state,raw in rows:
        tasks[(p,tz)]=(state,json.loads(raw or '[]'))
    consumers=defaultdict(set)
    def roots(key,path):
        if key in path:
            return {(min(path[path.index(key):]),'cycle')}
        if key not in tasks:
            return {(key,'missing')}
        state,needs=tasks[key]
        if state=='accepted':
            return set()
        if len(path)>32:
            return {(key,'depth_limit')}
        refs=[]
        for need in needs:
            if not isinstance(need,str): continue
            value=need.split('#',1)[0]
            p,sep,tz=value.partition('/')
            refs.append((p,tz) if sep else (key[0],value))
        unmet=[ref for ref in refs if ref not in tasks or tasks[ref][0]!='accepted']
        if not unmet:
            return {(key,state)}
        found=set()
        for ref in unmet:
            found.update(roots(ref,(*path,key)))
        return found
    for key,(state,needs) in tasks.items():
        if state not in {'ready','frozen'} or not needs: continue
        for root,reason in roots(key,()):
            if root!=key or reason=='cycle':
                consumers[(root,reason)].add(key)
    blockers=[{'project':key[0],'tz':key[1],'state':reason,'consumers':len(children),
               'ready':sum(tasks[child][0]=='ready' for child in children)}
              for (key,reason),children in consumers.items()]
    blockers.sort(key=lambda r:(-r['ready'],-r['consumers'],r['project'],r['tz']))
    return {'accepted':len(accepts),'integrated':len(integrated),
            'artifact_available':len(available),'awaiting_integration':len(accepts-integrated),
            'awaiting_artifact':len(integrated-available),'root_blockers':blockers[:8]}
