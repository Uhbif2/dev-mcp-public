"""Политика доступа — ЯДРО безопасности dev-mcp.

Модель — allowlist, а не blacklist. Разрешено ровно то, что перечислено в
`policy.toml`; всё остальное отказывается. Причина выбора именно такой модели
описана в README (коротко: shell тьюринг-полон, чёрный список команд обходится
случайно, а не только злым умыслом).

Здесь НЕТ ни одного вызова оболочки. Единственное, что делает этот модуль —
превращает пришедшую снаружи строку в путь и решает, лежит ли он внутри
разрешённого корня. Всё остальное (инструменты, git, задачи) полагается на этот
ответ, поэтому любая дыра здесь — дыра во всём сервере.

Windows-специфика, из-за которой наивное сравнение строк не работает:

* `..` и симлинки/junction — разворачиваются `os.path.realpath`;
* короткие имена 8.3 (`C:\\MYFOLD~1\\...`) — реально встречаются на этой машине
  (см. mcp_launcher.py), realpath их разворачивает НЕ всегда, поэтому путь
  проверяется ещё и после разворота ближайшего существующего предка;
* регистр — сравнение через `os.path.normcase`;
* UNC (`\\\\server\\share`), device-пути (`\\\\?\\`, `\\\\.\\`) и alternate data
  streams (`file.txt:stream`) — отклоняются целиком, они умеют указывать мимо
  корня в обход обычной нормализации.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path


# Отказ без пути дальше обрывает ход агента (07.10.2026: rg → deny → конец хода). Говорим, чем можно.
_SEARCH_HINT = ' Искать по файлам — инструмент search_files (regex, glob), он быстрее и не требует run.'
_TOOL_HINTS = {
    **{b: _SEARCH_HINT for b in ('rg', 'grep', 'findstr', 'ag', 'ack', 'select-string')},
    **{b: ' Читать файл — read_file; список папки — list_dir.' for b in ('cat', 'type', 'head', 'tail', 'less', 'more', 'ls', 'dir', 'find', 'tree')},
    **{b: ' Править файл — edit_file / apply_patch.' for b in ('sed', 'awk', 'perl')},
    **{b: ' Пульт флота для агента — run(argv=["fleet.cmd", "result", "<Проект>/<TZ>"]) только на чтение '
          '(result, active, decisions, flow).' for b in ('fleet', 'fleet.cmd')},
}

class PolicyError(Exception):
    """Отказ политики. Текст уходит клиенту — писать понятно и без внутренностей."""


class ScanTimeout(Exception):
    """Обход дерева не уложился в бюджет времени.

    Не отказ политики: клиенту отдаётся то, что успели найти, с честной
    пометкой. Отдельный тип нужен, чтобы search_files/list_dir не путали
    «не успел» с «не разрешено» — это разные сообщения для клиента.
    """


# FILE_ATTRIBUTE_REPARSE_POINT. Симлинк и junction на Windows — единственный
# способ увести обход за пределы корня, не меняя видимого пути, поэтому
# Policy.scan обходит их стороной (см. там же).
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


# Пути, которые не обрабатываем вообще: их семантика уводит мимо обычной
# нормализации, а законных применений в рамках dev-mcp у них нет.
# Отличает «в кэше лежит None, масок нет» от «в кэше пусто, ещё не считали».
_UNSET = object()

_UNC_PREFIXES = ('\\\\', '//')
_DEVICE_PREFIXES = ('\\\\?\\', '\\\\.\\', '//?/', '//./')

# Бинари, которые `run` разрешает как argv[0], если exec_allowed_bin не задан в
# конфиге. Список — про читаемость журнала и отсечение тривиального pivot в shell,
# а НЕ про «невозможность RCE»: как только в списке есть python/node, клиент внутри
# exec_root может исполнить что угодно. Именно поэтому exec_root — осознанный грант
# доверия (см. комментарий к exec_roots в policy.toml), а не «ещё одна папка».
_DEFAULT_EXEC_BIN = [
    'python', 'python3', 'py', 'pip', 'pipx', 'uv',
    'pytest', 'ruff', 'mypy', 'pyright', 'black', 'isort',
    'node', 'npm', 'npx', 'pnpm', 'yarn', 'deno', 'bun', 'tsc',
    'go', 'cargo', 'rustc', 'dotnet',
    'sing-box', 'xray', 'ffmpeg', 'rg', 'make', 'cmake', 'git',
]

# Оболочки и универсальные интерпретаторы командной строки: их наличие в
# exec_allowed_bin отменяет весь смысл `shell=False` (клиент передаёт
# `cmd /c "<что угодно>"`). Попадание любого из них в конфиг — жёсткий отказ на
# загрузке, как забытая deny-маска.
_SHELL_BIN_BLOCKLIST = {
    'cmd', 'cmd.exe', 'command.com',
    'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe',
    'bash', 'sh', 'zsh', 'dash', 'ksh', 'fish', 'ash', 'busybox',
    'wscript', 'wscript.exe', 'cscript', 'cscript.exe', 'mshta', 'mshta.exe',
    'rundll32', 'rundll32.exe', 'regsvr32', 'regsvr32.exe',
    'start', 'call',
}

# Окружение, которое `run` передаёт дочернему процессу. Всё, чего здесь нет, —
# срезается: mcp-proxy запущен с `--pass-environment`, поэтому в процесс сервера
# протекает окружение родителя целиком (ключи google-service-account, всё из
# профиля пользователя), и без явного среза каждый `run` отдавал бы это дочернему
# коду недоверенного клиента. Сравнение имён регистронезависимое (Windows).
_ENV_PASSTHROUGH = {
    'systemroot', 'windir', 'path', 'pathext', 'comspec', 'os',
    'temp', 'tmp', 'number_of_processors', 'processor_architecture',
    'processor_identifier', 'processor_level', 'processor_revision',
    'homedrive', 'homepath', 'userprofile', 'username', 'userdomain',
    'appdata', 'localappdata', 'programdata', 'allusersprofile',
    'programfiles', 'programfiles(x86)', 'programw6432',
    'commonprogramfiles', 'commonprogramfiles(x86)', 'commonprogramw6432',
    'public', 'sessionname', 'systemdrive', 'lang', 'lc_all', 'tz',
    'pythonutf8', 'pythonioencoding', 'pythondontwritebytecode',
}
# Дополнительно: имя переменной с таким фрагментом срезается ВСЕГДА, даже если
# как-то попало в passthrough — защита от «случайно назвали ключ SYSTEMKEY».
_ENV_NAME_DENY_FRAGMENTS = ('key', 'token', 'secret', 'password', 'passwd', 'credential', 'auth')


def _which_real(name: str) -> str | None:
    """shutil.which, но мимо Store-заглушек python из ...\\WindowsApps\\.

    Эти алиасы — нулёвые reparse-файлы, которые открывают Microsoft Store и
    возвращают код 9009. Для python/py вместо заглушки берём базовый интерпретатор
    (sys.base_prefix) — реальный system-python этой машины.
    """
    found = shutil.which(name)
    stem = name[:-4].lower() if name.lower().endswith('.exe') else name.lower()
    is_stub = found and os.sep + 'windowsapps' + os.sep in found.lower()
    if (not found or is_stub) and stem in ('python', 'python3', 'py'):
        for cand in (
            os.path.join(sys.base_prefix, 'python.exe'),
            os.path.join(sys.base_prefix, 'bin', 'python3'),
            sys.executable,
        ):
            if cand and os.path.isfile(cand):
                return str(Path(os.path.realpath(cand)))
    if is_stub:
        return None
    return str(Path(os.path.realpath(found))) if found else None


def scrub_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Минимальное безопасное окружение для дочернего процесса `run` / `run_task`."""
    src = dict(os.environ if source is None else source)
    out: dict[str, str] = {}
    for name, value in src.items():
        low = name.lower()
        if low not in _ENV_PASSTHROUGH:
            continue
        if any(frag in low for frag in _ENV_NAME_DENY_FRAGMENTS):
            continue
        out[name] = value
    return out


