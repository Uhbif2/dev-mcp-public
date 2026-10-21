"""Цели проектов флота из fleet-projects.toml — компас в памятке к каждой задаче.

Файл перечитывается по mtime; нет файла или он битый — памятка просто без цели.
"""
from __future__ import annotations

from dev_mcp import machine

import os
import hashlib
import threading
import tomllib
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[3] / 'fleet-projects.toml'

_lock = threading.Lock()
_cache: dict[str, object] = {'path': None, 'mtime': None, 'data': {},
                             'rejected_mtime': None}


def _verify_gate_change(project: str, command: str):
    """Trial-run a changed project gate against its registered source tree.

    The live registry is only read here.  A temp/test registry must never
    accidentally authorize running programs in another project.
    """
    from .exec_preflight import PreflightResult, preflight_gate
    from .gate_snapshot import SnapshotStore
    from .store import Store

    db_path = DEFAULT_PATH.parent / 'data' / 'fleet' / 'fleet.sqlite'
    if not db_path.is_file():
        return PreflightResult(False, 'fleet registry unavailable')
    store = Store.open(str(db_path))
    try:
        with store.read() as tx:
            project_row = tx.one('SELECT root FROM projects WHERE name=? AND active=1',
                                 (project,))
        if project_row is None:
            return PreflightResult(False, 'registered project root unavailable')
        root = Path(str(project_row['root']))
        if not root.is_dir():
            return PreflightResult(False, 'registered project tree unavailable')
        snapshots = SnapshotStore(DEFAULT_PATH.parent / 'data' / 'fleet' / 'preflight-objects')
        from .exec_preflight import project_deps
        deps = project_deps(root, tuple(project_info(project).get('deps') or ()))
        return preflight_gate(command, source_root=root, snapshots=snapshots, deps=deps)
    finally:
        store.close()


def _verify_resource_change(resource: str, key: str, command: str, cwd: str):
    """Prove a newly executable resource command in an isolated snapshot.

    Missing working directories are rejected, never inherited from dev-mcp.
    The production execution contract is exec_preflight.run_command.
    """
    from .exec_preflight import PreflightResult, preflight_gate
    from .gate_snapshot import SnapshotStore
    if key == 'install_command':
        # A command that installs persistent OS state cannot be "test run"
        # without its own pulse check and rollback. No generic subprocess
        # probe is an authorization to register Scheduler tasks or services.
        return PreflightResult(False, 'installer requires verified live pulse and rollback adapter')
    root = Path(cwd)
    if not cwd or not root.is_dir():
        return PreflightResult(False, 'resource command missing valid cwd')
    snapshots = SnapshotStore(DEFAULT_PATH.parent / 'data' / 'fleet' / 'preflight-objects')
    return preflight_gate(command, source_root=root, snapshots=snapshots)


def _resource_commands(data: dict) -> dict[tuple[str, str], tuple[str, str]]:
    """Executable resource configuration, including malformed values for rejection."""
    resources = data.get('resources', {})
    if not isinstance(resources, dict):
        raise ValueError('resources must be a table')
    commands = {}
    for resource, details in resources.items():
        if not isinstance(details, dict):
            raise ValueError(f'resource must be a table: {resource}')
        for key in ('command', 'run', 'install_command'):
            if key in details:
                command = details[key]
                if not isinstance(command, str) or not command.strip():
                    raise ValueError(f'invalid resource executable: {resource}.{key}')
                cwd = details.get('cwd', '')
                commands[(str(resource), key)] = (command, str(cwd) if isinstance(cwd, str) else '')
    return commands


