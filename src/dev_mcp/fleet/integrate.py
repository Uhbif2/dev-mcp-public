"""Интеграция после приёмки: коммит путей задачи + гейт всего проекта.

Зачем (26.09.2026). Эксперимент 21–26.09 показал: принятые по отдельности ТЗ не складываются в
рабочий проект. Приёмка каждого ТЗ зелёная, а сводная проверка Beta App красная; принятое не
закоммичено — ни откатить, ни атрибутировать. Ревьюер проверяет задачу, стык не проверяет никто.

Что делает (без агента, в фоне dev-mcp):
1. Новое событие `accept` → коммит в git проекта ровно тех путей, что задача владела (`owns`) и
   реально писала (события `write` её цепочки). Событие `integrate` {commit, paths}. Только если
   проект разрешил (`commit = true` в fleet-projects.toml): у проектов свои правила о коммитах.
2. После пачки коммитов проекта — один прогон его гейта (`gate` в fleet-projects.toml).
   Событие `gate` {rc, dur_s, summary, log_tail, after}. Гейт медленный (минута и больше),
   поэтому пачка: пять приёмок за время прогона дают один следующий прогон, а не пять.
3. Красный гейт → одна задача «Стык» в очередь флота (не больше одной открытой на проект):
   зона — файлы упавших тестов и записанное пачкой (не корень проекта), приёмка — сам гейт.
   Суждение делает обычный агент, а не код. Зелёный гейт закрывает стык (queue.close_resolved_styks).

Курсор — id последнего обработанного accept в `integrate-state.json` рядом с базой. Первый
запуск ставит курсор на текущий максимум: историю задним числом не коммитим.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dev_mcp.fleet import goals
from dev_mcp.fleet.model import norm_path, task_root
from dev_mcp.fleet.store import Store

INTERVAL_S = 20.0
GATE_TIMEOUT_S = float(os.environ.get('DEV_MCP_FLEET_GATE_TIMEOUT_S', 1800))
GIT_TIMEOUT_S = 60.0
MAX_COMMIT_PATHS = 400  # зона шире — это не задача, а «весь проект»; такое не коммитим молча
LOG_TAIL_CHARS = 3000
TICK_BATCH = 500  # урок 42: тик ограничен, курсор двигается после каждой приёмки
PENDING_BATCH = 10  # отложенные перепроверяются дорого (проверка кандидата) — малой порцией
PENDING_WITH_FRESH = 2
PENDING_HORIZON_DAYS = 14  # отложенное старше — не повторяется (20.10: окно 3000 событий забывало за часы)
PENDING_SUPERSEDE_DAYS = 3  # отложено дольше и файлы переписаны позже принятыми — закрыть как заменённое
PENDING_RECHECK_S = 600.0  # страховка пропущенного события: отложенное перепроверяется не чаще раза в 10 мин без нового факта
MOOT_RECHECK_S = 300.0  # закрытие интеграционных заявок, чья приёмка уже интегрирована
STYK_TITLE ='Стык: гейт проекта красный после приёмок'
_GATE_PYTHONPATH_MAX_FILES = 12000
_GATE_PYTHONPATH_MAX_BYTES = 64 * 1024 * 1024
_GATE_PYTHONPATH_SCAN_S = 3.0
VALIDATION_TIMEOUT_S = 300.0  # candidate only; production HEAD gate keeps GATE_TIMEOUT_S

Runner = Callable[..., subprocess.CompletedProcess]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# Стадии, чья приёмка верна только в момент сдачи («сценарии красные до кода»), и их
# вариант для перепроверки уже принятого (intents/stage_check.py). 18.10: revalidate стадии
# executable_goal после implemented замерзал навсегда — сценарии закономерно зелёные.
_HISTORICAL_STAGE_CHECKS = {'executable_goal': 'executable_goal_published'}


def revalidation_acceptance(raw: str) -> str:
    try:
        commands = json.loads(raw or '[]')
    except ValueError:
        return raw
    if not isinstance(commands, list):
        return raw
    out = []
    for command in commands:
        parts = str(command).split()
        if (len(parts) >= 2 and parts[-2].replace('\\', '/').endswith('.intent/check.py')
                and parts[-1] in _HISTORICAL_STAGE_CHECKS):
            command = ' '.join(parts[:-1] + [_HISTORICAL_STAGE_CHECKS[parts[-1]]])
        out.append(command)
    return json.dumps(out, ensure_ascii=False)


def _base_tz(tz: str) -> str:
    """review:fix:TZ-7:2 → TZ-7. Цепочка задачи — сама задача и её fix-раунды."""
    return task_root(tz)


def _rel_pathspec(path_norm: str, root: str) -> str | None:
    """Нормализованный путь флота (нижний регистр, /) → pathspec git относительно корня."""
    rootn = root.replace('\\', '/').rstrip('/').casefold() + '/'
    p = path_norm.replace('\\', '/')
    if not p.casefold().startswith(rootn):
        return None
    rel = p[len(rootn):]
    if not rel or rel.startswith('.git/') or rel == '.git':
        return None
    # icase: флот хранит пути в нижнем регистре, а файлы на диске — в своём.
    return f':(icase,glob){rel}' if any(ch in rel for ch in '*?[') else f':(icase){rel}'


def _styk_review_gate(command: str, root: str) -> tuple[str, str]:
    """Make the HEAD gate runnable by a reviewer, only with proven equivalence.

    The integrator can launch an absolute script, while dev-mcp.run rejects
    absolute argv paths. Do not rewrite arbitrary gates: the *known* HEAD-gate
    script uses cwd for its project and always tests a detached HEAD worktree.
    It is safe to use the project-local copy only when the sibling live and
    project-local scripts are both ordinary files with identical bytes.
    Otherwise retain the original gate and leave remediation to the queue.
    """
    try:
        argv = shlex.split(command)
        project = Path(root).absolute()
        if (len(argv) != 3 or argv[:2] != ['py', '-3']
                or not project.name.endswith('-work')):
            return command, ''
        script = Path(argv[2])
        relative = Path('tools') / 'gate_head.py'
        local = project / relative
        live = project.with_name(project.name[:-5]) / relative
        if not script.is_absolute() or script != live:
            return command, ''
        # Reject junctions, symlinks and missing/oversized files: lexical and
        # resolved identities must agree, and bytes must be the same now.
        if (script.resolve(strict=True) != script.absolute()
                or local.resolve(strict=True) != local.absolute()
                or not script.is_file() or not local.is_file()
                or script.stat().st_size > 128 * 1024
                or local.stat().st_size != script.stat().st_size):
            return command, ''
        original = script.read_bytes()
        if original != local.read_bytes():
            return command, ''
        return f'py -3 {relative.as_posix()}', hashlib.sha256(original).hexdigest()
    except (OSError, ValueError):
        return command, ''


@dataclass
class Batch:
    project: str
    root: str
    accepts: list[dict[str, Any]] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    pathspecs: set[str] = field(default_factory=set)
    owns: set[str] = field(default_factory=set)
    wrote: set[str] = field(default_factory=set)
    legacy_accepts: bool = False


@dataclass
class TrainEntry:
    """One independently sealed, currently committable shared acceptance."""
    accept: dict[str, Any]
    paths: list[str]
    fingerprints: dict[str, str | None]
    acceptance: list[str]


def _link_dir(source: Path, link: Path) -> Path:
    """Directory junction on Windows (no admin rights needed), symlink elsewhere."""
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(source), str(link))
    else:
        os.symlink(source, link, target_is_directory=True)
    return link


def _unlink_dir(link: Path) -> None:
    """Remove only the link itself, never what it points to."""
    try:
        if link.is_symlink() or getattr(link, 'is_junction', lambda: False)():
            os.rmdir(link) if os.name == 'nt' else link.unlink()
    except OSError:
        pass  # temp cleanup below; rmtree does not follow junctions (py3.8+)


class Integrator:
    def __init__(
        self,
        store: Store,
        state_path: Path,
        *,
        runner: Runner = subprocess.run,
        project_info: Callable[[str], dict] = goals.project_info,
        async_gates: bool = False,
    ) -> None:
        self.store = store
        # 17.10.2026: tick ждал самый медленный гейт (Beta App check_all 13–87 мин) — всё это
        # время не коммитилась ни одна приёмка ни одного проекта. В фоне гейт проекта идёт своим
        # потоком; пока он идёт, этому проекту не коммитим (приёмки ждут с отметкой gate_wait),
        # остальные проекты интегрируются сразу.
        self.async_gates = async_gates
        self._gate_threads: dict[str, threading.Thread] = {}
        self.state_path = state_path
        self.runner = runner
        self.project_info = project_info
        # Повтор отложенных приёмок — только по новому факту (план консорциума 11.10, п.1):
        # 1252 попытки интеграции на 88 приёмок, каждая тянула полный гейт.
        self._pending_sig: tuple | None = None
        self._pending_at = 0.0
        # Последнее решение интеграции на приёмку; читается приращением (_refresh_latest).
        self._latest: dict[tuple[str, int], tuple[int, str, dict]] = {}
        self._project_touch: dict[str, int] = {}
        self._scan_id = 0

    # ---- состояние курсора ----
    def _cursor(self) -> int | None:
        try:
            return int(json.loads(self.state_path.read_text(encoding='utf-8'))['last_accept_id'])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_cursor(self, value: int) -> None:
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'last_accept_id': value, 'at': _now()}), encoding='utf-8')
        os.replace(tmp, self.state_path)

    # ---- один проход ----
    def tick(self) -> list[dict[str, Any]]:
        """Обработать новые accept. Возвращает записанные события (для тестов и журнала)."""
        with self.store.read() as tx:
            top = int(tx.scalar("SELECT COALESCE(MAX(id),0) FROM events WHERE kind='accept'") or 0)
            cursor = self._cursor()
            if cursor is None:
                self._save_cursor(top)
                return []
            rows = tx.query(
                "SELECT id,project,tz,worker_id FROM events WHERE kind='accept' AND id>? "
                "ORDER BY id LIMIT ?",
                (cursor, TICK_BATCH),
            )
            roots = {r['name']: str(r['root']) for r in tx.query('SELECT name,root FROM projects WHERE active=1')}
        busy = self._busy_gates()
        # A deferred acceptance must not be forgotten when the cursor advances.
        # Retry after a supplier's later acceptance (and also after a transient failure).
        # Урок 46: отложенные — минуты каждая; при свежих приёмках не задерживать их больше чем на 2.
        pending = [r for r in self._pending_accepts() if str(r['project'] or '') not in busy
                   ][:PENDING_BATCH if not rows else PENDING_WITH_FRESH]
        if not rows and not pending:
            return []
        if not rows and pending:
            sig = self._pending_signature(pending, roots)
            if sig == self._pending_sig and time.monotonic() - self._pending_at < PENDING_RECHECK_S:
                return []
            self._pending_sig, self._pending_at = sig, time.monotonic()
        batches: dict[str, Batch] = {}
        held: list[dict[str, Any]] = []
        for row in rows:
            project = str(row['project'] or '')
            if project not in roots:
                continue
            if project in busy:
                held.append(dict(row))
                continue
            batch = batches.setdefault(project, Batch(project, roots[project]))
            batch.accepts.append(dict(row))
        fresh = {(str(r['project']), int(r['id'])) for r in rows}
        for row in pending:
            project = str(row['project'] or '')
            if project in roots and (project, int(row['id'])) not in fresh:
                batches.setdefault(project, Batch(project, roots[project])).accepts.append(row)
        # Validate all ready, sealed shared acceptances against the SAME HEAD
        # snapshot, rather than testing each one against an incomplete prefix.
        # Isolated claim revisions retain their own revision-bound flow.
        trains = {name: self._validate_train(batch) for name, batch in batches.items()}
        written: list[dict[str, Any]] = []
        # Сначала коммиты всех проектов, потом гейты — параллельно по проектам (07.10.2026). Было:
        # проект за проектом в одном потоке, и пока шёл гейт Beta App (медиана 880 с), приёмки
        # других проектов ждали даже своего коммита. Гейты разных проектов друг другу не мешают.
        # Урок 42 (11.10): после флуда ~1150 приёмок в одном тике; курсор писался только в
        # конце, перезапуск dev (каждая выкатка) начинал всё заново — интегратор стоял 4,5 ч.
        # Новые приёмки — строго по id, курсор после каждой; затем отложенные.
        held_ids = {int(r['id']) for r in held}
        for row in rows:
            if int(row['id']) in held_ids:
                written.append(self._record(kind='integrate', project=str(row['project']), tz=str(row['tz'] or ''),
                                            payload={'accept_id': int(row['id']), 'gate_wait': True, 'waiting_for': [],
                                                     'reason': 'идёт гейт проекта — коммит после его конца'}))
                self._save_cursor(int(row['id']))
                continue
            batch = batches.get(str(row['project'] or ''))
            if batch is not None:
                verdict = trains[batch.project].get(int(row['id']))
                if verdict and verdict[0] == 'red':
                    written.append(self._defer_train(batch, dict(row), verdict[1]))
                elif verdict and verdict[0] == 'green':
                    written.append(self._commit(batch,dict(row),validated=verdict[1]))
                else:
                    written.append(self._commit(batch,dict(row)))
            self._save_cursor(int(row['id']))
        for batch in batches.values():
            for accept in batch.accepts:
                if (batch.project, int(accept['id'])) not in fresh:
                    verdict = trains[batch.project].get(int(accept['id']))
                    if verdict and verdict[0] == 'red':
                        written.append(self._defer_train(batch, accept, verdict[1]))
                    elif verdict and verdict[0] == 'green':
                        written.append(self._commit(batch,accept,validated=verdict[1]))
                    else:
                        written.append(self._commit(batch,accept))
        # Isolated accepts run a revision-bound gate immediately after each
        # verified FF. A generic batch gate could incorrectly label deferred
        # accepted tasks as tested/deliverable, since they are not on trunk.
        gated = [b for b in batches.values()
                 if str(self.project_info(b.project).get('gate') or '').strip()
                 and not (self.project_info(b.project).get('claim_trees') is True
                          and self.project_info(b.project).get('commit') is True
                          and not b.legacy_accepts)]
        if self.async_gates:
            for batch in gated:
                thread = threading.Thread(target=self._gate_quiet, args=(batch,),
                                          name=f'fleet-gate:{batch.project}', daemon=True)
                self._gate_threads[batch.project] = thread
                thread.start()
            return written
        if len(gated) > 1:
            with ThreadPoolExecutor(max_workers=len(gated), thread_name_prefix='fleet-gate') as pool:
                results = list(pool.map(self._gate, gated))
        else:
            results = [self._gate(b) for b in gated]
        written.extend(r for r in results if r is not None)
        return written

    def _busy_gates(self) -> set[str]:
        for name in [n for n, t in self._gate_threads.items() if not t.is_alive()]:
            del self._gate_threads[name]
            self._pending_sig = None  # приёмки с gate_wait — сразу, не через 10 мин
        return set(self._gate_threads)

    def _gate_quiet(self, batch: Batch) -> None:
        """Фоновый гейт: событие пишет сам _gate; ошибка не должна убить поток молча."""
        try:
            self._gate(batch)
        except Exception as exc:  # noqa: BLE001
            import traceback
            try:
                self._record(kind='integrate_error', project=batch.project, payload={
                    'error': f'gate: {type(exc).__name__}: {exc}'[:500],
                    'traceback': traceback.format_exc()[-10000:]})
            except Exception:
                pass

    def _validate_train(self, batch: Batch) -> dict[int, tuple[str, Any]]:
        """One full candidate for all eligible accepts; bisect if red.

        Planning is read-only. _commit still checks the seal, dirty index,
        foreign writers and exact fingerprints immediately before each commit.
        A concurrent change therefore invalidates the proof rather than being
        silently swept into the validated train.
        """
        flags = self.project_info(batch.project)
        if flags.get('commit') is not True or len(batch.accepts) < 2:
            return {}
        # Inline diagnostic snippets are not a stable project test program:
        # preserve their historical commit-then-gate semantics. Normal
        # executable project gates (pytest, check_all.py, etc.) can validate
        # a materialized candidate before any accepted bytes reach trunk.
        from .exec_preflight import parse_command
        try:
            argv = parse_command(str(flags.get('gate') or ''))
        except ValueError:
            return {}
        if any(a in ('-c', '--eval', '-e') for a in argv):
            return {}
        entries: list[TrainEntry] = []
        seen_paths: set[str] = set()
        for accept in batch.accepts:
            tz = str(accept['tz'])
            if (flags.get('claim_trees') is True
                    and self._accept_checkout_mode(batch.project, tz, int(accept['id'])) != 'shared'):
                continue
            base = _base_tz(tz)
            owns, wrote = self._chain_paths(batch.project, base)
            specs = sorted({s for s in (_rel_pathspec(p, batch.root)
                                        for p in owns | wrote) if s})
            if not specs or self._git(batch.root, 'rev-parse', '--is-inside-work-tree').returncode:
                continue
            status = self._git(batch.root, 'status', '--porcelain', '-z', '-uall', '--', *specs)
            if status.returncode:
                continue
            paths = _porcelain_files(status.stdout)
            if not paths or len(paths) > MAX_COMMIT_PATHS:
                continue
            sealed, seal_reason = self._accepted_file_seal(
                batch.project, tz, int(accept['id']), paths)
            current = self._fingerprints(batch.root, paths)
            if sealed is None and seal_reason == 'pre_seal_epoch':
                sealed = current
            if sealed is None or current != sealed:
                continue
            staged = self._git(batch.root, 'diff', '--cached', '--name-only', '-z',
                               '--', *(f':(literal){p}' for p in paths))
            if (staged.returncode or staged.stdout
                    or self._foreign_writers(batch.project, base, batch.root, paths)):
                continue
            # One set of on-disk bytes cannot be independently attributed to
            # two overlapping acceptances. Retain the old per-item path.
            if seen_paths.intersection(paths):
                continue
            seen_paths.update(paths)
            entries.append(TrainEntry(dict(accept), paths, current,
                                      self._task_acceptance(batch.project, tz)))
        if len(entries) < 2:
            return {}

        def verify(items: list[TrainEntry]) -> tuple[bool, str]:
            files = sorted({p for item in items for p in item.paths})
            commands = list(dict.fromkeys(command for item in items
                                          for command in item.acceptance))
            if all(p.casefold().startswith('todo/') for p in files) and not commands:
                return True, ''
            return self._candidate(batch.root, files, batch.project, commands,
                                   full=True)

        ok, reason = verify(entries)
        if ok:
            return {int(e.accept['id']): ('green', e.fingerprints) for e in entries}
        # Delta isolation: a failed half narrows the suspect set; a green half
        # eliminates it. Finally validate the whole complement once, proving
        # the commits can coexist. At most 1+ceil(log2 N)+1 validations for a
        # single faulty acceptance, rather than N full project gates.
        suspect = entries
        culprit_reason = reason
        while len(suspect) > 1:
            half = suspect[:(len(suspect) + 1) // 2]
            half_ok, half_reason = verify(half)
            if half_ok:
                suspect = suspect[len(half):]
            else:
                suspect = half
                culprit_reason = half_reason
        culprit = suspect[0]
        remainder = [entry for entry in entries if entry is not culprit]
        remaining_ok, _remaining_reason = verify(remainder) if remainder else (True, '')
        if not remaining_ok:
            # Multiple defects or an interaction: fail closed; do not claim a
            # validated subset based on a single binary-search hypothesis.
            return {int(e.accept['id']): ('red', reason) for e in entries}
        result: dict[int, tuple[str, Any]] = {
            int(e.accept['id']): ('green', e.fingerprints) for e in remainder
        }
        result[int(culprit.accept['id'])] = ('red', culprit_reason)
        return result

    def _defer_train(self, batch: Batch, accept: dict[str, Any],
                     reason: str) -> dict[str, Any]:
        tz = str(accept['tz'])
        owns, wrote = self._chain_paths(batch.project, _base_tz(tz))
        specs = sorted({s for s in (_rel_pathspec(p, batch.root)
                                    for p in owns | wrote) if s})
        paths = _porcelain_files(self._git(
            batch.root, 'status', '--porcelain', '-z', '-uall', '--', *specs).stdout) if specs else []
        failures = _failed_tests(reason)
        payload = {'accept_id': accept['id'], 'paths': len(specs),
                   'deferred': paths or specs, 'deferred_paths': paths or specs,
                   'reason_code': 'batch_candidate_failed',
                   'reason': reason[:1500], 'failed_tests': failures,
                   'waiting_for': []}
        event = self._record(kind='integrate', project=batch.project, tz=tz, payload=payload)
        if paths:
            from . import review
            identifier = hashlib.sha256(
                f'{batch.project}/{tz}:batch_candidate_failed'.encode()).hexdigest()[:8]
            with self.store.read() as tx:
                row = tx.one('SELECT author_worker FROM tasks WHERE project=? AND tz=?',
                             (batch.project, _base_tz(tz)))
            author = str(row['author_worker']) if row and row['author_worker'] else None
            try:
                review.open_request(
                    self.store, identifier, kind='integration', to='queue',
                    project=batch.project, worker_id=author, source_tz=tz,
                    path=str(Path(batch.root) / paths[0]),
                    summary=f'Исправить красную приёмку пачки {batch.project}/{tz}',
                    detail='Бисекция общего кандидата: ' + reason[:800],
                )
            except (ValueError, OSError):
                pass  # Durable deferred integration event is the retry signal.
        return event

    def _pending_signature(self, pending: list[dict[str, Any]], roots: dict[str, str]) -> tuple:
        """Что может изменить исход отложенной интеграции: новые события очереди и HEAD проектов."""
        with self.store.read() as tx:
            last = int(tx.scalar(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE kind IN "
                "('accept','submit','release','reject','claim')") or 0)
        heads = []
        for project in sorted({str(r['project']) for r in pending}):
            root = roots.get(project)
            if root:
                heads.append((project, self._head(root, 'HEAD')))
        return (tuple(sorted(int(r['id']) for r in pending)), last, tuple(heads))

    @staticmethod
    def _head(root: str, rev: str) -> str:
        # Не через self.runner: он внедряется для команд гейта и коммитов, а это служебное чтение.
        try:
            proc = subprocess.run(['git', 'rev-parse', rev], cwd=root, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=GIT_TIMEOUT_S,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError):
            return ''
        return proc.stdout.strip() if proc.returncode == 0 else ''

    def _gate_key(self, batch: Batch, command: str) -> str:
        """Stable fingerprint of source, launch environment, executables and packages.

        Transient variables (TEMP, timestamps, job IDs) and generated bytecode
        must not invalidate the gate. An unreadable input disables caching.
        """
        sha = self._head(batch.root, 'HEAD^{tree}')
        if not sha:
            return ''
        try:
            from .exec_preflight import parse_command
            argv = parse_command(command)
            if not argv:
                return ''
            root = Path(batch.root).resolve()
            relevant = {
                'PATH', 'PATHEXT', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV',
                'CONDA_PREFIX', 'PYTHONUSERBASE', 'PYTHONNOUSERSITE',
                'PYTHONSAFEPATH', 'PYTHONWARNINGS', 'PYTEST_ADDOPTS',
                'PYTEST_PLUGINS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD',
                'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
                'NODE_PATH', 'NODE_OPTIONS', 'JAVA_HOME', 'CLASSPATH',
                'RUSTFLAGS', 'CARGO_HOME',
            }
            values = sorted((k.upper(), v) for k, v in os.environ.items()
                            if k.upper() in relevant)
            digest = hashlib.sha256()

            def add(*parts: object) -> None:
                digest.update(json.dumps(parts, sort_keys=True, ensure_ascii=False,
                                         separators=(',', ':')).encode('utf-8'))
                digest.update(b'\0')

            add('gate-v3', sha, command, values)
            # PYTHONPATH names alone are not evidence of its contents. An
            # untracked external module can change without moving a path or
            # Git HEAD (even retaining its size and mtime). Hash actual bytes
            # with hard work/time budgets; an incomplete scan disables memo.
            if 'PYTHONPATH' in os.environ:
                deadline = time.monotonic() + _GATE_PYTHONPATH_SCAN_S
                scanned_files = 0
                scanned_bytes = 0
                seen: set[str] = set()
                skipped = {'__pycache__', '.git', '.cache', '.pytest_cache',
                           '.ruff_cache', '.mypy_cache'}

                def fingerprint_file(file: Path, label: str) -> None:
                    nonlocal scanned_files, scanned_bytes
                    if file.is_symlink() or getattr(file, 'is_junction', lambda: False)():
                        raise ValueError('PYTHONPATH file link: cache proof unavailable')
                    if not file.is_file():
                        raise ValueError('PYTHONPATH nonregular file')
                    before = file.stat()
                    scanned_files += 1
                    if (scanned_files > _GATE_PYTHONPATH_MAX_FILES or
                            scanned_bytes + before.st_size > _GATE_PYTHONPATH_MAX_BYTES):
                        raise ValueError('PYTHONPATH scan budget exceeded')
                    content = hashlib.sha256()
                    with file.open('rb') as stream:
                        while True:
                            if time.monotonic() > deadline:
                                raise TimeoutError('PYTHONPATH scan timeout')
                            chunk = stream.read(64 * 1024)
                            if not chunk:
                                break
                            scanned_bytes += len(chunk)
                            if scanned_bytes > _GATE_PYTHONPATH_MAX_BYTES:
                                raise ValueError('PYTHONPATH changed during bounded scan')
                            content.update(chunk)
                    after = file.stat()
                    if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                            (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                        raise ValueError('PYTHONPATH input changed while hashing')
                    add('pythonpath-file', label, content.hexdigest())

                def scan_error(error: OSError) -> None:
                    raise error

                for index, entry in enumerate(os.environ['PYTHONPATH'].split(os.pathsep)):
                    if time.monotonic() > deadline:
                        raise TimeoutError('PYTHONPATH scan timeout')
                    path = Path(entry) if entry else root
                    if not path.is_absolute():
                        path = root / path
                    path = path.resolve(strict=True)
                    add('pythonpath-entry', index, str(path))
                    if path.is_file():
                        fingerprint_file(path, str(path))
                    elif path.is_dir():
                        if str(path).casefold() in seen:
                            continue
                        seen.add(str(path).casefold())
                        for folder, dirs, files in os.walk(path, onerror=scan_error):
                            if time.monotonic() > deadline:
                                raise TimeoutError('PYTHONPATH scan timeout')
                            here = Path(folder)
                            if here.is_symlink() or getattr(here, 'is_junction', lambda: False)():
                                raise ValueError('PYTHONPATH directory link')
                            # An empty directory is still an importable PEP 420
                            # namespace package. Its appearance/disappearance
                            # changes imports even when no file bytes change.
                            add('pythonpath-directory', here.relative_to(path).as_posix())
                            for dirname in dirs:
                                sub = here / dirname
                                if sub.is_symlink() or getattr(sub, 'is_junction', lambda: False)():
                                    raise ValueError('PYTHONPATH linked import subtree')
                            dirs[:] = sorted(d for d in dirs if d.casefold() not in skipped)
                            for name in sorted(files):
                                if time.monotonic() > deadline:
                                    raise TimeoutError('PYTHONPATH scan timeout')
                                item = here / name
                                if item.is_symlink():
                                    raise ValueError('PYTHONPATH linked file')
                                fingerprint_file(item, item.relative_to(path).as_posix())
                    else:
                        raise ValueError('PYTHONPATH entry is not a file or directory')
            binary = argv[0]
            if '/' in binary or '\\' in binary:
                executable = Path(binary)
                if not executable.is_absolute():
                    executable = root / executable
            else:
                found = shutil.which(binary)
                if found is None:
                    return ''
                executable = Path(found)
            executable = executable.resolve()
            info = executable.stat()
            add('launcher', str(executable), info.st_size, info.st_mtime_ns)

            roots = set()
            venv = root / '.venv'
            if venv.is_dir():
                roots.add(venv)
            name = executable.name.lower()
            if name.startswith(('python', 'py.exe', 'pytest')):
                # py.exe is only a launcher: resolve the *selected* interpreter.
                probe = [str(executable)]
                if name == 'py.exe' and len(argv) > 1 and re.fullmatch(
                        r'-(?:[23](?:\.\d+)?|V:[^ ]+)', argv[1]):
                    probe.append(argv[1])
                if name.startswith('pytest'):
                    roots.add(Path(sys.prefix))
                elif any(part.casefold() == 'windowsapps' for part in executable.parts):
                    # Windows Store execution aliases are not interpreters.
                    # Never spawn one: it can open the Store UI.
                    add('python-windowsapps-alias', str(executable))
                else:
                    script = ('import json,sys; print(json.dumps(['
                              'sys.executable,sys.version,sys.prefix,sys.base_prefix,'
                              'getattr(sys,"_base_executable","")]))')
                    result = subprocess.run(
                        [*probe, '-c', script], cwd=str(root), capture_output=True,
                        text=True, timeout=10,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    )
                    if result.returncode:
                        # WindowsApps python.exe alias can fail to launch. Its
                        # gate result is non-cacheable (rc 127), but the key must
                        # still respond to changed environment inputs.
                        add('python-unavailable', result.returncode,
                            str(Path(sys.executable).resolve()))
                    else:
                        selected, version, prefix, base, base_binary = json.loads(result.stdout)
                        add('python', selected, version, prefix, base, base_binary)
                        for python_binary in (selected, base_binary):
                            if python_binary:
                                path = Path(python_binary).resolve()
                                st = path.stat()
                                add('python-exe', str(path), st.st_size, st.st_mtime_ns)
                        roots.add(Path(prefix))

            def walk(directory: Path) -> None:
                if not directory.is_dir():
                    return
                def bad(error: OSError) -> None:
                    raise error
                for parent, folders, files in os.walk(directory, onerror=bad):
                    folders[:] = sorted(n for n in folders if n not in {
                        '__pycache__', '.cache', '.pytest_cache', '.ruff_cache',
                        '.mypy_cache', '.git', 'tmp', 'temp',
                    })
                    for filename in sorted(files):
                        if filename.endswith(('.pyc', '.pyo', '.log', '.tmp')):
                            continue
                        p = Path(parent) / filename
                        st = p.stat()
                        add('package', p.relative_to(directory).as_posix(),
                            st.st_size, st.st_mtime_ns)

            for prefix in sorted(roots, key=str):
                cfg = prefix / 'pyvenv.cfg'
                if cfg.is_file():
                    add('venv', str(cfg), hashlib.sha256(cfg.read_bytes()).hexdigest())
                package_dirs = [prefix / 'Lib' / 'site-packages']
                if (prefix / 'lib').is_dir():
                    package_dirs.extend(sorted((prefix / 'lib').glob('python*/site-packages')))
                for packages in package_dirs:
                    walk(packages)
                if prefix == venv:
                    for folder in ('Scripts', 'bin'):
                        scripts = prefix / folder
                        if scripts.is_dir():
                            for file in sorted(scripts.iterdir()):
                                if file.is_file() and file.suffix.lower() in (
                                        '.exe', '.dll', '.pyd', '.so', ''):
                                    st = file.stat()
                                    add('venv-bin', file.name, st.st_size, st.st_mtime_ns)

            # Include dirty lockfiles too; HEAD only covers committed contents.
            for pattern in ('uv.lock', 'poetry.lock', 'Pipfile.lock', 'pdm.lock',
                            'pyproject.toml', 'requirements*.txt', 'package-lock.json',
                            'pnpm-lock.yaml', 'yarn.lock', 'package.json'):
                for file in sorted(root.glob(pattern)):
                    if file.is_file():
                        add('lock', file.name, hashlib.sha256(file.read_bytes()).hexdigest())
            return digest.hexdigest()
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return ''  # Missing evidence means rerun, never reuse stale green.

    def _gate_done(self, project: str, key: str) -> bool:
        if not key:
            return False
        with self.store.read() as tx:
            row = tx.one("SELECT rc,payload FROM events WHERE kind='gate' AND project=? "
                         "ORDER BY id DESC LIMIT 1", (project,))
        if row is None:
            return False
        try:
            payload = json.loads(row['payload'] or '{}')
        except ValueError:
            return False
        # Сбой запуска/таймаут — не результат проверки: такой ключ гоняется снова.
        return payload.get('key') == key and row['rc'] not in (124, 126, 127)

    def _refresh_latest(self) -> None:
        """Latest `integrate` decision per (project, accept_id), read incrementally.

        20.10.2026: the old scan read only the newest 3000 `integrate` events. Retries
        write 9–23 thousand a day, so anything deferred more than a few hours earlier
        fell out of the window and was never retried: 406 accepted works sat
        uncommitted (Beta App 366). The first pass reads a horizon, later passes
        only events newer than the last one seen (kind/id index — cheap).
        """
        with self.store.read() as tx:
            if self._scan_id:
                rows = tx.query("SELECT id,ts,project,payload FROM events "
                                "WHERE kind='integrate' AND id>? ORDER BY id", (self._scan_id,))
            else:
                horizon = (datetime.now(timezone.utc) - timedelta(days=PENDING_HORIZON_DAYS)).isoformat()
                rows = tx.query("SELECT id,ts,project,payload FROM events "
                                "WHERE kind='integrate' AND ts>=? ORDER BY id", (horizon,))
        for row in rows:
            self._scan_id = max(self._scan_id, int(row['id']))
            project = str(row['project'] or '')
            self._project_touch[project] = int(row['id'])
            try:
                payload = json.loads(row['payload'] or '{}')
                key = (project, int(payload['accept_id']))
            except (ValueError, TypeError, KeyError):
                continue
            self._latest[key] = (int(row['id']), str(row['ts']), payload)
        horizon = (datetime.now(timezone.utc) - timedelta(days=PENDING_HORIZON_DAYS)).isoformat()
        for key in [k for k, v in self._latest.items() if v[1] < horizon]:
            del self._latest[key]

    def _supersede_stale(self, rows: list[dict[str, Any]]) -> set[tuple[str, int]]:
        """Deferred work older than PENDING_SUPERSEDE_DAYS whose every deferred path was
        sealed by a LATER accepted task is final: record it once, stop retrying.

        Without this the retry loop would re-validate long-replaced bytes forever
        (minutes of candidate checks each); the old 3000-event window hid that by
        silently forgetting everything, replaced or not.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PENDING_SUPERSEDE_DAYS)).isoformat()
        old = [r for r in rows if str(r.get('ts') or '') < cutoff]
        done: set[tuple[str, int]] = set()
        by_project: dict[str, list[dict[str, Any]]] = {}
        for row in old:
            by_project.setdefault(str(row['project']), []).append(row)
        for project, items in by_project.items():
            since = min(int(r['id']) for r in items)
            with self.store.read() as tx:
                seals = tx.query(
                    "SELECT e.id,e.tz,e.payload FROM events e JOIN tasks t "
                    "ON t.project=e.project AND t.tz=e.tz WHERE e.project=? "
                    "AND e.kind='submission_file_seal' AND e.id>? AND t.state='accepted'",
                    (project, since))
            later: dict[str, list[tuple[int, str]]] = {}
            for seal in seals:
                try:
                    files = (json.loads(seal['payload'] or '{}') or {}).get('files') or {}
                except (TypeError, ValueError):
                    continue
                for path in files:
                    later.setdefault(str(path), []).append((int(seal['id']), str(seal['tz'])))
            for row in items:
                payload = self._latest[(project, int(row['id']))][2]
                paths = [str(p) for p in payload.get('deferred') or [] if isinstance(p, str)]
                if not paths:
                    continue
                by: set[str] = set()
                for path in paths:
                    newer = [tz for sid, tz in later.get(path, ())
                             if sid > int(row['id']) and tz != str(row['tz'])]
                    if not newer:
                        break
                    by.update(newer)
                else:
                    self._record(kind='integrate', project=project, tz=str(row['tz']), payload={
                        'accept_id': int(row['id']), 'superseded': True,
                        'superseded_by': sorted(by)[:20], 'paths': len(paths),
                        'reason': 'отложена дольше %d сут.; все её файлы позже запечатаны '
                                  'другими принятыми работами — повтор прекращён' % PENDING_SUPERSEDE_DAYS})
                    done.add((project, int(row['id'])))
        if done:
            from . import platform_manual
            try:
                with self.store.tx() as tx:
                    platform_manual.enqueue(
                        tx, 'superseded-integrations-' + _now()[:10],
                        title=f'Интеграции закрыты как заменённые: {len(done)}',
                        detail='Отложенные приёмки, чьи файлы позже запечатаны другими принятыми '
                               'работами, больше не повторяются (event integrate superseded=true). '
                               'Сверить выборочно: ' + ', '.join(f'{p}/{a}' for p, a in sorted(done)[:30]))
            except Exception:  # noqa: BLE001 — журнал оператора не должен ронять интеграцию
                pass
        return done

    @staticmethod
    def _final_integration(payload: dict) -> bool:
        """Решение интеграции окончательное: коммит, «уже в истории»/пропуск или заменена."""
        return bool(payload.get('commit') or payload.get('superseded') is True
                    or ('skipped' in payload and not payload.get('deferred')
                        and not str(payload['skipped']).startswith('коммит не разрешён')))

    def _close_moot_requests(self) -> list[str]:
        """Интеграционная заявка, чья исходная приёмка уже окончательно интегрирована, закрыта.

        21.10.2026: заявки «Исправить неприменимую acceptance-версию X» рождались на сбое
        интеграции X и шли раундами (:2…:8); после того как X ушла в историю или была
        заменена (10231ae), их ревью продолжали замерзать «system-blocker» на давно
        исправленной ошибке, а владелец видел «ждёт приёмки N дней». `queue.close_obsolete_requests`
        их не ловит: источник интеграционной заявки accepted всегда, а саму заявку сразу берут.
        Взятое агентом не трогаем. Заявка и её ревью → accepted (цель достигнута), fix → rejected.
        """
        now_m = time.monotonic()
        if now_m - getattr(self, '_moot_at', 0.0) < MOOT_RECHECK_S:
            return []
        self._moot_at = now_m
        closed: list[str] = []
        with self.store.read() as tx:
            rows = tx.query("SELECT project,tz FROM tasks WHERE kind='request' "
                            "AND state IN ('ready','frozen','submitted') AND body LIKE ?",
                            ('%Тип: integration%',))
            roots = sorted({(str(r['project']), task_root(str(r['tz']))) for r in rows})
            verdicts: list[tuple[str, str, str, list[dict]]] = []
            for project, root in roots:
                family = [dict(r) for r in tx.query(
                    "SELECT tz,kind,state FROM tasks WHERE project=? AND (tz=? OR tz LIKE ? "
                    "OR tz LIKE ? OR tz LIKE ?) AND state IN ('draft','ready','taken','submitted','frozen')",
                    (project, root, root + ':%', 'review:%' + root[len('request:'):] + '%',
                     'fix:%' + root[len('request:'):] + '%'))
                    if task_root(str(r['tz'])) == root]
                if not family or any(str(r['state']) == 'taken' for r in family):
                    continue
                draft = tx.one("SELECT e.payload FROM events e WHERE e.project=? AND e.kind='request_draft' "
                               "AND (e.tz=? OR e.tz LIKE ?) ORDER BY e.id LIMIT 1", (project, root, root + ':%'))
                try:
                    source = str((json.loads(draft['payload'] or '{}') if draft else {}).get('source_tz') or '')
                except (TypeError, ValueError):
                    source = ''
                if not source:
                    continue
                accept = tx.one("SELECT id FROM events WHERE project=? AND tz=? AND kind='accept' "
                                "ORDER BY id DESC LIMIT 1", (project, source))
                if accept is not None:
                    latest = self._latest.get((project, int(accept['id'])))
                    if latest is None or not self._final_integration(latest[2]):
                        continue
                    why = f'исходная приёмка {source} окончательно интегрирована ({latest[1][:16]})'
                elif tx.one("SELECT 1 FROM events WHERE project=? AND tz=? AND kind='auto_obsolete' LIMIT 1",
                            (project, source)):
                    why = f'исходная заявка {source} сама закрыта как ненужная'
                else:
                    continue
                verdicts.append((project, root, why, family))
        for project, root, why, family in verdicts:
            ts = _now()
            statements = [("UPDATE tasks SET state=?,updated_at=? WHERE project=? AND tz=? AND state<>'taken'",
                           ('rejected' if str(r['kind']) == 'fix' else 'accepted', ts, project, str(r['tz'])))
                          for r in family]
            try:
                with self.store.tx() as tx:
                    tx.changes(statements, event={
                        'kind': 'auto_obsolete', 'worker_id': None, 'project': project, 'tz': root,
                        'payload': json.dumps({'reason': why + '; интеграционная заявка больше не нужна',
                                               'closed': [str(r['tz']) for r in family]},
                                              ensure_ascii=False)})
                closed.append(f'{project}/{root}')
            except Exception:  # noqa: BLE001 — уборка не должна ронять интеграцию
                continue
        return closed

    def _pending_accepts(self) -> list[dict[str, Any]]:
        """Latest integration decision per acceptance is authoritative across restarts."""
        self._refresh_latest()
        self._close_moot_requests()
        with self.store.read() as tx:
            active = {str(r['name']) for r in tx.query("SELECT name FROM projects WHERE active=1")}
            project_touch = dict(self._project_touch)
            deferred: list[tuple[str, int]] = []
            for (project, accept_id), (_, _, payload) in sorted(
                    self._latest.items(), key=lambda kv: kv[1][0]):
                if project not in active:
                    continue
                permission_restored = (
                    payload.get('skipped') == 'коммит не разрешён проектом (fleet-projects.toml: commit = true)'
                    and self.project_info(project).get('commit') is True)
                if payload.get('deferred') or payload.get('gate_wait') or permission_restored:
                    deferred.append((project, accept_id))
            if not deferred:
                return []
            result: list[dict[str, Any]] = []
            for project, accept_id in deferred:
                row = tx.one(
                    "SELECT id,ts,project,tz,worker_id FROM events WHERE id=? AND kind='accept' AND project=?",
                    (accept_id, project),
                )
                if row is not None:
                    result.append(dict(row))
        superseded = self._supersede_stale(result)
        if superseded:
            self._refresh_latest()
            result = [r for r in result if (str(r['project']), int(r['id'])) not in superseded]
            project_touch = dict(self._project_touch)
        for row in result:
            row.pop('ts', None)
        # A quiet project's one retry must not sit behind dozens of old
        # retries from the same busy project. Rotate projects by their last
        # real integration event; a completed attempt moves it to the end.
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in result:
            groups.setdefault(str(row['project']), []).append(row)
        names = sorted(groups, key=lambda p: (project_touch.get(p, 0), p))
        fair: list[dict[str, Any]] = []
        while any(groups.values()):
            for name in names:
                if groups[name]:
                    fair.append(groups[name].pop(0))
        return fair

    def _task_acceptance(self, project: str, tz: str) -> list[str]:
        """The accepted task's own commands, not the project's full gate."""
        with self.store.read() as tx:
            row = tx.one('SELECT acceptance FROM tasks WHERE project=? AND tz=?', (project, tz))
        try:
            commands = json.loads(row['acceptance'] or '[]') if row else []
        except (ValueError, TypeError):
            commands = []
        return [command for command in commands if isinstance(command, str) and command.strip()] \
            if isinstance(commands, list) else []

    @staticmethod
    def _candidate_tests(tree: Path, changed: list[str]) -> list[str]:
        """Select affected tests by path, module name and AST imports in the snapshot.

        Source modules imported by tests are included even when the test filename
        differs. Changed conftest/pytest config affects the full test subtree.
        """
        changed_paths = {Path(rel).as_posix() for rel in changed}
        source_modules: set[str] = set()
        selected: set[str] = set()
        for rel in changed_paths:
            parts = Path(rel).with_suffix('').parts
            if parts and parts[0] == 'src' and rel.endswith('.py'):
                parts = parts[1:]
                if parts:
                    source_modules.add('.'.join(parts[:-1] if parts[-1] == '__init__' else parts))
            if (rel.startswith(('tests/', 'test/')) and
                    (Path(rel).name.startswith('test_') or Path(rel).name.endswith('_test.py')) and
                    (tree / rel).is_file()):
                selected.add(rel)
        tests = sorted(p for folder in ('tests', 'test') if (tree / folder).is_dir()
                       for p in (tree / folder).rglob('*.py')
                       if p.name.startswith('test_') or p.name.endswith('_test.py'))
        if any(Path(rel).name == 'conftest.py' or rel in
               ('pytest.ini', 'pyproject.toml', 'setup.cfg', 'tox.ini') for rel in changed_paths):
            return sorted({p.relative_to(tree).as_posix() for p in tests} | selected)
        for file in tests:
            rel = file.relative_to(tree).as_posix()
            if rel in selected:
                continue
            if not source_modules:
                continue
            # A test named after the module may use fixtures/dynamic imports.
            if any(file.stem in {f'test_{module.rsplit(".", 1)[-1]}',
                                 f'test_{module.replace(".", "_")}',
                                 f'{module.rsplit(".", 1)[-1]}_test',
                                 f'{module.replace(".", "_")}_test'}
                   for module in source_modules):
                selected.add(rel)
                continue
            try:
                syntax = ast.parse(file.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, SyntaxError):
                # Unknown imports: do not silently exclude an unparseable test.
                selected.add(rel)
                continue
            imports: set[str] = set()
            for node in ast.walk(syntax):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
                    imports.update(f'{node.module}.{alias.name}' for alias in node.names)
            if any(name == module or name.startswith(module + '.')
                   for name in imports for module in source_modules):
                selected.add(rel)
        return sorted(selected)

    def _git(self, root: str, *args: str) -> subprocess.CompletedProcess:
        return self.runner(
            ['git', '-c', 'core.quotepath=off', *args], cwd=root, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=GIT_TIMEOUT_S,
        )

    def _chain_paths(self, project: str, base: str) -> tuple[set[str], set[str]]:
        with self.store.read() as tx:
            tasks = tx.query(
                "SELECT tz,owns FROM tasks WHERE project=? AND (tz=? OR tz LIKE ?)",
                (project, base, f'fix:%{base}%'),
            )
            chain = [str(t['tz']) for t in tasks if _base_tz(str(t['tz'])) == base]
            owns: set[str] = set()
            for t in tasks:
                if str(t['tz']) in chain:
                    try:
                        owns.update(str(x) for x in json.loads(t['owns'] or '[]'))
                    except ValueError:
                        pass
            wrote: set[str] = set()
            if chain:
                marks = ','.join('?' * len(chain))
                wrote = {
                    str(r['path_norm']) for r in tx.query(
                        f"SELECT DISTINCT path_norm FROM events WHERE kind='write' AND project=? "
                        f"AND tz IN ({marks}) AND path_norm<>''",
                        (project, *chain),
                    )
                }
        return owns, wrote

    def _record(self, **event: Any) -> dict[str, Any]:
        event.setdefault('ts', _now())
        with self.store.tx() as tx:
            tx.record_event(**event)
        if event.get('kind') == 'integrate':
            self._sync_outcome_obstacle(str(event.get('project') or ''),
                                        str(event.get('tz') or ''), event.get('payload') or {})
        return event

    def _sync_outcome_obstacle(self, project: str, tz: str, payload: dict[str, Any]) -> None:
        """Attach a real needs_result to mapped outcomes; legacy tasks retain the event.

        Integrations predate outcome_work, so do not invent a new outcome for an
        old task. The integrate event remains the durable waiting contract there.
        """
        with self.store.read() as tx:
            if not tx.one("SELECT name FROM sqlite_master WHERE type='table' AND name='outcome_work'"):
                return
            linked = tx.query(
                "SELECT o.id FROM outcome_work w JOIN outcomes o ON o.id=w.outcome_id "
                "WHERE w.project=? AND w.tz=? AND o.state='open'", (project, tz),
            )
            existing = {}
            for link in linked:
                rows = tx.query(
                    "SELECT id,predicate FROM obstacles WHERE outcome_id=? AND state='open' "
                    "AND cause_code IN ('gate_red_foreign','gate_red_own')", (link['id'],),
                )
                existing[str(link['id'])] = [r['id'] for r in rows
                    if json.loads(r['predicate'] or '{}').get('integrate_task') == f'{project}/{tz}']
        if not linked:
            return
        from dev_mcp.fleet import outcome

        waiting = [str(t) for t in payload.get('waiting_for', ())]
        deferred = bool(payload.get('deferred'))
        for link in linked:
            outcome_id = str(link['id'])
            old = existing[outcome_id]
            if not deferred:
                for obstacle_id in old:
                    outcome.resolve_obstacle(self.store, obstacle_id)
            elif not old:
                dependency = f'{project}/{waiting[0]}' if waiting else None
                outcome.open_obstacle(
                    self.store, outcome_id=outcome_id,
                    type='needs_result' if dependency else 'defect',
                    cause_code='gate_red_foreign' if dependency else 'gate_red_own',
                    predicate={'integrate_task': f'{project}/{tz}',
                               'requires_work': dependency or f'{project}/{tz}'},
                    change_source=dependency or f'{project}/{tz}',
                    resolver_work=dependency,
                )

    def _foreign_writers(self, project: str, base: str, root: str, changed: list[str]) -> list[str]:
        """Active unfinished tasks that wrote a file the accepted commit would absorb."""
        names = {norm_path(str(Path(root) / name)) for name in changed}
        with self.store.read() as tx:
            rows = tx.query(
                "SELECT e.tz,e.path_norm,t.state FROM events e "
                "JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
                "WHERE e.kind='write' AND e.project=? AND t.state NOT IN ('accepted','rejected') "
                "AND e.path_norm<>''",
                (project,),
            )
        return sorted({str(r['tz']) for r in rows if _base_tz(str(r['tz'])) != base
                       and str(r['path_norm']) in names})

    @staticmethod
    def _fingerprints(root: str, changed: list[str]) -> dict[str, str | None]:
        """Detect concurrent writers between candidate validation and git commit."""
        result: dict[str, str | None] = {}
        for rel in changed:
            path = Path(root) / rel
            if path.is_file():
                result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                result[rel] = None
        return result

    def _accepted_file_seal(
        self, project: str, tz: str, accept_id: int, changed: list[str],
    ) -> tuple[dict[str, str | None] | None, str]:
        """Legacy shared checkout: require a server-owned seal tied to submit.

        An 'owns' path or a write audit event does not attest to bytes. A
        separate trusted submit handler may emit submission_file_seal containing
        whole-file SHA-256s (None for deletion), after the actual submit event
        and before its accept. No seal means no legacy commit. We intentionally
        do not infer it from current shared working-tree bytes or author notes.
        """
        with self.store.read() as tx:
            submission = tx.one(
                "SELECT id FROM events WHERE project=? AND tz=? AND kind='submit' "
                "AND id<? ORDER BY id DESC LIMIT 1", (project, tz, accept_id),
            )
            # Урок 54: печати сдачи появились с request:8fe8a632. Сдача, сделанная до первой
            # печати в реестре, печати не имеет и не получит — для неё прежнее правило.
            first_seal = tx.scalar(
                "SELECT MIN(id) FROM events WHERE kind IN "
                "('submission_file_seal','submission_seal_epoch')")
            if first_seal is None or int(first_seal) > int(accept_id):
                return None, "pre_seal_epoch"
            if submission is None:
                return None, "missing_server_submission"
            rows = tx.query(
                "SELECT id,tool,worker_id,payload FROM events WHERE project=? AND tz=? "
                "AND kind='submission_file_seal' AND id>? AND id<? ORDER BY id DESC",
                (project, tz, int(submission['id']), accept_id),
            )
        for row in rows:
            if row['tool'] != 'fleet' or row['worker_id'] is not None:
                continue
            try:
                content = json.loads(row['payload'] or '{}')
            except (TypeError, ValueError):
                continue
            if not isinstance(content, dict) or content.get('submit_id') != int(submission['id']):
                continue
            hashes = content.get('files')
            if not isinstance(hashes, dict) or any(
                not isinstance(p, str)
                or (sha is not None and (
                    not isinstance(sha, str) or re.fullmatch('[0-9a-f]{64}', sha) is None
                ))
                for p, sha in hashes.items()
            ):
                continue
            if all(path in hashes for path in changed):
                return {path: hashes[path] for path in changed}, ''
        return None, "missing_trusted_submission_file_seal"

    def _superseded_by_later_accept(self, project: str, accept_id: int,
                                    drifted: dict[str, str | None]) -> set[str]:
        """Paths whose current bytes are sealed by a later submission of an accepted task."""
        if not drifted:
            return set()
        found: set[str] = set()
        with self.store.read() as tx:
            rows = tx.query(
                "SELECT e.tz,e.payload FROM events e JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
                "WHERE e.project=? AND e.kind='submission_file_seal' AND e.tool='fleet' "
                "AND e.worker_id IS NULL AND e.id>? AND t.state='accepted'", (project, accept_id))
        for row in rows:
            try:
                files = (json.loads(row['payload'] or '{}') or {}).get('files') or {}
            except (TypeError, ValueError):
                continue
            for path, sha in drifted.items():
                if path in files and files[path] == sha:
                    found.add(path)
        return found

    def _report_unexplained_drift(self, project: str, tz: str, accept_id: int,
                                  changed: list[str]) -> None:
        """Bytes changed outside any accepted submission: tell the operator once, not retry silently."""
        from . import platform_manual
        fingerprint = hashlib.sha256(f'{project}/{tz}/{accept_id}:drift'.encode()).hexdigest()
        try:
            with self.store.tx() as tx:
                platform_manual.enqueue(
                    tx, fingerprint, source_project=project, source_tz=tz,
                    title=f'Принятая {project}/{tz} не коммитится: файлы изменены вне флота',
                    detail=('Байты в общем дереве не совпадают ни с печатью этой сдачи, ни с более '
                            'поздней принятой. Кто-то правил файлы руками или незавершённая работа. '
                            'Оператор: сверить и закоммитить вручную или вернуть файлы. Файлы: '
                            + ', '.join(changed[:40])))
        except Exception:  # noqa: BLE001 — диагностика не должна ронять интеграцию
            pass

    def _candidate(self, root: str, changed: list[str], project: str,
                   acceptance: list[str] | None = None, *,
                   full: bool = False) -> tuple[bool, str]:
        """Validate HEAD plus exactly the candidate files, never the shared dirty tree.

        A detached worktree keeps git's live index/HEAD untouched. Both deleted and
        renamed paths come from porcelain -z; binary and non-ASCII paths are copied as bytes.
        """
        temp = Path(tempfile.mkdtemp(prefix='fleet-integrate-'))
        tree = temp / 'candidate'
        attached = False
        links: list[Path] = []
        try:
            added = self._git(root, 'worktree', 'add', '--detach', str(tree), 'HEAD')
            if added.returncode != 0:
                return False, (added.stderr or added.stdout or 'worktree add failed')[:400]
            attached = True
            for rel in changed:
                path = Path(rel)
                if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] == '.git':
                    return False, f'unsafe candidate path: {rel}'
                source, target = Path(root) / path, tree / path
                if source.is_file() or source.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.is_dir():
                        shutil.rmtree(target)
                    shutil.copy2(source, target, follow_symlinks=False)
                elif target.is_file() or target.is_symlink():
                    target.unlink()
            # Server-owned intent checker (.intent/) is untracked by design: the
            # stage acceptance `py -3 .intent/check.py ...` must find it in the
            # candidate, or every intent accept defers forever (15-17.10: 14 accepts).
            intent_dir = Path(root) / '.intent'
            if intent_dir.is_dir() and not (tree / '.intent').exists():
                shutil.copytree(intent_dir, tree / '.intent', symlinks=True)
            # 17.10: node acceptance in the candidate failed with ERR_MODULE_NOT_FOUND —
            # node_modules is ignored, so the worktree has none. Link the project's own
            # (read-only use); the link is removed by hand before cleanup (lesson 14.10).
            # 18.10: the same for `deps` of fleet-projects.toml (Beta App vendor/).
            from .exec_preflight import link_deps, project_deps
            deps = project_deps(root, tuple(self.project_info(project).get('deps') or ()))
            links.extend(link_deps(tree, deps))
            python = Path(root) / '.venv' / 'Scripts' / 'python.exe'
            # No project venv → the interpreter `py -3` acceptance runs under, not
            # dev-mcp's own venv (its mcp 2.x broke FastMCP imports, 15-17.10).
            interpreter = (str(python) if python.is_file()
                           else getattr(sys, '_base_executable', '') or sys.executable)
            env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(tree / 'src'), str(tree)]),
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
            py_paths = [p for p in changed if p.endswith('.py') and (tree / p).is_file()]
            # A deleted/renamed module can break an unchanged importer. Import
            # affected importers too; compile() alone cannot detect missing names.
            targets: set[str] = set()
            for rel in changed:
                parts = Path(rel).with_suffix('').parts
                if Path(rel).suffix != '.py':
                    continue
                if parts and parts[0] == 'src':
                    parts = parts[1:]
                if parts and parts[0] not in {'test', 'tests'}:
                    targets.add('.'.join(parts[:-1] if parts[-1] == '__init__' else parts))
            importer_paths: list[str] = []
            if targets:
                for source in (tree / 'src').rglob('*.py') if (tree / 'src').is_dir() else ():
                    try:
                        syntax = ast.parse(source.read_text(encoding='utf-8'))
                    except (SyntaxError, UnicodeError, OSError):
                        continue
                    imports = []
                    for node in ast.walk(syntax):
                        if isinstance(node, ast.Import):
                            imports.extend(alias.name for alias in node.names)
                        elif isinstance(node, ast.ImportFrom):
                            if node.level:
                                # Resolve relative imports from the importer's
                                # package: pkg.sub.consumer -> from ..shared
                                # resolves to pkg.shared. Deleted modules would
                                # otherwise leave intact importers unchecked.
                                importer_parts = source.relative_to(tree / 'src').with_suffix('').parts
                                package_parts = importer_parts[:-1]
                                if node.level > len(package_parts):
                                    continue
                                prefix = package_parts[:len(package_parts) - node.level + 1]
                                parts = [*prefix, *(node.module.split('.') if node.module else ())]
                                imported = '.'.join(parts)
                            else:
                                imported = node.module or ''
                            if imported:
                                imports.append(imported)
                                imports.extend(f'{imported}.{alias.name}' for alias in node.names)
                    if any(name == target or name.startswith(target + '.') for target in targets for name in imports):
                        importer_paths.append(source.relative_to(tree).as_posix())
            py_paths = sorted(set(py_paths + importer_paths))
            if py_paths:
                script = ('from pathlib import Path; import sys; '
                          '[(compile(Path(p).read_bytes(), p, "exec")) for p in sys.argv[1:]]')
                compiled = self.runner([interpreter, '-c', script, *py_paths], cwd=str(tree),
                                       capture_output=True, text=True, encoding='utf-8',
                                       errors='replace', timeout=GIT_TIMEOUT_S, env=env)
                if compiled.returncode:
                    return False, 'py_compile: ' + (compiled.stderr or compiled.stdout)[-550:]
                modules = []
                for rel in py_paths:
                    parts = Path(rel).with_suffix('').parts
                    if parts and parts[0] == 'src':
                        parts = parts[1:]
                    if (not parts or parts[-1] == '__main__' or parts[0] in {'tests', 'test'}
                            or parts[-1].startswith('test_')):
                        continue
                    if (Path(rel).parts[0] != 'src'
                            and not (tree / Path(rel).parent / '__init__.py').is_file()):
                        # A script directory, not a package: its sibling imports work
                        # only as `python dir/script.py`. compile() above covers it.
                        continue
                    if len(parts) > 1 or (tree / 'src' / (parts[0] + '.py')).is_file():
                        modules.append('.'.join(parts[:-1] if parts[-1] == '__init__' else parts))
                if modules:
                    script = 'import importlib,sys; [importlib.import_module(m) for m in sys.argv[1:]]'
                    imported = self.runner([interpreter, '-c', script, *sorted(set(modules))],
                                           cwd=str(tree), capture_output=True, text=True,
                                           encoding='utf-8', errors='replace', timeout=GIT_TIMEOUT_S, env=env)
                    if imported.returncode:
                        return False, 'import: ' + (imported.stderr or imported.stdout)[-550:]
            gate = str(self.project_info(project).get('gate') or '')
            if 'pytest' in gate:
                from .exec_preflight import deps_ignore_args
                collected = self.runner([interpreter, '-m', 'pytest', '--collect-only', '-q',
                                         '-p', 'no:cacheprovider', *deps_ignore_args(deps)],
                                        cwd=str(tree),
                                        capture_output=True, text=True, encoding='utf-8',
                                        errors='replace', timeout=VALIDATION_TIMEOUT_S, env=env)
                if collected.returncode == 124:
                    return False, 'validation_timeout'
                if collected.returncode not in (0, 5):
                    return False, 'pytest collect: ' + (collected.stdout + collected.stderr)[-550:]
            # Candidate validation is intentionally scoped. The original full
            # project gate runs on committed HEAD in _gate_locked, then at guard.
            # The Fleet Platform gate_head.py builds a *second* checkout of HEAD
            # and would ignore candidate bytes; only its candidate invocation
            # is replaced with scoped pytest on the materialized snapshot.
            from .exec_preflight import parse_command, preflight_gate, run_command
            from .gate_snapshot import SnapshotStore

            trial_timeout = VALIDATION_TIMEOUT_S
            def snapshot_runner(argv, **kwargs):
                # Both project gates and task-only acceptance must resolve
                # imports from this immutable candidate, never dirty trunk.
                snapshot_cwd = Path(kwargs['cwd'])
                trial_env = dict(kwargs['env'])
                trial_env['PYTHONPATH'] = os.pathsep.join(
                    (str(snapshot_cwd / 'src'), str(snapshot_cwd))
                )
                return self.runner(argv, **{**kwargs, 'env': trial_env})

            if gate:
                snapshots = SnapshotStore(temp / 'preflight-objects')
                trial_command = gate

                try:
                    gate_argv = parse_command(gate)
                except ValueError:
                    gate_argv = ()
                legacy_head = (
                    project == 'Fleet Platform'
                    and len(gate_argv) == 3
                    and gate_argv[0].casefold() in ('py', 'py.exe')
                    and gate_argv[1] == '-3'
                    and Path(gate_argv[2]).resolve()
                    == Path(__file__).resolve().parents[3] / 'tools' / 'gate_head.py'
                )
                pytest_gate = ('pytest' in gate_argv or
                               any(gate_argv[i:i + 2] == ('-m', 'pytest')
                                   for i in range(len(gate_argv) - 1)))
                if legacy_head or pytest_gate:
                    # gate_head.py explicitly requires the project's .venv,
                    # not a fallback/global interpreter. Keep that failure
                    # closed instead of silently testing a different Python.
                    if legacy_head and not python.is_file():
                        return False, 'exec_preflight: gate_head requires project .venv'
                    tests = [] if full else self._candidate_tests(tree, changed)
                    # Include explicit task acceptance unchanged; it is checked
                    # separately below, preserving markers and non-pytest gates.
                    # If no tests are affected, still verify the interpreter;
                    # preflight_gate will independently collect the whole suite.
                    test_args = (['-m', 'pytest', '-q', '-p', 'no:cacheprovider', *tests]
                                 if tests or full else ['-c', 'pass'])
                    trial_command = subprocess.list2cmdline([interpreter, *test_args])
                # A non-pytest gate runs whole on the candidate. Beta App check_all takes
                # 13–87 min against the 300 s candidate budget: every candidate timed out
                # (19.10.2026). `candidate_gate` scopes it like pytest gates are scoped; the
                # full gate still runs on committed HEAD in _gate_locked (red → styk).
                candidate_gate = str(self.project_info(project).get('candidate_gate') or '')
                if candidate_gate and not full:
                    trial_command = candidate_gate
                checked = preflight_gate(
                    trial_command, source_root=tree, snapshots=snapshots,
                    binary_root=root, runner=snapshot_runner,
                    python=interpreter, timeout_s=trial_timeout, deps=deps,
                )
                if (not checked.ok and checked.reason == 'pytest_collection_failed'
                        and checked.launch is not None and checked.launch.ok
                        and checked.collection is not None
                        and checked.collection.returncode != 124):
                    # Whole-tree collection errors that predate the candidate are not
                    # its fault: Beta App backend/tests need `bsapi` on their own path
                    # (39 errors on HEAD, 19.10.2026) and refused every candidate. Only
                    # duplicate module names or an error in a changed file still refuse.
                    # Same rule as registration (spec.py, 4e9587a).
                    out = (checked.collection.stdout or '') + (checked.collection.stderr or '')
                    norm = out.replace('\\', '/')
                    if ('import file mismatch' not in out
                            and not any(p.replace('\\', '/') in norm for p in changed)):
                        checked = None
                if checked is not None and not checked.ok:
                    if ((checked.launch and checked.launch.returncode == 124) or
                            (checked.collection and checked.collection.returncode == 124)):
                        return False, 'validation_timeout'
                    detail = ''
                    if full and checked.launch is not None:
                        detail = (checked.launch.stdout + checked.launch.stderr)[-1400:]
                    return False, 'exec_preflight: ' + checked.reason + (': ' + detail if detail else '')
            for command in acceptance or ():
                trial = run_command(command, cwd=tree, binary_root=root,
                                    runner=snapshot_runner, timeout_s=trial_timeout)
                if trial.returncode == 124:
                    return False, 'validation_timeout'
                if not trial.ok:
                    return False, 'acceptance: ' + (trial.stdout + trial.stderr)[-550:]
            return True, ''
        except subprocess.TimeoutExpired:
            return False, 'validation_timeout'
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return False, f'{type(exc).__name__}: {exc}'[:400]
        finally:
            for link in links:
                _unlink_dir(link)
            if attached:
                self._git(root, 'worktree', 'remove', '--force', str(tree))
            shutil.rmtree(temp, ignore_errors=True)

    def _commit(self, batch: Batch, accept: dict[str, Any], *,
                validated: dict[str, str | None] | None = None) -> dict[str, Any]:
        project, tz = batch.project, str(accept['tz'] or '')
        base = _base_tz(tz)
        owns, wrote = self._chain_paths(project, base)
        batch.owns.update(owns)
        batch.wrote.update(wrote)
        specs = sorted({s for s in (_rel_pathspec(p, batch.root) for p in owns | wrote) if s})
        payload: dict[str, Any] = {'accept_id': accept['id'], 'paths': len(specs)}
        flags = self.project_info(project)
        shared_epoch = (flags.get('commit') is True
                        and flags.get('claim_trees') is True
                        and self._accept_checkout_mode(project, tz, int(accept['id'])) == 'shared')
        if shared_epoch:
            batch.legacy_accepts = True
        try:
            if flags.get('commit') is not True:
                # Правила проектов разные: в Beta App коммиты по принятым ТЗ делает Claude Code,
                # в Nova VPN и EchoRedactor коммит — за владельцем / ведущей сессией.
                payload['skipped'] = 'коммит не разрешён проектом (fleet-projects.toml: commit = true)'
            elif flags.get('claim_trees') is True and not shared_epoch:
                # TZ-42: sealed snapshot alone is NOT trusted acceptance. The
                # server-owned claim_accept + independent reviewer runs must
                # bind this exact accept to one approved revision. Only claims
                # proven shared at take time can use legacy git add.
                return self._commit_trusted_claim(batch, accept, payload, tz)
            elif not specs:
                payload['skipped'] = 'нет путей задачи'
            elif self._git(batch.root, 'rev-parse', '--is-inside-work-tree').returncode != 0:
                payload['skipped'] = 'проект не в git'
            else:
                status = self._git(batch.root, 'status', '--porcelain', '-z', '-uall', '--', *specs)
                changed = _porcelain_files(status.stdout)
                if status.returncode != 0:
                    payload['error'] = status.stderr.strip()[:300]
                elif not changed:
                    payload['skipped'] = 'изменений нет (уже в истории)'
                elif len(changed) > MAX_COMMIT_PATHS:
                    payload['skipped'] = f'зона слишком широкая: {len(changed)} файлов'
                else:
                    sealed, seal_reason = self._accepted_file_seal(
                        project, tz, int(accept['id']), changed,
                    )
                    if sealed is None and seal_reason == 'pre_seal_epoch':
                        sealed = self._fingerprints(batch.root, changed)
                        payload['legacy_pre_seal'] = True
                    if sealed is None:
                        payload.update(deferred=changed, deferred_paths=changed,
                                       reason_code=seal_reason,
                                       reason='accepted candidate bytes are not sealed',
                                       waiting_for=[])
                        recovery = self._request_revalidation(batch, accept, changed, seal_reason)
                        if recovery:
                            payload.update(waiting_for=[recovery],
                                           obstacle={'type': 'needs_result', 'resolver_work': [recovery]})
                        return self._record(kind='integrate', project=project, tz=tz, payload=payload)
                    current = self._fingerprints(batch.root, changed)
                    if current != sealed:
                        # 20.10: two accepted tasks touching one file (most often the TZ .md) both
                        # deferred forever — each saw the other's bytes. A file whose bytes are a
                        # LATER accepted submission's is that task's to commit; drop it here.
                        drifted = {p: current[p] for p in changed if current[p] != sealed.get(p)}
                        superseded = self._superseded_by_later_accept(project, int(accept['id']), drifted)
                        if superseded:
                            changed = [p for p in changed if p not in superseded]
                            sealed = {p: sealed[p] for p in changed}
                            payload['superseded'] = sorted(superseded)
                            if not changed:
                                payload['skipped'] = 'все файлы переписаны более поздними принятыми задачами'
                                return self._record(kind='integrate', project=project, tz=tz, payload=payload)
                    if self._fingerprints(batch.root, changed) != sealed:
                        payload.update(deferred=changed, deferred_paths=changed,
                                       reason_code='accepted_candidate_bytes_changed',
                                       reason='shared worktree differs from accepted submission',
                                       waiting_for=[])
                        self._report_unexplained_drift(project, tz, int(accept['id']), changed)
                        return self._record(kind='integrate', project=project, tz=tz, payload=payload)
                    staged = self._git(
                        batch.root, 'diff', '--cached', '--name-only', '-z', '--',
                        *(f':(literal){f}' for f in changed),
                    )
                    if staged.returncode != 0 or staged.stdout:
                        payload.update(deferred=changed, deferred_paths=changed,
                                       reason_code='candidate_paths_already_staged',
                                       reason='pre-existing staged bytes in accepted paths',
                                       waiting_for=[])
                        return self._record(kind='integrate', project=project, tz=tz, payload=payload)
                    foreign = self._foreign_writers(project, base, batch.root, changed)
                    if foreign:
                        payload.update(deferred=changed, waiting_for=foreign,
                                       obstacle={'type': 'needs_result', 'resolver_work': foreign},
                                       reason='unfinished foreign writes inside candidate paths')
                        return self._record(kind='integrate', project=project, tz=tz, payload=payload)
                    fingerprints = self._fingerprints(batch.root, changed)
                    # Урок 44: документы ToDo/ не влияют на тесты — проверка кандидата
                    # (минуты, срывается на живом дереве) им не нужна; страж их тоже не ревьюит.
                    if validated is not None:
                        ready = set(changed) == set(validated) and fingerprints == validated
                        reason = '' if ready else 'candidate changed since train validation'
                    elif changed and all(f.casefold().startswith('todo/') for f in changed):
                        ready, reason = True, ''
                    else:
                        ready, reason = self._candidate(
                            batch.root, changed, project, self._task_acceptance(project, tz))
                    if not ready:
                        payload.update(deferred=changed, waiting_for=[],
                                       obstacle={'type': 'needs_result', 'resolver_work': []},
                                       reason=reason)
                        if reason == 'validation_timeout':
                            payload['reason_code'] = 'validation_timeout'
                        recorded = self._record(kind='integrate', project=project, tz=tz, payload=payload)
                        if reason.startswith('exec_preflight:'):
                            self._preflight_repair_request(batch, tz, changed, reason)
                        elif (reason.startswith(('acceptance:', 'import:', 'py_compile:'))
                              and not reason.startswith('acceptance: FileNotFoundError')):
                            # 17.10: такие accept ретраились молча по 2-8 суток без решателя.
                            # Провал запуска самой команды (нет npm/exe) — среда, не автор.
                            self._preflight_repair_request(
                                batch, tz, changed, reason, key=reason.split(':', 1)[0])
                        return recorded
                    if self._fingerprints(batch.root, changed) != fingerprints:
                        payload.update(deferred=changed, waiting_for=[], reason='candidate changed during validation')
                        return self._record(kind='integrate', project=project, tz=tz, payload=payload)
                    if fingerprints != sealed:
                        payload.update(deferred=changed, deferred_paths=changed, waiting_for=[],
                                       reason_code='accepted_candidate_bytes_changed',
                                       reason='accepted candidate drifted before staging')
                        return self._record(kind='integrate', project=project, tz=tz, payload=payload)
                    literal = [f':(literal){f}' for f in changed]
                    add = self._git(batch.root, 'add', '-A', '--', *literal)
                    if self._fingerprints(batch.root, changed) != fingerprints:
                        payload.update(deferred=changed, waiting_for=[], reason='candidate changed during staging')
                        return self._record(kind='integrate', project=project, tz=tz, payload=payload)
                    title = self._title(project, base)
                    msg = f'fleet: {base} принята — {title}'[:200] + f'\n\nFleet-Task: {project}/{tz}\nFleet-Reviewer: {accept.get("worker_id") or "?"}\n'
                    commit = self._git(batch.root, 'commit', '-q', '--no-verify', '-m', msg, '--', *literal)
                    if add.returncode != 0 or commit.returncode != 0:
                        payload['error'] = (add.stderr + commit.stderr + commit.stdout).strip()[:400]
                    else:
                        sha = self._git(batch.root, 'rev-parse', '--short', 'HEAD').stdout.strip()
                        payload.update(commit=sha, files=len(changed))
                        batch.commits.append(sha)
                        batch.pathspecs.update(specs)
        except (OSError, subprocess.SubprocessError) as exc:
            payload['error'] = f'{type(exc).__name__}: {exc}'[:300]
        return self._record(kind='integrate', project=project, tz=tz, payload=payload)

    def _request_revalidation(self, batch: Batch, accept: dict[str, Any],
                              changed: list[str], reason: str) -> str | None:
        """Recover old incomplete receipts without blessing present dirty bytes.

        A separate author + reviewer must attest the full current candidate.
        Only declared task paths and durable server claim grants authorize that
        new work. A naked out-of-zone write never becomes a permission grant.
        """
        if reason not in {'missing_trusted_submission_file_seal', 'missing_server_submission'}:
            return None
        from .model import task_root, paths_overlap
        base = task_root(str(accept['tz']))
        identity = hashlib.sha256(f'{batch.project}:{accept["id"]}:{reason}'.encode()).hexdigest()[:16]
        recovery = f'revalidate:{identity}'
        with self.store.read() as tx:
            original = tx.one('SELECT * FROM tasks WHERE project=? AND tz=?',
                              (batch.project, str(accept['tz'])))
            if original is None:
                return None
            declared = tx.query('SELECT tz,owns FROM tasks WHERE project=?', (batch.project,))
            grants = tx.query('SELECT c.tz,p.path_norm FROM claims c JOIN claim_paths p ON p.claim_id=c.id '
                              'WHERE c.project=?', (batch.project,))
        authorized = {p for task in declared if task_root(str(task['tz'])) == base
                      for p in json.loads(task['owns'] or '[]')}
        authorized.update(str(g['path_norm']) for g in grants if task_root(str(g['tz'])) == base)
        paths = [norm_path(str(Path(batch.root) / p)) for p in changed]
        if not paths or any(not any(paths_overlap(p, own) for own in authorized) for p in paths):
            return None
        report = norm_path(str(Path(batch.root) / 'ToDo' / 'integration-revalidation' / f'{identity}.md'))
        body = '\n'.join([
            f'Восстановить публикацию {batch.project}/{accept["tz"]}; исходное accept {accept["id"]}.',
            f'Причина: {reason}. Историческая приёмка остаётся принятой; новая печать не подделывает старую.',
            'Проверь ТЕКУЩИЕ файлы всей указанной зоны, исходное поручение и замечания ревью.',
            'Не меняй требования/приёмку исходного ТЗ; исправляй код только при доказанном дефекте.',
            'Прогони исходные acceptance. В отчёте сопоставь каждый путь и обязательство с проверкой,',
            'запиши фактические команды/rc, ограничения и необходимость смежных принятых модулей.',
            f'Новый отчёт: {report}. Не заявляй clean clone/live/product PASS по dirty tree.',
            'Сдай на НОВОЕ независимое ревью. Сервер запечатает текущие файлы; интегратор проверит',
            'кандидат HEAD + ровно эти файлы. Не коммить сам и не вбирай чужую работу.',
            '', 'Исходное поручение:', str(original['body'] or original['title']),
        ])
        with self.store.tx() as tx:
            if tx.one('SELECT 1 FROM tasks WHERE project=? AND tz=?', (batch.project, recovery)):
                return recovery
            stamp = _now()
            tx.change('INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,'
                      'owns,resources,acceptance,author_worker,body,created_at,updated_at) '
                      'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                      (batch.project, recovery, f'Перепроверить и опубликовать {accept["tz"]}', 'ready',
                       int(original['priority']), 'work', str(original['spec_path'] or ''), '', '[]',
                       json.dumps(sorted(set(paths) | {report}), ensure_ascii=False),
                       str(original['resources'] or '[]'),
                       revalidation_acceptance(str(original['acceptance'] or '[]')),
                       None, body, stamp, stamp),
                      event={'kind': 'integration_revalidation_open', 'project': batch.project, 'tz': recovery,
                             'payload': {'accept_id': accept['id'], 'source_tz': accept['tz'], 'reason': reason}})
        return recovery

    def _accept_checkout_mode(self, project: str, tz: str, accept_id: int) -> str:
        """Resolve historical take mode, never infer it from mutable flags.

        Unknown/invalid provenance stays isolated (trusted flow refuses it).
        A shared take is proved by a submit by that worker preceding accept
        and its corresponding server-owned take event.
        """
        with self.store.read() as tx:
            submits = tx.query(
                "SELECT id,worker_id FROM events WHERE kind='submit' AND "
                "project=? AND tz=? AND id<? ORDER BY id DESC LIMIT 1",
                (project, tz, accept_id),
            )
            if not submits:
                return "unknown"
            submission = submits[0]
            takes = tx.query(
                "SELECT payload FROM events WHERE kind='take' AND "
                "project=? AND tz=? AND worker_id=? AND id<? ORDER BY id DESC LIMIT 1",
                (project, tz, submission["worker_id"], submission["id"]),
            )
        if not takes:
            return "unknown"
        try:
            data = json.loads(takes[0]["payload"] or "{}")
        except (TypeError, ValueError):
            return "unknown"
        if not isinstance(data, dict) or type(data.get("claim_id")) is not int:
            return "unknown"
        binding = data.get("claim_context")
        if binding is None and "claim_context" not in data:
            return "shared"
        if isinstance(binding, dict) and binding.get("mode") in ("isolated", "shared"):
            return str(binding["mode"])
        return "unknown"

    def _commit_trusted_claim(self, batch: Batch, accept: dict[str, Any],
                              payload: dict[str, Any], tz: str) -> dict[str, Any]:
        """Safe opt-in bridge; never cherry-pick an unverified submission.

        A dedicated per-claim gate runs on the resulting trunk HEAD. The
        existing Integrator records its normal gate event/styk when red;
        ClaimAcceptFlow independently records its revision-bound gate receipt.
        Shared flag-off projects continue through the original _commit route.
        """
        from .claim_accept_flow import ClaimAcceptError, ClaimAcceptFlow
        from .claim_tree import ClaimTreeError
        project = batch.project
        info = self.project_info(project)
        if not str(info.get('gate') or '').strip():
            payload.update(deferred=['gate'], waiting_for=[],
                           reason='isolated integration needs a full trunk gate')
            return self._record(kind='integrate', project=project, tz=tz, payload=payload)
        with self.store.read() as tx:
            rows = tx.query(
                "SELECT payload FROM events WHERE kind='claim_accept' AND "
                "project=? AND tz=? AND worker_id=? AND id<? ORDER BY id DESC",
                (project, tz, accept.get('worker_id'), accept['id']),
            )
        evidence = None
        for row in rows:
            try:
                candidate = json.loads(row['payload'] or '{}')
            except (ValueError, TypeError):
                continue
            if (type(candidate.get('claim_id')) is int
                    and candidate.get('submission_revision')
                    and candidate.get('submission_id')):
                evidence = candidate
                break
        if evidence is None:
            payload.update(deferred=['acceptance'], waiting_for=[],
                           reason='missing independent claim acceptance evidence')
            return self._record(kind='integrate', project=project, tz=tz, payload=payload)

        def trunk_gate(trunk: Path) -> int:
            isolated = Batch(project, str(trunk))
            isolated.accepts.append(accept)
            isolated.commits.append(self._head(str(trunk), 'HEAD'))
            result = self._gate(isolated, force=True)
            return int(result['rc']) if result is not None and result.get('rc') is not None else 127

        try:
            result = ClaimAcceptFlow(
                self.store, settings=self.project_info, gate=trunk_gate,
            ).on_accept(int(evidence['claim_id']), str(evidence['submission_revision']))
        except (ClaimAcceptError, ClaimTreeError, ValueError, OSError) as exc:
            payload.update(deferred=['acceptance'], waiting_for=[],
                           reason=f'trusted claim integration refused: {exc}'[:400])
            return self._record(kind='integrate', project=project, tz=tz, payload=payload)
        payload.update(claim_id=result.claim_id,
                       submission_revision=evidence['submission_revision'],
                       accepted_sha=result.accepted_sha,
                       integrated_sha=result.integrated_sha,
                       gate_rc=result.gate_rc,
                       isolated=True, status=result.status,
                       repair_status=result.repair_status)
        if result.status == 'needs_result':
            if not result.repair_tz:
                raise ClaimAcceptError('conflict has no registered repair task')
            resolver = f'{project}/{result.repair_tz}'
            payload.update(deferred=list(result.conflict_paths),
                           conflict_paths=list(result.conflict_paths),
                           waiting_for=[result.repair_tz],
                           obstacle={'type': 'needs_result', 'resolver_work': [resolver],
                                     'requires_new_review': True})
        elif result.status == 'gate_failed':
            payload.update(commit=result.integrated_sha, gate_failed=True)
            batch.commits.append(result.integrated_sha)
        elif result.status in ('integrated', 'already_integrated'):
            payload.update(commit=result.integrated_sha, files='reviewed_only')
            batch.commits.append(result.integrated_sha)
        else:
            payload.update(deferred=['legacy'], waiting_for=[],
                           reason='isolated acceptance attempted legacy routing')
        return self._record(kind='integrate', project=project, tz=tz, payload=payload)

    def _isolated_revisions(self, project: str, base: str) -> list[str]:
        """Запечатанные кандидаты семьи задачи (TZ-41 claim_submission), по порядку сдачи.

        План консорциума 11.10, п.4: в ствол попадает коммит кандидата, а не файлы общей папки.
        Берётся последняя сдача каждого захвата семьи (задача и её fix:*).
        """
        with self.store.read() as tx:
            rows = tx.query(
                "SELECT payload FROM events WHERE kind='claim_submission' AND project=? "
                "AND (tz=? OR tz LIKE ?) ORDER BY id", (project, base, f'fix:{base}%'))
        latest: dict[Any, str] = {}
        for row in rows:
            try:
                data = json.loads(row['payload'] or '{}')
            except ValueError:
                continue
            if data.get('revision'):
                latest.pop(data.get('claim_id'), None)
                latest[data.get('claim_id')] = str(data['revision'])
        return list(latest.values())

    def _commit_isolated(self, batch: Batch, accept: dict[str, Any], revisions: list[str],
                         payload: dict[str, Any], base: str, tz: str) -> dict[str, Any]:
        """Перенести запечатанные кандидаты на актуальный ствол одним коммитом интегратора.

        cherry-pick без коммита каждого кандидата; конфликт — ничего не сливается, ствол
        возвращается как был, исход — отложенная интеграция с путями конфликта (ремонт по улике).
        """
        root = batch.root
        payload['isolated'] = revisions
        for revision in revisions:
            picked = self._git(root, 'cherry-pick', '--no-commit', '--allow-empty', revision)
            if picked.returncode != 0:
                conflicts = [f for f in self._git(
                    root, 'diff', '--name-only', '-z', '--diff-filter=U').stdout.split('\0') if f]
                self._git(root, 'cherry-pick', '--abort')
                self._git(root, 'reset', '--merge')
                payload.update(deferred=conflicts or ['?'], waiting_for=[],
                               obstacle={'type': 'needs_result', 'resolver_work': []},
                               reason='conflict landing sealed candidate ' + revision[:12] + ': '
                               + (picked.stderr or picked.stdout).strip()[:200])
                return self._record(kind='integrate', project=batch.project, tz=tz, payload=payload)
        staged = [f for f in self._git(root, 'diff', '--cached', '--name-only').stdout.splitlines() if f]
        if not staged:
            payload['skipped'] = 'изменений нет (уже в истории)'
            return self._record(kind='integrate', project=batch.project, tz=tz, payload=payload)
        title = self._title(batch.project, base)
        msg = (f'fleet: {base} принята — {title}'[:200]
               + f'\n\nFleet-Task: {batch.project}/{tz}\nFleet-Reviewer: {accept.get("worker_id") or "?"}\n'
               + ''.join(f'Fleet-Candidate: {r}\n' for r in revisions))
        commit = self._git(root, 'commit', '-q', '--no-verify', '-m', msg)
        if commit.returncode != 0:
            self._git(root, 'reset', '--merge')
            payload['error'] = (commit.stderr + commit.stdout).strip()[:400]
        else:
            sha = self._git(root, 'rev-parse', '--short', 'HEAD').stdout.strip()
            payload.update(commit=sha, files=len(staged))
            batch.commits.append(sha)
            batch.pathspecs.update(staged)
        return self._record(kind='integrate', project=batch.project, tz=tz, payload=payload)

    def _preflight_repair_request(self, batch: Batch, tz: str,
                                  changed: list[str], reason: str, *, key: str = '') -> None:
        """An uncommittable accepted candidate requires new author-side work.

        `key` — стабильный класс причины для дедупа, когда текст причины содержит
        временные пути и тайминги (трассировка приёмки меняется от прогона к прогону).
        """
        if not changed:
            return
        from . import review
        identifier = hashlib.sha256(
            f'{batch.project}/{tz}:{key or reason}'.encode('utf-8')
        ).hexdigest()[:8]
        candidate_path = str(Path(batch.root) / changed[0])
        try:
            with self.store.read() as tx:
                # Интегратор повторяет отложенное каждый круг: open_request
                # нумерует повтор как :2, :3 — одна открытая заявка на причину.
                if tx.one("SELECT 1 FROM tasks WHERE project=? AND (tz=? OR tz LIKE ?) "
                          "AND state IN ('ready','taken','submitted','frozen')",
                          (batch.project, f'request:{identifier}', f'request:{identifier}:%')):
                    return
                task = tx.one('SELECT author_worker FROM tasks WHERE project=? AND tz=?',
                              (batch.project, _base_tz(tz)))
            author = str(task['author_worker']) if task is not None and task['author_worker'] else None
            review.open_request(
                self.store, identifier, kind='integration', to='queue',
                project=batch.project, worker_id=author, source_tz=tz,
                path=candidate_path,
                summary=(f'Принятая работа не проходит приёмку на чистом HEAD: {batch.project}/{tz}'
                         if key else f'Исправить неприменимую acceptance-версию {batch.project}/{tz}'),
                detail=(f'Изменение отклонено до Git-коммита: {reason[-350:] if key else reason[:350]}. '
                        + ('Кандидат = HEAD + файлы задачи: работа опирается на неотслеживаемые '
                           'или чужие незакоммиченные файлы — внеси их в зону или убери зависимость. '
                           if key else '')
                        + f'Автор: {author or "неизвестен"}.'),
            )
        except (ValueError, OSError):
            # The integrate event is already durable and keeps the blocked
            # candidate in the retry queue; never commit after report failure.
            pass

    def _title(self, project: str, tz: str) -> str:
        with self.store.read() as tx:
            return str(tx.scalar('SELECT title FROM tasks WHERE project=? AND tz=?', (project, tz)) or '')

    def _changed(self, batch: Batch) -> list[str]:
        """Пути пачки относительно корня — гейту в DEV_MCP_FLEET_CHANGED (строка на путь): проект
        может проверять только задетые зоны. Гейт, который переменную не читает, работает как раньше."""
        specs = {s for s in (_rel_pathspec(p, batch.root) for p in batch.owns) if s} | set(batch.pathspecs)
        return sorted({re.sub(r'^:\([^)]*\)', '', s) for s in specs})  # без «магии» git: «:(icase)src/a.py»

    def _gate(self, batch: Batch, *, force: bool = False) -> dict[str, Any] | None:
        # The regater is optional until its own change is committed. Importing
        # it at module scope breaks pytest collection on an otherwise valid HEAD
        # where integrate.py landed before regate.py. Without a regater there is
        # no competing maintenance gate, so the fallback needs no mutex.
        try:
            from .regate import project_gate_lock
        except ModuleNotFoundError as exc:
            if exc.name != f'{__package__}.regate':
                raise  # A missing dependency *inside* regate is a real error.
            guard = nullcontext()
        else:
            # Shared reentrant lock when autonomous red-trunk regating exists.
            guard = project_gate_lock(batch.project)
        with guard:
            return self._gate_locked(batch, force=force)

    def _gate_locked(self, batch: Batch, *, force: bool = False) -> dict[str, Any] | None:
        command = str(self.project_info(batch.project).get('gate') or '').strip()
        if not command:
            return None
        # The executable check keeps running in either mode; its ability to
        # block reviews is governed by shadow.py (queue.red_zone consumes the
        # current mode). A changed gate command starts a new shadow version.
        from .shadow import ShadowRules
        shadow = ShadowRules(self.store)
        rule_id = f'project_gate:{batch.project}'
        shadow.register(rule_id, 'project_gate',
                        hashlib.sha256(command.encode('utf-8')).hexdigest())
        after = [str(a['tz']) for a in batch.accepts]
        key = self._gate_key(batch, command)
        if not force and self._gate_done(batch.project, key):
            return None
        started = time.monotonic()
        # TZ-50: both candidate preflight and this production invocation use
        # exactly the same argv parser, shell=False, cwd/env and timeout path.
        from .exec_preflight import run_command
        try:
            checked = run_command(command, cwd=batch.root, runner=self.runner,
                                  timeout_s=GATE_TIMEOUT_S, changed=self._changed(batch))
            rc, out = checked.returncode, checked.stdout + checked.stderr
        except ValueError as exc:
            rc, out = 127, f'{type(exc).__name__}: {exc}'
        failed_lines = _failed_tests(out) if rc != 0 else []
        failed = _failed_paths(failed_lines, batch.root)
        # Хвост лога у Beta App целиком съедало предупреждение i18n — имён упавших тестов в
        # нём не было (07.10.2026). Строки FAILED идут первыми.
        head = '\n'.join(failed_lines[:20]) + '\n…\n' if failed_lines else ''
        tail = head + out[-LOG_TAIL_CHARS:]
        summary = _gate_summary(out, rc, failed_lines)
        payload = {
            'rc': rc, 'dur_s': round(time.monotonic() - started, 1), 'summary': summary,
            'log_tail': tail, 'after': after, 'commits': batch.commits, 'command': command,
            'failed': failed, 'key': key,
        }
        event = self._record(kind='gate', project=batch.project, tz=after[-1] if after else None, rc=rc, payload=payload)
        shadow.check(
            rule_id, would_block=rc != 0,
            subject=f'{batch.project}/{after[-1] if after else "gate"}',
            revision=batch.commits[-1] if batch.commits else '',
            reason=summary, code=f'gate_rc:{rc}', defect_found=rc != 0,
        )
        shadow.advance(rule_id)
        if rc != 0:
            self._open_styk(batch, command, summary, tail, after, failed)
        return event

    def _open_styk(
        self, batch: Batch, command: str, summary: str, tail: str, after: list[str], failed: list[str] = (),
    ) -> None:
        acceptance, matched_sha = _styk_review_gate(command, batch.root)
        with self.store.tx() as tx:
            open_rows = tx.query(
                "SELECT tz,title FROM tasks WHERE project=? "
                "AND state IN ('ready','taken','submitted','draft','frozen') "
                "ORDER BY CASE state WHEN 'taken' THEN 0 WHEN 'submitted' THEN 1 "
                "WHEN 'ready' THEN 2 WHEN 'draft' THEN 3 ELSE 4 END,created_at,tz",
                (batch.project,),
            )
            open_row = next((r for r in open_rows if str(r['title']).startswith(STYK_TITLE)
                             or _base_tz(str(r['tz'])).startswith('styk:')), None)
            if open_row is not None:
                # A frozen repair or its still-open fix/review is the same gate
                # repair pipeline, not permission to create another full TZ.
                tx.record_event(kind='styk_coalesced', project=batch.project,
                                tz=_base_tz(str(open_row['tz'])), payload={
                                    'after': after, 'summary': summary,
                                    'gate_original': command, 'failed': list(failed),
                                })
                return
            n = int(tx.scalar("SELECT COUNT(*) FROM tasks WHERE project=? AND tz LIKE 'styk:%'", (batch.project,)) or 0)
            tz = f'styk:{n + 1}'
            owns = _styk_zone(batch, failed)
            review_note = ([f'Эквивалентная доступная ревьюеру команда: `{acceptance}` '
                            f'(байты HEAD-gate совпадают, sha256={matched_sha}).']
                           if matched_sha else [])
            body = '\n'.join([
                f'[fleet-styk] {batch.project}',
                f'Гейт проекта `{command}` красный после приёмок: {", ".join(after)}.',
                f'Кратко: {summary}',
                '',
                'Что сделать: найти, какое из принятых изменений (или их сочетание) ломает гейт, и',
                'починить стык в пределах зоны задач пачки. Не откатывать чужую работу без причины;',
                'если причина вне зоны — fleet_block с точным путём и строкой лога.',
                *review_note,
                '',
                'Хвост лога гейта:',
                '```',
                tail[-2500:],
                '```',
            ])
            now = _now()
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                "resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch.project, tz, f'{STYK_TITLE}: {", ".join(after)[:120]}', 'ready', 0, 'work', '', '',
                 '[]', json.dumps(owns, ensure_ascii=False), '[]', json.dumps([acceptance], ensure_ascii=False),
                 'integrator', body, now, now),
                event={'kind': 'styk_open', 'project': batch.project, 'tz': tz,
                       'payload': {'after': after, 'summary': summary,
                                   'gate_original': command, 'gate_acceptance': acceptance,
                                   'gate_script_sha256': matched_sha}},
            )


