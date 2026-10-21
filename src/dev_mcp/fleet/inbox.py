"""Persistent, non-blocking fleet notifications.

There is intentionally NO wait_for_message here. A browser worker only runs while an MCP
call is executing; waiting for another worker would serialize the fleet. Work goes through
the task queue. Inbox messages only report facts that have already happened.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Iterable, Iterator, Sequence
import uuid

try:
    from .model import Message
except ImportError:  # TZ-01 may not have landed yet.
    @dataclass(frozen=True)
    class Message:
        id: str
        kind: str
        sender: str
        body: str
        age_min: int


_IMPORTANCE = {
    "revoke": 0, "revoked": 0, "request_decided": 1,
    "stale_read": 2, "contract": 3, "broadcast": 4, "note": 5,
}
_KIND_LABEL = {
    "revoke": "отзыв", "revoked": "отзыв",
    "request_decided": "решение по заявке",
    "stale_read": "устаревшее чтение",
    "contract": "контракт опубликован",
    "broadcast": "сообщение владельца", "note": "уведомление",
}


class PendingMessages(list[Message]):
    """List-compatible delivery result carrying the number that did not fit."""

    def __init__(self, items: Iterable[Message] = (), *, remaining_count: int = 0,
                 max_bytes: int = 2048) -> None:
        super().__init__(items)
        self.remaining_count = max(0, int(remaining_count))
        self.max_bytes = int(max_bytes)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _parse_ts(raw: str | None) -> datetime:
    text = str(raw or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return _utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _find_sql_handle(obj: Any) -> Any:
    seen: set[int] = set()
    queue = [obj]
    for candidate in queue:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if callable(getattr(candidate, "execute", None)):
            return candidate
        for name in ("conn", "_conn", "connection", "_connection", "db", "_db"):
            nested = getattr(candidate, name, None)
            if nested is not None:
                queue.append(nested)
    raise TypeError("Store/transaction does not expose a DB-API execute() handle")


@contextmanager
def _transaction(store: Any) -> Iterator[Any]:
    tx_factory = getattr(store, "tx", None)
    if callable(tx_factory):
        with tx_factory() as tx:
            try:
                yield _find_sql_handle(tx)
            except TypeError:
                yield _find_sql_handle(store)
        return
    db = _find_sql_handle(store)
    if isinstance(db, sqlite3.Connection):
        with db:
            yield db
    else:
        yield db


@contextmanager
def _read_handle(store: Any) -> Iterator[Any]:
    """Снимок без замка записи, если у хранилища есть read(); иначе обычная транзакция."""
    reader = getattr(store, "read", None)
    if callable(reader):
        with reader() as tx:
            yield _find_sql_handle(tx)
        return
    with _transaction(store) as db:
        yield db


def _fetchall(db: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cur = db.execute(sql, tuple(params))
    rows = cur.fetchall()
    names = [item[0] for item in (cur.description or ())]
    result: list[dict[str, Any]] = []
    for row in rows:
        if hasattr(row, "keys"):
            result.append({name: row[name] for name in row.keys()})
        else:
            result.append(dict(zip(names, row)))
    return result


def _fetchone(db: Any, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    rows = _fetchall(db, sql, params)
    return rows[0] if rows else None


def _validate_message(*, from_kind: str, kind: str, body: str) -> None:
    if from_kind not in {"owner", "system", "worker"}:
        raise ValueError("from_kind must be owner, system, or worker")
    if kind in {"request_decided", "broadcast"} and from_kind != "owner":
        raise PermissionError(f"{kind} messages require from_kind='owner'")
    if not kind.strip() or not body.strip():
        raise ValueError("message kind and body must not be empty")
    if any(line.strip().endswith(("?", "？")) for line in body.splitlines()):
        raise ValueError("questions belong in queued work, not fleet messages")


def _address(to: str) -> tuple[str | None, str | None]:
    target = str(to).strip()
    if not target:
        raise ValueError("message target must not be empty")
    return (None, target) if "/" in target and not target.upper().startswith("W") else (target, None)


def _sender_token(from_kind: str, from_id: str) -> str:
    ident = str(from_id).strip()
    if from_kind == "owner":
        return f"owner:{ident}" if ident else "owner"
    if from_kind == "system":
        return f"system:{ident}" if ident else "system"
    return f"worker:{ident}" if ident else "worker"


def _existing_dedup(db: Any, *, dedup_key: str, to_worker: str | None,
                    to_task: str | None, broadcast_flag: int = 0) -> str | None:
    if not dedup_key:
        return None
    row = _fetchone(
        db,
        """SELECT id FROM messages
           WHERE dedup_key=? AND COALESCE(to_worker,'')=?
             AND COALESCE(to_task,'')=? AND broadcast=?
           ORDER BY created_at,id LIMIT 1""",
        (dedup_key, to_worker or "", to_task or "", broadcast_flag),
    )
    return str(row["id"]) if row else None


def _insert_message(db: Any, *, to_worker: str | None, to_task: str | None,
                    broadcast_flag: int, from_kind: str, from_id: str,
                    kind: str, body: str, dedup_key: str) -> str:
    existing = _existing_dedup(
        db, dedup_key=dedup_key, to_worker=to_worker,
        to_task=to_task, broadcast_flag=broadcast_flag,
    )
    if existing is not None:
        return existing
    message_id = "m-" + uuid.uuid4().hex[:12]
    db.execute(
        """INSERT INTO messages
           (id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,body,
            created_at,delivered_at,acked_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
        (message_id, dedup_key, to_worker, to_task, int(broadcast_flag),
         from_kind, from_id, kind, body, _iso_now()),
    )
    return message_id


