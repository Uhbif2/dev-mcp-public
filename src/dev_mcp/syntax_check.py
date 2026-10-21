"""Проверка синтаксиса только что записанного файла — «диагностика после правки».

Зачем: агент флота тратит ходы на цикл «записал → запустил → увидел опечатку →
поправил → запустил». Строка вердикта в ответе write_file/edit_file сразу
говорит, парсится ли файл, — опечатка ловится тем же вызовом, что её внёс.

Правило модуля: вердикт — подсказка, а не страж. `check()` никогда не бросает
исключений и ничего не запрещает; непонятный тип, огромный файл, отсутствие
node, таймаут или любая неожиданность — это None (строки вердикта просто нет).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
import warnings
from pathlib import Path

MAX_CHECK_BYTES = 2 * 1024 * 1024
NODE_TIMEOUT_S = 5.0
_MSG_LIMIT = 300

_PREFIX = 'проверка синтаксиса'


def _one_line(text: str) -> str:
    line = ' '.join(str(text).split())
    return line if len(line) <= _MSG_LIMIT else line[:_MSG_LIMIT] + '…'


def _ok(kind: str) -> str:
    return f'{_PREFIX} ({kind}): ok'


def _error(kind: str, where: str, msg: str) -> str:
    return f'{_PREFIX} ({kind}): ОШИБКА {_one_line(f"{where}: {msg}" if where else msg)}'


def _check_py(path: Path, raw: bytes) -> str:
    # compile() по БАЙТАМ сам понимает BOM и coding-cookie — так вердикт
    # совпадает с тем, что скажет интерпретатор при запуске.
    try:
        with warnings.catch_warnings():
            # SyntaxWarning (например, '\d' в обычной строке) — не ошибка, и
            # печатать его в stderr stdio-сервера незачем.
            warnings.simplefilter('ignore')
            compile(raw, str(path), 'exec', dont_inherit=True)
    except SyntaxError as exc:
        where = f'{path}:{exc.lineno or 0}:{exc.offset or 0}'
        return _error('py', where, exc.msg or str(exc))
    except ValueError as exc:  # например, NUL-байт в исходнике
        return _error('py', str(path), str(exc))
    return _ok('py')


def _check_json(path: Path, text: str) -> str:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return _error('json', f'{path}:{exc.lineno}:{exc.colno}', exc.msg)
    return _ok('json')


def _check_toml(path: Path, text: str) -> str:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return _error('toml', str(path), str(exc))
    return _ok('toml')


def _check_yaml(path: Path, text: str) -> str | None:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        # Многодокументный YAML (--- ... ---) — законный, проверяем все документы.
        for _ in yaml.safe_load_all(text):
            pass
    except yaml.YAMLError as exc:
        mark = getattr(exc, 'problem_mark', None)
        where = f'{path}:{mark.line + 1}:{mark.column + 1}' if mark is not None else str(path)
        problem = getattr(exc, 'problem', None) or str(exc)
        return _error('yaml', where, problem)
    return _ok('yaml')


def _check_js(path: Path) -> str | None:
    node = shutil.which('node')
    if not node:
        return None
    try:
        result = subprocess.run(
            [node, '--check', str(path)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=NODE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return _ok('js')
    # Формат node --check: первая строка «файл:строка», ниже код с ^^^ и в конце
    # «SyntaxError: сообщение». Из этого собираем одну строку.
    lines = [ln.strip() for ln in (result.stderr or '').splitlines() if ln.strip()]
    if not lines:
        return None
    where = lines[0] if ':' in lines[0] and not lines[0].startswith(('SyntaxError', 'Error')) else ''
    msg = next((ln for ln in lines if 'Error' in ln.split(':', 1)[0]), lines[-1])
    return _error('js', where, msg)


def check(path: Path) -> str | None:
    """Однострочный вердикт о синтаксисе файла или None, если тип не проверяется."""
    try:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in _CHECKED:
            return None
        if not path.is_file() or path.stat().st_size > MAX_CHECK_BYTES:
            return None
        if suffix in ('.js', '.mjs', '.cjs'):
            return _check_js(path)
        raw = path.read_bytes()
        if suffix == '.py':
            return _check_py(path, raw)
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            return None
        if suffix == '.json':
            return _check_json(path, text)
        if suffix == '.toml':
            return _check_toml(path, text)
        return _check_yaml(path, text)
    except Exception:  # вердикт — подсказка; уронить из-за него запись нельзя
        return None


_CHECKED = frozenset({'.py', '.json', '.toml', '.yaml', '.yml', '.js', '.mjs', '.cjs'})

__all__ = ['check', 'MAX_CHECK_BYTES', 'NODE_TIMEOUT_S']