def _report_gate_rejection(project: str, command: str, reason: str, *,
                           source: str = 'fleet-projects.toml') -> None:
    """Record a rejected reload and create one self-service repair request.

    Never report failures from test configs to the production registry.
    """
    if _path().resolve() != DEFAULT_PATH.resolve():
        return
    from .store import Store
    from . import review

    db = DEFAULT_PATH.parent / 'data' / 'fleet' / 'fleet.sqlite'
    if not db.is_file():
        return
    store = Store.open(str(db))
    try:
        with store.tx() as tx:
            tx.record_event(kind='exec_preflight_reject', project=project, payload={
                'source': source, 'reason': reason[:500],
                'candidate_sha256': hashlib.sha256(command.encode('utf-8')).hexdigest(),
            })
            from . import platform_manual
            if platform_manual.enabled():
                platform_manual.enqueue(tx,hashlib.sha256(f'{project}:{command}'.encode()).hexdigest(),
                    title=f'Исправить отклонённую команду {source} ({project})',detail=reason,
                    source_project=project,owns=['fleet-projects.toml'],
                    acceptance=['py -3 -m pytest -q tests/fleet/test_goals.py'])
                return
            platform = tx.one("SELECT root FROM projects WHERE name='Fleet Platform'")
        if platform is not None:
            work_config = Path(str(platform['root'])) / 'fleet-projects.toml'
            request_id = hashlib.sha256(f'{project}:{command}'.encode()).hexdigest()[:8]
            review.open_request(
                store, request_id, kind='other',
                summary=f'Исправить отклонённую команду {source} ({project})',
                detail=f'Exec preflight отверг изменение {source}: {reason[:350]}; прежнее сохранено.',
                project='Fleet Platform', path=str(work_config), to='queue',
            )
    except (OSError, ValueError):
        # Rejected config remains rejected even if diagnostics cannot be written.
        pass
    finally:
        store.close()


def _path() -> Path:
    raw = os.environ.get('DEV_MCP_FLEET_PROJECTS', '').strip()
    return Path(raw) if raw else DEFAULT_PATH


def _load() -> dict:
    return _load_doc().get('projects', {})


def _load_doc() -> dict:
    """Весь fleet-projects.toml: [projects.*] — цели, [resources.*] — ресурсы мира (world.py)."""
    path = _path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _cache['path'] == path and _cache['mtime'] == mtime:
            return _cache['data']  # type: ignore[return-value]
        if _cache['path'] == path and _cache.get('rejected_mtime') == mtime:
            return _cache['data']  # type: ignore[return-value]
        try:
            data = tomllib.loads(path.read_text(encoding='utf-8'))
        except (OSError, tomllib.TOMLDecodeError):
            # An invalid update is never allowed to remove the live settings.
            if _cache['path'] == path and _cache['mtime'] is not None:
                _cache['rejected_mtime'] = mtime
                return _cache['data']  # type: ignore[return-value]
            data = {}
        if _cache['path'] == path and _cache['mtime'] is not None:
            old = _cache['data'] if isinstance(_cache['data'], dict) else {}
            old_projects = old.get('projects', {})
            new_projects = data.get('projects', {})
            if not isinstance(old_projects, dict) or not isinstance(new_projects, dict):
                _cache['rejected_mtime'] = mtime
                return old
            for name, details in new_projects.items():
                if not isinstance(details, dict):
                    continue
                proposed_gate = str(details.get('gate') or '').strip()
                previous_details = old_projects.get(name, {})
                previous_gate = (str(previous_details.get('gate') or '').strip()
                                 if isinstance(previous_details, dict) else '')
                if proposed_gate == previous_gate:
                    continue
                if not proposed_gate:
                    reason = 'removing a gate requires an explicit checked change'
                    _cache['rejected_mtime'] = mtime
                    _report_gate_rejection(str(name), proposed_gate, reason)
                    return old
                try:
                    verdict = _verify_gate_change(str(name), proposed_gate)
                    ok, reason = verdict.ok, verdict.reason
                except Exception as exc:
                    ok, reason = False, f'{type(exc).__name__}: {exc}'
                if not ok:
                    _cache['rejected_mtime'] = mtime
                    _report_gate_rejection(str(name), proposed_gate, reason)
                    return old
            # Disallow removing an active gate by dropping the whole project.
            if any(isinstance(info, dict) and info.get('gate') and
                   name not in new_projects for name, info in old_projects.items()):
                _cache['rejected_mtime'] = mtime
                return old
            # World/resource commands share the same last-known-good activation
            # boundary as gates. A changed cwd also changes execution semantics.
            try:
                previous = _resource_commands(old)
                proposed = _resource_commands(data)
                for identity in previous:
                    if identity not in proposed:
                        raise ValueError(f'removing executable resource {identity} requires verification')
                for (resource, key), (command, cwd) in proposed.items():
                    if previous.get((resource, key)) == (command, cwd):
                        continue
                    result = _verify_resource_change(resource, key, command, cwd)
                    if not result.ok:
                        raise ValueError(f'{resource}.{key}: {result.reason}')
            except Exception as exc:
                _cache['rejected_mtime'] = mtime
                _report_gate_rejection('Fleet Platform', repr(data.get('resources')),
                                       str(exc), source='fleet-projects.toml:resources')
                return old
        _cache.update(path=path, mtime=mtime, data=data, rejected_mtime=None)
        return data


