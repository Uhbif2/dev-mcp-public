"""Real claims before browser creation; failed starts release their exact claim."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Callable
from . import queue

LEASE_S = 600


def reserve(store: Any, register: Callable[[], str], label: str, project: str) -> dict:
    with store.read() as tx:
        held = tx.one("SELECT c.id,c.worker_id,c.project,c.tz FROM claims c JOIN workers w ON w.id=c.worker_id "
                      "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                      "JOIN projects p ON p.name=c.project "
                      "WHERE w.label=? AND w.kind='chatgpt' AND w.revoked_at IS NULL "
                      "AND c.state='active' AND t.state='taken' AND p.active=1 "
                      "ORDER BY c.id DESC LIMIT 1", (label,))
    if held is None:
        task, _ = queue.peek_next(store, project=project)
        if task is None:
            return {'status': 'empty'}
        worker = register()
    else:
        worker = held['worker_id']
    if held is None:
        task = queue.take_next(store, worker, project=project, launch_label=label)
        if task is None:
            with store.tx() as tx:
                tx.change("UPDATE workers SET revoked_at=?,revoke_reason='launch lost admission race' "
                          "WHERE id=? AND NOT EXISTS (SELECT 1 FROM claims WHERE worker_id=?)",
                          (datetime.now(timezone.utc).isoformat(), worker, worker), event='launch_empty')
            return {'status': 'empty'}
        with store.read() as tx:
            held = tx.one("SELECT id,project,tz FROM claims WHERE worker_id=? AND state='active' "
                          "ORDER BY id DESC LIMIT 1", (worker,))
    if held is None or held['project'] != project:
        raise ValueError('launch label already bound to another project')
    result = {'status': 'reserved', 'worker_id': worker, 'label': label,
              'project': held['project'], 'tz': held['tz'], 'claim_id': int(held['id'])}
    with store.tx() as tx:
        if tx.one("SELECT 1 FROM events WHERE kind='launch_reserved' AND worker_id=? "
                  "AND json_extract(payload,'$.claim_id')=? LIMIT 1", (worker, held['id'])) is None:
            tx.record_event(kind='launch_reserved', worker_id=worker, project=held['project'], tz=held['tz'],
                            payload={**result, 'lease_s': LEASE_S})
    return result


def confirm(store: Any, worker: str) -> None:
    with store.read() as tx:
        held = tx.one("SELECT id FROM claims WHERE worker_id=? AND state='active' ORDER BY id DESC LIMIT 1",
                      (worker,))
        pending = held and tx.one("SELECT 1 FROM events WHERE kind='launch_reserved' AND worker_id=? "
                                  "AND json_extract(payload,'$.claim_id')=? LIMIT 1", (worker, held['id']))
        confirmed = held and tx.one("SELECT 1 FROM events WHERE kind='launch_confirmed' AND worker_id=? "
                                    "AND json_extract(payload,'$.claim_id')=? LIMIT 1", (worker, held['id']))
    if pending and not confirmed:
        with store.tx() as tx:
            if not tx.one("SELECT 1 FROM events WHERE kind='launch_confirmed' AND worker_id=? "
                          "AND json_extract(payload,'$.claim_id')=? LIMIT 1", (worker, held['id'])):
                tx.record_event(kind='launch_confirmed', worker_id=worker, payload={'claim_id': int(held['id'])})


def release(store: Any, label: str, claim_id: int) -> dict:
    with store.tx() as tx:
        claim = tx.one("SELECT c.*,w.label FROM claims c JOIN workers w ON w.id=c.worker_id WHERE c.id=?",
                       (claim_id,))
        if not claim or claim['label'] != label:
            raise ValueError('launch claim does not belong to label')
        if claim['state'] != 'active':
            return {'status':'released'}
        if tx.one("SELECT 1 FROM events WHERE kind='launch_confirmed' AND worker_id=? "
                  "AND json_extract(payload,'$.claim_id')=? LIMIT 1", (claim['worker_id'],claim_id)):
            return {'status':'confirmed'}
        if not tx.one("SELECT 1 FROM events WHERE kind='launch_reserved' AND worker_id=? "
                      "AND json_extract(payload,'$.claim_id')=?", (claim['worker_id'],claim_id)):
            raise ValueError('claim was not reserved by launcher')
        now = datetime.now(timezone.utc).isoformat()
        tx.change("UPDATE claims SET state='released',released_at=? WHERE id=?", (now,claim_id),
                  event={'kind':'launch_released','worker_id':claim['worker_id'],
                         'project':claim['project'],'tz':claim['tz'],'payload':{'claim_id':claim_id}})
        tx.change("UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=? AND state='taken'",
                  (now,claim['project'],claim['tz']),event='launch_task_restored')
        tx.change("UPDATE workers SET revoked_at=?,revoke_reason='launcher did not start chat' WHERE id=?",
                  (now,claim['worker_id']),event='launch_worker_retired')
    return {'status':'released'}


def release_expired(store: Any) -> None:
    before = (datetime.now(timezone.utc)-timedelta(seconds=LEASE_S)).isoformat()
    with store.read() as tx:
        rows = tx.query("SELECT c.id,w.label FROM claims c JOIN workers w ON w.id=c.worker_id "
                        "WHERE c.state='active' AND c.started_at<? AND EXISTS (SELECT 1 FROM events e "
                        "WHERE e.kind='launch_reserved' AND e.worker_id=c.worker_id "
                        "AND json_extract(e.payload,'$.claim_id')=c.id) AND NOT EXISTS (SELECT 1 FROM events e "
                        "WHERE e.kind='launch_confirmed' AND e.worker_id=c.worker_id "
                        "AND json_extract(e.payload,'$.claim_id')=c.id)", (before,))
    for row in rows:
        release(store,row['label'],int(row['id']))
