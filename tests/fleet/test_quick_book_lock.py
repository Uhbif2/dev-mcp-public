"""18.11: книга быстрых агентов склеилась из двух версий («Extra data») — замок крали у живого
писателя через 15 с, и оба писали в общий jobs.tmp."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from dev_mcp.fleet import quick


def test_live_writer_lock_is_not_stolen_after_15s(tmp_path):
    book = quick.Book(tmp_path)
    book.dir.mkdir(parents=True)
    book.lockfile.write_text(str(os.getpid()), encoding="ascii")
    old = time.time() - 60
    os.utime(book.lockfile, (old, old))
    assert quick._lock_is_stale(book.lockfile, 120) is False


def test_dead_writer_lock_is_taken(tmp_path):
    book = quick.Book(tmp_path)
    book.dir.mkdir(parents=True)
    book.lockfile.write_text("999999", encoding="ascii")
    old = time.time() - 5
    os.utime(book.lockfile, (old, old))
    assert quick._lock_is_stale(book.lockfile, 120) is True
    with book.locked() as jobs:
        jobs["q1"] = {"id": "q1"}
    assert json.loads(book.path.read_text(encoding="utf-8")) == {"q1": {"id": "q1"}}


def test_hung_writer_lock_expires(tmp_path):
    book = quick.Book(tmp_path)
    book.dir.mkdir(parents=True)
    book.lockfile.write_text(str(os.getpid()), encoding="ascii")
    old = time.time() - 300
    os.utime(book.lockfile, (old, old))
    assert quick._lock_is_stale(book.lockfile, 120) is True


def test_concurrent_writers_never_produce_mixed_book(tmp_path):
    book = quick.Book(tmp_path)
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(15):
                with book.locked() as jobs:
                    # Разная длина записей: смешение потоков дало бы «Extra data».
                    jobs[f"w{n}-{i}"] = {"pad": "x" * (2000 * (n + 1))}
                    if i % 3 == 0:
                        jobs.pop(f"w{n}-{i - 3}", None)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    json.loads(book.path.read_text(encoding="utf-8"))
    assert not list(book.dir.glob("*.tmp"))