def send(store: Any, *, to: str, from_kind: str, from_id: str,
         kind: str, body: str, dedup_key: str = "") -> str:
    """Persist one fact notification. Return means stored, never delivered."""

    _validate_message(from_kind=from_kind, kind=kind, body=body)
    to_worker, to_task = _address(to)
    with _transaction(store) as db:
        return _insert_message(
            db, to_worker=to_worker, to_task=to_task, broadcast_flag=0,
            from_kind=from_kind, from_id=str(from_id), kind=str(kind),
            body=str(body), dedup_key=str(dedup_key),
        )


def broadcast(store: Any, *, body: str, from_id: str = "",
              kind: str = "broadcast", dedup_key: str = "",
              from_kind: str = "owner") -> list[str]:
    """Store one owner broadcast row per active worker so each ack is independent."""

    _validate_message(from_kind=from_kind, kind=kind, body=body)
    if from_kind != "owner":
        raise PermissionError("only from_kind='owner' may broadcast")
    with _transaction(store) as db:
        workers = _fetchall(
            db, "SELECT id FROM workers WHERE revoked_at IS NULL ORDER BY id"
        )
        ids: list[str] = []
        for row in workers:
            worker_id = str(row["id"])
            scoped = f"{dedup_key}:{worker_id}" if dedup_key else ""
            ids.append(_insert_message(
                db, to_worker=worker_id, to_task=None, broadcast_flag=0,
                from_kind="owner", from_id=str(from_id), kind=str(kind),
                body=str(body), dedup_key=scoped,
            ))
        return ids


def _active_task_addresses(db: Any, worker_id: str) -> list[str]:
    try:
        rows = _fetchall(
            db,
            """SELECT project,tz FROM claims
               WHERE worker_id=? AND state='active'""",
            (worker_id,),
        )
    except Exception:
        return []
    return [f"{row['project']}/{row['tz']}" for row in rows]


def _candidate_rows(db: Any, worker_id: str) -> list[dict[str, Any]]:
    task_addresses = _active_task_addresses(db, worker_id)
    clauses = ["to_worker=?", "broadcast=1"]
    params: list[Any] = [worker_id]
    if task_addresses:
        placeholders = ",".join("?" for _ in task_addresses)
        clauses.append(f"to_task IN ({placeholders})")
        params.extend(task_addresses)
    return _fetchall(
        db,
        f"""SELECT id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,
                   body,created_at,delivered_at,acked_at
            FROM messages
            WHERE acked_at IS NULL AND ({" OR ".join(clauses)})""",
        params,
    )


