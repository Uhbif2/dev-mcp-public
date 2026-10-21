"""Durable intake of NEW published specs, outside the controller's fast loop.

First observation fences off historical files. Subsequent new IDs are registered
with strict isolated preflight, never by a destructive whole-backlog bootstrap.
Conflicts get one repair task. Restart, rename, partial writes and registry races
cannot silently discard pending work or reactivate completed work.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .drafts import _link_free, _safe_draft
from .model import norm_path
from .spec import SpecError, parse_spec, sync_selected

LOG = logging.getLogger(__name__)
_ID = re.compile(r'^(TZ-[A-Za-z0-9]+)(?:-|$)', re.I)


def install_schema(store: Any) -> None:
    with store.read() as tx:
        if tx.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                     "AND name IN ('queue_sources','queue_source_issues')") == 2:
            return
    with store.tx() as tx:
        tx.changes([('CREATE TABLE IF NOT EXISTS queue_sources ('
                  'project TEXT PRIMARY KEY,root_key TEXT NOT NULL,'
                  'inventory TEXT NOT NULL,pending TEXT NOT NULL,checked_at TEXT NOT NULL)', ()),
                  ('CREATE TABLE IF NOT EXISTS queue_source_issues ('
                  'project TEXT NOT NULL,tz TEXT NOT NULL,fingerprint TEXT NOT NULL,'
                  'detail TEXT NOT NULL,state TEXT NOT NULL,updated_at TEXT NOT NULL,'
                  'PRIMARY KEY(project,tz))', ())], event='queue_source_schema')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _files(root: Path) -> dict[str, dict[str, str]]:
    directory = root/'ToDo'/'tz'
    if not _link_free(root, directory) or not directory.is_dir():
        return {}
    result = {}
    for path in sorted(directory.glob('TZ-*.md')):
        match = _ID.match(path.stem)
        if not match:
            continue
        rel = path.relative_to(root).as_posix()
        record = {'tz': match[1].upper(), 'sha': '', 'error': ''}
        if not _safe_draft(root, path) or path.stat().st_size > 1_000_000:
            record['error'] = 'unsafe_or_oversize_spec'
        else:
            record['sha'] = hashlib.sha256(path.read_bytes()).hexdigest()
            try:
                item = parse_spec(str(path))
                if item.tz.casefold() != match[1].casefold():
                    record['error'] = 'filename_header_identity_mismatch'
            except (OSError, SpecError, ValueError) as exc:
                record['error'] = str(exc)[:300]
        result[rel] = record
    return result


def adopt(store: Any, project: str, tzs: list[str]) -> None:
    """Operator-only selection of existing missing specs; never an MCP capability."""
    install_schema(store)
    with store.tx() as tx:
        row = tx.one('SELECT pending FROM queue_sources WHERE project=?', (project,))
        if row is None:
            raise ValueError('observe the source before adopting historical specs')
        pending = set(json.loads(row['pending'])) | {tz.upper() for tz in tzs}
        tx.change('UPDATE queue_sources SET pending=? WHERE project=?',
                  (json.dumps(sorted(pending)), project), event={
                      'kind': 'queue_source_adopted', 'project': project,
                      'payload': {'tzs': sorted(pending), 'producer': 'operator'}})


def _issue(store: Any, project: str, root: Path, tz: str, detail: str,
           paths: list[str]) -> None:
    fingerprint = hashlib.sha256(json.dumps([detail, paths], ensure_ascii=False).encode()).hexdigest()
    fix = 'fix:queue-source:' + hashlib.sha256(tz.encode()).hexdigest()[:16]
    with store.tx() as tx:
        previous = tx.one('SELECT fingerprint,state FROM queue_source_issues WHERE project=? AND tz=?',
                          (project, tz))
        if previous and previous['fingerprint'] == fingerprint and previous['state'] == 'open':
            return
        tx.change('INSERT INTO queue_source_issues VALUES(?,?,?,?,?,?) '
                  'ON CONFLICT(project,tz) DO UPDATE SET fingerprint=excluded.fingerprint,'
                  'detail=excluded.detail,state=excluded.state,updated_at=excluded.updated_at',
                  (project, tz, fingerprint, detail[:2000], 'open', _now()),
                  event={'kind': 'queue_source_conflict', 'project': project, 'tz': tz,
                         'payload': {'fingerprint': fingerprint, 'detail': detail[:2000], 'paths': paths}})
        # Repair the definition, never the product or another task's acceptance.
        if not paths or any(not _link_free(root, root/p) for p in paths):
            return
        body = (f'[fleet-queue-source] target={tz}\n{detail}\n'
                'В опубликованном каталоге есть неоднозначное/неисполняемое ТЗ. '
                'Сверь прямое решение владельца и канон проекта. Сведи определения одного '
                'номера в одно полное ТЗ, сохрани все совместимые требования и проверки. '
                'Оставь у прежнего имени обычную ссылку без YAML-заголовка. Не выбирай по '
                'алфавиту/mtime, не ослабляй приёмку и не выполняй LIVE/OWNER действия. '
                'Если требования противоречат друг другу, опиши конкретную развилку через block. '
                'Контроллер сам повторит строгую регистрацию после исправления.\n'
                'Файлы: ' + ', '.join(paths))
        row = tx.one('SELECT state FROM tasks WHERE project=? AND tz=?', (project, fix))
        if row is None:
            tx.change('INSERT INTO tasks(project,tz,title,state,priority,kind,owns,body,author_worker,'
                      'created_at,updated_at) VALUES(?,?,?,\'ready\',0,\'fix\',?,?,\'queue-intake\',?,?)',
                      (project, fix, 'Согласовать источник ' + tz,
                       json.dumps([norm_path(str(root/p)) for p in paths]), body, _now(), _now()),
                      event={'kind': 'queue_source_repair_opened', 'project': project, 'tz': fix})
        elif row['state'] in ('accepted', 'rejected'):
            tx.change("UPDATE tasks SET state='ready',body=?,owns=?,updated_at=? WHERE project=? AND tz=?",
                      (body, json.dumps([norm_path(str(root/p)) for p in paths]), _now(), project, fix),
                      event={'kind': 'queue_source_repair_reopened', 'project': project, 'tz': fix})


def _resolve(store: Any, project: str, tz: str) -> None:
    with store.tx() as tx:
        if tx.one("SELECT 1 FROM queue_source_issues WHERE project=? AND tz=? AND state!='resolved'", (project, tz)):
            tx.change("UPDATE queue_source_issues SET state='resolved',updated_at=? WHERE project=? AND tz=?",
                      (_now(), project, tz), event={'kind': 'queue_source_resolved', 'project': project, 'tz': tz})


def _retire_obsolete_repair(store: Any, project: str, tz: str, dependencies: list[str]) -> None:
    """A corrected definition leaves only real ownership waits, no zombie repair."""
    fix = 'fix:queue-source:' + hashlib.sha256(tz.encode()).hexdigest()[:16]
    with store.read() as tx:
        row = tx.one('SELECT state,body FROM tasks WHERE project=? AND tz=?', (project, fix))
        claim = tx.one("SELECT 1 FROM claims WHERE project=? AND tz=? AND state IN ('active','preparing')", (project, fix))
    if not row or not row['body'].startswith('[fleet-queue-source] target='+tz+'\n'):
        return
    if claim:
        from .inbox import send
        send(store,to=project+'/'+fix,from_kind='system',from_id='queue-intake',kind='note',
             body=f'Структурные ошибки {tz} больше не обнаружены. Осталось дождаться владельцев файлов: '
                  +', '.join(dependencies[:12])+'. Не меняй приёмку/owns ради снятия ожидания. '
                  'Если иных дефектов определения нет, fleet_block с фактом ожидания; '
                  'сервер закроет лишний ремонт после освобождения claim.',
             dedup_key='queue-source-obsolete:'+project+':'+tz)
        return
    if row['state'] not in ('ready','frozen'):
        return
    with store.tx() as tx:
        last = tx.one("SELECT kind FROM events WHERE project=? AND tz=? AND kind IN ('freeze','block','reactivate') "
                      'ORDER BY id DESC LIMIT 1', (project,fix))
        if row['state']=='frozen' and (not last or last['kind']!='block'):
            return
        tx.change("UPDATE tasks SET state='rejected',updated_at=? WHERE project=? AND tz=? "
                  "AND state IN ('ready','frozen') AND NOT EXISTS (SELECT 1 FROM claims WHERE project=? AND tz=? "
                  "AND state IN ('active','preparing'))", (_now(),project,fix,project,fix),
                  event={'kind':'queue_source_repair_superseded','project':project,'tz':fix,
                         'payload':{'reason':'definition_valid_waiting_for_existing_work','dependencies':dependencies}})


def _demanded(tx: Any, project: str) -> set[str]:
    """TZ ids named in ``needs`` of this project's not-yet-finished tasks."""
    out: set[str] = set()
    for row in tx.query("SELECT needs FROM tasks WHERE project=? AND state IN "
                        "('ready','taken','submitted','frozen')", (project,)):
        try:
            needs = json.loads(row['needs'] or '[]')
        except (TypeError, ValueError):
            continue
        for need in needs if isinstance(needs, list) else ():
            if isinstance(need, str) and '/' not in need:
                match = _ID.match(need.split('#', 1)[0])
                if match:
                    out.add(match[1].upper())
    return out


