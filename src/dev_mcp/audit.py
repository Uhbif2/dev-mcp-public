"""Журнал вызовов.

Зачем отдельным модулем, а не print'ом: сервер рассчитан на клиента, которого мы
не контролируем (ChatGPT через шлюз Oracle). Единственный способ узнать, что он
делал, — собственный журнал на нашей стороне. Отказы пишутся наравне с успехами:
серия deny подряд — это либо клиент бьётся в границу и надо расширить корень,
либо кто-то щупает периметр.

Формат — JSONL, по строке на вызов: его читают и глазами (`Get-Content -Tail`),
и скриптом.

Ротация по суткам (UTC, 27.09.2026). Файл рос без предела (47 МБ за неделю), и любой разбор
читал его целиком. Теперь `audit.jsonl` — только текущие сутки; прошлые лежат рядом как
`audit-YYYY-MM-DD.jsonl` (дата первой записи архива). Разбор истории — `iter_entries`, он
сам берёт нужные архивы и текущий файл.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_LOCK = threading.Lock()
_MAX_ARG_CHARS = 300
_REDACTED = '[REDACTED]'
# server.py: write_file.content, edit_file.old_string/new_string,
# apply_patch.patch, request_note.summary/detail. Остальные — на будущее.
_CONTENT_KEYS = frozenset({
    'content', 'old_string', 'new_string', 'patch', 'text', 'body', 'data',
    'detail', 'summary', 'payload', 'script', 'stdin', 'message', 'prompt', 'code',
})
_CONTENT_SUFFIXES = ('_content', '_contents', '_text', '_body', '_data', '_patch', '_payload')
_SECRET_KEY_PARTS = ('key', 'token', 'secret', 'password')
_SECRET_VALUE = re.compile(
    r'(?i)\b(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]+|'
    r'(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]+|'
    r'xox[a-z0-9]*-[A-Za-z0-9-]+|AKIA[A-Z0-9]{16})'
)
# Пароли без узнаваемого префикса, переданные как URL query или --token=...
_SECRET_ASSIGNMENT = re.compile(
    r'(?i)(?:^|[?&,\s-])[\w-]*(?:key|token|secret|password)[\w-]*\s*[=:]'
)
_SECRET_CLI_FLAG = re.compile(r'(?i)^--?[\w-]*(?:key|token|secret|password)[\w-]*$')


def _is_content_key(key: str) -> bool:
    name = key.casefold()
    return name in _CONTENT_KEYS or name.endswith(_CONTENT_SUFFIXES)


def _has_secret(value: str) -> bool:
    return bool(_SECRET_VALUE.search(value) or _SECRET_ASSIGNMENT.search(value))


def _fingerprint(value: Any) -> dict[str, int | str]:
    """Только размер исходного текста и префикс SHA-256, без его фрагментов."""
    if isinstance(value, str):
        raw = value.encode('utf-8')
        length = len(value)
    elif isinstance(value, bytes):
        raw = value
        length = len(value)
    else:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        raw = serialized.encode('utf-8')
        length = len(serialized)
    return {'length': length, 'sha256': hashlib.sha256(raw).hexdigest()[:12]}


def _trim(value: Any, *, key: str = '') -> Any:
    """Рекурсивно скрываем содержимое и секреты до записи в JSONL."""
    name = key.casefold()
    if any(part in name for part in _SECRET_KEY_PARTS):
        return _REDACTED
    if _is_content_key(name):
        return _fingerprint(value)
    if isinstance(value, str):
        if _has_secret(value):
            return _REDACTED
        return value if len(value) <= _MAX_ARG_CHARS else value[:_MAX_ARG_CHARS] + f'…(+{len(value) - _MAX_ARG_CHARS})'
    if isinstance(value, dict):
        return {k: _trim(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        result = []
        mask_next = False
        for item in value[:10]:
            result.append(_REDACTED if mask_next else _trim(item))
            mask_next = isinstance(item, str) and bool(_SECRET_CLI_FLAG.fullmatch(item))
        return result
    return value


def _safe_reason(reason: str) -> str:
    # edit_file при ошибке сравнения включает несколько строк исходного файла.
    marker = '\nБлиже всего — строка'
    if marker in reason:
        reason = reason.split(marker, 1)[0] + '\n[content redacted]'
    return _REDACTED if _has_secret(reason) else reason


def _first_day(path: Path) -> str:
    """Дата (UTC) первой записи файла: имя архива говорит, с какого дня он начинается."""
    try:
        with path.open('rb') as fh:
            head = fh.readline()
        return str(json.loads(head.decode('utf-8'))['ts'])[:10]
    except (OSError, ValueError, KeyError, TypeError):
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date().isoformat()


def archives(path: str | os.PathLike[str]) -> list[Path]:
    """Архивы журнала по возрастанию даты начала."""
    base = Path(path)
    return sorted(base.parent.glob(f'{base.stem}-????-??-??*{base.suffix}'))


def iter_entries(path: str | os.PathLike[str], since: str = '') -> Iterator[dict[str, Any]]:
    """Записи журнала с `since` (ISO-префикс, например '2026-09-26') по сейчас, по порядку.

    Архив нужен, если следующий за ним начинается позже `since`: так разбор последних суток
    не читает неделю истории.
    """
    base = Path(path)
    files = archives(base)
    starts = [f.stem[len(base.stem) + 1:][:10] for f in files]
    chosen = [f for i, f in enumerate(files) if not since or i + 1 >= len(files) or starts[i + 1] > since[:10]]
    if base.exists():
        chosen.append(base)
    for f in chosen:
        try:
            fh = f.open('rb')
        except OSError:
            continue
        with fh:
            for raw in fh:
                try:
                    entry = json.loads(raw.decode('utf-8'))
                except ValueError:
                    continue
                if not since or str(entry.get('ts', '')) >= since:
                    yield entry


class Audit:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._day = ''

    def _rotate_locked(self, today: str) -> None:
        """Сменились сутки — текущий файл уходит в архив. Ошибка переименования (файл держит
        читатель) не страшна: попробуем при следующей записи, журнал не теряется."""
        if self._day == today:
            return
        try:
            if self.path.exists() and self.path.stat().st_size:
                start = _first_day(self.path)
                if start < today:
                    target = self.path.with_name(f'{self.path.stem}-{start}{self.path.suffix}')
                    n = 1
                    while target.exists():
                        target = self.path.with_name(f'{self.path.stem}-{start}.{n}{self.path.suffix}')
                        n += 1
                    self.path.rename(target)
        except OSError:
            return
        self._day = today

    def record(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        decision: str,
        reason: str = '',
        result_bytes: int = 0,
        dur_ms: int = 0,
        agent: str = '',
        inferred: bool = False,
        degraded: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        entry = {
            'ts': now.isoformat(timespec='seconds'),
            'tool': tool,
            'decision': decision,
            'reason': _safe_reason(reason),
            'result_bytes': result_bytes,
            # Сколько вызов занял. Без этого числа жалоба «сервер тормозит»
            # проверяется только замерами со стороны: журнал знал ЧТО вызывали,
            # но не знал, сколько это шло, и медленный инструмент было не
            # отличить от медленного транспорта.
            'dur_ms': dur_ms,
            'agent': _trim(agent or ''),
            'args': _trim(args),
        }
        if inferred:
            entry['inferred'] = True
        if degraded:
            entry['degraded'] = True
        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            self._rotate_locked(now.date().isoformat())
            with self.path.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')