def _row_to_message(row: dict[str, Any], *, now: datetime | None = None) -> Message:
    current = now or _utcnow()
    created = _parse_ts(str(row.get("created_at") or ""))
    age_min = max(0, int((current - created).total_seconds() // 60))
    return Message(
        id=str(row["id"]),
        kind=str(row["kind"]),
        sender=_sender_token(str(row["from_kind"]), str(row.get("from_id") or "")),
        body=str(row["body"]),
        age_min=age_min,
    )


def _priority(row: dict[str, Any]) -> tuple[int, str, str]:
    return (
        _IMPORTANCE.get(str(row.get("kind") or ""), 50),
        str(row.get("created_at") or ""),
        str(row.get("id") or ""),
    )


def unread_since(store: Any, worker_id: str, since_iso: str) -> int:
    """Сколько неподтверждённых сообщений пришло этому воркеру после since_iso.

    Нужен парковке fleet_take: новое сообщение обрывает ожидание, старые непрочитанные
    (особенно broadcast) — нет, иначе висящий broadcast сорвал бы каждую парковку.
    """
    worker = str(worker_id)
    with _read_handle(store) as db:
        rows = _candidate_rows(db, worker)
    since = _parse_ts(since_iso)
    # Сравнение датами, не строками: пульт пишет created_at с точностью до секунды,
    # сервер — до микросекунды, и строковое сравнение путало бы их в пределах секунды.
    return sum(1 for row in rows if _parse_ts(str(row.get("created_at") or "")) >= since)


def pending_for(store: Any, worker_id: str, *, max_items: int = 3,
                max_bytes: int = 2048) -> list[Message]:
    """Select unacked facts by importance and mark the first inclusion attempt.

    delivered_at never suppresses redelivery; only ack does. Therefore a transport failure
    cannot consume a message.
    """

    if max_items < 0:
        raise ValueError("max_items must be >= 0")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be > 0")

    worker = str(worker_id)
    now = _utcnow()
    with _read_handle(store) as rdb:  # пусто (почти всегда) — без замка записи
        if not _candidate_rows(rdb, worker):
            return PendingMessages([], remaining_count=0, max_bytes=max_bytes)
    with _transaction(store) as db:
        rows = sorted(_candidate_rows(db, worker), key=_priority)
        selected: list[Message] = []
        selected_ids: list[str] = []
        for row in rows:
            if len(selected) >= max_items:
                break
            msg = _row_to_message(row, now=now)
            tentative = selected + [msg]
            projected = _render_envelope_text(
                tentative, worker, remaining_count=len(rows) - len(tentative)
            )
            if len(projected.encode("utf-8")) <= max_bytes:
                selected.append(msg)
                selected_ids.append(msg.id)

        remaining_count = len(rows) - len(selected)
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            db.execute(
                f"""UPDATE messages
                    SET delivered_at=COALESCE(delivered_at,?)
                    WHERE id IN ({placeholders})""",
                (_iso_now(), *selected_ids),
            )

    return PendingMessages(
        selected, remaining_count=remaining_count, max_bytes=max_bytes
    )


def ack(store: Any, worker_id: str, ids: list[str]) -> int:
    """Acknowledge only concrete ids addressed to this worker."""

    unique_ids = list(dict.fromkeys(str(item) for item in ids if str(item)))
    if not unique_ids:
        return 0
    worker = str(worker_id)
    with _transaction(store) as db:
        task_addresses = _active_task_addresses(db, worker)
        address_clauses = ["to_worker=?"]
        address_params: list[Any] = [worker]
        if task_addresses:
            placeholders = ",".join("?" for _ in task_addresses)
            address_clauses.append(f"to_task IN ({placeholders})")
            address_params.extend(task_addresses)

        id_placeholders = ",".join("?" for _ in unique_ids)
        cur = db.execute(
            f"""UPDATE messages
                SET acked_at=COALESCE(acked_at,?)
                WHERE id IN ({id_placeholders})
                  AND acked_at IS NULL
                  AND ({" OR ".join(address_clauses)})""",
            (_iso_now(), *unique_ids, *address_params),
        )
        return max(0, int(getattr(cur, "rowcount", 0) or 0))


def _age_text(age_min: int) -> str:
    minutes = max(0, int(age_min))
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"
    return f"{hours // 24} д"


def _sender_text(sender: str) -> str:
    if sender == "owner":
        return "владелец"
    if sender.startswith("owner:"):
        ident = sender.split(":", 1)[1]
        return f"владелец ({ident})" if ident else "владелец"
    if sender == "system" or sender.startswith("system:"):
        return "система"
    if sender.startswith("worker:"):
        ident = sender.split(":", 1)[1]
        return f"от {ident or 'воркера'}"
    return f"от {sender}"


def _render_message(msg: Message) -> str:
    sender = _sender_text(str(msg.sender))
    label = _KIND_LABEL.get(str(msg.kind), str(msg.kind).replace("_", " "))
    if sender == "система":
        head = f"[{msg.id}] {sender} — {label}:"
    else:
        head = f"[{msg.id}] {sender}, {_age_text(msg.age_min)} назад — {label}:"
    body = "\n".join("         " + line for line in str(msg.body).splitlines())
    return f"{head}\n{body}"


def _render_envelope_text(msgs: Sequence[Message], worker_id: str, *,
                          remaining_count: int) -> str:
    lines: list[str] = []
    if msgs:
        ids_json = json.dumps(
            [str(msg.id) for msg in msgs], ensure_ascii=False, separators=(",", ":")
        )
        lines.append(
            f'--- dev-mcp · {len(msgs)} уведомления '
            f'(подтвердить: fleet_ack("{worker_id}", {ids_json})) ---'
        )
        lines.extend(_render_message(msg) for msg in msgs)
    if remaining_count:
        lines.append(
            f'--- ещё {remaining_count} уведомления: fleet_inbox("{worker_id}") ---'
        )
    return "\n".join(lines)


def render_envelope(msgs: list[Message], worker_id: str) -> str:
    """Render CONTRACT section 9; the sender is always explicit."""

    remaining = int(getattr(msgs, "remaining_count", 0) or 0)
    return _render_envelope_text(msgs, str(worker_id), remaining_count=remaining)


__all__ = [
    "Message", "PendingMessages", "ack", "broadcast",
    "pending_for", "render_envelope", "send",
]
