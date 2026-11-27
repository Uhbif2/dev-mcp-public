"""Поручения ведущему — объекты реестра, а не записки (LEAD-SYSTEM-DESIGN фаза 0.3, _todo/47 §3.1).

05–06.11: около 30 писем владельца ведущему Nova доставлены, но ни одно не записано в его
книгу; чат ведущего живёт одну веху, и следующий о письмах не знал. m-40a2e777 ушло в закрытый
раунд fix:TZ-LEAD:7 и не было доставлено вовсе.

Поручение — событие `instruction` (id, автор, текст, требуемый результат, критерий «сделано»,
источник); его судьба — события `instruction_status`:
    open → accepted(O-*) | declined(reason) | done(evidence)
Закрывает ведущий инструментом `fleet_lead(action='instruction')`, а не текстом в STATE.md.
`accepted` требует существующего O-* в его книге — поручение становится входом контракта.
Письмо владельца (или контракт) к любой задаче семьи TZ-LEAD само становится поручением
(`sync_messages`), поэтому ничего не теряется, даже если письмо никто не прочёл.
Хранение — события реестра (без DDL); состояние — свёртка (`items`).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KIND = "instruction"
STATUS_KIND = "instruction_status"
TARGET_KIND = "instruction_target"
STATUSES = ("accepted", "declined", "done")
LEGACY = "noted"  # id письма уже стоял в каноне ведущего до 06.11 — статус надо уточнить
CLOSED = ("declined", "done")
_OBLIGATION = re.compile(r"^O-[\w.\-]+$")


def family_patterns(project: str) -> tuple[str, str, str, str]:
    """Адреса семьи устава: письмо к закрытому раунду — всё равно ведущему (m-40a2e777)."""
    return (f"{project}/TZ-LEAD", f"{project}/TZ-LEAD:%", f"{project}/fix:TZ-LEAD%",
            f"{project}/review:TZ-LEAD%")


def _payload(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def queue_target(tx: Any, project: str) -> str:
    """The newest explicit owner link whose instruction is still open."""
    rows = tx.query(
        "SELECT payload FROM events WHERE project=? AND tz='TZ-LEAD' AND kind=? ORDER BY id DESC",
        (project, TARGET_KIND),
    )
    for row in rows:
        link = _payload(row)
        status = tx.one(
            "SELECT payload FROM events WHERE project=? AND tz='TZ-LEAD' AND kind=? "
            "AND json_extract(payload,'$.id')=? ORDER BY id DESC LIMIT 1",
            (project, STATUS_KIND, link["id"]),
        )
        if status is None or _payload(status).get("status") not in CLOSED:
            return str(link["task"])
    return ""


def _root(store: Any, project: str) -> Path | None:
    with store.read() as tx:
        row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
    return Path(str(row["root"])) if row is not None and row["root"] else None


def ledger_ids(root: Path | None) -> set[str]:
    """Все O-* книги ведущего: `obligations[].id` и ключи-идентификаторы в любом месте."""
    if root is None:
        return set()
    try:
        data = json.loads((root / "ToDo" / "lead" / "LEDGER.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            value = node.get("id")
            if isinstance(value, str) and _OBLIGATION.match(value):
                found.add(value)
            for key, child in node.items():
                if isinstance(key, str) and _OBLIGATION.match(key):
                    found.add(key)
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(data)
    return found


def items(store: Any, project: str) -> list[dict[str, Any]]:
    with store.read() as tx:
        rows = tx.query("SELECT id,ts,kind,payload FROM events WHERE project=? AND kind IN (?,?) "
                        "ORDER BY id", (project, KIND, STATUS_KIND))
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = _payload(row)
        iid = str(data.get("id") or "")
        if not iid:
            continue
        if row["kind"] == KIND:
            out.setdefault(iid, {**data, "ts": str(row["ts"]), "status": "open", "history": []})
        elif iid in out:
            item = out[iid]
            item["status"] = str(data.get("status") or item["status"])
            for key in ("obligation", "reason", "evidence", "by"):
                if data.get(key):
                    item[key] = data[key]
            item["history"].append({"ts": str(row["ts"]), "status": item["status"]})
    return list(out.values())


def register(store: Any, project: str, *, text: str, want: str = "", done_when: str = "",
             author: str = "owner", source: str = "", priority: int = 5, msg_id: str = "",
             iid: str = "", strict: bool = True) -> str:
    text = str(text or "").strip()
    if strict and (len(text) < 10 or len(str(done_when or "").strip()) < 5):
        raise ValueError("instruction: нужны текст (от 10 знаков) и критерий «сделано» (done_when)")
    if not text:
        raise ValueError("instruction: пустой текст")
    if not iid:
        iid = "i-" + (msg_id[2:] if msg_id.startswith("m-") else uuid.uuid4().hex[:10])
    with store.tx() as tx:
        known = tx.one("SELECT 1 FROM events WHERE kind=? AND project=? AND "
                       "json_extract(payload,'$.id')=? LIMIT 1", (KIND, project, iid))
        if known is None:
            tx.record_event(kind=KIND, project=project, tz="TZ-LEAD", payload={
                "id": iid, "author": author, "text": text[:4000], "want": str(want or "")[:1000],
                "done_when": str(done_when or "")[:1000], "priority": int(priority),
                "source": str(source or "")[:300], "msg_id": msg_id})
    return iid


def sync_messages(store: Any, project: str, *, days: int = 14) -> int:
    """Письма владельца/контракты к семье TZ-LEAD без объекта — завести поручением."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store.read() as tx:
        rows = [dict(r) for r in tx.query(
            "SELECT id,from_kind,from_id,kind,body,created_at,to_task FROM messages WHERE created_at>=? "
            "AND (from_kind='owner' OR kind='contract') AND (to_task=? OR to_task LIKE ? OR "
            "to_task LIKE ? OR to_task LIKE ?) ORDER BY created_at", (since, *family_patterns(project)))]
        known = {str(_payload(r).get("msg_id") or "") for r in tx.query(
            "SELECT payload FROM events WHERE kind=? AND project=?", (KIND, project))}
    fresh = [r for r in rows if str(r["id"]) not in known]
    if not fresh:
        return 0
    try:
        from .lead_admin import _lead_texts
        root = _root(store, project)
        canon = _lead_texts(root) if root is not None else ""
    except Exception:  # noqa: BLE001 — без канона просто нет старых отметок
        canon = ""
    for r in fresh:
        author = "owner" if r["from_kind"] == "owner" else f"{r['from_kind']}:{r.get('from_id') or ''}"
        iid = register(store, project, text=str(r["body"] or ""), author=author, strict=False,
                       source=f"письмо {r['id']} → {r['to_task']} ({str(r['created_at'])[:16]})",
                       msg_id=str(r["id"]))
        if str(r["id"]) in canon:
            with store.tx() as tx:
                tx.record_event(kind=STATUS_KIND, project=project, tz="TZ-LEAD", payload={
                    "id": iid, "status": LEGACY, "by": "import",
                    "reason": "id письма уже записан в каноне ведущего (до объектов-поручений)"})
    return len(fresh)


