"""Read-only process obstacle: match a script argument, never inline code text."""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import Any, Iterable

COMMAND = "process-idle"
_PYTHON = re.compile(r"(?:py|pythonw?|python\d+(?:\.\d+)?)(?:\.exe)?$", re.I)


def validate(argv: list[str]) -> list[str]:
    if len(argv) < 2 or argv[0] != COMMAND:
        raise ValueError("process-idle: expected relative script and optional exact arguments")
    script = argv[1].replace("\\", "/")
    path = PurePosixPath(script)
    if (path.is_absolute() or ":" in script or ".." in path.parts
            or path.suffix.lower() != ".py" or not path.parts):
        raise ValueError("process-idle: script must be a relative .py path inside the project")
    if len(argv) > 18 or any(not a or len(a) > 256 or "\n" in a for a in argv[2:]):
        raise ValueError("process-idle: invalid argument filter")
    return [COMMAND, script, *argv[2:]]


def _script(argv: list[str]) -> tuple[str, list[str]] | None:
    if not argv or not _PYTHON.fullmatch(argv[0].replace("\\", "/").rsplit("/", 1)[-1]):
        return None
    i = 1
    while i < len(argv):
        token = argv[i]
        if token.startswith(("-c", "-m")) or token == "-":
            return None
        if token in {"-X", "-W"}:
            i += 2
            continue
        if token == "--":
            i += 1
            break
        if token.startswith("-"):
            i += 1
            continue
        break
    return (argv[i], argv[i + 1:]) if i < len(argv) else None


def _path(path: str, cwd: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.join(cwd, path))).casefold()


def evaluate(argv: list[str], root: str, rows: Iterable[dict[str, Any]]) -> int:
    """0 absent, 1 present, -1 inspection incomplete; no process environment/data."""
    argv = validate(argv)
    target, wanted = _path(argv[1], root), argv[2:]
    uncertain = False
    for row in rows:
        cmdline = row.get("cmdline")
        if cmdline is None:
            uncertain = True
            continue
        parsed = _script(list(cmdline))
        if parsed is None:
            continue
        script, tail = parsed
        if wanted and not any(tail[i:i + len(wanted)] == wanted for i in range(len(tail))):
            continue
        cwd = row.get("cwd")
        if not os.path.isabs(script) and not cwd:
            if script.replace("\\", "/").casefold() == argv[1].casefold():
                uncertain = True
            continue
        if _path(script, str(cwd or root)) == target:
            return 1
    return -1 if uncertain else 0


def run(argv: list[str], root: str) -> int:
    """Access denial never proves absence. Inspect only Python launch metadata."""
    validate(argv)
    try:
        import psutil
    except ImportError:
        return -1
    rows: list[dict[str, Any]] = []
    try:
        for process in psutil.process_iter(attrs=["pid", "name"]):
            try:
                name = process.name()
                if name is None:
                    rows.append({"cmdline": None})
                    continue
                if not _PYTHON.fullmatch(str(name)):
                    continue
                cmdline = process.cmdline()
                try:
                    cwd = process.cwd()
                except psutil.AccessDenied:
                    cwd = None
                rows.append({"cmdline": cmdline, "cwd": cwd})
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                rows.append({"cmdline": None})
        return evaluate(argv, root, rows)
    except (psutil.Error, OSError):
        return -1
