"""08.11: фоновые потоки сервера — ниже обычного приоритета ОС, потоки запросов — нет."""
import sys
import threading

import pytest

from dev_mcp import winproc


def test_background_names():
    assert winproc.is_background("dev-mcp-fleet-controller")
    assert winproc.is_background("fleet-witness") and winproc.is_background("bite-P-TZ-1")
    assert not winproc.is_background("AnyIO worker thread") and not winproc.is_background("Thread-3")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows thread priority")
def test_background_thread_runs_below_normal():
    winproc.install()
    seen = {}

    def probe(key):
        kernel = winproc._thread_api()
        seen[key] = kernel.GetThreadPriority(kernel.GetCurrentThread())

    for name in ("dev-mcp-test", "request-handler"):
        t = threading.Thread(target=probe, args=(name,), name=name)
        t.start()
        t.join()
    assert seen["dev-mcp-test"] == -1 and seen["request-handler"] == 0