def set_status(store: Any, agent: str, project: str, params: dict[str, Any]) -> str:
    iid = str(params.get("id") or "").strip()
    status = str(params.get("status") or "").strip()
    if status not in STATUSES:
        raise ValueError("instruction: status ∈ accepted (obligation=O-…), declined (reason), done (evidence)")
    current = next((i for i in items(store, project) if i["id"] == iid), None)
    if current is None:
        raise ValueError(f"instruction: {iid or '?'} не найдено в поручениях {project}")
    if current["status"] in CLOSED:
        raise ValueError(f"instruction: {iid} уже закрыто ({current['status']})")
    payload: dict[str, Any] = {"id": iid, "status": status, "by": agent}
    if status == "accepted":
        obligation = str(params.get("obligation") or "").strip()
        if not _OBLIGATION.match(obligation):
            raise ValueError("instruction accepted: нужен obligation=O-… из твоей книги")
        if obligation not in ledger_ids(_root(store, project)):
            raise ValueError(f"instruction accepted: {obligation} нет в ToDo/lead/LEDGER.json — "
                             "сначала заведи обязательство в книге")
        payload["obligation"] = obligation
    elif status == "declined":
        reason = str(params.get("reason") or "").strip()
        if len(reason) < 15:
            raise ValueError("instruction declined: reason — почему не делаешь (от 15 знаков); "
                             "решение за владельцем — вопрос в OWNER.md с меткой owner-decision")
        payload["reason"] = reason[:1000]
    else:
        from .lead_focus import settings

        focus = settings(project)
        if focus.get("orchestrated") and current.get("source") == focus.get("instruction_source"):
            raise ValueError("Закрой поручение через fleet_lead result с проверяемым event_id.")
        evidence = str(params.get("evidence") or "").strip()
        if len(evidence) < 15:
            raise ValueError("instruction done: evidence — чем видно, что сделано (коммит, замер, квитанция)")
        payload["evidence"] = evidence[:1000]
        if current.get("obligation"):
            payload["obligation"] = current["obligation"]
    with store.tx() as tx:
        tx.record_event(kind=STATUS_KIND, worker_id=agent, project=project, tz="TZ-LEAD", payload=payload)
    return f"{project}: поручение {iid} → {status}"