def resources_info() -> dict:
    data = _load_doc().get('resources', {})
    return data if isinstance(data, dict) else {}


def project_info(project: str) -> dict:
    info = _load().get(project)
    if isinstance(info, dict):
        return info
    return _intent_project_defaults(project)


def _intent_project_defaults(project: str) -> dict:
    """Проект интенции (сервер завёл его сам, intents/spawn.py) — без строки в
    fleet-projects.toml: принятая работа коммитится в его собственный git (13.10:
    у local-library принятый код не попадал ни в какую историю)."""
    import json
    registered = _registered_intent_root(project)
    if registered is not None and (registered / '.git').is_dir():
        return {'commit': True}
    for parent in machine.WORKSPACES:
        manifest = parent / project / "component.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("kind") == "intent-outcome-tree" \
                and (parent / project / ".git").is_dir():
            return {"commit": True}
    return {}


def _registered_intent_root(project: str) -> Path | None:
    """Retargeted names need the registry root, with the same trusted origin."""
    import json
    import sqlite3
    db = DEFAULT_PATH.parent / 'data/fleet/fleet.sqlite'
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=1) as conn:
            rows = conn.execute(
                "SELECT p.root,o.spec,o.authority FROM projects p JOIN outcomes o ON o.project=p.name "
                "WHERE p.name=? AND p.active=1 AND o.parent_id IS NULL AND o.state IN ('open','done')",
                (project,)).fetchall()
        for raw_root, raw_spec, raw_authority in rows:
            root = Path(raw_root)
            spec, authority = json.loads(raw_spec), json.loads(raw_authority)
            if root.is_symlink() or not root.is_dir() or root.resolve() != root.absolute():
                continue
            if not any(root != Path(p).resolve() and root.is_relative_to(Path(p).resolve())
                       for p in authority.get('project_roots', ())):
                continue
            manifest = root / 'component.json'
            if manifest.is_symlink():
                continue
            data = json.loads(manifest.read_text(encoding='utf-8'))
            if (data.get('kind') == 'intent-outcome-tree' and data.get('origin') == {
                    'source_id': spec.get('source_id'), 'external_id': spec.get('external_id')}):
                return root
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None
    return None


def goal_block(project: str) -> str:
    info = project_info(project)
    goal = ' '.join(str(info.get('goal', '')).split())
    if not goal:
        return ''
    lines = [f'Цель проекта {project}: {goal}']
    sources = [str(s) for s in info.get('sources', []) if str(s).strip()]
    if sources:
        lines.append('Источники цели: ' + ', '.join(sources))
    lines.append('Задача, уводящая от этой цели, — повод для fleet_block/escalate, а не для молчаливого исполнения.')
    return '\n'.join(lines) + '\n\n'
