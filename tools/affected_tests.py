"""Какие тесты гонять после правки: только затронутые, а не весь набор (владелец, 05.11.2026).

Полный набор — ~2000 тестов, 15+ минут подряд; оператор гонял его после каждой правки, хотя
затронуто 3–7 файлов. Выбор: изменённые файлы тестов + тесты, где встречается имя изменённой
функции/класса (заголовки ханков `git diff -U0`) или функции, добавленной правкой. Полный
набор — перед публикацией пакета (CI), не на каждую правку.

    .venv\\Scripts\\python.exe tools\\affected_tests.py            # список файлов
    .venv\\Scripts\\python.exe tools\\affected_tests.py --run      # и прогнать их
    ... --base HEAD~1                                               # относительно ревизии
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEF = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
HUNK = re.compile(r"^@@ [^@]+ @@\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
# Слишком общие имена дают весь набор — их не считаем признаком.
BROAD_HITS = 12
GENERIC = {"main", "run", "tick", "__init__", "render", "close", "open", "get", "set", "load", "save"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def changed(base: str) -> tuple[list[str], set[str]]:
    files = [f for f in _git("diff", "--name-only", base, "--").splitlines() if f.endswith(".py")]
    files += [f for f in _git("ls-files", "--others", "--exclude-standard").splitlines()
              if f.endswith(".py")]
    names: set[str] = set()
    # Windows CreateProcess rejects huge argv (WinError 206); bound each diff call.
    for start in range(0, len(files), 30):
        for line in _git("diff", "-U0", base, "--", *files[start:start + 30]).splitlines():
            m = HUNK.match(line)
            if m:
                names.add(m.group(1))
            elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                m = DEF.match(line[1:])
                if m:
                    names.add(m.group(1))
    return sorted(set(files)), {n for n in names if n not in GENERIC and len(n) > 3}


def select(files: list[str], names: set[str]) -> list[str]:
    chosen = {f for f in files if Path(f).name.startswith("test_") and f.startswith("tests/")
              and (REPO / f).is_file()}  # удалённый тест — не цель прогона
    texts: dict[str, str] = {}
    for test in sorted((REPO / "tests").rglob("test_*.py")):
        try:
            texts[test.relative_to(REPO).as_posix()] = test.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    broad: list[str] = []
    for name in sorted(names):
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        hits = {path for path, text in texts.items() if pattern.search(text)}
        # Имя вроде submit/take_next есть в половине тестов: это вход API, а не признак
        # затронутого поведения. Такие тесты — дело полного набора, а не прогона правки.
        if len(hits) > BROAD_HITS:
            broad.append(f"{name} ({len(hits)})")
            continue
        chosen.update(hits)
    if broad:
        print("слишком общие имена (их тесты — в полном наборе): " + ", ".join(broad))
    return sorted(chosen)


def _positive_workers(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be a positive integer") from exc
    if workers < 1:
        raise argparse.ArgumentTypeError("workers must be a positive integer")
    return workers


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="HEAD", help="с чем сравнивать (по умолчанию рабочее дерево к HEAD)")
    ap.add_argument("--run", action="store_true", help="прогнать выбранные тесты")
    ap.add_argument("-n", "--workers", type=_positive_workers,
                    default=os.environ.get("AFFECTED_TESTS_WORKERS", "3"),
                    help="число xdist-воркеров (по умолчанию 3; AFFECTED_TESTS_WORKERS)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    files, names = changed(args.base)
    tests = select(files, names)
    print(f"изменено .py: {len(files)}; имён: {len(names)}; тестов выбрано: {len(tests)}")
    for t in tests:
        print("  " + t)
    if not args.run or not tests:
        return 0
    # Тест — это ~0,5 с на git init и схему SQLite, а не тяжёлый вызов: выигрыш даёт параллельность.
    # Консервативное умолчание: рядом живой dev-mcp и флотский Chrome.
    try:
        import xdist  # noqa: F401
        parallel = ["-n", str(args.workers)] if len(tests) > 1 and args.workers > 1 else []
    except ImportError:
        parallel = []
    # The py launcher may select a global Python without fastmcp/pytest plugins.
    # Run in the repository environment when present, regardless of launcher used.
    project_python = REPO / ".venv" / "Scripts" / "python.exe"
    interpreter = str(project_python) if project_python.is_file() else sys.executable
    return subprocess.run([interpreter, "-m", "pytest", "-q", "-p", "no:cacheprovider", *parallel, *tests],
                          cwd=REPO).returncode


if __name__ == "__main__":
    raise SystemExit(main())
