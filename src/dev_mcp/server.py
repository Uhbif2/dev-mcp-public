"""Инструменты dev-mcp.

Принцип набора: узкий и предсказуемый. Здесь НЕТ инструмента «выполни команду» —
именно его отсутствие и есть основная защита, потому что произвольный shell не
фильтруется (PowerShell тьюринг-полон, любой чёрный список обходится
переформулировкой). Вместо него — `run_task` с именами из policy.toml, где argv
задан заранее и из запроса клиента не подставляется.

Каждый путь, пришедший снаружи, проходит через `Policy.resolve()` — единственную
точку проверки границы. Инструменты сами по себе границ не знают и знать не
должны.

Размер ответа ограничен MAX_RESPONSE_CHARS: между этим сервером и внешним
клиентом стоит long-poll шлюз, который режет ответы больше ~15 КБ (см.
ADD_MCP_SERVER.md проекта Gateway Host). Лучше отдать усечённый ответ с
честной пометкой, чем потерять его целиком на транспорте.
"""

from __future__ import annotations

import base64
import difflib
import fnmatch
import hashlib
import ipaddress
import json
import ntpath
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anyio
from fastmcp import FastMCP

from dev_mcp.audit import Audit
from dev_mcp.context_docs import ContextDocs
from dev_mcp.fleet.location import backup_fleet_db_isolated as backup_fleet_db, fleet_db_path
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.claim_context import ClaimRoutingError
from dev_mcp.fleet.owner import OwnerSessions, secret_path
from dev_mcp.fleet.store import Store
from dev_mcp.fleet import world as fleet_world
from dev_mcp.fleet.tools import FleetContext, register as register_fleet_tools
from dev_mcp import request_triage
from dev_mcp import patch as patch_mod
from dev_mcp import syntax_check
from dev_mcp.operator_catalog import CatalogError, validate_operation
from dev_mcp.operator_core import verify_public_result
from dev_mcp.policy import Policy, PolicyError, ScanItem, ScanTimeout, scrub_env
from dev_mcp.runner import Job, JobRegistry, child_env, decode_output, describe_job
from dev_mcp.owner import overview as owner_overview
from dev_mcp.owner import taste_gallery as owner_taste
from dev_mcp.intents import inputs as intent_inputs

MAX_RESPONSE_CHARS = 12_000
RUN_STATUS_WAIT_S = float(os.environ.get('DEV_MCP_RUN_STATUS_WAIT_S', '25') or 25)

# Потолок по времени на один обход дерева (search_files, list_dir рекурсивно).
# Отдельно от max_search_results/max_list_entries: те режут число НАЙДЕННОГО,
# а этот — сам обход, когда искомого в дереве нет вовсе (частый случай — грубая
# оценка «есть ли такая строка») и его иначе пришлось бы дойти до конца, сколько
# бы каталог ни весил. См. Policy.prunes_dir — обход и так не спускается в
# .venv/node_modules/.git, это вторая, независимая страховка на случай, когда
# сам разрешённый корень целиком огромен (тысячи реальных файлов проекта).
_WALK_TIME_BUDGET_S = 20.0

# Сколько ТЯЖЁЛЫХ вызовов сервер выполняет одновременно. Это не безопасность, а
# защита от того, чтобы несколько агентов превращали друг друга в очередь.
# FastMCP выполняет синхронные инструменты в пуле потоков anyio (потолок — 40
# потоков), и сами по себе вызовы идут параллельно. Но обход дерева и регулярка —
# чистый питон, он идёт под GIL: одиннадцатый одновременный поиск не ускоряет ни
# один из десяти, зато отбирает интерпретатор у read_file и policy_info, и те
# вместо 20 мс отвечают секунду. Лимит ниже держит суммарный спрос на потоки
# заметно меньше сорока, поэтому лёгкому вызову всегда есть где выполниться.
_MAX_CONCURRENT_SCANS = 4   # search_files, list_dir(recursive=true) — упираются в GIL
_MAX_CONCURRENT_RUNS = 12   # run / run_task — ждут внешний процесс, GIL при этом свободен
_GATE_WAIT_S = 20.0         # сколько ждать очереди, прежде чем честно ответить «занято»

# Поиск ПО ТЕКСТУ не читает двоичные файлы — ровно как grep. Дело не в чистоте
# выдачи (совпадение в .so и так было бы мусором из-за errors='ignore'), а в
# цене: на реальном корне «Nova VPN» 49 000 файлов и 4 ГБ — это вендоренные
# SDK/NDK/тулчейны, и именно их ЧТЕНИЕ съедало весь бюджет времени. Поиск
# возвращался обрезанным, так и не дойдя до настоящих исходников, — а агент
# видел «ничего не найдено» и запускал поиск снова.
#
# Два уровня, оба дешёвые: по расширению — чтобы не открывать файл вовсе
# (под антивирусом открытие и есть основная цена), по NUL в первых килобайтах —
# чтобы поймать всё остальное, не читая файл целиком.
_BINARY_SUFFIXES = frozenset({
    '.exe', '.dll', '.so', '.dylib', '.a', '.o', '.obj', '.lib', '.pyd', '.class',
    '.jar', '.apk', '.aar', '.aab', '.dex', '.msi', '.bin', '.dat', '.db', '.sqlite',
    '.zip', '.gz', '.bz2', '.xz', '.7z', '.rar', '.tar', '.whl', '.egg',
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tif', '.tiff',
    '.mp3', '.mp4', '.avi', '.mkv', '.mov', '.wav', '.ogg', '.webm',
    '.pdf', '.woff', '.woff2', '.ttf', '.otf', '.eot', '.pak', '.jsa',
})
_BINARY_SNIFF_BYTES = 8192


def _blind_snapshot_sha(root: Path, batch: str, attempt_id: str | None = None) -> str | None:
    """Rehash the sealed *set* of case files, not just an event's SHA shape.

    Any missing, replaced, symlinked or modified blind file invalidates the
    snapshot. It may be sealed again only before candidate exposure.
    """
    if not re.fullmatch(r'batch-[a-z0-9]{6,40}', batch):
        return None
    manifest = root / 'data' / 'batches' / 'out' / batch / 'MANIFEST.md'
    review = root / 'data' / 'reviews' / batch
    if attempt_id is not None:
        if re.fullmatch(r'claim-[1-9][0-9]*', attempt_id) is None:
            return None
        review = review / 'attempts' / attempt_id
    try:
        if manifest.is_symlink() or review.is_symlink() or not review.is_dir():
            return None
        source = manifest.read_bytes()
        if not source or len(source) > 1024 * 1024:
            return None
        source_text = source.decode('utf-8')
        begin, end = '<!-- MILL_MANIFEST_JSON', 'MILL_MANIFEST_JSON -->'
        if source_text.count(begin) != 1 or source_text.count(end) != 1:
            return None
        payload = json.loads(source_text.split(begin, 1)[1].split(end, 1)[0])
        entries = payload.get('entries') if isinstance(payload, dict) else None
        if not isinstance(entries, dict) or not entries:
            return None
        candidates = sorted(cid for cid, item in entries.items()
                            if isinstance(cid, str) and isinstance(item, dict)
                            and item.get('outcome') == 'candidate')
        if not candidates:
            return None
        digests: list[tuple[str, str]] = []
        for cid in candidates:
            safe = (cid if re.fullmatch(r'[A-Za-z0-9._-]{1,120}', cid)
                    and cid not in ('.', '..') else
                    'case-' + hashlib.sha256(cid.encode('utf-8')).hexdigest()[:24])
            case_dir = review / safe
            blind = case_dir / 'blind.json'
            if case_dir.is_symlink() or blind.is_symlink() or not blind.is_file():
                return None
            content = blind.read_bytes()
            if not content or len(content) > 128 * 1024:
                return None
            digests.append((cid, hashlib.sha256(content).hexdigest()))
        return hashlib.sha256(json.dumps(digests, ensure_ascii=False).encode('utf-8')).hexdigest()
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


_PULT_READ = ("result", "active", "decisions", "flow", "report", "look")


def _pult_read(argv: list[str]) -> list[str] | str | None:
    """`fleet.cmd <cmd> …` (или `py tools/fleet.py …`) → argv пульта для чтения, текст отказа или None."""
    if not argv:
        return None
    head = os.path.basename(str(argv[0])).lower()
    rest = [str(a) for a in argv[1:]]
    if head in ("py", "py.exe", "python", "python.exe") and rest and rest[0] in ("-3",):
        rest = rest[1:]
    if head in ("py", "py.exe", "python", "python.exe"):
        if not rest or os.path.basename(rest[0]).lower() != "fleet.py":
            return None
        rest = rest[1:]
    elif head not in ("fleet.cmd", "fleet"):
        return None
    if not rest or rest[0] not in _PULT_READ:
        return ("пульт флота агенту доступен только на чтение: " + ", ".join(_PULT_READ)
                + ' — например run(argv=["fleet.cmd", "result", "<Проект>/<TZ>"]).')
    return rest


def _budget_left(budget_s: int, t0: float) -> int:
    """Бюджет синхронного ответа — от НАЧАЛА вызова, а не от старта процесса (07.10.2026).

    Ожидание слота в `runs.hold()` (до 20 с) и подготовка шли сверху бюджета: вызов длился 40–60 с
    при бюджете 35 и обрывался на шлюзе (65 с) — ход агента кончался на `run`.
    """
    return max(min(5, budget_s), int(budget_s - (time.monotonic() - t0)))


class _Gate:
    """Ограничитель одновременных тяжёлых вызовов с ЧЕСТНЫМ отказом.

    Именно честным: если за `wait_s` очередь не подошла, клиент получает
    внятное «сервер занят, повтори», а не бесконечное ожидание. Молчаливая
    очередь здесь хуже отказа — на том конце агент, который по таймауту всё
    равно перезапросит, и висящий вызов к этому моменту уже никому не нужен,
    но поток пула держит.
    """

    def __init__(self, limit: int, name: str, wait_s: float = _GATE_WAIT_S) -> None:
        self._sem = threading.BoundedSemaphore(limit)
        self._limit = limit
        self._name = name
        self._wait_s = wait_s
        self._inflight = 0
        self._lock = threading.Lock()

    @property
    def inflight(self) -> int:
        return self._inflight

    @contextmanager
    def hold(self):
        if not self._sem.acquire(timeout=self._wait_s):
            raise PolicyError(
                f'сервер занят: уже {self._limit} одновременных {self._name} '
                f'(ждал {self._wait_s:.0f} с). Повтори через несколько секунд '
                'или сузь запрос — path/glob.'
            )
        with self._lock:
            self._inflight += 1
        try:
            yield self._inflight
        finally:
            with self._lock:
                self._inflight -= 1
            self._sem.release()


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Убить процесс вместе с потомками и дождаться его смерти.

    `taskkill /T` — потому что убийство одного корня оставляет дерево (сборка
    запускает компилятор, adb — демон). Ожидание ограничено по времени со всех
    сторон: этот код зовётся из пула потоков, и «подождать подольше» здесь
    означает «занять поток навсегда».
    """
    try:
        subprocess.run(  # noqa: S603,S607 — фиксированный argv, наш же процесс
            ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
    for _ in range(2):
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            proc.kill()


def _read_output(path: Path, max_chars: int = 200_000) -> str:
    """Прочитать журнал вывода процесса, взяв хвост, если он велик."""
    try:
        size = path.stat().st_size
        with path.open('rb') as fh:
            if size > max_chars:
                fh.seek(size - max_chars)
            raw = fh.read()
    except OSError:
        return ''
    text = decode_output(raw)
    return f'…(начало отброшено, всего {size} б)\n{text}' if size > max_chars else text


def _drop_file(path: Path) -> None:
    """Удалить временный файл, не падая, если его ещё держит потомок."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # уберёт уборщик при следующем старте сервера


def _elapsed_ms(started: float | None) -> int:
    """Сколько занял вызов. Пишется в журнал: без этого числа «сервер тормозит»
    приходится проверять замерами со стороны, а не читать по журналу."""
    return 0 if started is None else int((time.monotonic() - started) * 1000)


def _run_capturing(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None,
    timeout_s: int,
    runs_dir: Path,
    jobs: JobRegistry | None = None,
    budget_s: int | None = None,
    on_exit=None,
) -> tuple[int | None, str, bool, Job | None]:
    """Запустить процесс и забрать вывод ЧЕРЕЗ ФАЙЛ.

    Возвращает (код, вывод, был_таймаут, ушла_в_фон).

    Почему файл, а не `stdout=PIPE` + `communicate()` — это и была причина
    «вызов навсегда завис»: communicate() ждёт не завершения процесса, а
    ЗАКРЫТИЯ ТРУБЫ, а трубу наследуют внуки. `adb` поднимает фоновый демон,
    dev-server — воркеров, инсталлятор — службу; они переживают сам вызов и
    держат трубу открытой. Вызов висел весь таймаут (до получаса по
    exec_timeout_cap_s), хотя команда отработала за секунду, а после
    `taskkill` уходил в `communicate()` уже БЕЗ таймаута — то есть навсегда,
    вместе с занятым потоком пула. Несколько таких вызовов при нескольких
    агентах — и свободных потоков не остаётся ни у кого: сервер «зависает»
    целиком, включая policy_info.

    С файлом ждать нечего: `proc.wait()` смотрит ровно на сам процесс. Побочно
    это чинит и потерю вывода при таймауте — то, что процесс успел сбросить на
    диск, остаётся в файле, тогда как из убитой трубы не возвращалось ничего
    (в журнале такие отказы стоят с пустым выводом).

    `budget_s` (вместе с `jobs`) — бюджет ОДНОГО вызова MCP, не таймаут команды.
    Не уложились в него — процесс НЕ убивается, а уезжает в реестр фоновых задач
    и возвращается четвёртым элементом; клиент получает job_id вместо обрыва
    соединения на шлюзе. Свой `timeout_s` команда при этом не теряет: остаток
    досторожит поток внутри `JobRegistry.adopt`.
    """
    log_path = runs_dir / f'sync-{uuid.uuid4().hex[:12]}.log'
    sink = log_path.open('wb')
    started_at = time.time()
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv собран политикой, shell=False
            argv, cwd=cwd, env=env,
            stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            shell=False, creationflags=(
                getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            ),
        )
    except OSError:
        sink.close()
        _drop_file(log_path)
        raise
    promote_at = min(timeout_s, budget_s) if (jobs is not None and budget_s) else timeout_s
    timed_out = False
    try:
        rc = proc.wait(timeout=promote_at)
    except subprocess.TimeoutExpired:
        if promote_at < timeout_s:
            # Бюджет вызова кончился раньше таймаута команды: отдаём процесс в фон
            # ВМЕСТЕ с его открытым логом (закрывать sink нельзя — в него ещё пишут).
            job = jobs.adopt(
                proc, argv, cwd, log_path, sink,
                started_at=started_at, remaining_s=timeout_s - promote_at,
                on_exit=on_exit,
            )
            return None, '', False, job
        timed_out = True
        _kill_process_tree(proc)
        rc = proc.returncode
    sink.close()
    out = _read_output(log_path)
    _drop_file(log_path)
    return rc, out, timed_out, None


def _permitted_walked(policy: Policy, rel: Path) -> bool:
    """Дёшево проверить находку обхода: маски по сегментам, без realpath.

    Корень обхода уже прошёл `policy.resolve()`, поэтому от находки требуется
    только одно — чтобы ни один её сегмент не был закрыт маской. Полный
    `resolve()` (realpath + exists) на КАЖДЫЙ файл стоил 0.75 мс и был главной
    причиной, по которой обход большого корня занимал минуту; см. Policy.scan.
    """
    for part in rel.parts:
        if policy.is_ignored(part) or policy.denied_by(part):
            return False
    return True

