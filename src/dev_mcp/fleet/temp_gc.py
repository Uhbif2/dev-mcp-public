"""Уборка брошенных временных папок флота в %TEMP%.

05.11.2026: с 25.10 накопилось 648 папок `fleet-*` (522 — `fleet-integrate-*` с целыми
git-worktree): TemporaryDirectory/rmtree на Windows молча не удаляет read-only файлы снимков
и занятые файлы. Здесь — независимая уборка: в каждой группе (имя без случайного хвоста
tempfile) остаются 2 самые свежие папки и всё, что моложе `min_age_s` (может быть в работе).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path

PREFIX = "fleet-"
# Python tempfile.mkdtemp: prefix + 8 из [a-z0-9_]; Node fs.mkdtemp (расширение «Вкладки
# флота»): prefix + 6 из [A-Za-z0-9].
_SUFFIX = re.compile(r"-([A-Za-z0-9]{6}|[a-z0-9_]{8})$")


def group_key(name: str) -> str:
    match = _SUFFIX.search(name)
    return name[:match.start() + 1] if match else name


def _force_remove(func, path, _exc) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(path: str | Path, *, attempts: int = 3) -> bool:
    """Удалить временное дерево флота; True — дерева больше нет.

    08.11: источник утечек (`_todo`-заметка dev-mcp ToDo/2026-11-07-temp-leaks.md) —
    `rmtree(ignore_errors=True)` и `TemporaryDirectory` на Windows молча оставляют read-only
    файлы (git-объекты `preflight-objects`, worktree) и занятые файлы. Здесь: снять read-only и
    повторить, на занятом подождать. Junction/symlink внутри rmtree не проходит (py3.8+), только
    удаляет саму ссылку. Неудача — строка в stderr, не исключение: уборка не валит работу.
    """
    import sys
    target = Path(path)
    if os.name == "nt" and os.path.lexists(target) and not target.is_symlink():
        # 08.11: обход 11,8 тыс. файлов дерева гейта на Python занимал ~15 % CPU сервера.
        # rd /s /q — нативно, отдельным процессом (ниже приоритетом, без GIL); junction внутри
        # удаляется как ссылка, цель не трогается. Остатки (read-only, занятые) — ниже.
        import subprocess
        try:
            subprocess.run(["cmd", "/d", "/c", "rd", "/s", "/q", str(target)], capture_output=True,
                           timeout=300, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    for attempt in range(attempts):
        if not os.path.lexists(target):
            return True
        try:
            shutil.rmtree(target, onerror=_force_remove)
        except OSError:
            pass
        if not os.path.lexists(target):
            return True
        time.sleep(0.2 * (attempt + 1))
    print(f"temp_gc: не удалено {target}", file=sys.stderr)
    return False


def plan(root: Path, *, keep: int = 2, min_age_s: float = 3600.0,
         now: float | None = None) -> list[Path]:
    """Папки к удалению. Ничего не трогает."""
    now = time.time() if now is None else now
    groups: dict[str, list[tuple[float, Path]]] = {}
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.startswith(PREFIX):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        groups.setdefault(group_key(entry.name), []).append((mtime, Path(entry.path)))
    doomed: list[Path] = []
    for items in groups.values():
        items.sort(key=lambda item: item[0], reverse=True)
        for mtime, path in items[keep:]:
            if now - mtime >= min_age_s:
                doomed.append(path)
    return doomed


def sweep(root: str | Path | None = None, *, keep: int = 2,
          min_age_s: float = 3600.0, now: float | None = None) -> dict:
    root = Path(root or tempfile.gettempdir())
    doomed = plan(root, keep=keep, min_age_s=min_age_s, now=now)
    removed = failed = 0
    for path in doomed:
        try:
            shutil.rmtree(path, onerror=_force_remove)
        except OSError:
            pass
        if path.exists():
            failed += 1
        else:
            removed += 1
    return {"candidates": len(doomed), "removed": removed, "failed": failed}