def tick(store: Any, policy: Any, runner: Any, *, max_registrations: int = 8) -> dict[str, Any]:
    """Bounded imports; slow filesystem/probes never run inside a write lock."""
    result: dict[str, Any] = {'registered': [], 'conflicts': [], 'deferred': [], 'baselined': []}
    if runner is None or policy is None:
        return result
    install_schema(store)
    with store.read() as tx:
        projects = [dict(r) for r in tx.query(
            'SELECT p.name,p.root FROM projects p LEFT JOIN queue_sources s ON s.project=p.name '
            'WHERE p.active=1 ORDER BY COALESCE(s.checked_at,\'\'),p.name')]
    budget = max_registrations
    observed = []
    for project in projects:
        name, root = project['name'], Path(project['root'])
        if not root.is_dir() or root.is_symlink() or root.resolve() != root.absolute():
            continue
        try:
            inventory = _files(root)
        except (OSError, ValueError):
            result['deferred'].append(name + '/source_unreadable')
            continue
        with store.read() as tx:
            source = tx.one('SELECT * FROM queue_sources WHERE project=?', (name,))
            known = {str(r['tz']).upper() for r in tx.query('SELECT tz FROM tasks WHERE project=?', (name,))}
            demanded = _demanded(tx, name)
        if source is None:
            with store.tx() as tx:
                tx.change('INSERT OR IGNORE INTO queue_sources VALUES(?,?,?,?,?)',
                          (name, norm_path(str(root)), json.dumps(inventory), '[]', _now()),
                          event={'kind': 'queue_source_baseline', 'project': name,
                                 'payload': {'files': len(inventory), 'historical_files_not_replayed': True}})
            result['baselined'].append(name)
            continue
        if source['root_key'] != norm_path(str(root)):
            result['conflicts'].append(name + '/root_identity_changed')
            continue
        previous = json.loads(source['inventory'])
        attempted = False
        old_ids = {item['tz'] for item in previous.values()}
        pending = set(json.loads(source['pending']))
        pending |= {item['tz'] for item in inventory.values()} - old_ids - known
        # 17.10: a live task needing a fenced-off historical spec waited forever
        # (Beta App TZ-526 → TZ-507). Demand by a live task makes it new work.
        pending |= ({item['tz'] for item in inventory.values()} & demanded) - known
        # Registered IDs can also acquire a conflicting second definition.
        duplicates = {}
        for path, entry in inventory.items():
            if not entry['error']:
                duplicates.setdefault(entry['tz'], []).append(path)
        for tz, paths in duplicates.items():
            if len(paths) > 1 and tz in known:
                _issue(store, name, root, tz, 'duplicate_published_specs', paths)
                result['conflicts'].append(name + '/' + tz)
        # A rename of a historical or completed spec is never new work.
        for tz in sorted(pending - known):
            entries = {p: r for p, r in inventory.items() if r['tz'] == tz}
            # A complete second observation is required before consuming a file.
            if not entries or any(previous.get(p) != r for p, r in entries.items()):
                result['deferred'].append(name + '/' + tz)
                continue
            valid = []
            for path, record in entries.items():
                if not record['error']:
                    valid.append(path)
            if len(valid) != 1:
                detail = 'duplicate_published_specs' if len(valid) > 1 else 'invalid_published_spec'
                _issue(store, name, root, tz, detail, sorted(entries))
                result['conflicts'].append(name + '/' + tz)
                continue
            # Tombstones have no YAML and may accompany the one real spec.
            if budget <= 0:
                result['deferred'].append(name + '/' + tz)
                continue
            budget -= 1
            attempted = True
            try:
                registration = sync_selected(store, name, str(root), [tz], policy=policy,
                                             strict_preflight=True, dry_runner=runner, new_only=True)
            except Exception as exc:
                LOG.warning('queue intake %s/%s deferred: %s', name, tz, type(exc).__name__)
                result['deferred'].append(name + '/' + tz)
                continue
            if registration['inserted'] == 1:
                known.add(tz)
                _resolve(store, name, tz)
                _retire_obsolete_repair(store, name, tz, [])
                result['registered'].append(name + '/' + tz)
            elif registration['errors'] == {'preflight': 'registry_changed_during_preflight'}:
                result['deferred'].append(name + '/' + tz)
            elif any(r['action'] == 'register_with_dependencies' for r in registration['preflight'].values()):
                result['deferred'].append(name + '/' + tz)
                dependencies = sorted({dep for report in registration['preflight'].values()
                                       for dep in report['dependencies']})
                _retire_obsolete_repair(store, name, tz, dependencies)
                with store.tx() as tx:
                    tx.change("UPDATE queue_source_issues SET state='waiting_dependencies',updated_at=? "
                              "WHERE project=? AND tz=? AND state='open'",
                              (_now(), name, tz), event={'kind':'queue_source_waiting','project':name,'tz':tz})
            else:
                details = {'errors': registration['errors'], 'obstacles': [
                    obstacle for report in registration['preflight'].values() for obstacle in report['obstacles']]}
                _issue(store, name, root, tz, json.dumps(details, ensure_ascii=False), valid)
                result['conflicts'].append(name + '/' + tz)
        pending -= known
        checked_at = (source['checked_at'] if pending and not attempted and budget <= 0 else _now())
        retained_inventory = {**previous, **inventory}
        observed.append(('UPDATE queue_sources SET inventory=?,pending=?,checked_at=? WHERE project=? '
                         'AND EXISTS (SELECT 1 FROM projects WHERE name=? AND active=1) '
                         'AND inventory=? AND pending=? AND root_key=?',
                         (json.dumps(retained_inventory), json.dumps(sorted(pending)), checked_at, name, name,
                          source['inventory'], source['pending'], source['root_key'])))
    if observed:
        with store.tx() as tx:
            tx.changes(observed, event={'kind': 'queue_sources_observed',
                                       'payload': {'projects': len(observed)}})
    return result


