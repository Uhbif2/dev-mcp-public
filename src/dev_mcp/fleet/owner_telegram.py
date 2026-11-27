"""Вопросы владельцу — в Telegram (владелец, 07.11.2026; №48 B4, 09.11.2026).

TickTick так и не стал рабочим каналом: владелец не узнавал о вопросах ведущих, а ведущие стояли
сутками. Канал — бот tg-notify (C:\\hub\\tg-notify, только отправка).

Источники (один канал вопросов, №48 B4):
- карточки препятствий ведущих (obstacles.py) — одно сообщение на карточку;
- метки owner-decision из проекции `_lib/owner_decisions.py` (правило 21 контракта) — одно
  сообщение на метку, повтор только при смене вопроса или цены ожидания. 06.11 вопрос lnb
  `O-visual-original` лёг меткой в OWNER.md, а в Telegram за всё время не ушло ни одного
  сообщения (owner_tg_sent = 0). Карточки препятствий из проекции (fleet-obstacles) и уже
  решённые владельцем стоячие вопросы второй раз не шлются.
Квитанция — событие owner_tg_sent с message_ids Telegram (квитанция API, не прочтение человеком).
Ответ владелец даёт в чате оператора или `fleet.cmd unblock`; решение пишется у канона линии.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import obstacles

LOG = logging.getLogger(__name__)
SENT = "owner_tg_sent"
INTERVAL_S = 60
MAX_PER_TICK = 5
TG_SERVER = os.environ.get("DEV_MCP_TG_NOTIFY", r"C:\hub\tg-notify\server.py")
DECISIONS = os.environ.get(
    "DEV_MCP_OWNER_DECISIONS",
    str(Path(__file__).resolve().parents[4] / "_lib" / "owner_decisions.py"))
_SKIP_LINES = {"fleet-obstacles"}


def enabled() -> bool:
    try:
        from .goals import _load_doc
        return bool((_load_doc().get("platform") or {}).get("telegram", True))
    except Exception:  # noqa: BLE001
        return True


def message(project: str, card: dict[str, Any]) -> str:
    held = ", ".join(card.get("unblocks") or card.get("obligations") or []) or "—"
    lines = [f"Нужно твоё решение — {project} ({card['id']})",
             f"Вопрос: {card.get('ask') or '—'}"]
    if card.get("option"):
        lines.append(f"Вариант ведущего: {card['option']}")
    if card.get("not_covered"):
        lines.append(f"Почему не покрыто твоими разрешениями: {card['not_covered']}")
    lines += [f"Держит: {held}",
              f"Ответ: в чате оператора «{card['id']}: да/нет …» "
              f"(или fleet.cmd unblock \"{project}\" {card['id']} \"…\")."]
    return "\n".join(lines)


def decision_digest(item: dict[str, Any]) -> str:
    raw = f"{item.get('id')}|{item.get('ask')}|{item.get('cost')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def decision_message(item: dict[str, Any]) -> str:
    age = item.get("age_days")
    since = item.get("since") or "дата не указана"
    lines = [f"Нужно твоё решение — {item.get('line')} ({item.get('id')})",
             f"Вопрос: {item.get('ask') or '—'}",
             f"Цена ожидания: {item.get('cost') or '—'}",
             f"Открыто: {since}" + (f" ({age} дн.)" if age is not None else ""),
             f"Где: {item.get('source') or '—'}",
             f"Ответ: в чате оператора «{item.get('id')}: …» — решение запишется у канона линии."]
    return "\n".join(lines)


def _decisions() -> list[dict[str, Any]]:
    """Open markers of the owner-decision projection (stdlib-only module of the hub)."""
    path = Path(DECISIONS)
    if not path.is_file():
        return []
    spec = importlib.util.spec_from_file_location("owner_decisions_projection", path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.collect().get("decisions") or [])


def _send(text: str) -> list[int] | bool:
    if not Path(TG_SERVER).is_file():
        return False
    run = subprocess.run([sys.executable, TG_SERVER, "send", text], capture_output=True,
                         text=True, encoding="utf-8", timeout=60,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if run.returncode != 0:
        LOG.warning("tg-notify send rc=%s: %s", run.returncode, (run.stderr or run.stdout)[-300:])
        return False
    try:
        ids = json.loads((run.stdout or "").strip().splitlines()[-1]).get("message_ids") or []
    except (ValueError, IndexError, AttributeError):
        ids = []
    return [int(i) for i in ids if isinstance(i, int)] or True


def _receipt(result: Any) -> dict[str, Any]:
    return {"message_ids": result} if isinstance(result, list) else {}


def tick(store: Any, send: Any = _send, decisions: Any = None) -> int:
    if not enabled():
        return 0
    with store.read() as tx:
        projects = [str(r["project"]) for r in tx.query(
            "SELECT DISTINCT project FROM events WHERE kind=? AND project IS NOT NULL", (obstacles.OPEN,))]
        rows = [json.loads(r["payload"] or "{}") for r in tx.query(
            f"SELECT payload FROM events WHERE kind='{SENT}'")]
    sent = {str(p.get("id")) for p in rows if not p.get("digest")}
    sent_decisions = {(str(p.get("id")), str(p.get("digest"))) for p in rows if p.get("digest")}
    count = 0
    for project in projects:
        for card in obstacles.cards(store, project):
            if card["id"] in sent or count >= MAX_PER_TICK:
                continue
            result = send(message(project, card))
            if not result:
                return count
            with store.tx() as tx:
                tx.record_event(kind=SENT, project=project, tz="TZ-LEAD",
                                payload={"id": card["id"], "channel": "telegram", **_receipt(result)})
            count += 1
    try:
        items = (decisions() or []) if decisions else []
    except Exception as exc:  # noqa: BLE001 — a broken projection must not stop cards
        LOG.warning("owner decisions projection failed: %s", exc)
        items = []
    for item in items:
        if (str(item.get("line")) in _SKIP_LINES or item.get("already_decided")
                or not item.get("id") or count >= MAX_PER_TICK):
            continue
        digest = decision_digest(item)
        if (str(item["id"]), digest) in sent_decisions:
            continue
        result = send(decision_message(item))
        if not result:
            return count
        with store.tx() as tx:
            tx.record_event(kind=SENT, payload={
                "id": str(item["id"]), "digest": digest, "kind": "owner_decision",
                "line": item.get("line"), "channel": "telegram", **_receipt(result)})
        count += 1
    return count


def _production(store: Any) -> bool:
    """Owner markers are read only for the live registry: a test DB must never message the owner."""
    try:
        from .location import fleet_db_path
        live = Path(fleet_db_path(Path(__file__).resolve().parents[3])).resolve()
        return Path(str(getattr(store, "path", ""))).resolve() == live
    except Exception:  # noqa: BLE001
        return False


def run_worker(store: Any, stop_event: Any) -> None:
    source = _decisions if _production(store) else None
    while not stop_event.is_set():
        try:
            tick(store, decisions=source)
        except Exception as exc:  # noqa: BLE001 — the notification channel never kills the server
            LOG.warning("owner telegram tick failed: %s", exc)
        stop_event.wait(INTERVAL_S)