@dataclass(frozen=True)
class Task:
    """Именованная команда. Аргументы фиксированы в конфиге, клиент их не задаёт."""

    name: str
    cwd: str
    argv: list[str]
    description: str = ''
    timeout_s: int = 300


@dataclass(slots=True)
class ScanItem:
    """Одна находка обхода. size у каталога не заполняется."""

    path: Path
    name: str
    is_dir: bool
    size: int


@dataclass
class Policy:
    roots: list[Path]
    write_roots: list[Path] = field(default_factory=list)
    exec_roots: list[Path] = field(default_factory=list)
    # Доверенные деревья: запись/запуск лишь ВНУТРИ их подпапок. Файлы
    # непосредственно в tree_root остаются за владельцем даже при allow_write.
    tree_roots: list[Path] = field(default_factory=list)
    # Исключения из деревьев для записи и исполнения; чтение сохраняется.
    tree_deny: list[Path] = field(default_factory=list)
    exec_allowed_bin: list[str] = field(default_factory=list)
    exec_args_unrestricted: list[str] = field(default_factory=list)
    exec_bin_project_local: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)
    ignore_patterns: list[str] = field(default_factory=list)
    tasks: dict[str, Task] = field(default_factory=dict)
    allow_write: bool = False
    allow_network_fetch: bool = False
    fetch_max_bytes: int = 100_000_000
    exec_timeout_cap_s: int = 1800
    # Сколько секунд синхронный `run`/`run_task` ЖДЁТ команду, прежде чем отдать
    # её в фон и вернуть job_id вместо ответа. Это не таймаут (процесс не убивают,
    # он доживает до своего timeout_s) — это бюджет ОДНОГО вызова MCP.
    # 19.09.2026: наружу dev-mcp ходит через шлюз Oracle, а там у воркера жёсткий
    # потолок на локальный вызов (WORKER_LOCAL_MCP_TIMEOUT). Любой вызов длиннее
    # этого потолка приходил агенту как 502 — «dev-mcp отвалился», — хотя команда
    # на машине спокойно доработала: 111 из 121 задачи длиннее 31с закончились так.
    # Агент терял результат и перезапускал сборку заново. Держать заметно ниже
    # WORKER_LOCAL_MCP_TIMEOUT (сейчас 45с).
    exec_sync_budget_s: int = 35
    max_read_bytes: int = 400_000
    max_write_bytes: int = 400_000
    max_list_entries: int = 500
    max_search_results: int = 200
    # {имя_бинаря: абсолютный путь} — заполняется на загрузке через shutil.which.
    _exec_bin_resolved: dict[str, str] = field(default_factory=dict)
    _exec_bin_missing: list[str] = field(default_factory=list)
    # Гасит гонку двух одновременных reload_from (вотчер-поток + ручной policy_reload).
    _reload_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    # Необязательная дополнительная стража записи. Она ставится интеграционным
    # слоем и намеренно не знает ничего про fleet: получает уже безопасный
    # абсолютный путь и возвращает объект решения с полями allow/code/reason.
    _write_guard: Callable[[str], object] | None = field(default=None, repr=False, compare=False)
    # Кэш скомпилированных масок имён: {'deny'|'ignore'|'prune': re.Pattern|None}.
    # Заполняется лениво, сбрасывается в reload_from — см. _name_re.
    _name_res: dict = field(default_factory=dict, repr=False, compare=False)

    # ---------- загрузка ----------

    @classmethod
    def load(cls, config_path: str | os.PathLike[str]) -> 'Policy':
        try:
            text = Path(config_path).read_text(encoding='utf-8')
        except OSError as exc:
            raise PolicyError(f'файл политики не читается: {exc}') from exc
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            # Самая частая ручная ошибка — забытая запятая в списке. Сообщение
            # tomllib уже содержит «(at line N, column M)».
            raise PolicyError(f'синтаксис TOML сломан — {exc}') from exc

        roots: list[Path] = []
        for item in raw.get('roots', []):
            resolved = Path(os.path.realpath(os.path.abspath(os.path.expanduser(item))))
            if not resolved.exists():
                raise PolicyError(f'корень из policy.toml не существует: {item}')
            roots.append(resolved)
        if not roots:
            raise PolicyError('в policy.toml не задан ни один корень — сервер бесполезен')

        # write_roots — точечное право записи: читать можно везде в roots, писать
        # только здесь. Это дешевле глобального allow_write, потому что открывает
        # ровно рабочую папку, а не весь корень с исходниками.
        write_roots = [
            cls._resolve_inside_roots(item, roots, 'write_root')
            for item in raw.get('write_roots', [])
        ]

        # exec_roots — право исполнить код (`run`). Отдельный список, НЕ производный
        # от write_roots: exec_root, который ещё и write_root, — это фактически RCE
        # для клиента в этом дереве (записал .py в граф импортов → запустил).
        # Поэтому решение о каждой папке принимается осознанно и отдельно.
        exec_roots = [
            cls._resolve_inside_roots(item, roots, 'exec_root')
            for item in raw.get('exec_roots', [])
        ]

        tree_roots = [
            cls._resolve_inside_roots(item, roots, 'tree_root')
            for item in raw.get('tree_roots', [])
        ]
        for root in tree_roots:
            if not root.is_dir():
                raise PolicyError(f'tree_root должен быть каталогом: {root}')
        tree_deny: list[Path] = []
        for item in raw.get('tree_deny', []):
            # Запреты могут относиться к ещё не созданным папкам. realpath
            # существующего префикса учитывает symlink/junction и здесь.
            denied = cls._real(Path(os.path.abspath(os.path.expanduser(item))))
            if not any(cls._within(denied, root, strict=True) for root in tree_roots):
                raise PolicyError(f'tree_deny вне tree_roots: {item}')
            tree_deny.append(denied)

        exec_allowed_bin = list(raw.get('exec_allowed_bin', _DEFAULT_EXEC_BIN))
        for name in exec_allowed_bin:
            base = os.path.basename(str(name)).lower()
            if base in _SHELL_BIN_BLOCKLIST or Path(base).stem in _SHELL_BIN_BLOCKLIST:
                raise PolicyError(
                    f'exec_allowed_bin содержит оболочку «{name}» — это отменяет shell=False. '
                    'Убери; для .cmd-обёрток заводи именованную задачу в [tasks.*].'
                )

        # Бинари, которым guard_exec_args НЕ режет абсолютные пути и `..` в argv[1:].
        # Нужно для инструментов, где путь к ресурсу выглядит абсолютным по своей
        # природе (adb: `/sdcard/...`). Это не ослабляет периметр: та же дыра уже
        # открыта через `python -c "..."` из любого exec_root (см. README). NUL и
        # предел длины аргумента проверяются всегда. Освобождать можно только
        # бинарь, уже перечисленный в exec_allowed_bin.
        exec_args_unrestricted = list(raw.get('exec_args_unrestricted', []))
        _allowed_stems = {
            (b[:-4] if str(b).lower().endswith('.exe') else str(b)).lower()
            for b in exec_allowed_bin
        }
        for name in exec_args_unrestricted:
            base = os.path.basename(str(name)).lower()
            stem = base[:-4] if base.endswith('.exe') else base
            if stem not in _allowed_stems:
                raise PolicyError(
                    f'exec_args_unrestricted содержит «{name}», которого нет в exec_allowed_bin — '
                    'освобождать от проверки аргументов можно только уже разрешённый бинарь.'
                )

        # Бинари проекта (sing-box.exe лежит в build/ Nova VPN): зовутся только
        # относительным путём внутри exec_root, в PATH сервера их нет намеренно —
        # «нет в PATH» для них не поломка, и боевой страж restart их не считает.
        exec_bin_project_local = list(raw.get('exec_bin_project_local', []))
        for name in exec_bin_project_local:
            base = os.path.basename(str(name)).lower()
            if (base[:-4] if base.endswith('.exe') else base) not in _allowed_stems:
                raise PolicyError(
                    f'exec_bin_project_local содержит «{name}», которого нет в exec_allowed_bin.'
                )

        limits = raw.get('limits', {})
        tasks: dict[str, Task] = {}
        for name, spec in raw.get('tasks', {}).items():
            tasks[name] = Task(
                name=name,
                cwd=spec['cwd'],
                argv=list(spec['argv']),
                description=spec.get('description', ''),
                timeout_s=int(spec.get('timeout_s', 300)),
            )

        policy = cls(
            roots=roots,
            write_roots=write_roots,
            exec_roots=exec_roots,
            tree_roots=tree_roots,
            tree_deny=tree_deny,
            exec_allowed_bin=exec_allowed_bin,
            exec_args_unrestricted=exec_args_unrestricted,
            exec_bin_project_local=exec_bin_project_local,
            deny_patterns=list(raw.get('deny_patterns', [])),
            ignore_patterns=list(raw.get('ignore_patterns', [])),
            tasks=tasks,
            allow_write=bool(raw.get('allow_write', False)),
            allow_network_fetch=bool(raw.get('allow_network_fetch', False)),
            fetch_max_bytes=int(raw.get('fetch_max_bytes', 100_000_000)),
            exec_timeout_cap_s=int(raw.get('exec_timeout_cap_s', 1800)),
            exec_sync_budget_s=int(raw.get('exec_sync_budget_s', 35)),
            max_read_bytes=int(limits.get('max_read_bytes', 400_000)),
            max_write_bytes=int(limits.get('max_write_bytes', 400_000)),
            max_list_entries=int(limits.get('max_list_entries', 500)),
            max_search_results=int(limits.get('max_search_results', 200)),
        )
        policy._resolve_exec_bin()
        policy._validate_tasks()
        return policy

    def reload_from(self, config_path: str | os.PathLike[str]) -> None:
        """Перечитать policy.toml и подменить поля ЭТОГО объекта на месте.

        Не создаёт новый Policy — `server.py` держит один и тот же объект в
        замыканиях всех инструментов (`policy.resolve(...)` и т.п.), поэтому
        правка атрибутов видна им сразу же, без пересборки сервера FastMCP и
        без перезапуска процесса dev-mcp/mcp-proxy. Так работает
        `watch_policy_file` ниже и инструмент `policy_reload` в server.py —
        владелец правит policy.toml (сам или через tools/review_requests.py),
        а сервис подхватывает это в течение секунд, а не после ручного рестарта.

        Если новый файл битый — кидает PolicyError, а поля self НЕ меняются:
        `Policy.load` целиком проходит на отдельном объекте `fresh`, и только
        успешный результат копируется — старая (рабочая) политика продолжает
        действовать до следующей исправленной правки.
        """
        fresh = Policy.load(config_path)
        with self._reload_lock:
            for attr in (
                'roots', 'write_roots', 'exec_roots', 'tree_roots', 'tree_deny', 'exec_allowed_bin',
                'exec_args_unrestricted', 'exec_bin_project_local', 'deny_patterns',
                'ignore_patterns', 'tasks',
                'allow_write', 'allow_network_fetch', 'fetch_max_bytes',
                'exec_timeout_cap_s', 'exec_sync_budget_s', 'max_read_bytes', 'max_write_bytes',
                'max_list_entries', 'max_search_results',
                '_exec_bin_resolved', '_exec_bin_missing',
            ):
                setattr(self, attr, getattr(fresh, attr))
            # Маски сменились — скомпилированные regex'ы больше не описывают
            # политику. Чистим кэш, следующий вызов соберёт их заново.
            self._name_res.clear()

    @staticmethod
    def _resolve_inside_roots(item: str, roots: list[Path], label: str) -> Path:
        """Развернуть путь из конфига и потребовать, чтобы он лежал внутри roots."""
        resolved = Path(os.path.realpath(os.path.abspath(os.path.expanduser(item))))
        if not resolved.exists():
            raise PolicyError(f'{label} из policy.toml не существует: {item}')
        inside = any(
            os.path.normcase(str(resolved)) == os.path.normcase(str(root))
            or os.path.normcase(str(resolved)).startswith(
                os.path.normcase(str(root)).rstrip(os.sep) + os.sep
            )
            for root in roots
        )
        if not inside:
            raise PolicyError(
                f'{label} вне разрешённых корней: {item}. Сначала добавь его (или его '
                'родителя) в roots — иначе туда нельзя даже читать.'
            )
        return resolved

    @staticmethod
    def _within(target: Path, root: Path, *, strict: bool = False) -> bool:
        """Сегментная принадлежность пути без ловушки sibling prefix, с Windows casefold."""
        target_norm = os.path.normcase(os.path.normpath(str(target)))
        root_norm = os.path.normcase(os.path.normpath(str(root)))
        return (not strict and target_norm == root_norm) or target_norm.startswith(
            root_norm.rstrip(os.sep) + os.sep
        )

    def _tree_denied(self, path: Path) -> bool:
        return any(self._within(path, denied) for denied in self.tree_deny)

    def _tree_root_owner_only(self, path: Path) -> bool:
        """Tree root и его обычные файлы — не объект записи/исполнения агента."""
        return any(
            self._within(path, root)
            and (path == root or self._within(path.parent, root) and
                 os.path.normcase(str(path.parent)) == os.path.normcase(str(root))
                 and not path.is_dir())
            for root in self.tree_roots
        )

    def _resolve_exec_bin(self) -> None:
        """Развернуть имена из exec_allowed_bin в абсолютные пути через PATH.

        Ненайденный бинарь — не ошибка конфига (на машине может не быть go/deno):
        имя уезжает в _exec_bin_missing, `run` для него честно ответит «нет в PATH».
        """
        local = {n.lower() for n in self.exec_bin_project_local}
        for name in self.exec_allowed_bin:
            found = _which_real(name)
            if found:
                self._exec_bin_resolved[name] = found
            elif name.lower() not in local:
                self._exec_bin_missing.append(name)

    def exec_bin_path(self, name: str, cwd: Path) -> str:
        """Абсолютный путь исполняемого argv[0] или PolicyError.

        Две формы:
        * голое имя из exec_allowed_bin (`python`, `sing-box`) → берётся из PATH сервера;
        * ОТНОСИТЕЛЬНЫЙ путь внутри того же exec_root, чей basename разрешён
          (`.venv/Scripts/python.exe`, `node_modules/.bin/tsc`) → так запускаются
          проектные venv'ы, не открывая приём абсолютных путей в argv[0].
        """
        raw = name.strip()
        base = os.path.basename(raw)
        key = base[:-4] if base.lower().endswith('.exe') else base
        allowed_stems = {(b[:-4] if b.lower().endswith('.exe') else b) for b in self.exec_allowed_bin}
        if key not in allowed_stems:
            raise PolicyError(
                f'бинарь «{base}» не в списке разрешённых. Разрешены: {", ".join(self.exec_allowed_bin)}'
                + _TOOL_HINTS.get(key.lower(), '')
            )

        if raw == base:  # голое имя
            resolved = self._exec_bin_resolved.get(key) or _which_real(key)
            if resolved:
                return resolved
            if any(os.path.basename(n).lower() in {base.lower(), base.lower() + '.exe'}
                   for n in self.exec_bin_project_local):
                raise PolicyError(f'бинарь «{name}» — проектный: зови относительным путём '
                                  f'внутри exec_root (например build/.../bin/{base})')
            raise PolicyError(f'бинарь «{name}» разрешён политикой, но не найден в PATH сервера')

        # относительный путь: слэши есть, но не абсолютный и не вверх по дереву
        norm = raw.replace('\\', '/')
        if norm.startswith('/') or (len(raw) >= 2 and raw[1] == ':') or '..' in norm.split('/'):
            raise PolicyError('argv[0] как путь допустим только относительный, внутри exec_root')
        target = self.resolve(str(cwd / raw), for_write=False)
        if self.exec_root_for(target) is None:
            raise PolicyError('argv[0]-путь вне exec_root')
        if not target.is_file():
            raise PolicyError(f'argv[0]-путь не найден: {raw}')
        return str(target)

    def exec_root_for(self, real: Path) -> Path | None:
        """Каким exec_root накрыт путь (сам путь или его предок), либо None."""
        lexical = real
        real = self._real(real)
        if self._tree_denied(lexical) or self._tree_denied(real):
            return None
        if self._tree_root_owner_only(lexical) or self._tree_root_owner_only(real):
            return None
        for root in self.tree_roots:
            if self._within(real, root, strict=True):
                return root
        target = os.path.normcase(str(real))
        for root in self.exec_roots:
            root_norm = os.path.normcase(str(root))
            if target == root_norm or target.startswith(root_norm.rstrip(os.sep) + os.sep):
                return root
        return None

    def _validate_tasks(self) -> None:
        """cwd задачи обязан лежать внутри корня — иначе задача обходит границу."""
        for task in self.tasks.values():
            try:
                self.resolve(task.cwd, for_write=False)
            except PolicyError as exc:
                raise PolicyError(f'задача «{task.name}»: cwd вне разрешённых корней ({exc})') from exc

    def task_status(self, task: Task) -> tuple[bool, str]:
        """Сможет ли run_task реально выполнить задачу сейчас. ('' в reason == да.)

        run_task резолвит cwd задачи с for_write=True (сборка/тесты пишут кэш) и
        запускает task.argv напрямую. Значит задача НЕ выполнима, если её cwd вне
        write_roots ЛИБО argv[0] — голое имя, которого нет в PATH сервера. И то и
        другое policy_info раньше молчал, объявляя задачу доступной (баг-репорт
        2026-09-10: policy_info обещает memory_service-test, run_task её отклоняет).
        Считаем статус здесь, чтобы выдача его показывала честно.
        """
        if not task.argv:
            return False, 'у задачи пустой argv'
        try:
            self.resolve(task.cwd, for_write=True)
        except PolicyError:
            return False, f'cwd «{task.cwd}» не в write_roots'
        argv0 = task.argv[0]
        if os.path.basename(argv0) == argv0 and shutil.which(argv0) is None:
            return False, f'бинарь «{argv0}» не найден в PATH сервера'
        return True, ''

    # ---------- проверка пути ----------

    def set_write_guard(self, fn: Callable[[str], object] | None) -> None:
        """Установить дополнительную проверку записи поверх периметра Policy.

        Хук вызывается только после всех обычных проверок roots/deny/write_roots.
        Без хука поведение resolve() остаётся прежним.
        """
        self._write_guard = fn

    def resolve(self, raw_path: str, *, for_write: bool) -> Path:
        """Единственный вход для любого пути извне. Возвращает безопасный Path.

        Кидает PolicyError, если путь уводит наружу, попадает под deny-маску или
        запись запрещена режимом.
        """
        if not raw_path or not raw_path.strip():
            raise PolicyError('пустой путь')

        text = raw_path.strip().replace('/', os.sep) if os.name == 'nt' else raw_path.strip()

        if text.startswith(_DEVICE_PREFIXES):
            raise PolicyError('device-пути (\\\\?\\, \\\\.\\) запрещены')
        if text.startswith(_UNC_PREFIXES):
            raise PolicyError('UNC-пути (сетевые шары) запрещены')
        if '\x00' in text:
            raise PolicyError('нулевой байт в пути')

        # ADS: двоеточие законно только как разделитель диска на позиции 1.
        colon_positions = [i for i, ch in enumerate(text) if ch == ':']
        if any(pos != 1 for pos in colon_positions):
            raise PolicyError('двоеточие в пути (alternate data stream) запрещено')

        candidate = Path(os.path.abspath(os.path.expanduser(text)))
        real = self._real(candidate)

        root = self._owning_root(real)
        if root is None:
            allowed = ', '.join(str(r) for r in self.roots)
            raise PolicyError(f'путь вне разрешённых корней. Разрешено: {allowed}')

        rel = real.relative_to(root)
        self._assert_not_denied(rel, real)

        if for_write:
            if self._tree_denied(candidate) or self._tree_denied(real):
                raise PolicyError('tree_deny: запись в защищённом поддереве запрещена')
            if self._tree_root_owner_only(candidate) or self._tree_root_owner_only(real):
                raise PolicyError('файлы корня правит владелец')
            if not self._may_write(real):
                if self.write_roots or self.tree_roots:
                    allowed = ', '.join(str(r) for r in [*self.write_roots, *self.tree_roots])
                    raise PolicyError(f'сюда писать нельзя. Запись разрешена только в: {allowed}')
                raise PolicyError('сервер запущен в режиме read-only (allow_write=false)')

            guard = self._write_guard
            if guard is not None:
                decision = guard(str(real))
                allow = getattr(decision, 'allow', None)
                if allow is None:
                    raise PolicyError('write guard вернул решение без поля allow')
                if not bool(allow):
                    code = str(getattr(decision, 'code', '') or '')
                    reason = str(getattr(decision, 'reason', '') or 'write guard отклонил запись')
                    prefix = f'{code}: ' if code else ''
                    raise PolicyError(prefix + reason)

        return real

    def _may_write(self, real: Path) -> bool:
        """Право записи: глобальное allow_write ИЛИ попадание в один из write_roots.

        Deny-маски проверяются раньше и независимо, поэтому секрет внутри
        write_root всё равно закрыт — точечное право записи не делает дырку
        в маске.
        """
        if self.allow_write:
            return True
        if any(self._within(real, root, strict=True) for root in self.tree_roots):
            return True
        target = os.path.normcase(str(real))
        for root in self.write_roots:
            root_norm = os.path.normcase(str(root))
            if target == root_norm or target.startswith(root_norm.rstrip(os.sep) + os.sep):
                return True
        return False

    @staticmethod
    def _real(path: Path) -> Path:
        """realpath, устойчивый к несуществующему хвосту.

        Для нового файла (запись) сам путь ещё не существует, а realpath на
        Windows несуществующий хвост не разворачивает — поэтому разворачиваем
        ближайшего существующего предка и приклеиваем хвост обратно. Иначе
        `C:\\MYFOLD~1\\новый.txt` проскочил бы проверку в коротком виде.
        """
        resolved = Path(os.path.realpath(path))
        if resolved.exists():
            return resolved

        tail: list[str] = []
        probe = resolved
        while not probe.exists() and probe.parent != probe:
            tail.append(probe.name)
            probe = probe.parent
        if not probe.exists():
            return resolved
        base = Path(os.path.realpath(probe))
        for part in reversed(tail):
            base = base / part
        return base

    def _owning_root(self, real: Path) -> Path | None:
        target = os.path.normcase(str(real))
        for root in self.roots:
            root_norm = os.path.normcase(str(root))
            if target == root_norm or target.startswith(root_norm.rstrip(os.sep) + os.sep):
                return root
        return None

    def _assert_not_denied(self, rel: Path, real: Path) -> None:
        """deny-маска применяется к КАЖДОМУ сегменту, а не только к имени файла.

        Так `проект/.git/config` отсекается маской `.git`, а не требует отдельной
        маски на каждый файл внутри.
        """
        matcher = self._name_re('deny')
        if matcher is None:
            return
        for segment in rel.parts:
            if matcher.match(segment) is None:
                continue
            # Какая именно маска сработала — нужно только ради текста отказа,
            # поэтому перебор по списку здесь, а не на горячем пути.
            pattern = self.denied_by(segment) or self.deny_patterns[0]
            raise PolicyError(
                f'путь закрыт маской «{pattern}» (сегмент «{segment}»): {real.name}'
            )

    # ---------- exec: разбор argv ----------

    def resolve_exec_cwd(self, root_hint: str, cwd_hint: str) -> Path:
        """Куда встать процессу `run`. Обязана быть внутри exec_root И доступна на запись.

        Сборка пишет артефакты, тесты — кэш; поэтому cwd проходит проверку for_write
        так же, как cwd именованной задачи.
        """
        base = self.resolve(root_hint, for_write=True)
        if self.exec_root_for(base) is None:
            allowed = ', '.join(str(r) for r in self.exec_roots) or 'нет ни одной'
            raise PolicyError(
                f'исполнение в этой папке не разрешено. exec_roots: {allowed}'
            )
        if not cwd_hint or not cwd_hint.strip():
            target = base
        else:
            # Относительный cwd — от root, а не от рабочей папки процесса dev-mcp.
            # 23.09.2026: cwd='.' уходил в папку сервера → «сюда писать нельзя» при
            # разрешённом root, и агенты останавливались после второго отказа.
            hint = cwd_hint.strip()
            if not os.path.isabs(hint.replace('/', os.sep) if os.name == 'nt' else hint):
                hint = str(base / hint)
            target = self.resolve(hint, for_write=True)
            if self.exec_root_for(target) is None:
                raise PolicyError('cwd вне exec_roots')
        if not target.is_dir():
            raise PolicyError('cwd не каталог')
        return target

    def guard_exec_args(self, args: list[str], *, bin_name: str = '') -> None:
        """argv[1:]: без NUL, без абсолютных путей вне roots, без выхода вверх по дереву.

        Настоящая изоляция запущенного процесса тут НЕ достигается (он всё равно
        может open('..') сам) — это гигиена командной строки и читаемость журнала.
        Реальная граница — сам факт того, что папка внесена в exec_roots.

        Бинарь из exec_args_unrestricted (`bin_name` совпал по basename-stem)
        освобождается от проверки путей в аргументах: NUL и предел длины проверяются
        всё равно, абсолютный путь и `..` — пропускаются.
        """
        base = os.path.basename(bin_name.strip()).lower()
        stem = base[:-4] if base.endswith('.exe') else base
        lax = bool(stem) and stem in {
            (b[:-4] if str(b).lower().endswith('.exe') else str(b)).lower()
            for b in self.exec_args_unrestricted
        }
        for tok in args:
            if not isinstance(tok, str):
                raise PolicyError('аргументы argv должны быть строками')
            if '\x00' in tok:
                raise PolicyError('нулевой байт в аргументе')
            if len(tok) > 8192:
                raise PolicyError('аргумент длиннее 8192 символов')
            if lax:
                continue
            probe = tok[tok.index('=') + 1:] if tok.startswith('-') and '=' in tok else tok
            norm = probe.replace('\\', '/')
            if norm == '..' or norm.startswith('../') or '/../' in norm or norm.endswith('/..'):
                raise PolicyError(f'выход вверх по дереву в аргументе запрещён: {tok}')
            if os.name == 'nt' and len(probe) >= 2 and probe[1] == ':' and probe[0].isalpha():
                # 21.10.2026: приёмки пульта (`py -3 C:/hub/_lib/owner_brief_check.py …`,
                # `C:/hub/_todo/hub-fleet/…`) отклонялись до запуска, и финальные ревью
                # замерзали «system-blocker». Путь внутри roots и не под deny-маской ничего
                # не добавляет к тому, что агент и так читает, — он допустим.
                try:
                    self.resolve(probe, for_write=False)
                except PolicyError:
                    raise PolicyError(f'абсолютный путь в аргументе запрещён: {tok}') from None
                continue
            if probe.startswith(('\\', '/')) and not probe.startswith('--'):
                raise PolicyError(f'абсолютный путь в аргументе запрещён: {tok}')

    # ---------- вспомогательное ----------

    def _name_re(self, kind: str) -> re.Pattern[str] | None:
        """Один скомпилированный regex на весь список масок (None — список пуст).

        Зачем: `fnmatch.fnmatch` на каждый вызов нормализует регистр и лезет в
        свой кэш шаблонов, а масок — два десятка. На обходе дерева это самая
        горячая точка: проверка одного файла стоила 24 маски × 8 сегментов
        пути, то есть ~200 вызовов fnmatch. Здесь весь список схлопнут в один
        regex, и проверка имени — один `match`.
        """
        cached = self._name_res.get(kind, _UNSET)
        if cached is not _UNSET:
            return cached
        if kind == 'deny':
            patterns = self.deny_patterns
        elif kind == 'ignore':
            patterns = self.ignore_patterns
        else:  # 'prune' — не обходить ни шум, ни закрытое
            patterns = [*self.ignore_patterns, *self.deny_patterns]
        compiled = (
            re.compile('|'.join(f'(?:{fnmatch.translate(p)})' for p in patterns), re.IGNORECASE)
            if patterns
            else None
        )
        self._name_res[kind] = compiled
        return compiled

    def denied_by(self, name: str) -> str | None:
        """Какая deny-маска закрывает это имя (None — ни одна)."""
        matcher = self._name_re('deny')
        if matcher is None or matcher.match(name) is None:
            return None
        for pattern in self.deny_patterns:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                return pattern
        return self.deny_patterns[0] if self.deny_patterns else None

    def is_ignored(self, name: str) -> bool:
        """Шум, который прячем из листинга и поиска. Не безопасность — читаемость."""
        matcher = self._name_re('ignore')
        return matcher is not None and matcher.match(name) is not None

    def prunes_dir(self, name: str) -> bool:
        """Каталог с этим именем не стоит даже ОБХОДИТЬ — не только скрывать из выдачи.

        `list_dir(recursive=true)` и `search_files` раньше спускались rglob'ом
        внутрь `.venv`/`node_modules`/`.git`/`dist` целиком (десятки-сотни тысяч
        файлов на больших деревьях вроде «projects») и только ПОТОМ, на
        каждый найденный файл, звали `is_ignored`/`resolve()`, чтобы его
        выбросить. Итог — обход, который занимал минуты, ничего не находя, и на
        нескольких одновременных вызовах (несколько агентов сразу) забирал GIL
        настолько, что остальные вызовы этого же процесса зависали в очереди.
        Эта проверка вызывается ДО спуска (см. `os.walk` в server.py, срез
        `dirnames[:] = ...`), поэтому такие каталоги вообще не листингуются —
        не только фильтруются на выходе. Смотрит и ignore_patterns (шум типа
        `.venv`), и deny_patterns (секретные `.git`/`.ssh`/…): обоих причин
        достаточно, чтобы каталог не стоил обхода.
        """
        matcher = self._name_re('prune')
        return matcher is not None and matcher.match(name) is not None

    def scan(
        self,
        root: Path,
        *,
        deadline: float | None = None,
        recursive: bool = True,
        want_dirs: bool = False,
    ) -> Iterator[ScanItem]:
        """Обход дерева под политикой — БЕЗ resolve() на каждый файл.

        Почему отдельный обход, а не `os.walk` + `resolve()` на каждую находку
        (как было): `resolve()` стоит ~0.75 мс — realpath, exists и fnmatch по
        каждому сегменту пути на каждую из двух десятков масок. На корне
        «projects» это 69 000 файлов × 0.75 мс = 51 СЕКУНДА чистого
        питоновского времени, при том что сам обход дерева занимает 3 с. Отсюда
        и брались «зависания, когда работает сразу десять агентов»: это время
        целиком проходит под GIL, десять одновременных поисков делят один
        интерпретатор, каждый упирается в бюджет времени, ничего толком не
        найдя, а лёгкие вызовы (read_file, policy_info) стоят в той же очереди
        за GIL и отвечают вместо 20 мс — секунду.

        Граница держится другим способом, не слабее прежнего:

        * `root` уже прошёл `resolve()` — это реальный путь внутри корня, а
          дальше идём только по именам из каталога, никаких путей «снаружи»;
        * вниз спускаемся ТОЛЬКО в обычные каталоги: reparse point (симлинк,
          junction) не обходим вовсе — именно он и был бы способом увести обход
          за пределы корня;
        * запись, которая сама является reparse point, отдаётся только после
          полной проверки `resolve()`;
        * deny/ignore-маски применяются к каждому сегменту, как и раньше: к
          каталогу — ОДИН раз при входе (и тогда всё поддерево не обходится), к
          файлу — по его имени. Множество проверенного то же, что у
          `_assert_not_denied`, просто без повторения родителей на каждый файл.

        `deadline` (в шкале `time.monotonic()`) прерывает обход через
        `ScanTimeout` — проверка на КАЖДОЙ записи, а не раз в каталог, иначе
        один каталог с сотней тысяч файлов перескакивает бюджет целиком.
        """
        stack: list[Path] = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue  # каталог исчез или нет прав — не повод рушить весь обход
            subdirs: list[Path] = []
            for entry in entries:
                if deadline is not None and time.monotonic() > deadline:
                    raise ScanTimeout
                name = entry.name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    st = entry.stat(follow_symlinks=False)
                    reparse = bool(
                        getattr(st, 'st_file_attributes', 0) & _FILE_ATTRIBUTE_REPARSE_POINT
                    ) or entry.is_symlink()
                except OSError:
                    continue
                path = Path(entry.path)
                if is_dir:
                    if self.prunes_dir(name):
                        continue
                    if reparse:
                        # Внутрь не идём (может увести за корень), но сам вход
                        # покажем, если он проходит полную проверку.
                        if want_dirs and self._passes_full_check(path):
                            yield ScanItem(path, name, True, 0)
                        continue
                    if recursive:
                        subdirs.append(path)
                    if want_dirs:
                        yield ScanItem(path, name, True, 0)
                    continue
                if self.is_ignored(name) or self.denied_by(name):
                    continue
                if reparse and not self._passes_full_check(path):
                    continue
                yield ScanItem(path, name, False, st.st_size)
            # В обратном порядке: stack.pop() снимает с конца, так обход
            # остаётся алфавитным и выдача воспроизводима от запуска к запуску.
            stack.extend(reversed(subdirs))

    def _passes_full_check(self, path: Path) -> bool:
        """Полная проверка границы для записи, которой нельзя доверять по имени."""
        try:
            self.resolve(str(path), for_write=False)
        except PolicyError:
            return False
        return True

    def describe(self) -> dict:
        tasks: dict[str, dict] = {}
        for name, task in self.tasks.items():
            runnable, reason = self.task_status(task)
            tasks[name] = {
                'description': task.description,
                'argv': task.argv,
                'cwd': task.cwd,
                'runnable': runnable,
                'blocked_reason': reason,
            }
        return {
            'roots': [str(r) for r in self.roots],
            'write_roots': [str(r) for r in self.write_roots],
            'exec_roots': [str(r) for r in self.exec_roots],
            'tree_roots': [str(r) for r in self.tree_roots],
            'tree_deny': [str(r) for r in self.tree_deny],
            'exec_allowed_bin': list(self.exec_allowed_bin),
            'exec_args_unrestricted': list(self.exec_args_unrestricted),
            'exec_bin_project_local': list(self.exec_bin_project_local),
            'exec_bin_missing': list(self._exec_bin_missing),
            'allow_write': self.allow_write,
            'allow_network_fetch': self.allow_network_fetch,
            'deny_patterns': self.deny_patterns,
            'ignore_patterns': self.ignore_patterns,
            'tasks': tasks,
            'limits': {
                'max_read_bytes': self.max_read_bytes,
                'max_write_bytes': self.max_write_bytes,
                'max_list_entries': self.max_list_entries,
                'max_search_results': self.max_search_results,
                'fetch_max_bytes': self.fetch_max_bytes,
                'exec_timeout_cap_s': self.exec_timeout_cap_s,
                'exec_sync_budget_s': self.exec_sync_budget_s,
            },
        }