def _porcelain_files(out: str) -> list[str]:
    """`git status --porcelain -z` → пути. У переименования за записью идёт старое имя — его
    тоже коммитим (удаление старого пути — часть того же изменения)."""
    files: list[str] = []
    tokens = out.split('\0')
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        files.append(entry[3:])
        if entry[0] in 'RC' and i < len(tokens) and tokens[i]:
            files.append(tokens[i])
            i += 1
    return files


_FAIL_LINE = re.compile(r'(FAIL|ERROR|Error|failed|Traceback|✗|\[X\]|rc=[1-9])')
# pytest: «FAILED test/test_x.py::TestY::test_z - AssertionError», «ERROR backend/tests/a.py»
_FAILED_TEST = re.compile(r'^(?:FAILED|ERROR)\s+([\w./\-]+\.(?:py|mjs|js|ts|kt))(?:::\S*)?')
MAX_FAILED = 50


def _failed_tests(out: str) -> list[str]:
    seen: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if _FAILED_TEST.match(line) and line not in seen:
            seen.append(line[:300])
    return seen


def _failed_paths(lines: list[str], root: str) -> list[str]:
    """Файлы упавших тестов относительно корня проекта. Чек гейта бывает запущен из подпапки
    (backend/), тогда путь ищется на уровень ниже корня."""
    base = Path(root)
    found: list[str] = []
    for line in lines:
        rel = _FAILED_TEST.match(line).group(1)
        candidates = [base / rel] + ([d / rel for d in sorted(base.iterdir()) if d.is_dir()] if base.is_dir() else [])
        for path in candidates:
            if path.is_file():
                rel_root = path.relative_to(base).as_posix()
                if rel_root not in found:
                    found.append(rel_root)
                break
        if len(found) >= MAX_FAILED:
            break
    return found


