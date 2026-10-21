"""Будильник: сервер сам будит замолчавшего агента в ЕГО чате (27.09.2026).

Зачем. Ход агента web-ChatGPT кончается сам — посреди задачи в 36% ходов. Раньше будить
его могло только расширение и только если вкладка с чатом была открыта; сервер адреса чата
не знал. 27.09 так встала Nova TZ-78, за которой 36 задач: W587 14 минут работал с
телефоном, ход кончился, вкладка закрылась, очередь «выдавать нечего» (задача числится за
W587) — новых чатов не открывают, старый никто не будит, а возврат задачи с записанной работой
срабатывал только через 180 минут.

Решение — лестница, которую ведёт сервер, а расширение лишь исполняет:
  тишина ≥ WAKE_AFTER_MIN   → приказ №1: открыть чат агента и отправить текст продолжения;
  ещё ≥ RETRY_AFTER_MIN     → приказ №2;
  ещё ≥ GIVE_UP_AFTER_MIN после последнего приказа (или чат неизвестен и тишина
  ≥ NO_CHAT_GIVE_UP_MIN)    → задача возвращается в очередь с сохранённой работой, старый
                               воркер отзывается — её продолжит другой агент.
Приоритет — число задач, которые ждут эту (критический путь первым).

Адреса чатов шлёт расширение в fleet_list (`launcher_chats`: строки «label url»);
храним в data/fleet/chats.json рядом с реестром. Приказы — события `wake_order` в реестре
(то же место, где видна вся остальная жизнь задачи); доставку подтверждает сам агент любым
следующим вызовом — отдельного «ack» не нужно, тишина просто прерывается.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WAKE_AFTER_MIN = float(os.environ.get("DEV_MCP_FLEET_WAKE_AFTER_MIN", "4"))
RETRY_AFTER_MIN = float(os.environ.get("DEV_MCP_FLEET_WAKE_RETRY_MIN", "8"))
GIVE_UP_AFTER_MIN = float(os.environ.get("DEV_MCP_FLEET_WAKE_GIVE_UP_MIN", "8"))
NO_CHAT_GIVE_UP_MIN = float(os.environ.get("DEV_MCP_FLEET_WAKE_NO_CHAT_MIN", "20"))
MAX_ORDERS = 2
ORDERS_PER_CALL = 2  # расширение исполняет по одному действию аккаунта в минуту
GENERATING_LIMIT_MIN = 45.0

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    try:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class ChatBook:
    """label → адрес чата. Файл маленький, пишет только этот процесс."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def remember(self, raw: str, now: datetime | None = None) -> int:
        """raw — строки «label url». Возвращает число принятых записей."""
        now_s = (now or _now()).isoformat(timespec="seconds")
        seen: dict[str, str] = {}
        for line in str(raw or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            label, url = parts[0], parts[1].strip()
            if url.startswith("https://chatgpt.com/") and "/c/" in url and len(label) <= 80:
                seen[label] = url
        if not seen:
            return 0
        with _lock:
            book = self.load()
            for label, url in seen.items():
                book[label] = {"url": url, "seen_at": now_s}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(book, ensure_ascii=False, indent=0), encoding="utf-8")
            os.replace(tmp, self.path)
        return len(seen)

    def url(self, label: str) -> str:
        return str((self.load().get(label) or {}).get("url") or "")


def _blocks(tx: Any) -> dict[tuple[str, str], int]:
    """(project, tz) → сколько незавершённых задач прямо ждут её (needs)."""
    counts: dict[tuple[str, str], int] = {}
    for row in tx.query(
        "SELECT project,needs FROM tasks WHERE state NOT IN ('accepted','rejected')"
    ):
        try:
            needs = json.loads(row["needs"] or "[]")
        except (TypeError, ValueError):
            continue
        for need in needs if isinstance(needs, list) else []:
            key = (str(row["project"]), str(need).split("#", 1)[0])
            counts[key] = counts.get(key, 0) + 1
    return counts


