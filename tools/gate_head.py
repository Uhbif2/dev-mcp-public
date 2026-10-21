"""Гейт по закоммиченному стволу, а не по общему рабочему дереву флота.

Запуск — команда `gate` проекта в fleet-projects.toml, cwd = корень проекта (git worktree):
    py -3 C:\\hub\\dev-mcp\\tools\\gate_head.py

Зачем (09.10.2026): гейт по рабочему дереву, которое одновременно правят до 16 агентов, ловил
недописанные файлы (35 ошибок сборки в 12:11, через минуту — 0). Красный ствол держал зону ревью,
ревью не выдавались, расширение видело «готовых 0». Ствол — это HEAD: проверяем ровно его.

Отдельная копия HEAD (git worktree --detach), интерпретатор — .venv проекта, пакет берётся из копии
(PYTHONPATH=<копия>/src впереди editable-установки). Вывод pytest — как есть: интегратор разбирает
строки FAILED/ERROR с путями относительно корня, они совпадают.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _project_python(root: Path) -> Path | None:
    local = root / '.venv' / 'Scripts' / 'python.exe'
    if local.is_file(): return local
    current = Path(sys.executable).resolve(strict=False)
    if current.is_file() and any(part.casefold() == '.venv' for part in current.parts):
        return current
    return None


def main() -> int:
    root = Path.cwd()
    python = _project_python(root)
    if python is None:
        print(f"gate_head: нет {python}", flush=True)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="gate-head-"))
    copy = tmp / "c"
    try:
        add = subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(copy), "HEAD"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             creationflags=NO_WINDOW)
        if add.returncode:
            print("gate_head: git worktree add не удался\n" + add.stderr, flush=True)
            return 2
        env = {**os.environ, "PYTHONPATH": str(copy / "src") + os.pathsep + str(copy)}
        env["PYTHONUNBUFFERED"] = "1"
        revision = subprocess.run(["git", "-C", str(copy), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", creationflags=NO_WINDOW)
        snapshot = revision.stdout.strip() if revision.returncode == 0 else "unknown"
        # 14.10.2026: принятый claim сломал синтаксис модуля расширения — флот час без
        # агентов, а pytest этого не видит. Загрузка модулей расширения — часть гейта.
        extension_tests = sorted((copy / 'extension' / 'fleet-tabs' / 'test').glob('*.mjs'))
        node = shutil.which("node")
        if extension_tests:
            if not node:
                print('gate_head: Node.js runtime unavailable',flush=True)
                return 2
            ext = subprocess.run([node, "--test", *map(str,extension_tests)], cwd=copy,
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=120, creationflags=NO_WINDOW)
            if ext.returncode:
                print("gate_head: модули расширения не загружаются\n" + ext.stdout[-3000:]
                      + ext.stderr[-1000:], flush=True)
                return ext.returncode
            print("gate_head: весь набор тестов расширения зелёный", flush=True)
        print(f"gate_head: pytest snapshot={snapshot}", flush=True)
        proc = subprocess.Popen([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                                cwd=copy, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, creationflags=NO_WINDOW)
        if proc.stdout is not None:
            for line in proc.stdout:
                print(line, end="", flush=True)
        rc = proc.wait()
        print(f"gate_head: pytest rc={rc}, snapshot={snapshot}", flush=True)
        return rc
    finally:
        subprocess.run(["git", "-C", str(root), "worktree", "remove", "--force", str(copy)],
                       capture_output=True, creationflags=NO_WINDOW)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
