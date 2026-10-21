"""Где живёт реестр флота, переезд из logs/ в data/ и резерв (21.09.2026).

Реестр — очередь задач, журнал событий, снимки захватов — класс `data`: ничем не
восстанавливается. До 21.09 он лежал в `logs/fleet` рядом с журналами работы, то есть
под именем, которое при уборке читается как «можно снести», и без резерва. Теперь его
место `data/fleet`, снимки на каждом старте сервера — в `data/backups`.

Переезд делает первый, кто открывает реестр этим кодом: переименованием ВСЕЙ папки
`logs/fleet` → `data/fleet` (база, `-wal`, `-shm`, снимки захватов — одной операцией на
одном томе). Пока хоть один процесс держит базу открытой, Windows переименовать папку
не даёт — тогда этот вызов работает со старым местом, а переезд случится при следующем
открытии. Две расходящиеся базы так не получить: новое место появляется только
переименованием старого, а не созданием рядом. Поэтому в `data/fleet` ничего нельзя
класть заранее — резерв живёт в соседней `data/backups`.
"""
from __future__ import annotations

import os
import errno
import shutil
import sqlite3
import time
import subprocess
import sys
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_NAME = "fleet.sqlite"
BACKUPS_KEPT = 14
BACKUP_BUDGET_S = 1800.0  # archive disk competes with product packaging; never gates readiness
BACKUP_RESERVE_BYTES = 8 * 1024 ** 3


def backup_files(folder: Path) -> list[Path]:
    """Rolling raw and losslessly compressed copies share the same retention."""
    return sorted([*folder.glob("fleet-*.sqlite"), *folder.glob("fleet-*.sqlite.gz")])


def backup_directory(db: Path) -> Path:
    """Only the installed registry may use the operator's configured archive."""
    installed = Path(__file__).resolve().parents[3] / 'data/fleet/fleet.sqlite'
    if db.resolve() == installed:
        from . import goals
        value = goals._load_doc().get('storage',{}).get('backup_root')
        if value:
            target = Path(value)
            if (not target.is_absolute() or target.is_symlink()
                    or target.is_junction() or target.resolve() != target.absolute()):
                raise ValueError('backup archive must be an ordinary canonical absolute directory')
            return target
    return db.parent.parent / 'backups'


def fleet_db_path(home: str | os.PathLike[str]) -> Path:
    """Путь к реестру флота для дома dev-mcp `home`; при необходимости переносит его."""
    home = Path(home)
    current_dir = home / "data" / "fleet"
    current = current_dir / DB_NAME
    legacy_dir = home / "logs" / "fleet"
    if current.exists() or not (legacy_dir / DB_NAME).exists():
        return current
    try:
        current_dir.parent.mkdir(parents=True, exist_ok=True)
        os.rename(legacy_dir, current_dir)
    except OSError:
        # Папку держит процесс со старым кодом — или её только что перенёс сосед.
        return current if current.exists() else legacy_dir / DB_NAME
    return current


def backup_fleet_db(db_path: str | os.PathLike[str], keep: int = BACKUPS_KEPT) -> Path | None:
    """Согласованный снимок реестра в `data/backups` рядом с `data/fleet`.

    Онлайн-бэкап SQLite: снимок целостен, даже пока другие процессы пишут. Хранятся
    последние `keep` снимков. Реестра нет или он не в `data/fleet` — ничего не делает.
    """
    db = Path(db_path)
    if db.parent.name != "fleet" or db.parent.parent.name != "data" or not db.exists():
        return None
    backups = backup_directory(db)
    backups.mkdir(parents=True, exist_ok=True)
    # Starting a multi-GB copy when the host is nearly full can kill the live
    # registry. Skip before opening the destination; previous copies survive.
    required = db.stat().st_size + BACKUP_RESERVE_BYTES
    if shutil.disk_usage(backups).free < required:
        raise OSError(errno.ENOSPC, "fleet backup deferred: insufficient space for copy plus reserve")
    target = backups / f"fleet-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite"
    partial = target.with_suffix(".partial")
    started = time.monotonic()

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() - started > BACKUP_BUDGET_S:
            raise TimeoutError("fleet backup exceeded copy budget; previous backups preserved")

    src = sqlite3.connect(db, timeout=5.0)
    try:
        # Pin a read snapshot before copying. Without it, the online backup
        # restarts whenever the already-running controller writes a page;
        # a busy fleet can then prevent MCP from listening indefinitely.
        src.execute("BEGIN")
        src.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        dst = sqlite3.connect(partial)
        try:
            # This is a NEW disposable .partial, never the previous backup.
            # Per-step rollback journals on a slower archive volume make a
            # multi-GB copy miss its deadline. Publish only after one durable
            # flush of the fully completed SQLite snapshot below.
            dst.execute('PRAGMA journal_mode=OFF')
            dst.execute('PRAGMA synchronous=OFF')
            src.backup(dst, pages=4096, progress=progress)
        finally:
            dst.close()
        with partial.open('r+b') as stable:
            os.fsync(stable.fileno())
        os.replace(partial, target)
    finally:
        src.close()
        partial.unlink(missing_ok=True)
    for old in backup_files(backups)[:-max(1, keep)]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


@contextmanager
def _backup_lease(db: Path):
    """Kernel lease survives a parent restart and releases on child exit."""
    path = db.with_name('backup.lock')
    with path.open('a+b') as lock:
        lock.seek(0,os.SEEK_END)
        if not lock.tell():
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(lock,fcntl.LOCK_UN)


def backup_fleet_db_isolated(db_path: str | os.PathLike[str]) -> Path | None:
    """Bounded child avoids contention with the serving interpreter's workload."""
    db = Path(db_path).resolve()
    if not db.is_file():
        return None
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])+os.pathsep+env.get('PYTHONPATH','')
    completed = subprocess.run(
        [sys.executable,'-X','utf8','-m','dev_mcp.fleet.location',str(db)],
        capture_output=True,text=True,encoding='utf-8',env=env,
        timeout=BACKUP_BUDGET_S+30,
        creationflags=(getattr(subprocess,'CREATE_NO_WINDOW',0) |
                       getattr(subprocess,'NORMAL_PRIORITY_CLASS',0)),check=False,
    )
    if completed.returncode:
        raise RuntimeError('isolated fleet backup failed: '+completed.stderr[-1200:])
    result = json.loads(completed.stdout)
    return Path(result['path']) if result.get('path') else None


def _main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('expected installed registry path')
    db = Path(sys.argv[1])
    with _backup_lease(db) as acquired:
        if acquired and os.name == 'nt':
            # Launcher deliberately runs the serving fleet below normal. Its
            # inherited LOW I/O starved a 3.23 GB copy for many minutes, while
            # NORMAL completed the same snapshot in seconds. Only this short
            # lived worker changes priority; serving/core/browser stay intact.
            import psutil
            try:
                own = psutil.Process()
                own.nice(psutil.NORMAL_PRIORITY_CLASS)
                own.ionice(psutil.IOPRIO_NORMAL)
            except psutil.Error as exc:
                print('backup priority unchanged: '+type(exc).__name__,file=sys.stderr)
        made = backup_fleet_db(db) if acquired else None
    print(json.dumps({'path':str(made) if made else None}))


if __name__ == '__main__':
    _main()
