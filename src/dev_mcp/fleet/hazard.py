"""Detect stale reads while suppressing ordinary repository churn."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from .inbox import (
    Message, _existing_dedup, _fetchall, _fetchone, _insert_message, _read_handle,
    _parse_ts, _row_to_message, _transaction, _utcnow,
)

_TEMP_DIRS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git", ".idea", ".vscode", "tmp", "temp",
}
_TEMP_SUFFIXES = (".tmp", ".temp", ".swp", ".swo", ".bak", ".pyc", ".pyo", "~")


def _payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _norm_display(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/")


def _project_roots(db: Any) -> set[str]:
    try:
        rows = _fetchall(db, "SELECT root FROM projects WHERE active=1")
    except Exception:
        return set()
    return {_norm_display(str(row["root"])).casefold() for row in rows}


def _is_noisy(path: str, payload: dict[str, Any], roots: set[str]) -> bool:
    norm = _norm_display(path)
    folded = norm.casefold()
    if not norm or folded in roots:
        return True
    parts = [part.casefold() for part in norm.split("/") if part]
    if "logs" in parts or any(part in _TEMP_DIRS for part in parts):
        return True
    if folded.endswith(_TEMP_SUFFIXES):
        return True
    if parts and parts[-1] in {".ds_store", "thumbs.db"}:
        return True
    if norm.endswith("/**"):
        return True
    if any(payload.get(key) is True for key in ("is_dir", "directory", "recursive", "walk_root")):
        return True
    shape = str(payload.get("kind") or payload.get("type") or "").casefold()
    return shape in {"directory", "dir", "root", "walk", "traversal"}


def _age_minutes(now: datetime, raw: str) -> int:
    return max(0, int((now - _parse_ts(raw)).total_seconds() // 60))


def _ago(minutes: int) -> str:
    if minutes < 1:
        return "меньше минуты назад"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} д назад"


def _reads_since(db: Any, worker: str) -> str | None:
    """С какого момента чтения воркера ещё относятся к делу; "" — ни одно не относится.

    Чат web-ChatGPT берёт задачи подряд (22.09.2026). Чтения прошлых задач к текущей не
    относятся, а уведомление «файл, который ты читал, изменился» о них уводит агента назад:
    W37 на пятой задаче (TZ-31) получил ~30 таких про файлы прошлой TZ-250, пошёл её
    перепроверять и сдал её работу под TZ-31. Поэтому: есть активная задача — только чтения с
    её начала; задач было, но активной нет (между задачами) — никаких; задач не было вовсе —
    все чтения, как раньше.
    """
    active = _fetchone(
        db,
        "SELECT started_at FROM claims WHERE worker_id=? AND state='active' ORDER BY started_at DESC LIMIT 1",
        (worker,),
    )
    if active is not None:
        return str(active["started_at"])
    any_claim = _fetchone(db, "SELECT 1 AS hit FROM claims WHERE worker_id=? LIMIT 1", (worker,))
    return "" if any_claim is not None else None


def stale_reads(store: Any, worker_id: str, *, stale_min: int = 20) -> list[Message]:
    """Notify once per read when another worker writes that path after this worker read it."""

    if stale_min < 0:
        raise ValueError("stale_min must be >= 0")
    worker = str(worker_id)
    now = _utcnow()
    created: list[Message] = []
    pending: list[tuple[str, str]] = []

    # The expensive read/write join and latest-read grouping are a WAL snapshot,
    # not a BEGIN IMMEDIATE. The envelope must not wait for a writer to finish.
    with _read_handle(store) as db:
        since = _reads_since(db, worker)
        if since == "":
            return created
        roots = _project_roots(db)
        rows = _fetchall(
            db,
            """SELECT w.id AS writer_event_id, w.path_norm AS path_norm,
                      w.worker_id AS writer_id, w.ts AS write_ts,
                      w.payload AS write_payload, r.id AS read_event_id,
                      r.ts AS read_ts, r.payload AS read_payload
                 FROM events AS r
                 JOIN events AS w ON w.path_norm=r.path_norm
                WHERE r.kind='read' AND r.worker_id=?
                  AND NOT EXISTS (
                      SELECT 1
                        FROM events AS r2
                       WHERE r2.kind='read'
                         AND r2.worker_id=r.worker_id
                         AND r2.path_norm=r.path_norm
                         AND (r2.ts>r.ts OR (r2.ts=r.ts AND r2.id>r.id))
                  )
                  AND w.kind='write' AND w.worker_id IS NOT NULL
                  AND w.worker_id<>? AND w.ts>r.ts
                  AND r.ts>=?
                ORDER BY w.id""",
            (worker, worker, since or ""),
        )

        # Одно уведомление на одно чтение: о последней чужой записи и сколько их было. Раньше
        # каждая запись соседа давала своё уведомление — до шести про один файл подряд.
        latest: dict[int, dict[str, Any]] = {}
        writes: dict[int, int] = {}
        for row in rows:
            read_id = int(row["read_event_id"])
            latest[read_id] = row
            writes[read_id] = writes.get(read_id, 0) + 1

        for read_id, row in latest.items():
            writer_event_id = int(row["writer_event_id"])
            path = _norm_display(str(row["path_norm"]))
            merged_payload = _payload(row.get("read_payload"))
            merged_payload.update(_payload(row.get("write_payload")))
            if _is_noisy(path, merged_payload, roots):
                continue

            read_age = _age_minutes(now, str(row["read_ts"]))
            if read_age <= stale_min:
                continue
            write_age = _age_minutes(now, str(row["write_ts"]))
            writer = str(row.get("writer_id") or "другим воркером")
            dedup_key = f"stale:{worker}:{path}:read{read_id}"

            if _existing_dedup(
                db, dedup_key=dedup_key, to_worker=worker,
                to_task=None, broadcast_flag=0,
            ):
                continue

            pending.append((dedup_key,
                            f"{path}, прочитан тобой {_ago(read_age)}, "
                            f"изменён {writer} {_ago(write_age)}"
                            + (f" (чужих записей с твоего чтения: {writes[read_id]})"
                               if writes[read_id] > 1 else "")))

    # One bounded write transaction for each new message. _insert_message
    # rechecks dedup under the write lock (concurrent envelopes are harmless).
    for dedup_key, body in pending:
        with _transaction(store) as db:
            existed = _existing_dedup(
                db, dedup_key=dedup_key, to_worker=worker,
                to_task=None, broadcast_flag=0,
            )
            if existed is not None:
                continue
            message_id = _insert_message(
                db, to_worker=worker, to_task=None, broadcast_flag=0,
                from_kind="system", from_id="fleet", kind="stale_read",
                body=body, dedup_key=dedup_key,
            )
            stored = _fetchone(
                db,
                """SELECT id,from_kind,from_id,kind,body,created_at
                     FROM messages WHERE id=?""",
                (message_id,),
            )
            if stored is not None:
                created.append(_row_to_message(stored, now=now))

    return created


__all__ = ["stale_reads"]