def _short(text: Any, size: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:size]


def lines(store: Any, project: str) -> list[str]:
    """Бриф ведущего: открытые поручения — всегда, пока у них нет статуса."""
    try:
        sync_messages(store, project)
    except Exception:  # noqa: BLE001 — бриф не падает из-за импорта писем
        pass
    # Reports imported before the owner/worker split remain in history.
    its = [item for item in items(store, project) if not str(item.get("author") or "").startswith("worker:")]
    open_ = [i for i in its if i["status"] == "open"]
    legacy = [i for i in its if i["status"] == LEGACY]
    accepted = [i for i in its if i["status"] == "accepted"]
    if not (open_ or legacy or accepted):
        return []
    out = [f"Поручения владельца и сигналы поля: без статуса {len(open_)}, старые отметки {len(legacy)}, "
           f"приняты в книгу и не сделаны {len(accepted)}. Каждое закрой fleet_lead(action='instruction', "
           "params={id, status: accepted+obligation=O-… | declined+reason | done+evidence}) — "
           "текст в STATE.md поручение не закрывает."]
    def priority(item: dict[str, Any]) -> tuple[bool, int, str]:
        return (item.get("author") != "owner", int(item.get("priority") or 5), item["ts"])

    visible_open = sorted(open_, key=priority)[:8]
    visible_accepted = sorted(reversed(accepted), key=lambda item: item.get("author") != "owner")[:5]
    visible = sorted([*visible_open, *visible_accepted], key=lambda item: item.get("author") != "owner")
    for i in visible:
        if i["status"] == "accepted":
            out.append(f"  ⧗ [{i['id']}] → {i.get('obligation')}: {_short(i.get('text'), 140)}")
            continue
        extra = ""
        if i.get("want"):
            extra += f" | результат: {_short(i['want'], 160)}"
        if i.get("done_when"):
            extra += f" | сделано, когда: {_short(i['done_when'], 160)}"
        out.append(f"  [{i['id']}] {i['ts'][:16]}: {_short(i.get('text'), 360)}{extra}")
    if len(open_) > 8:
        out.append(f"  … и ещё {len(open_) - 8} без статуса")
    if legacy:
        out.append("  Старые отметки (id письма есть в каноне, статуса нет): "
                   + ", ".join(i["id"] for i in legacy[:12]) + " — переведи в accepted/declined/done.")
    return out


def review_lines(store: Any, project: str) -> list[str]:
    """Ревьюеру вехи: что владелец поручил и что ещё не закрыто — раздел «забытое»."""
    its = [i for i in items(store, project) if i["status"] not in CLOSED]
    if not its:
        return []
    out = [f"Открытые поручения владельца ({len(its)}): веха не должна их терять. Если веха их "
           "касается — проверь статус (accepted → O-* вехи, done → доказательство)."]
    for i in its[:6]:
        out.append(f"  [{i['id']}] {i['status']}: {_short(i.get('text'), 200)}")
    return out


__all__ = ["register", "sync_messages", "set_status", "items", "lines", "review_lines",
           "ledger_ids", "family_patterns", "KIND", "STATUS_KIND"]