def _styk_zone(batch: Batch, failed: list[str]) -> list[str]:
    """Зона стыка: файлы упавших тестов и то, что пачка реально записала. Не корень проекта:
    стык с зоной «весь проект» не получал выдачу, пока в проекте работал хоть один агент, —
    styk:4 Beta App простоял так девять дней (07.10.2026). Нужен путь шире — guard расширит
    зону на свободный путь сам."""
    root = norm_path(batch.root)
    zone = {norm_path(f'{batch.root}/{rel}') for rel in failed} | set(batch.wrote)
    if not zone:
        zone = set(batch.owns)
    return sorted(p for p in zone if p != root)


def _gate_summary(out: str, rc: int, failed_lines: list[str] = ()) -> str:
    if rc == 0:
        return 'зелёный'
    lines = list(failed_lines[:4]) or [ln.strip() for ln in out.splitlines() if _FAIL_LINE.search(ln)][-4:]
    return ('; '.join(lines) or f'rc={rc}')[:400]


def run_integrator(db_path: str | Path, stop_event: threading.Event, *, interval_s: float = INTERVAL_S) -> None:
    """Фоновый цикл dev-mcp. Ошибка прохода не убивает поток: следующий проход повторит."""
    store = Store.open(str(db_path))
    integrator = Integrator(store, Path(db_path).with_name('integrate-state.json'), async_gates=True)
    while not stop_event.is_set():
        try:
            integrator.tick()
        except Exception as exc:  # noqa: BLE001 — фон обязан жить
            try:
                import traceback
                integrator._record(kind='integrate_error', payload={
                    'error': f'{type(exc).__name__}: {exc}'[:500],
                    'traceback': traceback.format_exc()[-10000:]})
            except Exception:
                pass
        stop_event.wait(interval_s)