def _silent_claims(tx: Any, now: datetime, min_silence: float) -> list[dict[str, Any]]:
    rows = tx.query(
        """SELECT c.id AS claim_id,c.project,c.tz,c.worker_id,c.started_at,
                  w.label,w.last_seen
             FROM claims c
             JOIN tasks t ON t.project=c.project AND t.tz=c.tz
             JOIN workers w ON w.id=c.worker_id
            WHERE c.state='active' AND t.state='taken' AND w.revoked_at IS NULL
            ORDER BY c.id"""
    )
    out = []
    for row in rows:
        last = _parse(row["last_seen"])
        if last is None:
            continue
        silence = (now - last).total_seconds() / 60
        if silence >= min_silence:
            item = dict(row)
            item["silence_min"] = silence
            item["last_seen_dt"] = last
            out.append(item)
    return out


def _orders_by_claim(tx: Any, now: datetime) -> dict[int, list[dict[str, Any]]]:
    """Приказы и ответы по захвату: один проход по событиям, а не N×events."""
    by_claim: dict[int, list[dict[str, Any]]] = {}
    by_wake: dict[str, dict[str, Any]] = {}
    for row in tx.query(
        "SELECT ts,kind,payload FROM events "
        "WHERE kind IN ('wake_order','wake_state') AND ts>=? ORDER BY id",
        ((now - timedelta(days=2)).isoformat(),),
    ):
        try:
            payload = json.loads(row["payload"] or "{}")
            ts = _parse(payload.get("at") or row["ts"])
            if ts is None:
                continue
            token = str(payload.get("wake") or "")
            if row["kind"] == "wake_order":
                entry = {"at": ts, "wake": token, "reports": []}
                by_claim.setdefault(int(payload["claim_id"]), []).append(entry)
                if token:
                    by_wake[token] = entry
            elif row["kind"] == "wake_state":
                entry = by_wake.get(token)
                if entry is not None and payload.get("state") in (
                    "generating", "idle", "error", "not_found"
                ):
                    entry["reports"].append((ts, str(payload["state"])))
        except (TypeError, ValueError, KeyError):
            continue
    return by_claim


def _ladder(history: list[dict[str, Any]], last_seen: datetime) -> tuple[list[datetime], datetime | None]:
    """Засчитанные приказы и начало непрерывного наблюдаемого generating."""
    recent = [item for item in history if item["at"] >= last_seen]
    counted: list[datetime] = []
    reports: list[tuple[datetime, str]] = []
    for item in recent:
        observations = item["reports"]
        reports.extend(observations)
        if not observations or observations[-1][1] != "generating":
            # Старое расширение без отчёта сохраняет прежнюю лестницу.
            counted.append(max(item["at"], observations[-1][0]) if observations else item["at"])
    generating_since: datetime | None = None
    for at, state in sorted(reports, key=lambda pair: pair[0]):
        if state == "generating":
            if generating_since is None:
                generating_since = at
        else:
            generating_since = None
    return counted, generating_since