def submission_refusal(store: Any, worker_id: str) -> str:
    """A source repair cannot be submitted while its source is still ambiguous."""
    with store.read() as tx:
        row = tx.one('SELECT t.body,p.root FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz '
                     'JOIN projects p ON p.name=c.project WHERE c.worker_id=? AND c.state=\'active\'', (worker_id,))
    if row is None or not row['body'].startswith('[fleet-queue-source] target='):
        return ''
    tz = row['body'].splitlines()[0].split('target=', 1)[1]
    root = Path(row['root'])
    files = _files(root)
    valid = [path for path, entry in files.items() if entry['tz'] == tz and not entry['error']]
    if len(valid) != 1:
        return f'Источник {tz} ещё неоднозначен: действующих определений {len(valid)}; исправь до сдачи.'
    from .spec_preflight import _command
    task = parse_spec(str(root/valid[0]))
    if not task.acceptance or any(_command(command, root)[1] for command in task.acceptance):
        return f'Источник {tz}: команда приёмки ещё не проходит структурную проверку.'
    return ''


def run_worker(store: Any, policy: Any, runner: Any, stop_event: Any) -> None:
    while not stop_event.is_set():
        started = time.perf_counter()
        try:
            result = tick(store, policy, runner)
            payload = {'status': 'completed', **result}
        except Exception as exc:
            LOG.exception('queue source intake deferred')
            payload = {'status': 'failed', 'error': type(exc).__name__}
        try:
            with store.tx() as tx:
                tx.record_event(kind='queue_intake_tick', payload={**payload,
                    'duration_ms': round((time.perf_counter()-started)*1000, 1)})
        except Exception:
            LOG.exception('queue source heartbeat deferred')
        stop_event.wait(30)


def health(store: Any) -> str:
    """A pure query: a current heartbeat with errors is not a healthy intake."""
    with store.read() as tx:
        latest = tx.one("SELECT ts,payload FROM events WHERE kind='queue_intake_tick' ORDER BY id DESC LIMIT 1")
        if latest is None:
            return 'приём ТЗ: нет тиков'
        data = json.loads(latest['payload'])
        age = max(0, int((datetime.now(timezone.utc)-datetime.fromisoformat(latest['ts'])).total_seconds()))
        issues = int(tx.scalar("SELECT COUNT(*) FROM queue_source_issues i JOIN projects p ON p.name=i.project "
                               "WHERE i.state='open' AND p.active=1") or 0)
        status = 'пропуск >5 мин' if age > 300 else 'ошибка' if data.get('status') != 'completed' else 'жив'
    return f'приём ТЗ: {status}, {age} с; конфликтов {issues}'
