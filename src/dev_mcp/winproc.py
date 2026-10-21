"""Windows: child processes of the server never get a console and never show error boxes.

13.10.2026: the owner kept seeing «git.exe — Ошибка при запуске приложения (0xc0000142)».
The server runs without a console, so every console child (git, py, pytest) allocated
its own conhost; dozens of them exhaust the desktop heap, a new one fails to start and
Windows shows a modal box on the owner's desktop. Two process-wide defaults fix both:

* CREATE_NO_WINDOW for every Popen that did not choose its own console flags — no
  conhost, nothing flashes, nothing consumes the desktop heap;
* SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX), inherited by children —
  a failed start returns an exit code to the caller instead of a box for the owner.
"""
from __future__ import annotations

import subprocess
import sys

_CONSOLE_FLAGS = 0x00000010 | 0x00000008 | 0x08000000  # NEW_CONSOLE | DETACHED | NO_WINDOW
_PRIORITY_FLAGS = 0x20 | 0x40 | 0x80 | 0x100 | 0x4000 | 0x8000
_installed = False


def install() -> None:
    global _installed
    if sys.platform != "win32" or _installed:
        return
    _installed = True
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
    except Exception:  # noqa: BLE001 — only cosmetics for the owner's desktop
        pass
    original = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        flags = kwargs.get("creationflags", 0) or 0
        if not flags & _CONSOLE_FLAGS:
            flags |= subprocess.CREATE_NO_WINDOW
        if not flags & _PRIORITY_FLAGS:
            flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
        kwargs['creationflags'] = flags
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]


def normalize_serving_priority() -> dict[str, int]:
    """Only this MCP server is latency-sensitive; children keep a lower CPU class."""
    if sys.platform != 'win32':
        return {}
    import psutil
    process=psutil.Process()
    process.nice(psutil.NORMAL_PRIORITY_CLASS)
    process.ionice(psutil.IOPRIO_NORMAL)
    return {'pid':process.pid,'cpu':int(process.nice()),'io':int(process.ionice())}
