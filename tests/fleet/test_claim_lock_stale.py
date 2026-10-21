import os
import time
from pathlib import Path

from dev_mcp.fleet import claim_tree


def test_dead_pid_lock_is_stale(tmp_path: Path) -> None:
    lock = tmp_path / ".integration.lock"
    lock.write_text("999999 0", encoding="ascii")
    assert claim_tree._lock_stale(lock)


def test_live_pid_fresh_lock_is_not_stale(tmp_path: Path) -> None:
    lock = tmp_path / ".integration.lock"
    lock.write_text(f"{os.getpid()} {time.time():.0f}", encoding="ascii")
    assert not claim_tree._lock_stale(lock)


def test_old_lock_is_stale(tmp_path: Path) -> None:
    lock = tmp_path / ".integration.lock"
    lock.write_text("", encoding="ascii")
    old = time.time() - claim_tree.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert claim_tree._lock_stale(lock)
