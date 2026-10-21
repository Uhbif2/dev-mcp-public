"""Инструкции папки при первом касании — как AGENTS.md в Codex и вложенные CLAUDE.md в Claude Code.

Агент флота погружается в проект через list_dir/read_file/search_files. Раньше AGENTS.md он
читал сам — если вспоминал (26.09.2026: пустые чаты перечитывали протокол, а AGENTS.md проекта
открывали через раз). Теперь сервер сам подкладывает AGENTS.md/CLAUDE.md всех уровней от корня
политики до затронутой папки в ответ на первое касание.

Правила:
- один раз на агента и версию файла (mtime): повторные касания ничего не добавляют;
- только для вызовов с `agent` — без него некому помнить «уже выдано»;
- бюджет ответа (MAX_RESPONSE_CHARS) общий с полезной нагрузкой: не влезает целиком —
  отдаём начало и точный `read_file(..., offset=N)` на продолжение; не влезает даже начало —
  одна строка-указатель, а выдача откладывается до следующего ответа;
- агент сам прочитал такой файл через read_file — считаем выданным.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

NAMES = ('AGENTS.md', 'CLAUDE.md')
MIN_CHUNK = 1500          # меньше — не начало, а обрывок; тогда только указатель
POINTER_RESERVE = 400     # запас на строку-указатель и рамку


class ContextDocs:
    def __init__(self, roots: Callable[[], list[Path]], permitted: Callable[[Path], bool]) -> None:
        self._roots = roots
        self._permitted = permitted
        self._seen: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def _chain(self, target: Path) -> list[Path]:
        folder = target if target.is_dir() else target.parent
        norm = os.path.normcase(str(folder))
        owner: Path | None = None
        for root in self._roots():
            rn = os.path.normcase(str(root)).rstrip(os.sep)
            if norm == rn or norm.startswith(rn + os.sep):
                if owner is None or len(str(root)) > len(str(owner)):
                    owner = root
        if owner is None:
            return []
        dirs = [folder]
        while os.path.normcase(str(dirs[-1])).rstrip(os.sep) != os.path.normcase(str(owner)).rstrip(os.sep):
            parent = dirs[-1].parent
            if parent == dirs[-1]:
                break
            dirs.append(parent)
        found: list[Path] = []
        for d in reversed(dirs):
            for name in NAMES:
                p = d / name
                try:
                    if p.is_file() and self._permitted(p):
                        found.append(p)
                except OSError:
                    continue
        return found

    def mark_read(self, agent: str, path: Path) -> None:
        if not agent or path.name not in NAMES:
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        with self._lock:
            self._seen.setdefault(agent, {})[os.path.normcase(str(path))] = mtime

    def for_touch(self, agent: str, target: Path, room: int) -> str:
        """Блок инструкций для этого касания; пустая строка — нечего добавлять."""
        agent = str(agent or '').strip()
        if not agent:
            return ''
        try:
            chain = self._chain(target)
        except OSError:
            return ''
        pending: list[tuple[Path, float]] = []
        with self._lock:
            seen = self._seen.setdefault(agent, {})
            for p in chain:
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if seen.get(os.path.normcase(str(p))) != mtime:
                    pending.append((p, mtime))
        if not pending:
            return ''

        parts: list[str] = []
        budget = room - POINTER_RESERVE
        for p, mtime in pending:
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            if budget >= len(text) + 200:
                parts.append(f'[{p}]\n{text.rstrip()}')
                budget -= len(text) + 200
                self._mark(agent, p, mtime)
            elif budget >= MIN_CHUNK:
                cut = text.rfind('\n', 0, budget - 200)
                cut = cut if cut > 0 else budget - 200
                head = text[:cut]
                next_line = head.count('\n') + 2
                parts.append(
                    f'[{p}] — начало ({len(head)} из {len(text)} символов)\n{head.rstrip()}\n'
                    f'[…продолжение обязательно к прочтению: read_file("{p}", offset={next_line})]'
                )
                budget = 0
                self._mark(agent, p, mtime)
            else:
                parts.append(
                    f'[{p}] ({len(text)} символов) — инструкции этой папки, прочитай до работы в ней: '
                    f'read_file("{p}")'
                )
        if not parts:
            return ''
        return (
            '--- dev-mcp · инструкции папки (первое касание; как AGENTS.md в Codex) ---\n'
            + '\n\n'.join(parts)
            + '\n--- конец инструкций папки ---\n'
        )

    def _mark(self, agent: str, path: Path, mtime: float) -> None:
        with self._lock:
            self._seen.setdefault(agent, {})[os.path.normcase(str(path))] = mtime
