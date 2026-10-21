"""Контрольная точка захвата: что установлено, решено, отвергнуто, неизвестно и следующий шаг
(архитектура v2 §4, 27.09.2026).

Зачем. Ход агента web-ChatGPT кончается сам — посреди задачи в трети случаев, — а задача
переживает и ход, и чат: будильник будит тот же чат, отказ от него возвращает задачу в очередь
другому агенту. Раньше продолжающий видел только факты из журнала (что записано, что
запускалось), но не мысль: что уже проверено, что отвергнуто и почему, какой шаг следующий.
Контрольная точка — отдельный вызов, а не finish (тот возвращает задачу в очередь): задача
долговечна, исполнитель заменяем.

Хранится событием `checkpoint` в реестре (там же, где вся жизнь задачи). Руль: если захват
идёт дольше DUE_MIN без точки, конверт следующего ответа просит её поставить.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

DUE_MIN = float(os.environ.get("DEV_MCP_FLEET_CHECKPOINT_DUE_MIN", "15"))
FIELD_MAX = 1500
FIELDS = (
    ("done", "установлено"),
    ("decided", "решено"),
    ("rejected", "отвергнуто"),
    ("unknown", "неизвестно"),
    ("running", "идёт сейчас"),
    ("next", "следующий шаг"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(raw: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _active_claim(tx: Any, worker_id: str) -> Any:
    return tx.one(
        "SELECT c.id,c.project,c.tz,c.started_at,t.kind FROM claims c "
        "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
        "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
        (worker_id,),
    )


def record(store: Any, worker_id: str, fields: dict[str, str]) -> str:
    body = {k: str(fields.get(k) or "").strip()[:FIELD_MAX] for k, _ in FIELDS}
    if not body["done"] or not body["next"]:
        raise ValueError("контрольной точке нужны хотя бы done (что установлено) и next_step (следующий шаг)")
    with store.tx() as tx:
        claim = _active_claim(tx, worker_id)
        if claim is None:
            raise ValueError("активной задачи нет — контрольная точка ставится внутри захвата (после fleet_take)")
        body["claim_id"] = int(claim["id"])
        tx.record_event(kind="checkpoint", worker_id=worker_id, project=str(claim["project"]),
                        tz=str(claim["tz"]), payload=body)
    return f"{claim['project']}/{claim['tz']}"


def latest(store: Any, project: str, tz: str) -> dict[str, Any] | None:
    """Последняя точка задачи — от любого исполнителя: продолжающий видит мысль предыдущего."""
    with store.read() as tx:
        row = tx.one(
            "SELECT ts,worker_id,payload FROM events WHERE kind='checkpoint' AND project=? AND tz=? "
            "ORDER BY id DESC LIMIT 1",
            (project, tz),
        )
    if row is None:
        return None
    try:
        data = json.loads(row["payload"] or "{}")
    except ValueError:
        return None
    return {**data, "ts": str(row["ts"]), "worker_id": str(row["worker_id"] or "")}


def render(cp: dict[str, Any] | None, title: str = "Контрольная точка") -> str:
    if not cp:
        return ""
    when = _parse(cp.get("ts"))
    stamp = when.astimezone().strftime("%d.%m %H:%M") if when else "?"
    lines = [f"{title} ({cp.get('worker_id') or '?'}, {stamp}):"]
    for key, label in FIELDS:
        value = str(cp.get(key) or "").strip()
        if value:
            lines.append(f"  {label}: {value}")
    return "\n".join(lines)


def _sees_new_tools(tx: Any, worker_id: str) -> bool:
    """Видит ли агент fleet_checkpoint. ChatGPT берёт список инструментов при подключении
    коннектора; до переподключения руль звал бы инструмент, которого у агента нет (07.10: ноль
    вызовов за 10 дней). Признак переподключения — первый вызов нового инструмента кем угодно;
    видят его агенты, подключившиеся после этого."""
    first = tx.one(
        "SELECT MIN(ts) AS ts FROM events WHERE kind='checkpoint' OR tool IN ('fleet_checkpoint','apply_patch')"
    )
    if first is None or not first["ts"]:
        return False
    worker = tx.one("SELECT first_seen FROM workers WHERE id=?", (worker_id,))
    return worker is not None and str(worker["first_seen"]) >= str(first["ts"])[:19] or (
        tx.one("SELECT 1 FROM events WHERE worker_id=? AND (kind='checkpoint' OR tool IN "
               "('fleet_checkpoint','apply_patch')) LIMIT 1", (worker_id,)) is not None
    )


def due(store: Any, worker_id: str, now: datetime | None = None) -> str:
    """Текст руля, если захват идёт дольше DUE_MIN без контрольной точки; иначе ''."""
    now = now or _now()
    with store.read() as tx:
        claim = _active_claim(tx, worker_id)
        if claim is None or str(claim["kind"]) == "review":
            return ""
        if not _sees_new_tools(tx, worker_id):
            return ""
        last = tx.one(
            "SELECT ts FROM events WHERE kind='checkpoint' AND worker_id=? AND project=? AND tz=? "
            "AND ts>=? ORDER BY id DESC LIMIT 1",
            (worker_id, claim["project"], claim["tz"], claim["started_at"]),
        )
    since = _parse(last["ts"] if last is not None else claim["started_at"])
    if since is None or (now - since).total_seconds() / 60 < DUE_MIN:
        return ""
    minutes = int((now - since).total_seconds() // 60)
    return (
        f"руль: по {claim['project']}/{claim['tz']} {minutes} мин без контрольной точки. Одним вызовом "
        f"fleet_checkpoint(agent='{worker_id}', done=…, next_step=…, decided/rejected/unknown/running по "
        "делу) — и продолжай работу. Если ход оборвётся, с неё продолжит будильник или другой агент."
    )
