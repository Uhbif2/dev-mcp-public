"""Retry-safe JSON snapshots shared between Windows processes.

Readers never turn a temporarily inaccessible existing book into an empty book
when strict=True. Writers stage to a unique file to avoid colliding writers.
This is *not* a read-modify-write transaction lock: callers must lock those.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def read_json(path: Path, *, default: Any = None, strict: bool = False,
              attempts: int = 40, delay: float = 0.05) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            # Only a truly missing book is empty; Windows replace can transiently
            # hide an existing file. Give it the same brief retry window.
            last = exc
        except (OSError, ValueError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(delay)
    if isinstance(last, FileNotFoundError):
        return default
    if strict and last is not None:
        raise RuntimeError(f"JSON book unreadable: {path}") from last
    return default


def write_json(path: Path, value: Any, *, indent: int | None = None,
               trailing_newline: bool = False, attempts: int = 40,
               delay: float = 0.05) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=indent)
    if trailing_newline:
        content += "\n"
    staged: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as file:
            staged = file.name
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        for attempt in range(attempts):
            try:
                os.replace(staged, path)
                staged = None
                return
            except PermissionError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(delay)
    finally:
        if staged is not None:
            try:
                os.unlink(staged)
            except FileNotFoundError:
                pass