# ---------------------------------------------------------------------------
# Живое перечитывание policy.toml — без перезапуска процесса dev-mcp/mcp-proxy.
#
# Раньше любая правка exec_roots/write_roots/roots требовала restart.cmd (кто-то
# правит файл руками → бинарь `run` продолжает жить по старой политике, пока
# процесс не перезапущен). Теперь __main__.py поднимает `watch_policy_file` в
# фоновом daemon-потоке на весь срок жизни процесса: он поллит mtime policy.toml
# и на каждое реальное изменение зовёт `policy.reload_from(...)`. Итог: владелец
# правит policy.toml (сам или через tools/review_requests.py) — сервер подхватывает
# это в течение `interval_s`, никого не роняя, если правка окажется битой.
# ---------------------------------------------------------------------------


def check_policy_file_changed(
    policy: Policy,
    config_path: str | os.PathLike[str],
    last_mtime: float | None,
) -> tuple[float | None, bool, bool, str]:
    """Один тик поллинга. Возвращает (новый last_mtime, changed, ok, message).

    changed=False на первом вызове (last_mtime=None) — это старт наблюдения,
    а не редактирование, шуметь в журнал незачем. changed=True, ok=False —
    файл поменялся, но не прошёл Policy.load (см. reload_from): текущая,
    уже работающая политика НЕ тронута, старый last_mtime не запоминается —
    заново битый файл продолжит попытки на каждом тике, пока его не поправят.
    """
    path = Path(config_path)
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        return last_mtime, False, False, f'policy.toml не читается: {exc}'
    if last_mtime is not None and mtime == last_mtime:
        return last_mtime, False, True, ''
    if last_mtime is None:
        return mtime, False, True, ''
    try:
        policy.reload_from(config_path)
    except (PolicyError, OSError) as exc:
        return mtime, True, False, str(exc)
    return mtime, True, True, 'policy.toml перечитан'


def watch_policy_file(
    policy: Policy,
    config_path: str | os.PathLike[str],
    *,
    interval_s: float = 3.0,
    on_change: Callable[[bool, str], None] | None = None,
    stop_event: threading.Event,
) -> None:
    """Фоновый цикл поллинга. Вызывай из daemon-потока на весь срок жизни сервера.

    `on_change(ok, message)` зовётся ТОЛЬКО когда файл реально поменялся (не на
    каждый тик) — `__main__.py` отдаёт сюда `audit.record`, чтобы автоперечитывание
    было видно в журнале наравне с обычными вызовами. Битый файл цикл не глушит:
    поток продолжает поллить и подхватит следующую, уже исправленную, правку.
    """
    last_mtime: float | None = None
    while not stop_event.is_set():
        last_mtime, changed, ok, message = check_policy_file_changed(policy, config_path, last_mtime)
        if changed and on_change:
            on_change(ok, message)
        stop_event.wait(interval_s)
