"""Bounded host storage observation and lossless compaction of own rolling backups."""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
import threading
from datetime import datetime, timezone
from typing import Any

MIN_FREE_BYTES = 8 * 1024 ** 3
COMPACT_BELOW_BYTES = 12 * 1024 ** 3
_BACKUP = re.compile(r"fleet-\d{8}-\d{6}(?:-\d+)?\.sqlite$")


def compact_one(home: Path, *, budget_s: float = 90) -> dict[str, Any] | None:
    """Keep latest raw backup. Verify decompressed SHA before replacing an older copy.

    No worktree, project output, general temp folder or symlink is removed.
    """
    from .location import backup_directory
    folders = {home / "data" / "backups", backup_directory(home / "data/fleet/fleet.sqlite")}
    # Preserve the newest raw across both the former local location and the
    # configured archive. A newly published archive can release the last large
    # local copy for lossless compaction; a .partial never grants that authority.
    raws = sorted((p for folder in folders if folder.is_dir() and not folder.is_symlink()
                   and not folder.is_junction() for p in folder.glob("fleet-*.sqlite")
                   if _BACKUP.fullmatch(p.name)), key=lambda p: p.name)
    for source in raws[:-1]:
        folder = source.parent
        if source.is_symlink() or source.stat().st_mtime > time.time() - 600:
            continue
        if any(p.exists() and p.stat().st_size for p in
               (Path(str(source) + "-wal"), Path(str(source) + "-journal"))):
            continue
        target = Path(str(source) + ".gz")
        partial = Path(str(target) + ".partial")
        if target.exists() or partial.exists():
            continue
        before = source.stat()
        started = time.monotonic()
        written = False
        digest = hashlib.sha256()
        try:
            with source.open("rb") as inp:
                if inp.read(16) != b"SQLite format 3\x00":
                    continue
                inp.seek(0)
                with partial.open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1) as out:
                    written = True
                    while chunk := inp.read(1024 ** 2):
                        if time.monotonic() - started > budget_s:
                            raise TimeoutError("backup compression budget exceeded")
                        if shutil.disk_usage(folder).free < 256 * 1024 ** 2:
                            raise OSError("backup compression storage reserve reached")
                        digest.update(chunk)
                        out.write(chunk)
            verified = hashlib.sha256()
            with gzip.open(partial, "rb") as inp:
                while chunk := inp.read(1024 ** 2):
                    if time.monotonic() - started > budget_s:
                        raise TimeoutError("backup verification budget exceeded")
                    verified.update(chunk)
            now = source.stat()
            if (digest.digest() != verified.digest() or before.st_size != now.st_size
                    or before.st_mtime_ns != now.st_mtime_ns):
                raise OSError("backup changed or verification failed")
            if partial.stat().st_size >= before.st_size:
                return None
            with partial.open("r+b") as stable:
                os.fsync(stable.fileno())
            os.replace(partial, target)
            source.unlink()
            return {"source": source.name, "archive": target.name,
                    "sha256": digest.hexdigest(), "saved_bytes": before.st_size - target.stat().st_size}
        finally:
            if written:
                partial.unlink(missing_ok=True)
    return None


def run_worker(store: Any, stop_event: Any, *, interval_s: float = 60) -> None:
    home = Path(store.path).resolve().parents[2]
    maintenance: threading.Thread | None = None
    pending: dict[str, Any] = {}
    lock = threading.Lock()

    def compact() -> None:
        try:
            result = {"compacted": compact_one(home, budget_s=300)}
        except Exception as exc:
            logging.getLogger(__name__).exception("storage compaction deferred")
            result = {"maintenance_error": type(exc).__name__}
        with lock:
            pending.update(result)

    while not stop_event.is_set():
        try:
            free = shutil.disk_usage(home).free
            with lock:
                result = dict(pending)
                pending.clear()
            if free < COMPACT_BELOW_BYTES and (maintenance is None or not maintenance.is_alive()):
                maintenance = threading.Thread(target=compact, daemon=True, name="fleet-backup-compactor")
                maintenance.start()
            state = "down" if free < MIN_FREE_BYTES else "up"
            with store.tx() as tx:
                old = tx.one("SELECT payload FROM events WHERE kind='storage_tick' ORDER BY id DESC LIMIT 1")
                previous = json.loads(old["payload"]).get("state") if old else None
                tx.record_event(kind="storage_tick", payload={"state": state, "free_bytes": free,
                    "min_free_bytes": MIN_FREE_BYTES, "compacting": bool(maintenance and maintenance.is_alive()),
                    **result})
            if state != previous:
                alert = {"ts": datetime.now(timezone.utc).isoformat(), "kind": "storage_pressure",
                         "state": "active" if state == "down" else "resolved", "free_bytes": free,
                         "message": f"Диск флота: свободно {free / 1024**3:.1f} ГиБ; порог сборок 8 ГиБ."}
                with (home / "data" / "owner-alerts.jsonl").open("a", encoding="utf-8") as out:
                    out.write(json.dumps(alert, ensure_ascii=False) + "\n")
        except Exception as exc:
            logging.getLogger(__name__).exception("storage maintenance deferred")
            try:
                with store.tx() as tx:
                    tx.record_event(kind="storage_error", payload={"error": type(exc).__name__})
            except Exception:
                logging.getLogger(__name__).exception("storage heartbeat unavailable")
        stop_event.wait(interval_s)


def health(store: Any) -> str:
    with store.read() as tx:
        row = tx.one("SELECT ts,payload FROM events WHERE kind='storage_tick' ORDER BY id DESC LIMIT 1")
    if row is None:
        return "storage: наблюдение ещё не подтверждено"
    try:
        payload = json.loads(row["payload"])
        free = int(payload["free_bytes"])
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])).total_seconds()
    except (TypeError, ValueError, KeyError):
        return "storage: некорректное наблюдение"
    status = "устарело" if age > 180 else "мало места" if payload["state"] == "down" else "ok"
    return f"storage: {status}; свободно {free / 1024**3:.1f} ГиБ; {age:.0f}с"
