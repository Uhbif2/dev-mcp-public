"""Stdlib-only verification for persistent process installation.

This module ships inside the immutable guard package. It cannot depend on
dev_mcp: guard's watchdog survives a broken or absent server installation.
"""
from __future__ import annotations

import time
from typing import Any, Callable


def install_verified(
    install: Callable[[], Any], *, alive: Callable[[], bool],
    pulse: Callable[[], Any], rollback: Callable[[], None],
    duration_s: float, poll_s: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Require a living process and >=2 fresh heartbeat changes for the full window.

    All unsuccessful installs restore the previous state through rollback.
    The baseline heartbeat is read BEFORE install; an old pulse is not proof.
    """
    if duration_s <= 0 or poll_s <= 0 or poll_s > duration_s:
        raise ValueError("invalid heartbeat observation window")
    previous = pulse()
    start = clock()
    beats = 0
    last_beat = start
    try:
        install()
        while True:
            now = clock()
            if not alive():
                break
            current = pulse()
            if current is not None and current != previous:
                previous = current
                last_beat = now
                beats += 1
            if now - last_beat > max(2 * poll_s, duration_s / 2):
                break
            if now - start >= duration_s:
                if beats >= 2:
                    return True
                break
            sleep(min(poll_s, max(0, duration_s - (now - start))))
    except Exception:
        pass
    rollback()
    return False