def report_state(store: Any, *, wake_id: str, state: str,
                 now: datetime | None = None) -> bool:
    """Отчёт расширения по выданному токену: не обновляет heartbeat модели."""
    if not wake_id.startswith("w-") or len(wake_id) != 8:
        raise ValueError("invalid wake token")
    if state not in ("generating", "idle", "error", "not_found"):
        raise ValueError("invalid wake state")
    now = now or _now()
    with store.tx() as tx:
        issued = None
        for row in tx.query(
            "SELECT ts,payload,project,tz,worker_id FROM events "
            "WHERE kind='wake_order' AND payload LIKE ? ORDER BY id DESC LIMIT 10",
            (f"%{wake_id}%",),
        ):
            if json.loads(row["payload"] or "{}").get("wake") == wake_id:
                issued = row
                break
        if issued is None:
            raise ValueError("wake token not issued")
        meta = json.loads(issued["payload"] or "{}")
        claim_id = int(meta["claim_id"])
        active = tx.one(
            "SELECT c.state,t.state AS task_state,w.revoked_at,w.last_seen "
            "FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "JOIN workers w ON w.id=c.worker_id "
            "WHERE c.id=? AND c.worker_id=? AND c.project=? AND c.tz=?",
            (claim_id, issued["worker_id"], issued["project"], issued["tz"]),
        )
        if (active is None or active["state"] != "active" or active["task_state"] != "taken"
                or active["revoked_at"] is not None or
                (_parse(active["last_seen"]) or now) > (_parse(meta.get("at") or issued["ts"]) or now)):
            return False
        for row in tx.query(
            "SELECT payload FROM events WHERE kind='wake_state' AND payload LIKE ? "
            "ORDER BY id DESC LIMIT 10", (f"%{wake_id}%",),
        ):
            value = json.loads(row["payload"] or "{}")
            if value.get("wake") == wake_id:
                if value.get("state") == state:
                    return False
                break
        tx.record_event(
            kind="wake_state", project=str(issued["project"]), tz=str(issued["tz"]),
            payload={"claim_id": claim_id, "wake": wake_id, "state": state,
                     "at": now.isoformat()},
        )
    return True


def wake_text(item: dict[str, Any]) -> str:
    """Текст будки. Токен wake доказывает доставку: агент приходит с ним в fleet_resume, и сервер
    отличает «разбудили» от «работал и так» (эксперимент E3). Следующий шаг — из контрольной точки."""
    step = str(item.get("next_step") or "").strip()
    step_line = f" Следующий шаг по твоей контрольной точке: {step[:300]}" if step else ""
    return (
        f"Продолжай {item['project']}/{item['tz']} — твой ход прервался посреди задачи. "
        f"Первым вызовом fleet_resume(agent='{item['worker_id']}', wake='{item.get('wake', '')}'): "
        f"там границы, контрольная точка и сделанное.{step_line} "
        f"Доведи задачу до fleet_submit, потом бери следующую."
    )