# git читает настройки репозитория, а некоторые из них умеют запускать сторонние
# программы (core.fsmonitor, hooksPath). Репозитории здесь свои, но сервер
# рассчитан на недоверенного клиента, поэтому эти рычаги гасим явно.
_GIT_SAFE = ['git', '--no-pager', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=']

_FETCH_UA = 'dev-mcp/0.2 (+fetch_url)'
_FETCH_TIMEOUT_S = 60
_FETCH_MAX_REDIRECTS = 5


def _is_public_host(host: str) -> tuple[bool, str]:
    """True, если ВСЕ адреса хоста — публичные. SSRF-страж для fetch_url.

    На этой машине на localhost висят mcp-proxy (:8765), obsidian (:3001, с Bearer
    в .mcp.json), memory_service, панель VPN (:9443). Скачивание с loopback/приватного
    адреса дало бы недоверенному клиенту дотянуться до них — поэтому отказ.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f'имя не разрешается: {exc}'
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split('%')[0])
        except ValueError:
            return False, f'нераспознанный адрес: {addr}'
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f'{host} → {ip} (приватный/loopback адрес)'
    return True, ''


def _read_keep_eol(path: Path) -> tuple[str, str]:
    """Текст файла с переводами строк '\n' и его собственный перевод строк.

    Раньше edit_file читал read_text и писал write_text: на Windows это превращало каждый
    LF-файл в CRLF целиком — дифф на весь файл после правки одной строки (27.09.2026).
    """
    raw = path.read_bytes()
    text = raw.decode('utf-8')
    crlf = raw.count(b'\r\n')
    eol = '\r\n' if crlf and crlf * 2 >= raw.count(b'\n') else '\n'
    return text.replace('\r\n', '\n'), eol


def _write_keep_eol(path: Path, text: str, eol: str) -> None:
    body = text.replace('\r\n', '\n')
    if eol != '\n':
        body = body.replace('\n', eol)
    with path.open('w', encoding='utf-8', newline='') as fh:
        fh.write(body)


def _closest_hint(text: str, old: str) -> str:
    """Где в файле похожий фрагмент — чтобы после промаха edit_file агент не перечитывал
    весь файл (17 промахов «old_string не найден» за сутки 26.09)."""
    want = old.strip('\n').split('\n')
    first = want[0].strip()
    if not first:
        return ''
    lines = text.split('\n')
    best, score = -1, 0.0
    for i, line in enumerate(lines):
        cand = line.strip()
        if not cand:
            continue
        m = difflib.SequenceMatcher(None, first, cand, autojunk=False)
        if m.real_quick_ratio() <= score or m.quick_ratio() <= score:
            continue
        r = m.ratio()
        if r > score:
            best, score = i, r
    if best < 0 or score < 0.6:
        return ''
    shown = '\n'.join(f'{best + 1 + k}\t{line}' for k, line in enumerate(lines[best:best + len(want) + 1]))
    return f'\nБлиже всего — строка {best + 1} (сходство {score:.0%}); сверь пробелы, отступы и текст:\n{shown}'


def _missing_hint(policy: Policy, raw: str) -> str:
    """Похожие имена рядом с несуществующим путём (41 «файл не найден» за сутки 26.09)."""
    try:
        path = Path(raw)
        parent = path.parent
        while not parent.is_dir() and parent != parent.parent:
            parent = parent.parent
        policy.resolve(str(parent), for_write=False)
        names = [e.name for e in os.scandir(parent)][:2000]
    except (OSError, PolicyError, ValueError):
        return ''
    close = difflib.get_close_matches(path.name, names, n=5, cutoff=0.5)
    if not close:
        return ''
    return f' Похожие в {parent}: ' + ', '.join(close)


def _clip(text: str, reserve: int = 0) -> tuple[str, bool]:
    # reserve — место под хвост, который допишут ПОСЛЕ обрезки (вердикт
    # синтаксиса у записи): он обязан дожить до клиента при любом конверте.
    limit = max(0, MAX_RESPONSE_CHARS - reserve)
    if len(text) <= limit:
        return text, False
    return text[:limit] + f'\n\n[…обрезано, всего {len(text)} символов]', True


def build_server(
    policy: Policy,
    audit: Audit,
    runs_dir: Path | None = None,
    policy_path: str | Path | None = None,
    fleet_db: Path | None = None,
    operator_client: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> FastMCP:
    mcp = FastMCP('dev-mcp')
    jobs = JobRegistry(runs_dir or (audit.path.parent / 'runs'))
    # Реестр флота — класс data: живёт в data/fleet рядом с logs/, а не внутри него,
    # и снимается в data/backups на каждом старте (fleet/location.py).
    fleet_db = fleet_db or fleet_db_path(audit.path.parent.parent)
    def _backup_fleet() -> None:
        started = time.monotonic()
        try:
            backup = backup_fleet_db(fleet_db)
            if backup is not None:
                audit.record('fleet_backup', {'path': str(backup), 'bytes': backup.stat().st_size,
                                             'duration_s': round(time.monotonic() - started, 3)},
                             decision='allow')
        except Exception as exc:  # noqa: BLE001 — backup failure cannot stop MCP
            audit.record('fleet_backup', {'path': str(fleet_db)}, decision='deny',
                         reason=f'{type(exc).__name__}: {exc}')

    # A multi-GB online copy is maintenance, not a readiness prerequisite.
    # Copy in a bounded child: a background thread alone still competes with
    # this serving interpreter. Snapshot, kernel lease and deadline live in location.
    if fleet_db.exists():
        threading.Thread(target=_backup_fleet, name='dev-mcp-fleet-backup', daemon=True).start()
    fleet_store = Store.open(str(fleet_db))
    # Сессии владельца — только в памяти этого процесса (fleet/owner.py): пароль задаёт
    # владелец локально, fleet.cmd owner-password; хеш лежит в data/ под маской *secret*.
    owner_sessions = OwnerSessions(secret_file=secret_path(audit.path.parent.parent))
    fleet_ctx = FleetContext(store=fleet_store, policy=policy, owner=owner_sessions)

    def _claim_target(path: str, agent: str, *, write: bool = False):
        """Resolve a trusted claim checkout *before* policy and fleet guard.

        Both coordinates are checked: the original by the usual source owns
        guard, and the checkout by the filesystem policy and claim adapter.
        """
        from dev_mcp.fleet.claim_context import ClaimRoutingError
        try:
            actual, source, context = fleet_ctx.route_claim_path(
                agent, path, read_only=not write,
            )
            # A worktree never acquires broader policy rights than its source.
            try:
                source = policy.resolve(str(source), for_write=write)
                actual = policy.resolve(str(actual), for_write=write)
            except PolicyError:
                if write:
                    raise
                # Only a live claim of the project may use a reference outside
                # regular roots. The server registry, never inputs.md, grants it.
                external = intent_inputs.resolve_granted_input(
                    fleet_store, policy, agent, path,
                )
                return external, external, context
            return actual, source, context
        except (ClaimRoutingError, OSError, ValueError) as exc:
            raise PolicyError(f'claim checkout: {exc}') from exc

    def _sealed_review_inspect(agent: str, path: str, operation: str, **options):
        """Review reads of the source project must use its pinned submission.

        Never use the author's mutable checkout, the reviewer's base or trunk as
        fallback when a trusted reviewer inspection is missing or rejects.
        """
        if not agent:
            return None
        from dev_mcp.fleet.review import _review_target
        from dev_mcp.fleet import claim_review_bite
        with fleet_store.read() as tx:
            review = tx.one(
                "SELECT c.id,t.project,t.tz,t.body,p.root FROM claims c "
                "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                "JOIN projects p ON p.name=t.project "
                "WHERE c.worker_id=? AND c.state='active' AND t.kind='review' "
                "ORDER BY c.id DESC LIMIT 1", (agent,),
            )
            if review is None:
                return None
            project, tz = _review_target(str(review['tz']), str(review['body']),
                                          str(review['project']))
            source = tx.one(
                "SELECT c.id,p.root FROM claims c JOIN tasks t "
                "ON t.project=c.project AND t.tz=c.tz "
                "JOIN projects p ON p.name=c.project "
                "WHERE c.project=? AND c.tz=? AND t.state='submitted' "
                "ORDER BY c.id DESC LIMIT 1", (project, tz),
            )
        if source is None:
            raise PolicyError('FLEET_SEALED_REVIEW: submitted source missing')
        try:
            _, source_context = claim_review_bite._claim(fleet_store,int(source['id']))
        except Exception as exc:
            raise PolicyError(f'FLEET_SEALED_REVIEW: {exc}') from exc
        if source_context.mode == 'shared':
            return None  # ordinary guarded shared review; not an isolated Git seal
        root = Path(str(source['root'])).resolve(strict=True)
        raw = Path(path or '.')
        if '..' in raw.parts:
            raise PolicyError('FLEET_SEALED_REVIEW: parent traversal denied')
        candidate = raw if raw.is_absolute() else root / raw
        foreign = root.parent / '.fleet-trees' / root.name
        physical = candidate.resolve(strict=False)
        if candidate == foreign or foreign in candidate.parents or (
                physical == foreign or foreign in physical.parents):
            raise PolicyError('FLEET_SEALED_REVIEW: physical claim paths forbidden')
        if physical != root and root not in physical.parents:
            if raw.is_absolute():
                return None  # A genuinely different policy root is not the review source.
            raise PolicyError('FLEET_SEALED_REVIEW: source path escaped project')
        rel = physical.relative_to(root).as_posix() or '.'
        inspector = getattr(claim_review_bite, 'reviewer_inspect', None)
        if not callable(inspector):
            raise PolicyError('FLEET_SEALED_REVIEW: trusted inspector unavailable')
        try:
            result = inspector(fleet_store, int(source['id']), agent,
                               operation, rel, **options)
        except Exception as exc:
            raise PolicyError(f'FLEET_SEALED_REVIEW: {exc}') from exc
        if result is None or (isinstance(result, dict) and (
                result.get('ok') is False or result.get('error'))):
            raise PolicyError('FLEET_SEALED_REVIEW: inspector refused source')
        return (root, rel, result)

    def _sealed_values(result, *keys):
        if isinstance(result, dict):
            for key in keys:
                if key in result:
                    return result[key]
            raise PolicyError('FLEET_SEALED_REVIEW: invalid evidence envelope')
        return result

    def _sealed_entry_path(value: str) -> Path:
        if not isinstance(value, str) or not value or '\\x00' in value:
            raise PolicyError('FLEET_SEALED_REVIEW: invalid result path')
        candidate = Path(value.replace('\\', '/'))
        if candidate.is_absolute() or '..' in candidate.parts or candidate == Path('.'):
            raise PolicyError('FLEET_SEALED_REVIEW: invalid result path')
        return candidate

    def _active_blind_claims() -> list[dict[str, Any]]:
        with fleet_store.read() as tx:
            return [dict(row) for row in tx.query(
                "SELECT c.id,c.worker_id,c.tz,p.root,t.kind,t.owns,t.body,"
                "t.project,p.root AS project_root FROM claims c "
                "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                "JOIN projects p ON p.name=c.project "
                "WHERE c.state='active' AND c.project='Mill Works' "
                "AND (t.tz LIKE 'TZ-MR-batch-%' OR "
                "(t.kind='fix' AND (t.tz LIKE 'fix:request:%' "
                "OR t.tz LIKE 'fix:TZ-MR-batch-%')))")]

    def _claim_attempt_id(row: dict[str, Any]) -> str | None:
        return (f"claim-{int(row['id'])}"
                if row.get('kind') == 'fix'
                and str(row.get('tz') or '').startswith('fix:TZ-MR-batch-')
                else None)

    def _claim_seal(row: dict[str, Any], batch: str) -> str | None:
        """A matching server event, tied to this *active claim*, not its worker alone."""
        with fleet_store.read() as tx:
            events = tx.query(
                "SELECT payload FROM events WHERE kind='blind_seal' "
                "AND project='Mill Works' AND worker_id=? AND tz=? "
                "ORDER BY id DESC LIMIT 10", (row['worker_id'], row['tz']))
        for event in events:
            try:
                item = json.loads(event['payload'] or '{}')
            except (ValueError, TypeError):
                continue
            if (isinstance(item, dict) and item.get('claim_id') == int(row['id'])
                    and item.get('batch_id') == batch
                    and item.get('attempt_id') == _claim_attempt_id(row)):
                digest = item.get('seal_sha256')
                return (digest if isinstance(digest, str)
                        and re.fullmatch(r'[0-9a-f]{64}', digest) else None)
        return None

    def _blind_path_claim(path: Path, row: dict[str, Any]) -> str | None:
        from dev_mcp.fleet.queue import _blind_batch_for_task
        key = _blind_batch_for_task(row)
        if key is None:
            return None
        batch = 'batch-' + key
        root = str(row['root']).replace('\\', '/').casefold().rstrip('/')
        shown = str(path).replace('\\', '/').casefold()
        review_prefix = root + '/data/reviews/' + batch
        manifest_prefix = root + '/data/batches/out/' + batch
        if (shown == review_prefix or shown.startswith(review_prefix + '/') or
                shown == manifest_prefix or shown.startswith(manifest_prefix + '/')):
            return batch
        return None

    def _blind_read_gate(path: Path) -> bool:
        """Protect candidate paths for ANY active case-only reviewer.

        Do not trust an optional client-supplied agent as an authentication
        boundary: another agent or a missing agent cannot bypass this MCP gate.
        Executable ``run`` is outside this read-path boundary (no OS sandbox).
        """
        from dev_mcp.fleet.queue import blind_read_denial

        shown = str(path)
        normalized = shown.replace('\\', '/').casefold()
        if not any(segment in normalized for segment in
                   ('/data/batches/out/', '/data/reviews/')):
            return False
        for row in _active_blind_claims():
            batch = _blind_path_claim(path, row)
            if batch is None:
                continue
            if blind_read_denial(fleet_store, str(row['worker_id']), shown) is not None:
                return True
            # A seal is valid only while every blind.json still has the exact
            # bytes it had before the reviewer saw the candidate. The older
            # briefing check authenticates the event, not these current bytes.
            digest = _claim_seal(row, batch)
            if digest is None or _blind_snapshot_sha(
                Path(row['root']), batch, _claim_attempt_id(row)
            ) != digest:
                return True
        return False

    def _require_blind_read(path: Path) -> None:
        if _blind_read_gate(path):
            raise PolicyError('FLEET_BLIND_SEAL_REQUIRED: candidate/review доступен только после server seal')

    def _require_blind_write(path: Path) -> None:
        """Keep previous attempts immutable, including their verdict and sweep."""
        shown = str(path).replace('\\', '/').casefold()
        for row in _active_blind_claims():
            batch = _blind_path_claim(path, row)
            if not batch:
                continue
            attempt_id = _claim_attempt_id(row)
            if attempt_id:
                review_root = str(Path(row['root']) / 'data' / 'reviews' / batch)
                prefix = review_root.replace('\\', '/').casefold().rstrip('/')
                current = prefix + '/attempts/' + attempt_id
                if (shown == prefix or shown.startswith(prefix + '/')) and not (
                    shown == current or shown.startswith(current + '/')
                ):
                    raise PolicyError('FLEET_BLIND_OLD_ATTEMPT: prior blind/verdict immutable')
            if path.name.casefold() == 'blind.json' and _claim_seal(row, batch) is not None:
                raise PolicyError(
                    'FLEET_BLIND_SEALED: blind.json immutable after seal; '
                    'a replacement needs a new independent review claim')
    scans = _Gate(_MAX_CONCURRENT_SCANS, 'обходов дерева (search_files/list_dir)')
    runs = _Gate(_MAX_CONCURRENT_RUNS, 'запусков процессов (run/run_task/fetch_url)')
    # Журналы синхронного `run` от прошлой жизни процесса: файл мог быть занят
    # потомком в момент удаления, поэтому подчищаем на старте, а не только после.
    for stale in jobs.runs_dir.glob('sync-*.log'):
        _drop_file(stale)
    requests_path = audit.path.parent / 'requests.jsonl'
    requests_history_path = audit.path.parent / 'requests.history.jsonl'

    def _request_rows(*paths: Path) -> list[dict]:
        rows: list[dict] = []
        for path in paths:
            if not path.exists():
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def _write_request_rows(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        tmp.write_text(body + ('\n' if body else ''), encoding='utf-8')
        os.replace(tmp, path)

    def _drain_legacy_requests() -> int:
        """Resolve pre-auto-triage request_note rows without involving the owner."""
        rows = _request_rows(requests_path)
        if not rows:
            return 0
        for row in rows:
            if not row.get('id'):
                row['id'] = uuid.uuid4().hex[:8]
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        count = request_triage.triage_pending(rows, policy, stamp=stamp)
        resolved = [row for row in rows if row.get('decision')]
        active = [row for row in rows if not row.get('decision')]
        if resolved:
            history = _request_rows(requests_history_path)
            seen = {str(row.get('id')) for row in history if row.get('id')}
            for row in resolved:
                req_id = str(row.get('id') or '')
                if req_id and req_id in seen:
                    continue
                history.append(row)
                if req_id:
                    seen.add(req_id)
            _write_request_rows(requests_history_path, history)
        _write_request_rows(requests_path, active)
        if count:
            audit.record(
                'request_triage', {'count': count}, decision='allow',
                reason='legacy pending requests drained automatically',
            )
        return count

    try:
        _drain_legacy_requests()
    except Exception as exc:  # noqa: BLE001 — legacy queue must never block server startup
        audit.record(
            'request_triage', {}, decision='deny',
            reason=f'legacy drain failed: {type(exc).__name__}: {exc}',
        )

    def _fleet_audit_identity(args: dict, fleet_decision=None) -> tuple[str, bool, bool]:
        explicit = str(args.get('agent') or '')
        decided = str(getattr(fleet_decision, 'worker_id', '') or '')
        agent = explicit or decided
        inferred = bool(decided and not explicit)
        degraded = bool(getattr(fleet_decision, 'degraded', False))
        return agent, inferred, degraded

    def _fail(
        tool: str,
        args: dict,
        exc: Exception,
        started: float | None = None,
        *,
        fleet_decision=None,
    ) -> str:
        agent, inferred, degraded = _fleet_audit_identity(args, fleet_decision)
        audit.record(
            tool, args, decision='deny', reason=str(exc),
            dur_ms=_elapsed_ms(started), agent=agent,
            inferred=inferred, degraded=degraded,
        )
        payload = f'Отказано: {exc}'
        return fleet_ctx.with_envelope(agent, payload)

    def _ok(
        tool: str,
        args: dict,
        payload: str,
        started: float | None = None,
        *,
        fleet_decision=None,
        tail: str | None = None,
    ) -> str:
        agent, inferred, degraded = _fleet_audit_identity(args, fleet_decision)
        combined = fleet_ctx.with_envelope(agent, payload)
        # tail — последняя строка ответа, которую обрезка не трогает (вердикт
        # syntax_check после записи). Место под неё и под пометку об обрезке
        # резервируется заранее.
        reserve = len(tail) + 80 if tail else 0
        clipped, was_clipped = _clip(combined, reserve)
        if tail:
            clipped = f'{clipped}\n{tail}'
            combined = f'{combined}\n{tail}'
        fleet_code = str(getattr(fleet_decision, 'code', '') or '')
        fleet_reason = str(getattr(fleet_decision, 'reason', '') or '')
        reasons = []
        if fleet_code or fleet_reason:
            reasons.append((f'{fleet_code}: ' if fleet_code else '') + fleet_reason)
        if was_clipped:
            reasons.append('clipped')
        audit.record(
            tool,
            args,
            decision='allow',
            reason='; '.join(reasons),
            result_bytes=len(combined),
            dur_ms=_elapsed_ms(started),
            agent=agent,
            inferred=inferred,
            degraded=degraded,
        )
        return clipped

    fleet_ctx.ok = lambda tool, args, payload: _ok(tool, args, payload)

    def _doc_permitted(doc: Path) -> bool:
        try:
            policy.resolve(str(doc), for_write=False)
            return True
        except PolicyError:
            return False

    context_docs = ContextDocs(lambda: list(policy.roots), _doc_permitted)

    def _with_folder_docs(agent: str, target: Path, payload: str) -> str:
        # Запас 1500 — под конверт флота, который _ok допишет сверху.
        room = MAX_RESPONSE_CHARS - len(payload) - 1500
        try:
            block = context_docs.for_touch(agent, target, room)
        except Exception:  # инструкции папки — подсказка, не повод уронить чтение
            return payload
        return f'{block}\n{payload}' if block else payload
    fleet_ctx.fail = lambda tool, args, exc: _fail(tool, args, exc)

    # ---------- ориентация ----------

    @mcp.tool(
        name='policy_info',
        description=(
            'Показывает границы этого сервера: в каких корнях разрешено работать, '
            'разрешена ли запись, какие имена файлов закрыты, какие задачи доступны. '
            'Вызови первым — это дешевле, чем упираться в отказы наугад.'
        ),
    )
    def policy_info() -> str:
        described = policy.describe()
        lines = ['Разрешённые корни:']
        lines += [f'  {r}' for r in described['roots']]
        if described['allow_write']:
            lines.append('Запись: разрешена во всех корнях')
        elif described['write_roots']:
            lines.append('Запись: ТОЛЬКО в этих каталогах (остальное — чтение):')
            lines += [f'  {r}' for r in described['write_roots']]
        else:
            lines.append('Запись: ЗАПРЕЩЕНА (read-only)')
        lines.append(f'Закрытые имена: {", ".join(described["deny_patterns"])}')
        if described['exec_roots']:
            lines.append('Исполнение (run): РАЗРЕШЕНО в этих каталогах:')
            lines += [f'  {r}' for r in described['exec_roots']]
            lines.append(f'  бинари: {", ".join(described["exec_allowed_bin"])}')
            if described['exec_bin_missing']:
                lines.append(f'  (нет в PATH сервера: {", ".join(described["exec_bin_missing"])})')
            if described.get('exec_args_unrestricted'):
                lines.append(
                    '  без проверки путей в аргументах (абсолютные пути, "..") : '
                    + ', '.join(described['exec_args_unrestricted'])
                )
        else:
            lines.append('Исполнение (run): ЗАПРЕЩЕНО (exec_roots пуст)')
        lines.append(
            f'Скачивание из сети (fetch_url): {"разрешено" if described["allow_network_fetch"] else "запрещено"}'
        )
        if described['tasks']:
            lines.append('Задачи (run_task):')
            for name, spec in described['tasks'].items():
                head = f'  {name} — {spec["description"] or " ".join(spec["argv"])}'
                if not spec['runnable']:
                    head += f'   [НЕ ЗАПУСТИТСЯ: {spec["blocked_reason"]} — оставь заявку request_note]'
                lines.append(head)
        else:
            lines.append('Задач нет.')
        lines.append(
            'Не хватает права/бинаря/папки — сначала используй уже разрешённый безопасный путь или named run_task. '
            'request_note разбирается сервером автоматически: если запрос безопасно решаем — он превращается в обычную fleet-задачу/подсказку; '
            'если расширение периметра не допускается — получишь отказ и альтернативу. Не жди ручного review владельца и не повторяй одну заявку циклом.'
        )
        return _ok('policy_info', {}, '\n'.join(lines))

    # ---------- чтение ----------

    @mcp.tool(
        name='fleet_blind_seal',
        description=(
            'Для активного Mill Works/TZ-MR ревью подтверждает свежие blind.json '
            'по серверным write-событиям, форме и SHA. Только после успеха '
            'разрешается читать MANIFEST. agent — worker_id текущего claim.'
        ),
    )
    def fleet_blind_seal(agent: str) -> str:
        t0 = time.monotonic()
        args = {'agent': agent}
        try:
            if not agent:
                raise PolicyError('FLEET_BLIND: требуется активный worker_id')
            with fleet_store.read() as tx:
                claim = tx.one(
                    "SELECT c.id,c.started_at,c.project,c.tz,p.root,t.body,t.kind,t.owns,"
                    "p.root AS project_root FROM claims c "
                    "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                    "JOIN projects p ON p.name=c.project "
                    "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
                    (agent,),
                )
                if claim is None or claim['project'] != 'Mill Works':
                    raise PolicyError('FLEET_BLIND: нет активного review claim')
                from dev_mcp.fleet.queue import _blind_batch_for_task
                batch_id = _blind_batch_for_task(dict(claim))
                if batch_id is None:
                    raise PolicyError('FLEET_BLIND: задача не batch review')
                marker = str(claim['body'] or '').splitlines()[0].strip()
                expected = '[fleet-blind] '
                if not marker.startswith(expected):
                    raise PolicyError('FLEET_BLIND: отсутствует sanitized server handoff')
                try:
                    marker_data = json.loads(marker[len(expected):])
                except ValueError as exc:
                    raise PolicyError('FLEET_BLIND: invalid handoff marker') from exc
                if marker_data != {'batch_id': 'batch-' + batch_id, 'phase': 'case_only'}:
                    raise PolicyError('FLEET_BLIND: mismatched handoff marker')
                # Eligibility was checked by _blind_batch_exposed BEFORE take.
                # Calling it on the current claim would always find its own MR
                # take/claim and incorrectly refuse the very first seal.
                events = tx.query(
                    "SELECT kind,path_norm,ts FROM events WHERE worker_id=? AND "
                    "ts>=? AND kind IN ('write','read') ORDER BY id",
                    (agent, str(claim['started_at'])),
                )
            batch = 'batch-' + batch_id
            root = Path(str(claim['root'])).resolve()
            manifest_file = root / 'data' / 'batches' / 'out' / batch / 'MANIFEST.md'
            if manifest_file.is_symlink() or not manifest_file.is_file():
                raise PolicyError('FLEET_BLIND: отсутствует безопасный MANIFEST')
            raw = manifest_file.read_bytes()
            if not raw or len(raw) > 1024 * 1024:
                raise PolicyError('FLEET_BLIND: MANIFEST вне лимита')
            manifest_text = raw.decode('utf-8')
            start, end = '<!-- MILL_MANIFEST_JSON', 'MILL_MANIFEST_JSON -->'
            if manifest_text.count(start) != 1 or manifest_text.count(end) != 1:
                raise PolicyError('FLEET_BLIND: нет единственной секции MANIFEST')
            payload = json.loads(manifest_text.split(start, 1)[1].split(end, 1)[0])
            entries = payload.get('entries') if isinstance(payload, dict) else None
            if not isinstance(entries, dict) or not entries:
                raise PolicyError('FLEET_BLIND: пустой/некорректный MANIFEST')
            candidates = sorted(
                cid for cid, entry in entries.items()
                if isinstance(cid, str) and isinstance(entry, dict)
                and entry.get('outcome') == 'candidate'
            )
            if not candidates:
                raise PolicyError('FLEET_BLIND: candidates отсутствуют')
            attempt_id = _claim_attempt_id(dict(claim))
            review_dir = root / 'data' / 'reviews' / batch
            if attempt_id is not None:
                review_dir = review_dir / 'attempts' / attempt_id
            if review_dir.is_symlink() or not review_dir.is_dir():
                raise PolicyError('FLEET_BLIND: отсутствует безопасный review directory')
            from dev_mcp.fleet.model import norm_path
            written = {
                str(event['path_norm']) for event in events if event['kind'] == 'write'
            }
            exposed = (
                '/data/batches/out/' + batch + '/manifest.md',
                '/data/reviews/' + batch + '/'
            )
            if any(
                any(term in str(event['path_norm'] or '').replace('\\', '/').casefold()
                    for term in exposed)
                for event in events if event['kind'] == 'read'
            ):
                raise PolicyError('FLEET_BLIND: candidate был прочитан до server seal')
            digests: list[tuple[str, str]] = []
            for cid in candidates:
                if re.fullmatch(r'[A-Za-z0-9._-]{1,120}', cid) and cid not in ('.', '..'):
                    safe = cid
                else:
                    safe = 'case-' + hashlib.sha256(cid.encode('utf-8')).hexdigest()[:24]
                case_dir = review_dir / safe
                if case_dir.is_symlink():
                    raise PolicyError('FLEET_BLIND: unsafe case directory')
                blind = case_dir / 'blind.json'
                # 15–17.10: агент записал один общий <batch>/blind.json, получил «нет свежего
                # write-event» и решил, что виноват коннектор; цепочка Mill Works стояла 2 суток.
                # Ошибка называет ожидаемый путь и что именно не так.
                layout = (' Нужен отдельный файл на каждый Case: ' +
                          blind.relative_to(root).as_posix() +
                          ' (общий blind.json партии не принимается).')
                if blind.is_symlink() or not blind.is_file() or blind.stat().st_size > 128 * 1024:
                    raise PolicyError('FLEET_BLIND: blind.json отсутствует/опасен.' + layout)
                if norm_path(str(blind)) not in written:
                    raise PolicyError('FLEET_BLIND: этот файл не записан в текущем claim '
                                      '(лежит старый от другой попытки).' + layout)
                if datetime.fromtimestamp(blind.stat().st_mtime, timezone.utc) < datetime.fromisoformat(
                    str(claim['started_at']).replace('Z', '+00:00')
                ):
                    raise PolicyError('FLEET_BLIND: blind.json старше claim')
                content = blind.read_bytes()
                item = json.loads(content)
                questions = item.get('hidden_queries') if isinstance(item, dict) else None
                if (not isinstance(item, dict) or
                    set(item) != {'case_id','independent_finding','alternative','hidden_queries'} or
                    item.get('case_id') != cid or
                    any(not isinstance(item.get(key), str) or not item[key].strip()
                        for key in ('independent_finding','alternative')) or
                    not isinstance(questions, list) or len(questions) != 3 or
                    any(not isinstance(q, dict) or set(q) != {'query','negative'} or
                        not isinstance(q['query'], str) or not q['query'].strip() or
                        type(q['negative']) is not bool for q in questions) or
                    sum(q['negative'] for q in questions) != 1):
                    raise PolicyError('FLEET_BLIND: неверная схема трёх hidden replay')
                digests.append((cid, hashlib.sha256(content).hexdigest()))
            digest = hashlib.sha256(
                json.dumps(digests, ensure_ascii=False).encode('utf-8')
            ).hexdigest()
            with fleet_store.tx() as tx:
                current = tx.one(
                    "SELECT id FROM claims WHERE worker_id=? AND project=? AND tz=? "
                    "AND state='active' ORDER BY id DESC LIMIT 1",
                    (agent, 'Mill Works', str(claim['tz'])),
                )
                if current is None or int(current['id']) != int(claim['id']):
                    raise PolicyError('FLEET_BLIND: claim изменился')
                tx.record_event(
                    kind='blind_seal', worker_id=agent, project='Mill Works',
                    tz=str(claim['tz']), payload={
                        'claim_id': int(claim['id']), 'batch_id': batch,
                        'attempt_id': attempt_id,
                        'seal_sha256': digest,
                    },
                )
            return _ok('fleet_blind_seal', args,
                       f'blind_seal: {batch}; attempt={attempt_id or "initial"}; sha256={digest}')
        except (PolicyError, OSError, ValueError, TypeError, UnicodeError) as exc:
            return _fail('fleet_blind_seal', args, PolicyError(str(exc)), t0)

    @mcp.tool(
        name='list_dir',
        description=(
            'Список содержимого каталога внутри разрешённых корней. '
            'pattern — glob-фильтр по имени (например "*.py"). '
            'recursive=true обходит подкаталоги. agent — необязательный worker_id флота.'
        ),
    )
    def list_dir(path: str, pattern: str = '*', recursive: bool = False, agent: str = '') -> str:
        t0 = time.monotonic()
        args = {'path': path, 'pattern': pattern, 'recursive': recursive, 'agent': agent}
        fleet_ctx.touch(agent)
        try:
            sealed = _sealed_review_inspect(agent, path, 'list_dir', pattern=pattern)
            if sealed is not None:
                if recursive:
                    raise PolicyError('FLEET_SEALED_REVIEW: recursive list is unsupported')
                root, rel, evidence = sealed
                records = _sealed_values(evidence, 'entries', 'items', 'result', 'data')
                if not isinstance(records, list):
                    raise PolicyError('FLEET_SEALED_REVIEW: invalid directory result')
                base = Path(rel)
                entries = []
                for record in records[:policy.max_list_entries + 1]:
                    if isinstance(record, str):
                        name, is_dir = record.rstrip('/'), record.endswith('/')
                        item = _sealed_entry_path(name)
                    elif isinstance(record, dict):
                        item = _sealed_entry_path(record.get('path'))
                        typed = record.get('type')
                        flag = record.get('is_dir')
                        if typed is not None and typed not in ('file', 'dir'):
                            raise PolicyError('FLEET_SEALED_REVIEW: invalid entry type')
                        if flag is not None and type(flag) is not bool:
                            raise PolicyError('FLEET_SEALED_REVIEW: invalid is_dir flag')
                        if typed is None and flag is None:
                            raise PolicyError('FLEET_SEALED_REVIEW: missing entry type')
                        if typed is not None and flag is not None and (typed == 'dir') != flag:
                            raise PolicyError('FLEET_SEALED_REVIEW: conflicting entry types')
                        is_dir = (typed == 'dir') if typed is not None else flag
                    else:
                        raise PolicyError('FLEET_SEALED_REVIEW: invalid entry')
                    try:
                        local = item.relative_to(base) if rel != '.' else item
                    except ValueError as exc:
                        raise PolicyError('FLEET_SEALED_REVIEW: entry outside directory') from exc
                    if len(local.parts) != 1 or not fnmatch.fnmatch(local.as_posix(), pattern):
                        continue
                    entries.append(f"{local.as_posix()}{'/' if is_dir else ''}")
                if len(entries) > policy.max_list_entries:
                    entries = entries[:policy.max_list_entries] + ['[…показаны первые записи]']
                fleet_ctx.record_event('read', agent, tool='list_dir', path=str(root / rel))
                return _ok('list_dir', args, f"{root / rel}\n\n" +
                           ('\n'.join(entries) if entries else '(пусто)'), t0)
            target, _, _ = _claim_target(path, agent)
            _require_blind_read(target)
            if not target.is_dir():
                raise PolicyError('это не каталог')
            # A glob is a filter within the requested directory, not a second
            # path selector.  `Path.glob('../secret*')` walks outside it even
            # when the original target passed a claim-scoped input grant.
            normalized_pattern = pattern.replace('\\', '/')
            if (ntpath.isabs(pattern) or Path(pattern).is_absolute()
                    or any(part == '..' for part in normalized_pattern.split('/'))):
                raise PolicyError('pattern должен оставаться внутри указанного каталога')
        except PolicyError as exc:
            return _fail('list_dir', args, exc, t0)

        entries: list[str] = []
        truncated_by_time = False
        if recursive:
            # Policy.scan вместо os.walk + resolve() на каждую находку: он режет
            # .venv/node_modules/.git ДО спуска и проверяет маски по имени, без
            # realpath на файл. Почему это принципиально — см. Policy.scan.
            try:
                with scans.hold():
                    deadline = time.monotonic() + _WALK_TIME_BUDGET_S
                    try:
                        for item in policy.scan(target, deadline=deadline, want_dirs=True):
                            if _blind_read_gate(item.path):
                                continue
                            if not fnmatch.fnmatch(item.name, pattern):
                                continue
                            rel = item.path.relative_to(target)
                            entries.append(f'{rel}/' if item.is_dir else f'{rel}  ({item.size} б)')
                            if len(entries) >= policy.max_list_entries:
                                entries.append(f'[…показаны первые {policy.max_list_entries}]')
                                break
                    except ScanTimeout:
                        truncated_by_time = True
            except PolicyError as exc:  # шлюз занят
                return _fail('list_dir', args, exc, t0)
        else:
            # Глубина 1, но pattern может быть и путевым ('sub/*.py') — поэтому
            # здесь по-прежнему glob, а граница проверяется по сегментам.
            for item in sorted(target.glob(pattern)):
                if _blind_read_gate(item):
                    continue
                rel = item.relative_to(target)
                if not _permitted_walked(policy, rel):
                    continue
                try:
                    # Check the whole candidate, not just its last component:
                    # `alias/file` may cross a directory junction even though
                    # `file` itself is not a symlink.  External intent inputs
                    # must remain inside this requested subtree and grant.
                    if not item.resolve(strict=False).is_relative_to(target):
                        continue
                    if policy._owning_root(target) is not None:
                        policy.resolve(str(item), for_write=False)
                    else:
                        intent_inputs.resolve_granted_input(
                            fleet_store, policy, agent, str(item),
                        )
                    is_dir = item.is_dir()
                    size = 0 if is_dir else item.stat().st_size
                except (PolicyError, OSError, ValueError):
                    continue
                entries.append(f'{rel}/' if is_dir else f'{rel}  ({size} б)')
                if len(entries) >= policy.max_list_entries:
                    entries.append(f'[…показаны первые {policy.max_list_entries}]')
                    break
        if truncated_by_time:
            entries.append(f'[…обход прерван по времени ({_WALK_TIME_BUDGET_S:.0f} с) — сузь path/pattern]')

        body = '\n'.join(entries) if entries else '(пусто)'
        fleet_ctx.record_event('read', agent, tool='list_dir', path=str(target))
        return _ok('list_dir', args, _with_folder_docs(agent, target, f'{target}\n\n{body}'), t0)

    @mcp.tool(
        name='read_file',
        description=(
            'Читает текстовый файл внутри разрешённых корней. '
            'offset — номер первой строки (с 1), limit — сколько строк вернуть. '
            'Для больших файлов строки выбираются потоково; объём ответа ограничен. '
            'Ответ нумерован по строкам, чтобы на них можно было ссылаться. '
            'agent — необязательный worker_id флота для события read.'
        ),
    )
    def read_file(path: str, offset: int = 1, limit: int = 400, agent: str = '') -> str:
        t0 = time.monotonic()
        args = {'path': path, 'offset': offset, 'limit': limit, 'agent': agent}
        fleet_ctx.touch(agent)
        try:
            sealed = _sealed_review_inspect(agent, path, 'read_file')
            if sealed is not None:
                root, rel, evidence = sealed
                content = _sealed_values(evidence, 'content', 'text', 'result', 'data')
                if not isinstance(content, str):
                    raise PolicyError('FLEET_SEALED_REVIEW: invalid file result')
                lines = content.splitlines()
                start, count = max(1, offset), max(1, limit)
                chunks = [f'{i}\t{line}' for i, line in
                          enumerate(lines[start - 1:start - 1 + count], start)]
                preview = '\n'.join(chunks)
                if len(preview.encode('utf-8')) > min(8000, policy.max_read_bytes):
                    preview = preview.encode('utf-8')[:min(7800, policy.max_read_bytes)].decode(
                        'utf-8', errors='ignore') + '\n[…ответ ограничен по байтам]'
                fleet_ctx.record_event('read', agent, tool='read_file', path=str(root / rel))
                return _ok('read_file', args,
                           f'{root / rel}  (строк всего: {len(lines)}, показаны {start}–'
                           f'{start + len(chunks) - 1})\n\n{preview}', t0)
            target, _, _ = _claim_target(path, agent)
            _require_blind_read(target)
            if not target.is_file():
                raise PolicyError('файл не найден' + ('' if target.exists() else _missing_hint(policy, str(target))))
        except PolicyError as exc:
            return _fail('read_file', args, exc, t0)

        start = max(1, offset)
        count = max(1, limit)
        # The outer MCP formatter clips at MAX_RESPONSE_CHARS (12k). Leave
        # room for file headers, folder instructions and a truncation notice.
        byte_budget = min(8_000, policy.max_read_bytes)
        used_bytes = 0
        chunk: list[str] = []
        total_lines = 0
        truncated = False
        try:
            # A Case may be >400 KiB because its JSON envelope is one very long
            # line. Neither read_text() nor a file-size refusal is appropriate:
            # paginate on disk, and bound the MCP response independently.
            with target.open('r', encoding='utf-8', errors='replace') as handle:
                for number, raw_line in enumerate(handle, 1):
                    total_lines = number
                    if number < start or number >= start + count or truncated:
                        continue
                    prefix = f'{number}\t'
                    space = byte_budget - used_bytes - len(prefix.encode('utf-8')) - 1
                    if space <= 0:
                        truncated = True
                        continue
                    line = raw_line.rstrip('\r\n').encode('utf-8')
                    if len(line) > space:
                        visible = line[:max(0, space - 80)].decode('utf-8', errors='ignore')
                        chunk.append(f'{prefix}{visible} […строка обрезана по лимиту ответа]')
                        truncated = True
                    else:
                        chunk.append(prefix + line.decode('utf-8'))
                    used_bytes += len(chunk[-1].encode('utf-8')) + 1
        except OSError as exc:
            return _fail('read_file', args, PolicyError(f'не читается: {exc}'), t0)

        numbered = '\n'.join(chunk)
        header = f'{target}  (строк всего: {total_lines}, показаны {start}–{start + len(chunk) - 1})'
        if truncated:
            numbered += '\n[…ответ ограничен по байтам; сузь offset/limit]'
        fleet_ctx.record_event('read', agent, tool='read_file', path=str(target))
        if start == 1:
            context_docs.mark_read(agent, target)
        return _ok('read_file', args, _with_folder_docs(agent, target, f'{header}\n\n{numbered}'), t0)

    @mcp.tool(
        name='search_files',
        description=(
            'Ищет текст в файлах внутри разрешённых корней и возвращает совпадения '
            'с путём и номером строки. query — подстрока, либо регулярное выражение '
            'при regex=true. path может быть файлом или каталогом; '
            'glob сужает поиск по имени файла ("*.py"). '
            'agent — необязательный worker_id флота для события read.'
        ),
    )
    def search_files(
        query: str,
        path: str = '',
        glob: str = '*',
        regex: bool = False,
        max_results: int = 60,
        agent: str = '',
    ) -> str:
        t0 = time.monotonic()
        args = {'query': query, 'path': path, 'glob': glob, 'regex': regex, 'agent': agent}
        fleet_ctx.touch(agent)
        if not query:
            return _fail('search_files', args, PolicyError('пустой запрос'), t0)
        try:
            sealed = _sealed_review_inspect(
                agent, path or '.', 'search_files', query=query,
                pattern=glob, max_results=min(max_results, policy.max_search_results))
            if sealed is not None:
                if regex:
                    raise PolicyError('FLEET_SEALED_REVIEW: regex search is unsupported')
                root, rel, evidence = sealed
                rows = _sealed_values(evidence, 'matches', 'hits', 'result', 'data')
                if not isinstance(rows, list):
                    raise PolicyError('FLEET_SEALED_REVIEW: invalid search result')
                hits = []
                for entry in rows[:min(max_results, policy.max_search_results)]:
                    if isinstance(entry, str):
                        if entry.startswith(('/', '\\')) or '..' in Path(entry.split(':', 1)[0]).parts:
                            raise PolicyError('FLEET_SEALED_REVIEW: invalid search hit')
                        hits.append(entry)
                    elif isinstance(entry, dict):
                        item = _sealed_entry_path(entry.get('path'))
                        line = entry.get('line', entry.get('line_no'))
                        content = entry.get('text', entry.get('content'))
                        if type(line) is not int or line < 1 or not isinstance(content, str):
                            raise PolicyError('FLEET_SEALED_REVIEW: invalid hit fields')
                        hits.append(f'{root / item}:{line}: {content}')
                    else:
                        raise PolicyError('FLEET_SEALED_REVIEW: invalid search hit')
                fleet_ctx.record_event('read', agent, tool='search_files',
                                       path=str(root / rel), payload={'query': query})
                return _ok('search_files', args, '\n'.join(hits) if hits else 'Совпадений нет.', t0)
            if path:
                roots = [_claim_target(path, agent)[0]]
            else:
                with fleet_ctx.store.read() as tx:
                    held = tx.one(
                        "SELECT p.root FROM claims c JOIN projects p ON p.name=c.project "
                        "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
                        (agent,),
                    ) if agent else None
                if held is not None:
                    routed, _, context = _claim_target(str(held["root"]), agent)
                    roots = [routed] if context is not None and context.mode == 'isolated' else list(policy.roots)
                else:
                    roots = list(policy.roots)
            for root in roots:
                _require_blind_read(root)
        except PolicyError as exc:
            return _fail('search_files', args, exc, t0)

        try:
            matcher = re.compile(query if regex else re.escape(query), re.IGNORECASE)
        except re.error as exc:
            return _fail('search_files', args, PolicyError(f'некорректное регулярное выражение: {exc}'), t0)

        # Предфильтр: одна проверка по ВСЕМУ тексту файла (сишный цикл) вместо
        # splitlines + поиск в каждой строке (питоновский цикл на файл в
        # несколько тысяч строк). Совпадение внутри строки — это совпадение и в
        # полном тексте при re.MULTILINE, поэтому «нет» предфильтра надёжно
        # означает, что построчный проход тоже ничего бы не нашёл. Исключение —
        # якоря \A и \Z: построчно они значат край строки, в полном тексте —
        # край файла; с ними предфильтр выключаем.
        if not regex:
            prefilter: re.Pattern[str] | None = matcher
        elif '\\A' in query or '\\Z' in query:
            prefilter = None
        else:
            prefilter = re.compile(query, re.IGNORECASE | re.MULTILINE)

        limit = min(max_results, policy.max_search_results)
        hits: list[str] = []
        read_paths: set[str] = set()
        truncated_by_time = False
        try:
            with scans.hold():
                deadline = time.monotonic() + _WALK_TIME_BUDGET_S
                for root in roots:
                    if len(hits) >= limit or truncated_by_time:
                        break
                    # Policy.scan: маски по имени, спуск только в обычные каталоги,
                    # никакого realpath на каждый файл. Раньше здесь был os.walk +
                    # resolve() на находку — 0.75 мс на файл, то есть минута на
                    # большом корне под GIL. См. Policy.scan.
                    try:
                        if root.is_file():
                            # An explicit file path has passed policy.resolve.
                            # Policy.scan walks directories only, so a direct
                            # search previously returned "Совпадений нет".
                            items = (ScanItem(root, root.name, False, root.stat().st_size),)
                        else:
                            items = policy.scan(root, deadline=deadline)
                        for item in items:
                            if _blind_read_gate(item.path):
                                continue
                            if len(hits) >= limit:
                                break
                            if not fnmatch.fnmatch(item.name, glob):
                                continue
                            if item.path.suffix.lower() in _BINARY_SUFFIXES:
                                continue
                            if item.size > policy.max_read_bytes:
                                # The normal prefilter reads small files in one
                                # pass. Large text files need a bounded, linewise
                                # path instead of being silently skipped.
                                try:
                                    with item.path.open('rb') as fh:
                                        if b'\x00' in fh.read(_BINARY_SNIFF_BYTES):
                                            continue
                                        fh.seek(0)
                                        for number, raw_line in enumerate(fh, 1):
                                            if time.monotonic() >= deadline:
                                                truncated_by_time = True
                                                break
                                            line = raw_line.decode('utf-8', errors='ignore').rstrip('\r\n')
                                            if matcher.search(line):
                                                hits.append(f'{item.path}:{number}: {line.strip()[:200]}')
                                                read_paths.add(str(item.path))
                                                if len(hits) >= limit:
                                                    break
                                except OSError:
                                    continue
                                if truncated_by_time:
                                    break
                                continue
                            try:
                                with item.path.open('rb') as fh:
                                    head = fh.read(_BINARY_SNIFF_BYTES)
                                    if b'\x00' in head:
                                        continue  # двоичный — как решает grep
                                    raw = head + fh.read()
                            except OSError:
                                continue
                            text = raw.decode('utf-8', errors='ignore')
                            if prefilter is not None and prefilter.search(text) is None:
                                continue
                            for number, line in enumerate(text.splitlines(), 1):
                                if matcher.search(line):
                                    hits.append(f'{item.path}:{number}: {line.strip()[:200]}')
                                    read_paths.add(str(item.path))
                                    if len(hits) >= limit:
                                        break
                    except ScanTimeout:
                        truncated_by_time = True
        except PolicyError as exc:  # шлюз занят: внутри блока PolicyError не поднимается
            return _fail('search_files', args, exc, t0)

        if truncated_by_time and not hits:
            hits.append(f'[…поиск прерван по времени ({_WALK_TIME_BUDGET_S:.0f} с) — сузь path/glob]')
        elif truncated_by_time:
            hits.append(f'[…поиск прерван по времени ({_WALK_TIME_BUDGET_S:.0f} с), возможны непросмотренные файлы]')

        body = '\n'.join(hits) if hits else 'Совпадений нет.'
        for read_path in sorted(read_paths):
            fleet_ctx.record_event(
                'read',
                agent,
                tool='search_files',
                path=read_path,
                payload={'query': query, 'matched': True},
            )
        if path and len(roots) == 1:
            body = _with_folder_docs(agent, roots[0], body)
        return _ok('search_files', args, body, t0)

    # ---------- запись ----------

    @mcp.tool(
        name='write_file',
        description=(
            'Создаёт файл или перезаписывает существующий (overwrite=true обязателен '
            'для перезаписи). Работает только если сервер запущен с allow_write=true. '
            'Для точечной правки существующего файла используй edit_file — он безопаснее. '
            'agent — необязательный worker_id флота; передавай его после fleet_take.'
        ),
    )
    def write_file(path: str, content: str, overwrite: bool = False, agent: str = '') -> str:
        args = {'path': path, 'content': content, 'overwrite': overwrite, 'agent': agent}
        decision = None
        try:
            target, source, claim = _claim_target(path, agent, write=True)
            decision = fleet_ctx.check_claim_write(agent, str(source), claim)
            if not decision.allow:
                raise PolicyError(f'{decision.code}: {decision.reason}')
            _require_blind_write(target)
            if len(content.encode('utf-8')) > policy.max_write_bytes:
                raise PolicyError(f'содержимое больше лимита {policy.max_write_bytes} б')
            if target.exists() and not overwrite:
                raise PolicyError('файл уже существует — передай overwrite=true, если это осознанно')
            if target.exists() and target.is_dir():
                raise PolicyError('по этому пути каталог')
        except PolicyError as exc:
            return _fail('write_file', args, exc, fleet_decision=decision)

        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        eol = os.linesep
        if existed:
            try:
                eol = _read_keep_eol(target)[1]
            except (OSError, UnicodeDecodeError):
                pass
        _write_keep_eol(target, content, eol)
        event_agent = str(getattr(decision, 'worker_id', '') or agent)
        fleet_ctx.record_event('write', event_agent, tool='write_file', path=str(target), declared=bool(str(agent or '').strip()))
        verb = 'перезаписан' if existed else 'создан'
        return _ok(
            'write_file', args, f'{verb}: {target} ({len(content)} символов)',
            fleet_decision=decision, tail=syntax_check.check(target),
        )

    @mcp.tool(
        name='edit_file',
        description=(
            'Точечная замена в файле: old_string меняется на new_string. '
            'По умолчанию old_string обязан встречаться РОВНО ОДИН раз — так правка '
            'не задевает похожие места случайно; replace_all=true снимает это условие. '
            'Повтор уже применённой точной правки идемпотентен: если old_string исчез, '
            'а непустой new_string уже встречается ровно один раз, файл не меняется и вызов успешен. '
            'Требует allow_write=true.'
        ),
    )
    def edit_file(
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        agent: str = '',
    ) -> str:
        args = {
            'path': path, 'old_string': old_string, 'new_string': new_string,
            'replace_all': replace_all, 'agent': agent,
        }
        decision = None
        try:
            target, source, claim = _claim_target(path, agent, write=True)
            decision = fleet_ctx.check_claim_write(agent, str(source), claim)
            if not decision.allow:
                raise PolicyError(f'{decision.code}: {decision.reason}')
            _require_blind_write(target)
            _require_blind_read(target)
            if not target.is_file():
                raise PolicyError('файл не найден')
            if not old_string:
                raise PolicyError('old_string пуст')
        except PolicyError as exc:
            return _fail('edit_file', args, exc, fleet_decision=decision)

        try:
            text, eol = _read_keep_eol(target)
        except UnicodeDecodeError:
            return _fail('edit_file', args, PolicyError('файл не в UTF-8 — правь через write_file'),
                         fleet_decision=decision)
        old_string = old_string.replace('\r\n', '\n')
        new_string = new_string.replace('\r\n', '\n')
        count = text.count(old_string)
        if count == 0:
            # Идемпотентный повтор после уже применённой правки — штатный no-op. Это
            # особенно важно при параллельных/восстановленных ходах: агент не должен
            # перечитывать файл и заново строить edit только потому, что предыдущий
            # вызов успел примениться. Строгая уникальность не маскирует реальное
            # несовпадение: пустой или неоднозначный new_string по-прежнему даёт отказ.
            if new_string and text.count(new_string) == 1:
                return _ok(
                    'edit_file', args,
                    f'правка уже применена: new_string уже присутствует ровно один раз; файл не менялся: {target}',
                    fleet_decision=decision,
                )
            return _fail(
                'edit_file', args, PolicyError('old_string в файле не найден' + _closest_hint(text, old_string)),
                fleet_decision=decision,
            )
        if count > 1 and not replace_all:
            return _fail(
                'edit_file',
                args,
                PolicyError(f'old_string встречается {count} раз — уточни фрагмент или передай replace_all=true'),
                fleet_decision=decision,
            )

        _write_keep_eol(target, text.replace(old_string, new_string), eol)
        event_agent = str(getattr(decision, 'worker_id', '') or agent)
        fleet_ctx.record_event('write', event_agent, tool='edit_file', path=str(target), declared=bool(str(agent or '').strip()))
        return _ok(
            'edit_file', args, f'заменено вхождений: {count} в {target}',
            fleet_decision=decision, tail=syntax_check.check(target),
        )

    @mcp.tool(
        name='apply_patch',
        description=(
            'Правка НЕСКОЛЬКИХ файлов одним вызовом в формате Codex: «*** Begin Patch», затем для '
            'каждого файла «*** Update File: путь» с фрагментами (@@ строка-якорь; строки с « » — '
            'контекст, «-» — удалить, «+» — добавить) или «*** Add File: путь» (все строки с «+»), '
            'в конце «*** End Patch». Всё или ничего: если хоть один файл не прошёл границы, стража '
            'или сверку контекста — не пишется ни один. Относительные пути — от root (корень '
            'проекта). Удаление и перенос файлов не поддерживаются. agent — worker_id флота.'
        ),
    )
    def apply_patch(patch: str, root: str = '', agent: str = '') -> str:
        args = {'patch': patch, 'root': root, 'agent': agent}
        decision = None
        plan: list[tuple[Path, str, str, str]] = []
        try:
            ops = patch_mod.parse(patch)
            base, _, _ = _claim_target(root, agent) if root else (None, None, None)
            if base is not None and not base.is_dir():
                raise PolicyError('root — не каталог')
            for op in ops:
                candidate = Path(op.path)
                if not candidate.is_absolute():
                    if base is None:
                        raise PolicyError(f'{op.path}: относительный путь — передай root (корень проекта) или абсолютный путь')
                    candidate = base / candidate
                target, source, claim = _claim_target(str(candidate), agent, write=True)
                decision = fleet_ctx.check_claim_write(agent, str(source), claim)
                if not decision.allow:
                    raise PolicyError(f'{op.path}: {decision.code}: {decision.reason}')
                _require_blind_write(target)
                if op.kind != 'add':
                    _require_blind_read(target)
                if op.kind == 'add':
                    if target.exists():
                        raise PolicyError(f'{op.path}: файл уже существует — для правки используй «*** Update File»')
                    new_text, eol = op.content, os.linesep
                    stat = f'создан: {target} (+{op.content.count(chr(10))})'
                else:
                    if not target.is_file():
                        raise PolicyError(f'{op.path}: файл не найден' + _missing_hint(policy, str(target)))
                    try:
                        text, eol = _read_keep_eol(target)
                    except UnicodeDecodeError:
                        raise PolicyError(f'{op.path}: файл не в UTF-8 — правь через write_file') from None
                    new_text, added, removed = patch_mod.apply(text, op)
                    stat = f'изменён: {target} (+{added} −{removed})'
                if len(new_text.encode('utf-8')) > policy.max_write_bytes:
                    raise PolicyError(f'{op.path}: результат больше лимита {policy.max_write_bytes} б')
                plan.append((target, new_text, eol, stat))
        except (PolicyError, patch_mod.PatchError) as exc:
            return _fail('apply_patch', args, exc, fleet_decision=decision)

        declared = bool(str(agent or '').strip())
        event_agent = str(getattr(decision, 'worker_id', '') or agent)
        verdicts = []
        for target, new_text, eol, _stat in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_keep_eol(target, new_text, eol)
            fleet_ctx.record_event('write', event_agent, tool='apply_patch', path=str(target), declared=declared)
            verdict = syntax_check.check(target)
            if verdict:
                verdicts.append(f'{target.name}: {verdict}')
        body = f'применено файлов: {len(plan)}\n' + '\n'.join(stat for *_, stat in plan)
        return _ok('apply_patch', args, body, fleet_decision=decision, tail='\n'.join(verdicts) or None)

    # ---------- git (только чтение) ----------

    def _git(repo: str, git_args: list[str], tool: str) -> str:
        args = {'repo': repo, 'args': git_args}
        try:
            target = policy.resolve(repo, for_write=False)
            if not target.is_dir():
                raise PolicyError('это не каталог')
        except PolicyError as exc:
            return _fail(tool, args, exc)

        try:
            result = subprocess.run(
                [*_GIT_SAFE, '-C', str(target), *git_args],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=60,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _fail(tool, args, PolicyError(f'git не отработал: {exc}'))

        if result.returncode != 0:
            return _fail(tool, args, PolicyError(f'git вернул {result.returncode}: {result.stderr.strip()[:400]}'))
        return _ok(tool, args, result.stdout or '(пусто)')

    @mcp.tool(
        name='git_status',
        description='Состояние рабочего дерева репозитория (git status). Только чтение.',
    )
    def git_status(repo: str) -> str:
        return _git(repo, ['status', '--porcelain=v1', '--branch'], 'git_status')

    @mcp.tool(
        name='git_diff',
        description='Диff незакоммиченных изменений. staged=true — то, что уже в индексе. Только чтение.',
    )
    def git_diff(repo: str, staged: bool = False) -> str:
        return _git(repo, ['diff', '--staged'] if staged else ['diff'], 'git_diff')

    @mcp.tool(
        name='git_log',
        description='Последние коммиты репозитория одной строкой на коммит. Только чтение.',
    )
    def git_log(repo: str, count: int = 15) -> str:
        return _git(repo, ['log', f'-{max(1, min(count, 100))}', '--oneline', '--no-decorate'], 'git_log')

    # ---------- задачи ----------

    @mcp.tool(
        name='run_task',
        description=(
            'Запускает ЗАРАНЕЕ ОПИСАННУЮ задачу по имени (список — в policy_info). '
            'Произвольные команды выполнять нельзя: имя задачи выбирает готовый набор '
            'аргументов из конфига, ничего из запроса в команду не подставляется. '
            'Требует allow_write=true. agent — необязательный worker_id флота.'
        ),
    )
    def run_task(name: str, agent: str = '') -> str:
        t0 = time.monotonic()
        args = {'name': name, 'agent': agent}
        task = policy.tasks.get(name)
        if task is None:
            known = ', '.join(policy.tasks) or 'нет ни одной'
            return _fail('run_task', args, PolicyError(f'задача «{name}» не описана. Доступны: {known}'), t0)
        # Задача меняет состояние (сборка, тесты пишут кэш), поэтому требует того же
        # права, что запись: либо глобального, либо cwd внутри write_roots.
        decision = None
        try:
            cwd, source, claim = _claim_target(task.cwd, agent, write=True)
            decision = fleet_ctx.check_claim_exec(agent, str(source), claim)
            if not decision.allow:
                raise PolicyError(f'{decision.code}: {decision.reason}')
            executable = task.argv
            run_env = None
            if claim is not None and claim.mode == 'isolated':
                from dev_mcp.fleet.claim_take_routing import (
                    claim_command_environment, validate_claim_command_argv,
                )
                executable = list(validate_claim_command_argv(task.argv, claim))
                route = claim_command_environment(claim)
                run_env = route.environment
                if executable and Path(executable[0]).name.casefold() in {'python', 'python.exe'}:
                    if not route.python_executable.is_file():
                        raise PolicyError('isolated Python requires the shared project .venv')
                    executable[0] = str(route.python_executable)
        except (PolicyError, ClaimRoutingError) as exc:
            return _fail('run_task', args, exc, t0, fleet_decision=decision)

        def _record_promoted_task(job: Job) -> None:
            """rc ушедшей в фон задачи — в журнал флота (см. `run`: приёмка ждёт rc)."""
            fleet_ctx.record_event(
                'run', agent, tool='run_task', path=str(cwd), rc=job.returncode,
                payload={'task': name, 'argv': task.argv, 'promoted': job.job_id},
            )

        # Окружение НЕ вычищается: argv задачи целиком из конфига (не из запроса
        # клиента), а memory_service-test читает ключи LLM из среды. Инструмент `run`,
        # где argv влияет клиент, окружение чистит — там это оправдано.
        try:
            with runs.hold():
                rc, out, timed_out, promoted = _run_capturing(
                    executable, cwd=str(cwd), env=run_env, timeout_s=task.timeout_s, runs_dir=jobs.runs_dir,
                    jobs=jobs, budget_s=_budget_left(policy.exec_sync_budget_s, t0),
                    on_exit=_record_promoted_task,
                )
        except PolicyError as exc:  # шлюз занят
            return _fail('run_task', args, exc, t0, fleet_decision=decision)
        except OSError as exc:
            return _fail(
                'run_task', args, PolicyError(f'не запустилось: {exc}'), t0,
                fleet_decision=decision,
            )
        if promoted is not None:
            # Событие с rc запишет _record_promoted_task по завершении процесса.
            return _ok(
                'run_task', args,
                f'задача: {name}\nработает дольше {policy.exec_sync_budget_s} с и переведена в ФОН, '
                f'чтобы вызов не оборвался на шлюзе: {promoted.job_id}\n'
                f'процесс НЕ убит и доживёт свои {task.timeout_s} с. СРАЗУ, в этом же ходе, вызови '
                f'run_status("{promoted.job_id}") — он сам ждёт до {int(RUN_STATUS_WAIT_S)} с; повторяй до кода '
                f'возврата.\nНе запускай задачу заново — она уже выполняется.',
                t0,
                fleet_decision=decision,
            )
        if timed_out:
            return _fail(
                'run_task', args,
                PolicyError(f'превышен таймаут {task.timeout_s} с — процесс убит.\n\n{out[-4000:]}'), t0,
                fleet_decision=decision,
            )

        payload = (
            f'задача: {name}\nкоманда: {" ".join(task.argv)}\nкод возврата: {rc}\n\n'
            f'--- вывод (stdout+stderr) ---\n{out}'
        )
        fleet_ctx.record_event(
            'run', agent, tool='run_task', path=str(cwd), rc=rc,
            payload={'task': name, 'argv': task.argv},
        )
        return _ok('run_task', args, payload, t0, fleet_decision=decision)

    # ---------- исполнение кода (exec_roots) ----------

    def _prepare_run(root: str, argv: list[str], cwd: str, *,
                     binary_root: Path | None = None) -> tuple[Path, list[str]]:
        """Общая проверка для run/run(background): границы, бинарь, аргументы."""
        if not argv:
            raise PolicyError('argv пуст')
        if not policy.exec_roots and not policy.tree_roots:
            raise PolicyError('исполнение выключено: в политике нет exec_roots и tree_roots')
        work = policy.resolve_exec_cwd(root, cwd)
        raw0 = str(argv[0]).strip()
        low0 = raw0.casefold()

        def _project_batch(script_arg: str, script_args: list[str]) -> tuple[Path, list[str]]:
            # Windows batch acceptance is common in the product repos. We never expose
            # arbitrary cmd.exe: only a RELATIVE project-local .cmd/.bat inside the already
            # trusted exec_root may be launched, and caller-supplied shell metacharacters
            # remain forbidden.
            norm = script_arg.replace("\\", "/")
            if (
                not script_arg
                or not script_arg.casefold().endswith((".cmd", ".bat"))
                or os.path.isabs(script_arg)
                or norm.startswith("/")
                or ".." in norm.split("/")
            ):
                raise PolicyError("batch-файл допустим только относительный .cmd/.bat внутри exec_root")
            script = policy.resolve(str(work / script_arg), for_write=False)
            if policy.exec_root_for(script) is None or not script.is_file():
                raise PolicyError(f"batch-файл не найден внутри exec_root: {script_arg}")
            policy.guard_exec_args(script_args, bin_name=script_arg)
            for tok in script_args:
                if any(ch in tok for ch in "&|<>^%\r\n"):
                    raise PolicyError(f"shell-метасимвол в аргументе batch-команды запрещён: {tok}")
            comspec = os.environ.get("ComSpec") or shutil.which("cmd.exe")
            if not comspec:
                raise PolicyError("cmd.exe не найден в системе для project-local batch")
            command = subprocess.list2cmdline([str(script), *script_args])
            return work, [comspec, "/d", "/s", "/c", command]

        if low0.endswith((".cmd", ".bat")):
            return _project_batch(raw0, list(argv[1:]))

        if low0 in {"cmd", "cmd.exe"}:
            # Legacy acceptance specs often spell the same safe project-local batch through
            # cmd.exe. Parse only that narrow wrapper so agents can copy acceptance verbatim
            # without asking the owner to allow a general-purpose shell.
            rest = [str(value) for value in argv[1:]]
            index = 0
            while index < len(rest) and rest[index].casefold() in {"/d", "/s"}:
                index += 1
            if index >= len(rest) or rest[index].casefold() != "/c":
                raise PolicyError("cmd.exe разрешён только как wrapper: /d [/s] /c <relative .cmd/.bat> [args]")
            index += 1
            if index < len(rest) and rest[index].casefold() == "call":
                index += 1
            if index >= len(rest):
                raise PolicyError("после cmd.exe /c нужен относительный project-local .cmd/.bat")
            return _project_batch(rest[index], rest[index + 1:])

        # The only caller supplying binary_root is an isolated claim's exact
        # project .venv Python. Scripts and cwd still resolve from the claim tree.
        binary = policy.exec_bin_path(raw0, binary_root if binary_root is not None else work)
        policy.guard_exec_args(argv[1:], bin_name=raw0)
        return work, [binary, *argv[1:]]

    @mcp.tool(
        name='run',
        description=(
            'Запускает команду внутри каталога из exec_roots (список — в policy_info). '
            'argv[0] — голое имя разрешённого бинаря ("python", "pytest", "sing-box"), '
            'относительный executable или project-local .cmd/.bat внутри той же папки. '
            'Legacy acceptance "cmd.exe /d [/s] /c <relative.cmd|bat> ..." принимается только '
            'как строгий wrapper над таким batch; arbitrary shell не запускается. Абсолютные пути и ".." в аргументах '
            'запрещены, окружение вычищено от секретов. background=true — долгий процесс '
            '(TUN-сеанс, watcher): вернётся job_id для run_status/run_stop. '
            'В background timeout_s действует для pytest; сервисы работают до run_stop. Команда, не уложившаяся в бюджет одного '
            'вызова (exec_sync_budget_s в policy_info), САМА уходит в фон и возвращает '
            'job_id — процесс при этом не убит, перезапускать команду не нужно, просто '
            'опроси run_status. agent — необязательный worker_id флота.'
        ),
    )
    def run(
        root: str,
        argv: list[str],
        cwd: str = '',
        timeout_s: int = 300,
        background: bool = False,
        agent: str = '',
    ) -> str:
        t0 = time.monotonic()
        args = {
            'root': root, 'argv': argv, 'cwd': cwd, 'timeout_s': timeout_s,
            'background': background, 'agent': agent,
        }
        decision = None
        if list(argv) == ['fleet.cmd', 'blind-seal']:
            # Cached clients cannot discover a newly exposed tool. Invoke the
            # exact same server verifier, with its original claim/write/exposure
            # checks; never shell out or let a caller supply a digest/claim id.
            if cwd or background:
                return _fail('run', args, PolicyError('blind-seal: cwd/background недопустимы'), t0)
            return fleet_blind_seal(agent=agent)
        pult = _pult_read(list(argv))
        if pult is not None:
            # Агенты зовут `fleet.cmd result …`, чтобы прочитать историю задачи, и получали «сюда писать
            # нельзя» — ход кончался (07.10.2026). Чтение пульта безвредно: отдаём его как есть.
            from dev_mcp.fleet.tools import _run_pult

            if isinstance(pult, list) and pult[0] == "look":
                # Compatibility for clients whose cached tools/list predates
                # fleet_look. Reuse the claim-scoped service, never a subprocess.
                from dev_mcp.fleet.look import look_for_agent
                try:
                    paths, rubric = pult[1:], ""
                    if "--rubric" in paths:
                        index = paths.index("--rubric")
                        if index != len(paths) - 2:
                            raise ValueError("look: --rubric требует одну последнюю строку")
                        paths, rubric = paths[:index], paths[index + 1]
                    for item in paths:
                        policy.resolve(str(Path(root) / item), for_write=False)
                    text = json.dumps(look_for_agent(fleet_ctx.store, agent, paths, rubric, policy=policy),
                                      ensure_ascii=False)
                except (PolicyError, ValueError) as exc:
                    return _fail('run', args, exc, t0)
            else:
                text = pult if isinstance(pult, str) else _run_pult(fleet_ctx, pult)
            return _ok('run', args, text, t0)
        # TZ-41: independent reviewers execute only sealed immutable author
        # snapshots. This MUST precede the ordinary claim/trunk cwd mapping:
        # a generic subprocess is not review evidence for an isolated claim.
        try:
            if fleet_ctx.sealed_review_source(agent) is not None:
                policy.resolve_exec_cwd(root, cwd)
                if argv:
                    policy.guard_exec_args(argv[1:], bin_name=str(argv[0]))
                reviewed = fleet_ctx.trusted_reviewer_run(
                    agent, root, cwd, list(argv),
                    timeout=max(1, min(int(timeout_s), policy.exec_timeout_cap_s)),
                    background=background,
                )
                if reviewed is None:
                    raise PolicyError('isolated review lost its sealed source')
                return _ok(
                    'run', args,
                    f'verified sealed review: claim {reviewed.claim_id}, '
                    f'revision {reviewed.submission_revision}\n'
                    f'код возврата: {reviewed.rc}\n\n'
                    f'{reviewed.output or "(пустой вывод)"}',
                    t0,
                )
        except (PolicyError, ValueError, OSError) as exc:
            return _fail('run', args, exc, t0)
        except Exception as exc:
            # Do not silently downgrade a failed isolated reviewer to the
            # ordinary shared-root runner.
            from dev_mcp.fleet.claim_review_bite import ClaimReviewError
            if isinstance(exc, ClaimReviewError):
                return _fail('run', args, PolicyError(str(exc)), t0)
            return _fail('run', args, PolicyError(f'isolated reviewer failed: {type(exc).__name__}'), t0)
        try:
            # Resolve only the requested cwd first: an isolated task may have
            # created an executable script which does not exist in the trunk.
            source_work = policy.resolve_exec_cwd(root, cwd)
            work, source, claim = _claim_target(str(source_work), agent, write=True)
            decision = fleet_ctx.check_claim_exec(agent, str(source), claim)
            if not decision.allow:
                raise PolicyError(f'{decision.code}: {decision.reason}')
            env = child_env(scrub_env())
            if claim is not None and claim.mode == 'isolated':
                from dev_mcp.fleet.claim_take_routing import (
                    claim_command_environment, validate_claim_command_argv,
                )
                validate_claim_command_argv(argv, claim)
                route = claim_command_environment(claim, env)
                env = route.environment
                # A clean Git checkout deliberately has no untracked .venv.
                # Resolve ONLY the canonical relative project interpreter against
                # the trusted project root before checking executable existence;
                # every other relative script must exist inside this claim tree.
                project_python = ('.venv/scripts/python.exe' if os.name == 'nt'
                                  else '.venv/bin/python')
                is_project_python = str(argv[0]).replace('\\', '/').casefold() == project_python
                if is_project_python and not route.python_executable.is_file():
                    raise PolicyError('isolated Python requires the shared project .venv')
                _, real_argv = _prepare_run(
                    str(work), list(argv), '',
                    binary_root=claim.project_root if is_project_python else None,
                )
                if Path(real_argv[0]).name.casefold() in {'python', 'python.exe'}:
                    if not route.python_executable.is_file():
                        raise PolicyError('isolated Python requires the shared project .venv')
                    real_argv[0] = str(route.python_executable)
            else:
                work, real_argv = _prepare_run(root, list(argv), cwd)
        except (PolicyError, ClaimRoutingError) as exc:
            return _fail('run', args, exc, t0, fleet_decision=decision)

        completion_subject = fleet_ctx._subject(str(agent or "").strip()) if agent else None
        completion_paths = list(completion_subject["paths"]) if completion_subject else []
        if claim is not None and claim.mode == "isolated":
            from dev_mcp.fleet.claim_take_routing import project_path_to_claim
            completion_paths = [str(project_path_to_claim(p, claim, read_only=True))
                                for p in completion_paths]

        def _record_promoted_run(job: Job) -> None:
            """rc ушедшей в фон команды всё равно попадает в журнал флота.

            Иначе приёмка ломается ровно на том, ради чего она есть: `fleet_review
            accept` требует собственное run-событие ревьюера с rc=0, а прогон
            тестов — как раз самая долгая команда и первая, кто уходит в фон.
            """
            if not agent:
                return
            # Bind completion to the launch subject. A recovery/handoff while
            # the process runs must not credit its result to the next task.
            payload = {'argv': argv, 'promoted': job.job_id,
                       'source_claim_id': completion_subject['claim_id'] if completion_subject else None}
            if claim is not None and claim.mode == "isolated":
                payload.update(claim_id=claim.claim_id, tree_revision=route.tree_revision)
            tree = fleet_ctx._tree(completion_paths) if completion_paths else None
            if tree:
                payload['tree'] = tree
            with fleet_ctx.store.tx() as tx:
                tx.record_event(kind='run', worker_id=str(agent).strip(),
                                project=completion_subject['project'] if completion_subject else None,
                                tz=completion_subject['tz'] if completion_subject else None,
                                tool='run', path_norm=norm_path(str(work)),
                                rc=job.returncode, payload=payload)

        # Ресурс «на время процесса» (fleet/world.py, scope="run"): сборка берёт heavy-build
        # только пока идёт, а не на весь захват.
        try:
            release, busy = fleet_world.hold_for_run(
                fleet_ctx.store, agent, real_argv, f'{agent or "?"}: {" ".join(argv)[:80]}')
        except Exception:  # noqa: BLE001 — сбой учёта ресурса не должен ронять run
            release, busy = (lambda: None), ''
        if release is None:
            return _ok('run', args, busy, t0, fleet_decision=decision)
        cap = max(1, min(int(timeout_s), policy.exec_timeout_cap_s))
        if cap < policy.exec_timeout_cap_s and fleet_ctx.is_acceptance_run(agent, list(argv)):
            cap = policy.exec_timeout_cap_s
        if background:
            try:
                from dev_mcp.runner import is_test_command
                job = jobs.start(real_argv, str(work), env,
                                 timeout_s=cap if is_test_command(real_argv) else None)
            except (OSError, RuntimeError) as exc:
                release()
                return _fail(
                    'run', args, PolicyError(f'фоновый запуск не удался: {exc}'), t0,
                    fleet_decision=decision,
                )
            def _background_exit(done: Job) -> None:
                try:
                    _record_promoted_run(done)
                finally:
                    release()

            jobs.when_done(job.job_id, _background_exit)
            fleet_ctx.record_event(
                'run', agent, tool='run', path=str(work),
                payload={'argv': argv, 'background': True, 'job_id': job.job_id},
            )
            return _ok(
                'run', args,
                f'фоновая задача: {job.job_id}\nкоманда: {" ".join(real_argv)}\ncwd: {work}\n'
                f'опрашивай run_status("{job.job_id}"), гаси run_stop("{job.job_id}")',
                t0,
                fleet_decision=decision,
            )

        def _promoted_exit(job: Job) -> None:
            try:
                _record_promoted_run(job)
            finally:
                release()

        try:
            with runs.hold():
                rc, out, timed_out, promoted = _run_capturing(
                    real_argv, cwd=str(work), env=env, timeout_s=cap, runs_dir=jobs.runs_dir,
                    jobs=jobs, budget_s=_budget_left(policy.exec_sync_budget_s, t0),
                    on_exit=_promoted_exit,
                )
        except PolicyError as exc:  # шлюз занят
            release()
            return _fail('run', args, exc, t0, fleet_decision=decision)
        except OSError as exc:
            release()
            return _fail(
                'run', args, PolicyError(f'не запустилось: {exc}'), t0,
                fleet_decision=decision,
            )
        if promoted is None:
            release()
        if promoted is not None:
            # Само событие с rc запишет _record_promoted_run, когда процесс закончит:
            # приёмке нужен фактический код возврата, а не «команду запустили».
            return _ok(
                'run', args,
                f'команда работает дольше {policy.exec_sync_budget_s} с и переведена в ФОН, '
                f'чтобы вызов не оборвался на шлюзе: {promoted.job_id}\n'
                f'команда: {" ".join(real_argv)}\ncwd: {work}\n'
                f'процесс НЕ убит и доживёт свои {cap} с. СРАЗУ, в этом же ходе, вызови '
                f'run_status("{promoted.job_id}") — он сам ждёт до {int(RUN_STATUS_WAIT_S)} с; повторяй, пока '
                f'не будет кода возврата. Не заканчивай ход на ожидании: такой ход сервер будит только через '
                f'4 мин. Погасить раньше — run_stop("{promoted.job_id}").\n'
                f'Не перезапускай команду заново — она уже выполняется.',
                t0,
                fleet_decision=decision,
            )
        if timed_out:
            return _fail(
                'run', args,
                PolicyError(f'превышен таймаут {cap} с — процесс убит.\n\n{out[-4000:]}'), t0,
                fleet_decision=decision,
            )

        payload = (
            f'команда: {" ".join(real_argv)}\ncwd: {work}\nкод возврата: {rc}\n\n{out or "(пустой вывод)"}'
        )
        fleet_ctx.record_event(
            'run', agent, tool='run', path=str(work), rc=rc, payload={'argv': argv},
        )
        return _ok('run', args, payload, t0, fleet_decision=decision)

    def _run_status_sync(job_id: str) -> str:
        args = {'job_id': job_id}
        target = str(job_id or '').strip()
        job = jobs.get(target)
        if job is not None:
            return _ok('run_status', args, f'{describe_job(job)}\n\n--- лог ---\n{jobs.tail(job)}')

        # JobRegistry живёт в памяти процесса, поэтому обычный рестарт dev-mcp раньше
        # превращал корректный job_id в ложный «нет такой задачи». Для promoted run
        # фактический rc уже пишется в durable fleet events; для explicit background
        # остаётся <job_id>.log. Восстанавливаем столько истины, сколько реально есть,
        # и не заставляем агента запускать тяжёлую команду заново вслепую.
        if re.fullmatch(r'[0-9a-f]{12}', target):
            archived = None
            try:
                with fleet_ctx.store.tx() as tx:
                    archived = tx.one(
                        """SELECT ts,rc FROM events
                           WHERE kind='run' AND rc IS NOT NULL AND payload LIKE ?
                           ORDER BY id DESC LIMIT 1""",
                        (f'%{target}%',),
                    )
            except Exception:
                archived = None
            if archived is not None:
                return _ok(
                    'run_status',
                    args,
                    f'{target}: завершилась, код возврата {archived["rc"]} '
                    f'(результат восстановлен из журнала флота после рестарта dev-mcp; '
                    f'событие {archived["ts"]}). Не запускай команду заново.',
                )

            archived_log = jobs.runs_dir / f'{target}.log'
            if archived_log.is_file():
                try:
                    log_text = decode_output(archived_log.read_bytes())
                except OSError:
                    log_text = '(лог недоступен)'
                if len(log_text) > 4000:
                    log_text = '…' + log_text[-4000:]
                return _ok(
                    'run_status',
                    args,
                    f'{target}: задача была из предыдущей сессии dev-mcp; процесс уже не '
                    'отслеживается, поэтому финальный rc неизвестен. Сохранённый лог ниже. '
                    'Не перезапускай команду вслепую — сначала проверь её артефакт/результат.'
                    f'\n\n--- сохранённый лог ---\n{log_text}',
                )

        return _fail(
            'run_status', args,
            PolicyError('нет такой фоновой задачи; возможно, dev-mcp перезапустился до сохранения результата'),
        )

    @mcp.tool(
        name='run_status',
        description=(
            'Состояние фоновой задачи run: выполняется/код возврата + хвост лога. '
            'После рестарта dev-mcp восстанавливает финальный rc promoted-run из fleet journal '
            'или хотя бы сохранённый лог обычной background-задачи. Если команда ещё идёт, '
            'вызов сам ждёт её завершения до ~25 с — вызывай снова, пока не придёт код возврата, '
            'и не заканчивай ход, пока ждёшь.'
        ),
    )
    async def run_status(job_id: str) -> str:
        # 27.09.2026: 11% ходов агентов обрывались сразу после run — модель писала «сборка
        # идёт, проверю позже» и заканчивала ход, а продолжение стоило новой будки. Теперь
        # опрос сам ждёт завершения (до RUN_STATUS_WAIT_S < WORKER_LOCAL_MCP_TIMEOUT 45 с).
        # Ждём асинхронно: поток пула FastMCP не занят.
        target = str(job_id or '').strip()
        waited = 0.0
        job = jobs.get(target)
        while job is not None and job.returncode is None and waited < RUN_STATUS_WAIT_S:
            await anyio.sleep(1.0)
            waited += 1.0
            job = jobs.get(target)
        text = await anyio.to_thread.run_sync(_run_status_sync, job_id)
        if job is not None and job.returncode is None and waited:
            text += (
                '\n\n'
                f'[ждал {int(waited)} с — команда ещё идёт. Сразу снова вызови '
                f'run_status("{target}"): он сам ждёт до {int(RUN_STATUS_WAIT_S)} с. Не заканчивай ход.]'
            )
        return text

    @mcp.tool(name='run_stop', description='Останавливает фоновую задачу run (убивает всё дерево процессов).')
    def run_stop(job_id: str) -> str:
        if not jobs.stop(job_id):
            return _fail('run_stop', {'job_id': job_id}, PolicyError('нет такой фоновой задачи'))
        job = jobs.get(job_id)
        return _ok('run_stop', {'job_id': job_id}, describe_job(job) if job else f'{job_id}: остановлена')

    @mcp.tool(name='run_list', description='Все фоновые задачи run этой сессии сервера, свежие сверху.')
    def run_list() -> str:
        rows = jobs.all()
        if not rows:
            return _ok('run_list', {}, '(фоновых задач нет)')
        return _ok('run_list', {}, '\n\n'.join(describe_job(j) for j in rows))

    # ---------- скачивание из сети ----------

    @mcp.tool(
        name='fetch_url',
        description=(
            'Скачивает http(s)-URL в файл внутри разрешённой на запись папки. '
            'dest — путь назначения (перезапись только с overwrite=true). Адреса в '
            'приватных/loopback-диапазонах отклоняются. Требует allow_network_fetch=true. '
            'agent — необязательный worker_id флота; передавай его после fleet_take.'
        ),
    )
    def fetch_url(url: str, dest: str, overwrite: bool = False, agent: str = '') -> str:
        t0 = time.monotonic()
        args = {'url': url, 'dest': dest, 'overwrite': overwrite, 'agent': agent}
        decision = None
        if not policy.allow_network_fetch:
            return _fail('fetch_url', args, PolicyError('скачивание из сети выключено (allow_network_fetch=false)'))
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ('http', 'https'):
            return _fail('fetch_url', args, PolicyError('только http/https'))
        if not parsed.hostname:
            return _fail('fetch_url', args, PolicyError('в URL нет хоста'))
        try:
            target, source, claim = _claim_target(dest, agent, write=True)
            decision = fleet_ctx.check_claim_write(agent, str(source), claim)
            if not decision.allow:
                raise PolicyError(f'{decision.code}: {decision.reason}')
        except PolicyError as exc:
            return _fail('fetch_url', args, exc, fleet_decision=decision)
        if target.exists() and not overwrite:
            return _fail(
                'fetch_url', args, PolicyError('файл существует — overwrite=true, если осознанно'),
                fleet_decision=decision,
            )

        current = url.strip()
        for _ in range(_FETCH_MAX_REDIRECTS + 1):
            host = urllib.parse.urlparse(current).hostname or ''
            ok, why = _is_public_host(host)
            if not ok:
                return _fail('fetch_url', args, PolicyError(f'адрес отклонён: {why}'), fleet_decision=decision)
            req = urllib.request.Request(current, headers={'User-Agent': _FETCH_UA}, method='GET')
            try:
                resp = urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S)  # noqa: S310 — схема проверена
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308) and exc.headers.get('Location'):
                    current = urllib.parse.urljoin(current, exc.headers['Location'])
                    continue
                return _fail('fetch_url', args, PolicyError(f'HTTP {exc.code}'), fleet_decision=decision)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                return _fail('fetch_url', args, PolicyError(f'не скачалось: {exc}'), fleet_decision=decision)
            redirect = resp.headers.get('Location') if resp.status in (301, 302, 303, 307, 308) else None
            if redirect:
                current = urllib.parse.urljoin(current, redirect)
                resp.close()
                continue
            break
        else:
            return _fail('fetch_url', args, PolicyError('слишком много редиректов'), fleet_decision=decision)

        limit = policy.fetch_max_bytes
        got = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with resp, target.open('wb') as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > limit:
                        fh.close()
                        target.unlink(missing_ok=True)
                        return _fail('fetch_url', args, PolicyError(f'превышен лимит {limit} б'), fleet_decision=decision)
                    fh.write(chunk)
        except OSError as exc:
            return _fail('fetch_url', args, PolicyError(f'ошибка записи: {exc}'), fleet_decision=decision)
        event_agent = str(getattr(decision, 'worker_id', '') or agent)
        fleet_ctx.record_event('write', event_agent, tool='fetch_url', path=str(target), declared=bool(str(agent or '').strip()))
        return _ok(
            'fetch_url', args, f'скачано {got} б → {target}', t0,
            fleet_decision=decision,
        )

    # ---------- заявки на доступ ----------
    #
    # request_note — единственный способ для клиента ПОПРОСИТЬ больше прав. С 25.09.2026
    # решение принимает автомат (dev_mcp.request_triage) прямо в вызове и пишет его в
    # logs/requests.history.jsonl: «уже разрешено» или отказ с рецептом. Периметр
    # (policy.toml) автомат не расширяет никогда. Обычный throughput-контур не ждёт
    # ручного approve; расширение периметра — отдельная административная операция вне задач флота.

    @mcp.tool(
        name='request_note',
        description=(
            'Заявка; решение приходит СРАЗУ в ответе, владелец вручную ничего не разбирает. '
            'kind=other с to="<project>/<tz>" или to=queue (или с path внутри проекта флота) — '
            'сразу создаёт готовую к выдаче задачу fleet на чужую правку (нужен path или адресат с owns). '
            'kind=access|bin|root|network — автомат отвечает approved (периметр это уже даёт) или '
            'denied с рецептом; периметр он не расширяет. Прочее к владельцу — denied с рецептом '
            '(решай сам в пределах ТЗ или fleet_block). agent — worker_id источника. '
            'Для доступа укажи точный path и lists (roots|write_roots|exec_roots|exec_allowed_bin).'
        ),
    )
    def request_note(
        kind: str,
        summary: str,
        detail: str = '',
        path: str = '',
        lists: list[str] | None = None,
        to: str = 'owner',
        agent: str = '',
    ) -> str:
        args = {
            'kind': kind, 'summary': summary, 'detail': detail, 'path': path,
            'lists': lists, 'to': to, 'agent': agent,
        }
        fleet_ctx.touch(agent)
        k = (kind or 'other').strip().lower()
        if k not in ('access', 'bin', 'root', 'network', 'other'):
            k = 'other'
        if not summary or not summary.strip():
            return _fail('request_note', args, PolicyError('summary пуст'))

        request_id = uuid.uuid4().hex[:8]
        target = (to or 'owner').strip() or 'owner'
        cleaned_lists = [x.strip() for x in (lists or []) if x and x.strip()][:5]

        # Большая текущая задача не должна дробиться на request:* только потому, что автор
        # ТЗ не перечислил соседний свободный файл в owns. Если request_note пришёл от
        # активного chatgpt-worker с path внутри его проекта, сначала пропускаем путь через
        # тот же fleet write-guard: он либо подтвердит уже имеющееся владение, либо
        # атомарно расширит claim на свободный путь. Реальный чужой claim/review/другой
        # проект по-прежнему получит deny и пойдёт обычным dependency-маршрутом ниже.
        if k == 'other' and agent and path and path.strip():
            try:
                with fleet_ctx.store.tx() as tx:
                    source_claim = tx.one(
                        """SELECT c.project,c.tz,p.root
                           FROM claims c JOIN projects p ON p.name=c.project
                           WHERE c.worker_id=? AND c.state='active'
                           ORDER BY c.id DESC LIMIT 1""",
                        (agent,),
                    )
                if source_claim is not None:
                    raw_path = path.strip()
                    candidate = Path(raw_path) if os.path.isabs(raw_path) else Path(str(source_claim['root'])) / raw_path
                    real = policy.resolve(str(candidate), for_write=True)
                    claim_decision = fleet_ctx.check_write(agent, str(real))
                    if claim_decision.allow:
                        reason = str(claim_decision.reason or '').strip()
                        if not reason:
                            reason = (
                                f"Путь уже входит в активную зону "
                                f"{source_claim['project']}/{source_claim['tz']}."
                            )
                        audit.record(
                            'request_note', args, decision='allow',
                            reason='fleet_claim_expand', agent=agent,
                        )
                        return fleet_ctx.with_envelope(
                            agent,
                            f"Отдельная request-задача не создана. {reason} "
                            "Продолжай текущую задачу сейчас; owner approve не нужен.",
                        )
            except (PolicyError, OSError, ValueError):
                # Путь вне write-периметра/проекта или его нельзя безопасно разрешить:
                # обычный request/triage ниже даст точный отказ или создаст dependency.
                pass

        # kind=other с путём внутри проекта флота — это чужая правка, а не вопрос владельцу:
        # агенты по привычке шлют её to=owner, и она лежала бы в очереди review.cmd вечно.
        if k == 'other' and target.casefold() == 'owner' and path and path.strip():
            try:
                with fleet_ctx.store.tx() as tx:
                    for row in tx.query('SELECT name,root FROM projects WHERE active=1'):
                        if request_triage.covered(path.strip(), [str(row['root'])]):
                            target = 'queue'
                            break
            except Exception:  # noqa: BLE001 — реестр недоступен: решит автомат ниже
                pass
        legacy = k in ('access', 'bin', 'root', 'network') or target.casefold() == 'owner'

        if not legacy:
            try:
                project = None
                source_tz = None
                if agent:
                    with fleet_ctx.store.tx() as tx:
                        claim = tx.one(
                            """SELECT project,tz FROM claims
                               WHERE worker_id=? AND state='active'
                               ORDER BY id DESC LIMIT 1""",
                            (agent,),
                        )
                    if claim is not None:
                        project = str(claim['project'])
                        source_tz = str(claim['tz'])
                review = fleet_ctx.module('review')
                if review is None:
                    raise RuntimeError('fleet review module is unavailable')
                draft_tz = review.open_request(
                    fleet_ctx.store,
                    request_id,
                    kind=k,
                    summary=summary.strip()[:500],
                    detail=(detail or '').strip()[:4000],
                    to=target,
                    project=project,
                    worker_id=agent or None,
                    source_tz=source_tz,
                    path=(path or '').strip()[:1000],
                    lists=cleaned_lists,
                )
                if draft_tz is None:
                    raise RuntimeError('fleet request unexpectedly routed to legacy channel')

                # Явный blocker не должен превращаться в бесконечное «продолжи».
                # Раньше ответ советовал отдельный fleet_block, но старые ChatGPT-коннекторы
                # кэшируют tools/list и могли вообще не иметь этого инструмента. Теперь request_note
                # сам освобождает claim и связывает block reason с созданной request-задачей:
                # thaw_frozen вернёт исходную работу, когда dependency будет принята.
                blocking_text = f'{summary}\n{detail or ""}'
                looks_blocking = bool(re.search(
                    r'(?i)(\bblocked\b|\bblocking\b|\bcannot\b|\bcan.t\b|'
                    r'\brequires?\b.*\bbefore\b|\bneeds?\b.*\b(?:outside|foreign|before)\b|'
                    r'блокир|заблок|невозмож|не могу|нельзя заверш|требует.{0,80}(?:до|перед))',
                    blocking_text,
                ))
                auto_blocked = False
                if agent and source_tz and looks_blocking:
                    queue = fleet_ctx.module('queue')
                    if queue is not None:
                        try:
                            queue.block(
                                fleet_ctx.store,
                                agent,
                                f'автоблок: ждём {draft_tz}; {summary.strip()[:300]}',
                            )
                            auto_blocked = True
                        except ValueError:
                            # Claim мог уже освободиться между чтением и созданием заявки.
                            pass
            except Exception as exc:
                return _fail('request_note', args, exc)

            audit.record('request_note', args, decision='allow', reason='fleet_request', agent=agent)
            payload = (
                f'Заявка стала задачей флота: id={request_id}, задача={draft_tz}, адресат={target}. '
                'Она уже в очереди и будет выдана свободному агенту — ответа владельца не жди.'
            )
            if auto_blocked:
                payload += (
                    ' Текущая задача автоматически освобождена и frozen до выполнения этой зависимости; '
                    'после её принятия она сама вернётся в ready. Бери следующую доступную задачу.'
                )
            else:
                payload += ' Если это не blocker — продолжай текущую задачу; отдельного owner approve нет.'
            return fleet_ctx.with_envelope(agent, payload)

        now_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
        entry = {
            'id': request_id,
            'ts': now_iso,
            'kind': k,
            'summary': summary.strip()[:500],
            'detail': (detail or '').strip()[:4000],
        }
        if agent:
            entry['agent'] = agent
        if path and path.strip():
            entry['path'] = path.strip()[:1000]
        if cleaned_lists:
            entry['lists'] = cleaned_lists
        # Решение — сразу, автоматом (request_triage): владелец очередь не разбирает, а
        # вкладка, ждущая ответа, не должна держать claim и получать «продолжи» раз за разом.
        request_triage.triage_pending([entry], policy, stamp=now_iso)
        denied_permission_blocked = False
        if agent and k in ('access', 'bin', 'root', 'network') and entry.get('decision') == 'denied':
            queue = fleet_ctx.module('queue')
            if queue is not None:
                try:
                    queue.block(
                        fleet_ctx.store,
                        agent,
                        f"автоблок: request_note {k} denied; {entry.get('note', '')[:300]}",
                    )
                    denied_permission_blocked = True
                except ValueError:
                    # Нет active claim / claim уже освободился: блокировать нечего.
                    pass
        try:
            with requests_history_path.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except OSError as exc:
            return _fail('request_note', args, PolicyError(f'не записалось: {exc}'))
        audit.record('request_note', args, decision='allow', reason=f'{k}:{entry["decision"]}', agent=agent)
        payload = f'Заявка {entry["id"]} ({k}) разобрана автоматически: {entry["decision"]}. {entry["note"]}'
        if denied_permission_blocked:
            payload += (
                ' Текущий claim автоматически освобождён, задача frozen: не жди владельца и не повторяй заявку. '
                'Сразу бери следующую доступную задачу.'
            )
        return fleet_ctx.with_envelope(agent, payload)

    @mcp.tool(
        name='request_status',
        description=(
            'Статус ранее оставленной заявки по id из request_note. Legacy access/root возвращает '
            'approved/denied; заявка, превращённая в fleet-задачу, возвращает её lifecycle '
            '(ready/taken/submitted/accepted/frozen). Ответа владельца для fleet-задачи ждать не нужно.'
        ),
    )
    def request_status(id: str) -> str:
        args = {'id': id}
        try:
            _drain_legacy_requests()
        except Exception:
            pass
        target = (id or '').strip()
        if not target:
            return _fail('request_status', args, PolicyError('id пуст'))
        for e in _request_rows(requests_path, requests_history_path):
            if e.get('id') != target:
                continue
            status = e.get('decision') or 'pending'
            tail = f' — {e["note"]}' if e.get('note') else ''
            when = f' ({e["reviewed_at"]})' if e.get('reviewed_at') else ''
            return _ok('request_status', args, f'{target}: {status}{when}{tail}')

        # Non-access request_note больше не живёт в legacy JSONL: он сразу становится
        # request:<id> задачей флота. Старый request_status отвечал «не найдена», и агент
        # начинал повторять заявку/ждать owner, хотя dependency уже была в очереди.
        try:
            with fleet_ctx.store.tx() as tx:
                fleet_request = tx.one(
                    """SELECT project,tz,state FROM tasks
                       WHERE kind='request' AND body LIKE ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (f'[fleet-request] id={target}\n%',),
                )
        except Exception:
            fleet_request = None
        if fleet_request is not None:
            project = str(fleet_request['project'])
            tz = str(fleet_request['tz'])
            state = str(fleet_request['state'])
            return _ok(
                'request_status',
                args,
                f'{target}: fleet task {project}/{tz} — {state}. '
                'Это автоматическая dependency: ответа владельца не жди.',
            )
        return _fail('request_status', args, PolicyError(f'заявка id={target} не найдена'))

    @mcp.tool(
        name='request_list',
        description='Показывает только заявки, которые ещё ждут решения владельца.',
    )
    def request_list() -> str:
        try:
            _drain_legacy_requests()
        except Exception:
            pass
        if not requests_path.exists():
            return _ok('request_list', {}, '(заявок нет)')
        out = []
        for e in _request_rows(requests_path)[-50:]:
            if e.get('decision'):
                continue  # совместимость со старой очередью до первого review.cmd
            try:
                tag = f'#{e["id"]} ' if e.get('id') else ''
                head = f'[{e["ts"]}] {tag}(pending) {e["kind"]}: {e["summary"]}'
                if e.get('detail'):
                    head += f' — {e["detail"]}'
                out.append(head)
            except KeyError:
                continue
        return _ok('request_list', {}, '\n'.join(out) or '(заявок нет)')

    # ---------- живое перечитывание policy.toml ----------

    @mcp.tool(
        name='policy_reload',
        description=(
            'Форсирует немедленное перечитывание policy.toml прямо сейчас. Обычно не '
            'нужен: сервер сам подхватывает правку файла в фоне за несколько секунд '
            '(см. watch_policy_file). Полезен сразу после того, как владелец одобрил '
            'заявку через review_requests.py, — не ждать очередного опроса и убедиться, '
            'что новое право уже действует.'
        ),
    )
    def policy_reload() -> str:
        if not policy_path:
            return _fail(
                'policy_reload', {},
                PolicyError('сервер запущен без --policy пути — reload недоступен, только сам файл (см. владельца)'),
            )
        try:
            policy.reload_from(policy_path)
        except (PolicyError, OSError) as exc:
            return _fail('policy_reload', {}, exc)
        d = policy.describe()
        return _ok(
            'policy_reload', {},
            f'policy.toml перечитан. roots={len(d["roots"])} write_roots={len(d["write_roots"])} '
            f'exec_roots={len(d["exec_roots"])} tasks={len(d["tasks"])}',
        )

    @mcp.tool(
        name='fleet_op',
        description=(
            'Безопасный оператор эффектов: только имя из неизменяемого каталога и JSON-параметры. '
            'Бюджеты, секреты, shell, пути к драйверам и полномочия нельзя задавать клиентом. '
            'Операцию исполняет только отдельно запущенный доверенный Operator-процесс; '
            'если IPC-клиент не подключён, возвращается operator_unavailable без эффекта.'
        ),
    )
    def fleet_op(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        # Не передавать raw params в audit, _fail или _ok: URL или имя файла
        # могут содержать личные данные. Обязательная валидация ДО IPC.
        try:
            validated = validate_operation(operation, params)
        except CatalogError as exc:
            return {'ok': False, 'error': {'code': str(exc)}}
        if operator_client is None:
            return {'ok': False, 'error': {'code': 'operator_unavailable'}}
        try:
            return verify_public_result(
                operator_client(validated.name, dict(validated.params)), operation
            )
        except Exception:
            # Ошибка IPC/драйвера может содержать конфиденциальный payload.
            return {'ok': False, 'error': {'code': 'operator_transport_failed'}}

    # --------- owner dashboard: narrow versioned projection and visual proofs ---------
    # Dashboard tokens are scoped to these four data tools, NOT OwnerSessions:
    # the latter also authorizes fleet_owner and must never see a dashboard token.
    # In-memory grants vanish on restart; no raw personal context leaves here.
    dashboard_sessions: dict[str, float] = {}
    dashboard_lock = threading.Lock()
    dashboard_ttl_s = 3600.0

    def _dashboard_denied() -> str:
        return json.dumps({"status": "error", "reason": "owner_authorization_required"},
                          ensure_ascii=False)

    def _dashboard_owner(session_token: str) -> bool:
        if not isinstance(session_token, str) or not re.fullmatch(
                r"dash-[A-Za-z0-9_-]{43}", session_token):
            return False
        with dashboard_lock:
            until = dashboard_sessions.get(session_token, 0.0)
            if until <= time.time():
                dashboard_sessions.pop(session_token, None)
                return False
            return True

    @mcp.tool(
        name="owner_dashboard_login",
        description="Exchange locally configured owner password for a short-lived dashboard capability.",
    )
    def owner_dashboard_login(password: str) -> str:
        owner = fleet_ctx.owner
        if (owner is None or not isinstance(password, str)
                or not password or len(password) > 1024):
            return _dashboard_denied()
        # Reuse OwnerSessions' existing scrypt and lockout policy, but only
        # for an unexposed, one-use identity. Immediately revoke that identity
        # before minting the dashboard-only capability.
        challenge = "dashboard-auth-" + secrets.token_urlsafe(32)
        try:
            try:
                ok, _ = owner.unlock(challenge, password)
            finally:
                owner.lock(challenge)
        except Exception:
            ok = False
        if not ok:
            return _dashboard_denied()
        token = "dash-" + secrets.token_urlsafe(32)
        now = time.time()
        with dashboard_lock:
            for expired in [key for key, until in dashboard_sessions.items() if until <= now]:
                del dashboard_sessions[expired]
            dashboard_sessions[token] = now + min(dashboard_ttl_s, owner.ttl_s)
        return json.dumps({"status": "ready", "session_token": token})

    @mcp.tool(
        name="owner_dashboard_overview",
        description="Owner-granted role only: versioned results tree, proof stages and typed waits; no raw personal context.",
    )
    def owner_dashboard_overview(session_token: str, project: str = "") -> str:
        if not _dashboard_owner(session_token):
            return _dashboard_denied()
        result = owner_overview.snapshot(fleet_store, project=project or None)
        body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        # The public MCP gateway has a ~15 KiB envelope. Never hand it a
        # truncated JSON tree or leak raw content through fallback formatting.
        if len(body) > MAX_RESPONSE_CHARS:
            return json.dumps({
                "schema": 1, "status": "error", "message": (
                    "Снимок слишком велик для одного ответа. "
                    "Укажите конкретный проект."
                ), "reason": "result_too_large", "intents": [], "issues": [],
                "revision": "0" * 64,
            }, ensure_ascii=False)
        return body

    def _owner_done_action(project: str, action_id: str,
                           expected_revision: str) -> str:
        """Acknowledge an action in the public owner projection under one write lock.

        This is distinct from the outcome/obstacle acknowledgement below. The
        newer dashboard can name a public action and projection revision; the
        existing obstacle UI continues to use its own subject-version contract.
        """
        def refused(reason: str) -> str:
            return json.dumps({"status": "error", "reason": reason})

        if (not isinstance(project, str) or not 0 < len(project) <= 256
                or project != project.strip()
                or any(ord(char) < 32 for char in project)
                or not isinstance(action_id, str)
                or not re.fullmatch(r"[0-9a-f]{20}", action_id)
                or not isinstance(expected_revision, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_revision)):
            return refused("invalid_owner_action")

        try:
            with fleet_store.tx() as tx:
                existing = tx.one(
                    "SELECT payload FROM events WHERE kind='owner.done' AND project=? "
                    "AND json_extract(payload, '$.action_id')=? LIMIT 1",
                    (project, action_id),
                )
                if existing is not None:
                    # A replay is idempotent, but must not acknowledge a
                    # different revision under the same opaque action ID.
                    receipt = json.loads(existing['payload'])
                    if receipt.get('revision') != expected_revision:
                        return refused("stale_revision")
                    return json.dumps({"status": "ready", "project": project,
                                       "action_id": action_id, "already_done": True})

                # Store.read() reuses the active write transaction, so neither
                # the revision nor human.actions may change before insertion.
                snapshot = owner_overview.snapshot(fleet_store)
                if snapshot.get('status') != 'ready':
                    return refused("action_unavailable")
                if snapshot.get('revision') != expected_revision:
                    return refused("stale_revision")
                human = snapshot.get('human')
                actions = human.get('actions') if isinstance(human, dict) else None
                if (not isinstance(actions, list)
                        or not any(isinstance(action, dict)
                                   and action.get('action_id') == action_id
                                   and action.get('project', project) == project
                                   and action.get('done') is not True
                                   for action in actions)):
                    return refused("action_unavailable")
                tx.record_event(
                    kind='owner.done', project=project,
                    payload={'action_id': action_id,
                             'revision': expected_revision,
                             'source': 'owner_dashboard'},
                )
            return json.dumps({"status": "ready", "project": project,
                               "action_id": action_id, "already_done": False})
        except Exception:
            return refused("owner_action_unavailable")

    @mcp.tool(
        name="owner_dashboard_done",
        description=(
            "Owner-only acknowledgement, without fabricating completion proof. "
            "Supply either project/action_id/expected_revision for a current "
            "public dashboard action or obstacle_id/outcome_id/"
            "expected_subject_version for an owner_action obstacle; never both."
        ),
    )
    def owner_dashboard_done(session_token: str, obstacle_id: str | None = None,
                             outcome_id: str | None = None,
                             expected_subject_version: str | None = None,
                             project: str | None = None,
                             action_id: str | None = None,
                             expected_revision: str | None = None) -> str:
        # The dashboard password exchange is the ONLY way to mint a capability.
        # Neither a worker ID nor a copied obstacle ID authorizes an owner action.
        if not _dashboard_owner(session_token):
            return _dashboard_denied()
        def refused(reason: str) -> str:
            return json.dumps({"status": "error", "reason": reason},
                              ensure_ascii=False, separators=(",", ":"))
        by_action = any(value is not None for value in
                        (project, action_id, expected_revision))
        by_obstacle = any(value is not None for value in
                          (obstacle_id, outcome_id, expected_subject_version))
        if by_action:
            if by_obstacle:
                return refused("mixed_action_reference")
            return _owner_done_action(project, action_id, expected_revision)
        if not by_obstacle:
            return refused("invalid_action_reference")
        # Keep internal record IDs opaque; never accept arbitrary paths, SQL
        # snippets or client-controlled confirmation text into the audit event.
        if (not isinstance(obstacle_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:#-]{1,128}", obstacle_id)
                or not isinstance(outcome_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:#-]{1,128}", outcome_id)
                or not isinstance(expected_subject_version, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:#-]{1,128}", expected_subject_version)):
            return refused("invalid_action_reference")
        try:
            with fleet_store.tx() as tx:
                obstacle = tx.one(
                    "SELECT b.id,b.outcome_id,b.type,b.state AS obstacle_state,"
                    "o.state AS outcome_state,o.rev,o.evidence_spec "
                    "FROM obstacles b JOIN outcomes o ON o.id=b.outcome_id "
                    "WHERE b.id=? AND b.outcome_id=?",
                    (obstacle_id, outcome_id),
                )
                if obstacle is None or obstacle["type"] != "owner_action":
                    return refused("owner_action_not_found")
                try:
                    spec = json.loads(obstacle["evidence_spec"])
                except (TypeError, ValueError):
                    return refused("invalid_outcome_version")
                if not isinstance(spec, dict):
                    return refused("invalid_outcome_version")
                current_version = spec.get("subject_version") or f"rev:{obstacle['rev']}"
                if (not isinstance(current_version, str)
                        or current_version != expected_subject_version):
                    return refused("stale_action_reference")
                # An identical retry must be idempotent. In particular it must
                # not fabricate multiple completions or re-open a closed item.
                if not tx.scalar(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                    "AND name='owner_dashboard_done_receipts'"
                ):
                    tx.change(
                        "CREATE TABLE IF NOT EXISTS owner_dashboard_done_receipts("
                        "obstacle_id TEXT PRIMARY KEY, outcome_id TEXT NOT NULL,"
                        "subject_version TEXT NOT NULL, recorded_at TEXT NOT NULL)",
                        event="owner.dashboard_done_schema",
                    )
                existing = tx.one(
                    "SELECT outcome_id,subject_version,recorded_at "
                    "FROM owner_dashboard_done_receipts WHERE obstacle_id=?",
                    (obstacle_id,),
                )
                if existing is not None:
                    if (existing["outcome_id"] != outcome_id
                            or existing["subject_version"] != expected_subject_version):
                        return refused("stale_action_reference")
                    return json.dumps({
                        "status": "recorded_pending_verification",
                        "obstacle_id": obstacle_id, "outcome_id": outcome_id,
                        "subject_version": expected_subject_version, "replayed": True,
                    }, ensure_ascii=False, separators=(",", ":"))
                if (obstacle["obstacle_state"] != "open"
                        or obstacle["outcome_state"] != "open"):
                    return refused("action_no_longer_open")
                # A click is an owner ATTESTATION, not proof that an external
                # physical/private/money action succeeded. Keep the obstacle
                # open; the controller must verify and close it independently.
                recorded_at = datetime.now(timezone.utc).isoformat()
                tx.change(
                    "INSERT INTO owner_dashboard_done_receipts "
                    "(obstacle_id,outcome_id,subject_version,recorded_at) "
                    "VALUES (?,?,?,?)",
                    (obstacle_id, outcome_id, current_version, recorded_at),
                    event={"kind": "owner.dashboard_done_reported",
                           "payload": {"obstacle_id": obstacle_id,
                                       "outcome_id": outcome_id,
                                       "subject_version": current_version}},
                )
            return json.dumps({
                "status": "recorded_pending_verification",
                "obstacle_id": obstacle_id, "outcome_id": outcome_id,
                "subject_version": expected_subject_version, "replayed": False,
            }, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            # DB exceptions may disclose private identifiers, values or file
            # paths; never return their messages to the browser.
            return refused("recording_unavailable")

    @mcp.tool(
        name="owner_dashboard_gallery",
        description="Owner-granted role only: public labels and opaque verified visual proof IDs for a taste decision.",
    )
    def owner_dashboard_gallery(outcome_id: str, session_token: str) -> str:
        if not _dashboard_owner(session_token):
            return _dashboard_denied()
        return json.dumps(owner_taste.gallery_snapshot(fleet_store, outcome_id),
                          ensure_ascii=False, separators=(",", ":"))

    @mcp.tool(
        name="owner_dashboard_choose",
        description="Owner-granted role only: CAS visual taste selection, atomic DecisionBook receipt; never product QA.",
    )
    def owner_dashboard_choose(outcome_id: str, option_id: str,
                               expected_revision: int, session_token: str) -> str:
        if not _dashboard_owner(session_token):
            return _dashboard_denied()
        try:
            result = owner_taste.choose_taste(
                fleet_store, outcome_id=outcome_id, option_id=option_id,
                expected_revision=expected_revision,
                owner_authorized=True,  # checked against OwnerSessions above
            )
        except (ValueError, PermissionError):
            result = {"status": "error", "reason": "stale_or_unverified_choice"}
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    @mcp.tool(
        name="owner_dashboard_image",
        description="Owner-granted role only: allowlisted binary image from a CURRENT server-verdict screen proof.",
    )
    def owner_dashboard_image(outcome_id: str, image_proof_id: str,
                              session_token: str) -> str:
        if not _dashboard_owner(session_token):
            return _dashboard_denied()
        gallery = owner_taste.gallery_snapshot(fleet_store, outcome_id)
        if gallery.get("status") not in {"ready", "chosen"} or not any(
            item["image_proof_id"] == image_proof_id
            for item in gallery.get("options", ())
        ):
            return json.dumps({"status": "error", "reason": "unverified_image"})
        try:
            with fleet_store.read() as tx:
                row = tx.one(
                    "SELECT e.ref,e.kind,e.verdict,e.subject_version,o.project,p.root "
                    "FROM evidence e JOIN outcomes o ON o.id=e.outcome_id "
                    "JOIN projects p ON p.name=o.project "
                    "WHERE e.id=? AND e.outcome_id=?",
                    (image_proof_id, outcome_id),
                )
            if (row is None or row["kind"] != "screen" or row["verdict"] != "pass"
                    or row["subject_version"] != gallery["subject_version"]):
                raise ValueError("invalid evidence")
            reference = str(row["ref"])
            # No URI, relative client path, symlink escape or cross-project read.
            if not ntpath.isabs(reference) or reference.startswith(("\\\\.\\", "\\\\?\\")):
                raise ValueError("untrusted file reference")
            image_path = policy.resolve(reference, for_write=False)
            project_root = Path(str(row["root"])).resolve()
            image_path.relative_to(project_root)
            suffix = image_path.suffix.casefold()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("untrusted image format")
            with image_path.open("rb") as source:
                raw = source.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024 or not raw:
                raise ValueError("image too large")
            mime = (
                "image/png" if suffix == ".png" and raw.startswith(b"\x89PNG\r\n\x1a\n")
                else "image/jpeg" if suffix in {".jpg", ".jpeg"} and raw.startswith(b"\xff\xd8\xff")
                else "image/webp" if suffix == ".webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
                else None
            )
            if mime is None:
                raise ValueError("image bytes and format mismatch")
            return json.dumps({"mime": mime, "base64": base64.b64encode(raw).decode("ascii")})
        except (OSError, ValueError, PolicyError):
            return json.dumps({"status": "error", "reason": "unverified_image"})

    register_fleet_tools(mcp, fleet_ctx)
    return mcp
