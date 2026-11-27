"""Препятствия ведущего и готовность его устава (LEAD-SYSTEM-DESIGN фаза 1.1, Ш3).

05–06.11: у ведущих 454 вердикта и 0 переходов в works_live. Обязательства ждали владельца
(разрешение на сбор данных, живой корпус, отладку телефона), а устав возвращался «сразу после
приёмки» — и ведущий находил, чем заняться: укреплял свой градусник. Здесь:

- препятствие — типизированный объект: `owner_action` (решение/действие владельца) или
  `external_fact` (внешний факт); у него список O-*, вопрос, готовый вариант и предикат снятия:
  `owner_answer` (ответ владельца: `fleet.cmd unblock` или поручение, упоминающее id),
  `resource_up` (ресурс мира на связи), `file_exists` (файл в проекте появился);
- готовность устава — есть ли O-*, которое можно продвинуть: не завершено и не заблокировано
  открытым препятствием или статусом owner_question; или есть поручение без статуса;
- не готов — устав не возвращается (`lead_parked`), владелец получает по одной карточке на
  препятствие (метка owner-decision в data/fleet/owner-cards/<проект>.md — корень очереди
  решений пульта), карточки ранжированы по числу разблокируемых O-*;
- предикат выполнился — препятствие закрывается само, устав возвращается на следующем шаге.
- исполнитель задаётся полем actor=owner|operator|external; operator ведёт в ручной
  журнал платформы (без постановки задачи агенту), старые записи распознаются по тексту.
Хранение — события реестра (obstacle_open / obstacle_closed), без DDL.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPEN = "obstacle_open"
CLOSED = "obstacle_closed"
TYPES = ("owner_action", "external_fact")
PREDICATES = ("owner_answer", "resource_up", "file_exists")
DONE = frozenset({"works_live", "done", "delivered", "retired", "superseded", "declined", "released"})
OWNER_STATUS = "owner_question"
ACTORS = ("owner", "operator", "external")
_OPERATOR_PATTERN = re.compile(
    r"(?i)(?:\boperator\b|\bclaude\s*code\b|оператор(?:у|ом|а)?|"
    r"isolated clean main|clean git|oracle mcp|oracle.*маршрут|"
    r"интеграц(?:ия|ию|ировать).*git)"
)
_OBLIGATION = re.compile(r"^O-[\w.\-]+$")


def _payload(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _root(store: Any, project: str) -> Path | None:
    with store.read() as tx:
        row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
    return Path(str(row["root"])) if row is not None and row["root"] else None


def ledger_obligations(root: Path | None) -> list[dict[str, Any]] | None:
    """O-* книги со статусом; None — книги нет или она нечитаема (тогда не паркуем)."""
    if root is None:
        return None
    try:
        data = json.loads((root / "ToDo" / "lead" / "LEDGER.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("obligations") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        raw = [{"id": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    if not isinstance(raw, list):
        return None
    return [o for o in raw if isinstance(o, dict) and isinstance(o.get("id"), str)]


def items(store: Any, project: str) -> list[dict[str, Any]]:
    with store.read() as tx:
        rows = tx.query("SELECT id,ts,kind,payload FROM events WHERE project=? AND kind IN (?,?) "
                        "ORDER BY id", (project, OPEN, CLOSED))
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = _payload(row)
        oid = str(data.get("id") or "")
        if not oid:
            continue
        if row["kind"] == OPEN:
            out[oid] = {**data, "ts": str(row["ts"]), "event_id": int(row["id"]), "open": True}
        elif oid in out:
            out[oid].update(open=False, closed_ts=str(row["ts"]), closed_by=data.get("by"),
                            answer=data.get("answer") or data.get("reason") or "")
    return list(out.values())


def open_items(store: Any, project: str) -> list[dict[str, Any]]:
    return [i for i in items(store, project) if i.get("open")]


def operator_required(item: dict[str, Any]) -> bool:
    """Explicit actor takes precedence over heuristic for legacy obstacle events."""
    actor = str(item.get("actor") or "").casefold()
    if actor in ACTORS:
        return actor == "operator"
    return bool(_OPERATOR_PATTERN.search(" ".join(
        str(item.get(key) or "") for key in ("ask", "option", "not_covered"))))


def operator_action(item: dict[str, Any]) -> str:
    return str(item.get("option") or item.get("ask") or "").strip()


def operator_acceptance(item: dict[str, Any], project: str = "") -> str:
    command = (f'fleet.cmd unblock "{project}" {item["id"]} "<receipt/evidence>"'
               if project else f'fleet.cmd unblock <project> {item["id"]} "<receipt/evidence>"')
    return str(item.get("acceptance") or (
        f"После выполнения и проверки результата записать свидетельство: {command}; "
        f"условие снятия: {json.dumps(item.get('predicate') or {}, ensure_ascii=False)}"))


def operator_lead_blocks(tx: Any, project: str | None = None) -> list[dict[str, Any]]:
    """Legacy frozen TZ-LEAD blocks that never declared an obstacle_open event."""
    sql = ("SELECT t.project,t.tz,e.id AS event_id,e.payload FROM tasks t "
           "JOIN events e ON e.id=(SELECT MAX(b.id) FROM events b "
           "WHERE b.project=t.project AND b.tz=t.tz AND b.kind='block') "
           "WHERE t.state='frozen' AND UPPER(t.tz) LIKE 'TZ-LEAD%'")
    params: tuple[str, ...] = ()
    if project is not None:
        sql += " AND t.project=?"
        params = (project,)
    rows = tx.query(sql, params) if hasattr(tx, "query") else tx.execute(sql, params)
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        reason = str(data.get("reason") or "")
        if not operator_required({"actor": data.get("actor"), "ask": reason}):
            continue
        project_name = str(row["project"])
        block_id = f"lead-block-{row['event_id']}"
        out.append({
            "id": block_id, "project": project_name, "tz": str(row["tz"]),
            "event_id": int(row["event_id"]), "action": reason,
            "acceptance": (f"Оператор устранил причину, подтвердил независимый receipt/SHA "
                           f"и возобновил {project_name}/{row['tz']}; после смены frozen "
                           "запись снимается автоматически"),
        })
    return out


def sync_operator_backlog(store: Any) -> None:
    """Project operator-owned obstacles into the existing deduplicated manual journal."""
    from . import platform_manual
    with store.read() as tx:
        projects = [str(r["project"]) for r in tx.query(
            "SELECT DISTINCT project FROM events WHERE kind=? AND project IS NOT NULL", (OPEN,))]
        required = {str(json.loads(r["payload"]).get("id")) for r in tx.query(
            "SELECT payload FROM events WHERE kind='manual_platform_required'")}
        resolved = {str(json.loads(r["payload"]).get("id")) for r in tx.query(
            "SELECT payload FROM events WHERE kind='manual_platform_resolved'")}
        legacy_blocks = operator_lead_blocks(tx)
    for project in projects:
        for item in items(store, project):
            if not operator_required(item):
                continue
            # A reopened obstacle is a new operator action, not an already resolved front.
            fingerprint = f"lead-obstacle:{project}:{item['id']}:{item['event_id']}"
            ident = "manual-platform:" + fingerprint
            if ident not in required:
                with store.tx() as tx:
                    platform_manual.enqueue(
                        tx, fingerprint, title=f"Оператор: {project}/{item['id']}",
                        detail=operator_action(item), source_project=project,
                        source_tz="TZ-LEAD", acceptance=[operator_acceptance(item, project)])
                required.add(ident)
            if not item.get("open") and ident not in resolved:
                with store.tx() as tx:
                    tx.record_event(kind="manual_platform_resolved", project=project,
                                    tz="TZ-LEAD", payload={"id": ident, "reason": "obstacle_closed"})
                resolved.add(ident)
    active_block_ids: set[str] = set()
    for block in legacy_blocks:
        fingerprint = f"lead-block:{block['project']}:{block['event_id']}"
        ident = "manual-platform:" + fingerprint
        active_block_ids.add(ident)
        if ident not in required:
            with store.tx() as tx:
                platform_manual.enqueue(
                    tx, fingerprint,
                    title=f"Оператор: {block['project']}/{block['id']}",
                    detail=block["action"][:16000],
                    source_project=block["project"], source_tz=block["tz"],
                    acceptance=[block["acceptance"]])
            required.add(ident)
    # A newer block or a thaw replaces the operator action; never leave stale fronts.
    for ident in required:
        if (ident.startswith("manual-platform:lead-block:") and
                ident not in active_block_ids and ident not in resolved):
            with store.tx() as tx:
                tx.record_event(kind="manual_platform_resolved", payload={
                    "id": ident, "reason": "lead_block_superseded_or_thawed"})
            resolved.add(ident)
    platform_manual.export(store)


def declare(store: Any, agent: str, project: str, params: dict[str, Any]) -> str:
    kind = str(params.get("type") or "owner_action").strip()
    actor = str(params.get("actor") or "").strip().casefold()
    if actor and actor not in ACTORS:
        raise ValueError("obstacle: actor ∈ owner, operator, external")
    ask = " ".join(str(params.get("ask") or "").split())
    option = " ".join(str(params.get("option") or "").split())
    obligations = [str(x).strip() for x in params.get("obligations") or [] if str(x).strip()]
    predicate = params.get("predicate") or {"kind": "owner_answer"}
    if kind not in TYPES:
        raise ValueError("obstacle: type ∈ owner_action (решение/действие владельца), external_fact")
    if len(ask) < 20:
        raise ValueError("obstacle: ask — конкретный вопрос владельцу да/нет или выбор (от 20 знаков)")
    if kind == "owner_action" and len(option) < 10:
        raise ValueError("obstacle: option — готовый вариант, который владелец может просто принять")
    # 07.11.2026 (владелец): карточки на уже выданные разрешения (личный корпус, публичные
    # источники). Вопрос владельцу — только после сверки с его разрешениями, с причиной.
    not_covered = " ".join(str(params.get("not_covered") or "").split())
    if kind == "owner_action" and actor != "operator" and len(not_covered) < 30:
        from .lead import owner_grants
        raise ValueError(
            "obstacle: сначала сверь вопрос с разрешениями владельца — покрытое ими решай сам:\n"
            + "\n".join(f"  - {g}" for g in owner_grants(project))
            + "\nЕсли ни одно не покрывает — not_covered: почему (деньги, необратимое внешнее "
              "действие, новый аккаунт, юридическое решение — что именно).")
    if not obligations or not all(_OBLIGATION.match(o) for o in obligations):
        raise ValueError("obstacle: obligations — список O-* из книги, которые это держит")
    known = {o["id"] for o in ledger_obligations(_root(store, project)) or []}
    missing = [o for o in obligations if o not in known]
    if missing:
        raise ValueError(f"obstacle: нет в ToDo/lead/LEDGER.json: {', '.join(missing)}")
    if not isinstance(predicate, dict) or predicate.get("kind") not in PREDICATES:
        raise ValueError("obstacle: predicate.kind ∈ owner_answer, resource_up {resource}, file_exists {path}")
    if predicate["kind"] == "resource_up" and not str(predicate.get("resource") or "").strip():
        raise ValueError("obstacle: predicate resource_up требует resource")
    if predicate["kind"] == "file_exists":
        path = str(predicate.get("path") or "").strip()
        if not path or ".." in path or ":" in path:
            raise ValueError("obstacle: predicate file_exists требует path внутри проекта")
    oid = "ob-" + hashlib.sha256(f"{project}/{kind}/{ask}".encode("utf-8")).hexdigest()[:8]
    if any(i["id"] == oid for i in open_items(store, project)):
        return f"{project}: препятствие {oid} уже открыто"
    with store.tx() as tx:
        tx.record_event(kind=OPEN, worker_id=agent, project=project, tz="TZ-LEAD", payload={
            "id": oid, "type": kind, "actor": actor, "ask": ask[:600], "option": option[:600], "not_covered": not_covered[:600],
            "acceptance": str(params.get("acceptance") or "")[:1000],
            "obligations": obligations[:40], "predicate": {k: str(v)[:300] for k, v in predicate.items()},
            "by": agent})
    destination = ("оператор получит запись в журнале" if actor == "operator" else
                   "ответственный получит карточку/запись по классификации")
    return (f"{project}: препятствие {oid} открыто ({kind}, держит {len(obligations)} O-*); "
            f"{destination}. Бери другое O-*. Нечего продвинуть — устав запаркуется сам.")


def close(store: Any, project: str, oid: str, *, by: str, answer: str = "") -> bool:
    if not any(i["id"] == oid for i in open_items(store, project)):
        return False
    with store.tx() as tx:
        tx.record_event(kind=CLOSED, project=project, tz="TZ-LEAD", payload={
            "id": oid, "by": by, "answer": str(answer or "")[:2000]})
        # 07.11.2026: ведущий local-library заморозил устав `block` без пробы until, сославшись
        # на ob-55966a3a; владелец ответил в 09:21 — устав так и остался frozen. Ответ на
        # препятствие возвращает замороженный устав ведущего в очередь.
        for row in tx.query("SELECT tz FROM tasks WHERE project=? AND state='frozen' AND "
                            "UPPER(tz) LIKE 'TZ-LEAD%'", (project,)):
            tx.change("UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=? "
                      "AND state='frozen'", (datetime.now(timezone.utc).isoformat(), project, row["tz"]),
                      event={"kind": "thaw", "project": project, "tz": row["tz"],
                             "payload": {"why": f"obstacle {oid} closed by {by}", "obstacle": oid}})
    return True


def withdraw(store: Any, agent: str, project: str, params: dict[str, Any]) -> str:
    oid = str(params.get("id") or "").strip()
    reason = " ".join(str(params.get("reason") or "").split())
    if len(reason) < 15:
        raise ValueError("obstacle_close: reason — как обошёл препятствие (от 15 знаков)")
    if not close(store, project, oid, by=agent, answer=reason):
        raise ValueError(f"obstacle_close: {oid or '?'} не открыто в {project}")
    return f"{project}: препятствие {oid} снято ведущим"


def resolve(store: Any) -> list[str]:
    """Шаг контроллера: снять препятствия, чей предикат выполнился (не по слову агента)."""
    from . import world
    done: list[str] = []
    with store.read() as tx:
        projects = [str(r["project"]) for r in tx.query(
            "SELECT DISTINCT project FROM events WHERE kind=? AND project IS NOT NULL", (OPEN,))]
    for project in projects:
        for item in open_items(store, project):
            pred = item.get("predicate") or {}
            kind = pred.get("kind")
            answer = ""
            if kind == "resource_up":
                with store.read() as tx:
                    down = world.down_resources(tx, [str(pred.get("resource"))])
                if not down:
                    answer = f"ресурс {pred.get('resource')} на связи"
            elif kind == "file_exists":
                root = _root(store, project)
                if root is not None and (root / str(pred.get("path"))).exists():
                    answer = f"появился {pred.get('path')}"
            else:
                with store.read() as tx:
                    row = tx.one(
                        "SELECT payload FROM events WHERE kind='instruction' AND project=? AND id>"
                        "(SELECT MAX(id) FROM events WHERE kind=? AND project=? AND "
                        "json_extract(payload,'$.id')=?) AND json_extract(payload,'$.author')='owner' "
                        "AND json_extract(payload,'$.text') LIKE ? ORDER BY id LIMIT 1",
                        (project, OPEN, project, item["id"], f"%{item['id']}%"))
                if row is not None:
                    answer = "ответ владельца: " + str(_payload(row).get("text") or "")[:1500]
            if answer and close(store, project, item["id"], by="platform", answer=answer):
                done.append(f"{project}/{item['id']}")
    return done


def _finished(store: Any, project: str, obligations: list[dict[str, Any]]) -> set[str]:
    """Завершённые O-*: состояние из доказательств (product.state) поверх статуса книги.

    LEAD-SYSTEM-DESIGN 2.1 (Р2): `met` по принятому прибору — завершено, что бы ни писала книга;
    красный свидетель (`broken`) — не завершено, даже если книга пишет works_live. O-* без
    замеров остаётся по книге: иначе каждое неизмеренное «готово» снова будит устав, и ведущий
    без настоящей работы правит вокруг (05–06.11), а прибор строит рабочий, не он."""
    measured: dict[str, dict[str, Any]] = {}
    try:
        from . import product
        measured = {str(s["id"]): s for s in product.state(store, project)}
    except Exception:  # noqa: BLE001 — нет проекции/реестра: только книга
        pass
    done: set[str] = set()
    for o in obligations:
        s = measured.get(o["id"]) or {}
        if s.get("met") or (str(o.get("status") or "") in DONE and not s.get("broken")):
            done.add(o["id"])
    return done


def readiness(store: Any, project: str) -> tuple[bool, str]:
    """Можно ли сейчас продвинуть хоть одно O-* (или есть поручение без статуса)."""
    obligations = ledger_obligations(_root(store, project))
    if not obligations:
        return True, "книги нет или она пуста — ведущему есть что делать"
    try:
        from . import instructions
        if any(i["status"] in ("open", "noted") for i in instructions.items(store, project)):
            return True, "есть поручения владельца без статуса"
    except Exception:  # noqa: BLE001
        pass
    # A message to any lead charter round is independent new work even when every
    # existing obligation is blocked. Do not unpark on already acknowledged mail:
    # that would restore the empty lead loop retired in November.
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    with store.read() as tx:
        pending = tx.one(
            "SELECT id FROM messages WHERE acked_at IS NULL AND created_at>=? "
            "AND (to_task=? OR to_task LIKE ? OR to_task LIKE ?) LIMIT 1",
            (since, f"{project}/TZ-LEAD", f"{project}/TZ-LEAD:%",
             f"{project}/fix:TZ-LEAD%"))
    if pending is not None:
        return True, f"неподтверждённое письмо ведущему {pending['id']}"
    blocked: dict[str, list[str]] = {}
    for item in open_items(store, project):
        for o in item.get("obligations") or []:
            blocked.setdefault(o, []).append(item["id"])
    finished = _finished(store, project, obligations)
    advanceable = [o["id"] for o in obligations
                   if o["id"] not in finished and str(o.get("status") or "") != OWNER_STATUS
                   and o["id"] not in blocked]
    if advanceable:
        return True, f"продвигаемых O-*: {len(advanceable)}"
    waiting = [o["id"] for o in obligations if o["id"] not in finished]
    if not waiting:
        return False, "все O-* книги завершены — нужна разведка или новый контракт владельца"
    return False, (f"все {len(waiting)} незавершённых O-* ждут владельца или внешний факт: "
                   + ", ".join(sorted({x for o in waiting for x in blocked.get(o, ['owner_question'])}))[:300])


def cards(store: Any, project: str) -> list[dict[str, Any]]:
    """Карточки владельцу: по одной на открытое препятствие, больше разблокирует — выше."""
    ledger = ledger_obligations(_root(store, project)) or []
    finished = _finished(store, project, ledger)
    out = []
    for item in open_items(store, project):
        held = [o for o in item.get("obligations") or [] if o not in finished]
        out.append({**item, "unblocks": held})
    return sorted(out, key=lambda c: (-len(c["unblocks"]), c["ts"]))


def _clean(text: str) -> str:
    return " ".join(str(text or "").replace("|", "/").replace("-->", "→").split())


def write_cards(store: Any) -> list[str]:
    """Проекция карточек в корень очереди решений пульта (_cards/owner-decisions.json)."""
    base = Path(str(getattr(store, "path", "") or "")).parent / "owner-cards"
    with store.read() as tx:
        projects = [str(r["project"]) for r in tx.query(
            "SELECT DISTINCT project FROM events WHERE kind=? AND project IS NOT NULL", (OPEN,))]
    written = []
    for project in projects:
        rows = [c for c in cards(store, project) if not operator_required(c)]
        slug = re.sub(r"[^\w.-]+", "-", project).strip("-").lower() or "project"
        path = base / f"{slug}.md"
        lines = [f"# Карточки владельцу — {project}", "",
                 "Проекция реестра флота (obstacles.py). Не править: ответ — "
                 f"`fleet.cmd unblock \"{project}\" <id> \"<ответ>\"` или поручение с id.", ""]
        for c in rows:
            since = str(c["ts"])[:10]
            ask = _clean(c.get("ask"))
            option = _clean(c.get("option"))
            cost = f"держит {len(c['unblocks'])} O-*: {', '.join(c['unblocks'][:8])}"
            lines.append(f"## {c['id']} ({c.get('type')}) — {ask}")
            lines.append(f"<!-- owner-decision: fleet-{slug}/{c['id']} | since: {since} | ask: {ask}"
                         f"{' Вариант: ' + option if option else ''} | cost: {_clean(cost)} -->")
            lines.append(f"Вариант ведущего: {option or '—'}. Снимется само: {c.get('predicate')}.")
            lines.append("")
        if not rows:
            lines.append("Открытых препятствий нет.")
        text = "\n".join(lines) + "\n"
        try:
            if path.is_file() and path.read_text(encoding="utf-8") == text:
                continue
            base.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            written.append(str(path))
        except OSError:
            continue
    return written


def tick(store: Any) -> None:
    resolve(store)
    write_cards(store)
    sync_operator_backlog(store)


def lines(store: Any, project: str) -> list[str]:
    """Бриф ведущего: открытые препятствия и состояние готовности."""
    rows = cards(store, project)
    out: list[str] = []
    if rows:
        out.append(f"Препятствия (владелец/оператор/мир; операторские — в журнале): {len(rows)}. Не ищи, чем "
                   "заняться вокруг них — бери O-*, которое можно продвинуть.")
        for c in rows[:6]:
            responsible = "оператор" if operator_required(c) else c.get("type")
            out.append(f"  ⛔ {c['id']} {responsible}: {_clean(c.get('ask'))[:200]} → "
                       f"держит {', '.join(c['unblocks'][:6]) or '—'}")
    out.append("  Новое препятствие: fleet_lead(action='obstacle', params={obligations:[O-…], "
               "type: owner_action|external_fact, actor: owner|operator|external, ask, option, "
               "acceptance, predicate:{kind: owner_answer|"
               "resource_up+resource|file_exists+path}}); обошёл — action='obstacle_close' {id, reason}.")
    return out


__all__ = ["declare", "withdraw", "close", "resolve", "readiness", "cards", "write_cards",
           "tick", "lines", "items", "open_items", "OPEN", "CLOSED"]