def orders(store: Any, book: ChatBook, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Выдать приказы «разбудить» (и записать их событиями). Порядок — критический путь."""
    now = now or _now()
    issued: list[dict[str, Any]] = []
    with store.read() as rx:  # частый вызов: пустой случай — без замка записи
        if not _silent_claims(rx, now, WAKE_AFTER_MIN):
            return []
    with store.tx() as tx:
        candidates = _silent_claims(tx, now, WAKE_AFTER_MIN)
        if not candidates:
            return []
        by_claim = _orders_by_claim(tx, now)
        blocks = _blocks(tx)
        for item in candidates:
            item["blocks"] = blocks.get((str(item["project"]), str(item["tz"])), 0)
        candidates.sort(key=lambda i: (-i["blocks"], -i["silence_min"]))
        for item in candidates:
            if len(issued) >= ORDERS_PER_CALL:
                break
            url = book.url(str(item["label"] or ""))
            if not url:
                continue
            history = [entry for entry in by_claim.get(int(item["claim_id"]), [])
                       if entry["at"] >= item["last_seen_dt"]]
            stamps, generating_since = _ladder(history, item["last_seen_dt"])
            if generating_since and (now - generating_since).total_seconds() / 60 > GENERATING_LIMIT_MIN:
                continue
            if len(stamps) >= MAX_ORDERS and generating_since is None:
                continue
            if history and (now - history[-1]["at"]).total_seconds() / 60 < RETRY_AFTER_MIN:
                continue
            order = {
                "claim_id": int(item["claim_id"]), "label": str(item["label"]), "url": url,
                "project": str(item["project"]), "tz": str(item["tz"]),
                "worker_id": str(item["worker_id"]), "attempt": len(stamps) + 1,
                "silence_min": round(item["silence_min"], 1), "blocks": item["blocks"],
                "wake": "w-" + uuid.uuid4().hex[:6],
            }
            cp = tx.one(
                "SELECT payload FROM events WHERE kind='checkpoint' AND project=? AND tz=? "
                "ORDER BY id DESC LIMIT 1", (order["project"], order["tz"]),
            )
            if cp is not None:
                try:
                    order["next_step"] = str(json.loads(cp["payload"] or "{}").get("next") or "")
                except ValueError:
                    pass
            tx.record_event(
                kind="wake_order", worker_id=order["worker_id"], project=order["project"],
                tz=order["tz"],
                payload={"claim_id": order["claim_id"], "attempt": order["attempt"],
                         "at": now.isoformat(), "wake": order["wake"],
                         "silence_min": order["silence_min"], "blocks": order["blocks"]},
            )
            order["text"] = wake_text(order)
            issued.append(order)
    return issued


def give_up(store: Any, book: ChatBook, *, now: datetime | None = None) -> list[str]:
    """Вернуть в очередь задачи, которые будильник не смог поднять. Работа на диске
    сохраняется, старый воркер отзывается, чтобы не писал поверх нового."""
    now = now or _now()
    released: list[str] = []
    with store.read() as rx:  # пустой случай — без замка записи
        if not _silent_claims(rx, now, WAKE_AFTER_MIN):
            return []
    with store.tx() as tx:
        by_claim = _orders_by_claim(tx, now)
        for item in _silent_claims(tx, now, WAKE_AFTER_MIN):
            known_chat = bool(book.url(str(item["label"] or "")))
            history = [entry for entry in by_claim.get(int(item["claim_id"]), [])
                       if entry["at"] >= item["last_seen_dt"]]
            stamps, generating_since = _ladder(history, item["last_seen_dt"])
            generating_expired = bool(generating_since and
                (now - generating_since).total_seconds() / 60 > GENERATING_LIMIT_MIN)
            if generating_since and not generating_expired:
                continue
            if known_chat:
                if len(stamps) < MAX_ORDERS and not generating_expired:
                    continue
                if not generating_expired and (
                    not stamps or (now - stamps[-1]).total_seconds() / 60 < GIVE_UP_AFTER_MIN
                ):
                    continue
                why = ("будильник: generating >45 мин без вызова инструмента"
                       if generating_expired else
                       f"будильник: {len(stamps)} приказа без ответа")
            else:
                if item["silence_min"] < NO_CHAT_GIVE_UP_MIN:
                    continue
                why = f"будильник: чат неизвестен, тишина {int(item['silence_min'])} мин"
            accepted = tx.one(
                "SELECT 1 AS hit FROM events WHERE project=? AND tz=? AND kind='accept' AND ts>=? LIMIT 1",
                (item["project"], item["tz"], item["started_at"]),
            )
            if accepted is not None:
                continue
            ts = now.isoformat()
            reason = f"automatic recovery {item['project']}/{item['tz']}: {why}; preserved worktree"
            tx.changes(
                [
                    ("UPDATE claims SET state='requeued',released_at=? WHERE id=?", (ts, item["claim_id"])),
                    ("UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=?",
                     (ts, item["project"], item["tz"])),
                    ("UPDATE workers SET revoked_at=COALESCE(revoked_at,?),revoke_reason=? WHERE id=?",
                     (ts, reason, item["worker_id"])),
                ],
                event={
                    "kind": "requeue", "worker_id": str(item["worker_id"]),
                    "project": str(item["project"]), "tz": str(item["tz"]),
                    "payload": {"automatic_recovery": True, "preserved_work": True, "why": why,
                                "wake_orders": len(stamps), "generating_expired": generating_expired},
                },
            )
            released.append(f"{item['project']}/{item['tz']}")
    return released


def order_lines(items: list[dict[str, Any]]) -> list[str]:
    """Строки для fleet_list: `разбудить: <label> <url> :: <текст>`."""
    return [f"разбудить: {i['label']} {i['url']} :: {i['text']}" for i in items]
