"""Stack dump on request: create logs/dump-stacks.request -> logs/stacks-<time>.txt.

14.10.2026: fleet_list hung for minutes, the extension went silent and the fleet
dropped to one agent; nothing could show which thread held what. Windows has no
SIGQUIT, so a daemon thread polls a request file. Standard library only.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

POLL_S = 2.0


def dump(path: Path) -> Path:
    names = {t.ident: t.name for t in threading.enumerate()}
    lines = [time.strftime("%Y-%m-%dT%H:%M:%S") + " stacks\n"]
    for ident, frame in sys._current_frames().items():
        lines.append(f"\n--- thread {names.get(ident, '?')} ({ident})\n")
        lines.extend(traceback.format_stack(frame))
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _watch(folder: Path) -> None:
    request = folder / "dump-stacks.request"
    while True:
        try:
            if request.exists():
                request.unlink(missing_ok=True)
                dump(folder / time.strftime("stacks-%Y%m%dT%H%M%S.txt"))
        except OSError:
            pass
        time.sleep(POLL_S)


def install(folder: str | Path) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_watch, args=(folder,), name="stackdump", daemon=True).start()
