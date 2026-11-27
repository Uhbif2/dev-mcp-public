"""Fast, reversible lead judgments. Owner-only and uncertain questions remain owner obstacles.

The public entry point is fleet_lead(action="ask", params={question, options, obligations}).
Only an active TZ-LEAD holder may call it (lead_admin.act enforces that boundary).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import judgment, obstacles

_OWNER_ONLY = re.compile(
    r"owner[-_ ]decision|только владельц|решени[ея] владельц|"
    r"архитектур|architectur|необратим|irreversib|"
    r"юридич|legal approval|лицензи|публиков|deploy to production|"
    r"удалить данные|delete data|сменить конфиг|change config",
    re.I,
)
_MIN_CONFIDENCE = 0.68
_MIN_MARGIN = 0.15


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(project: str, question: str, options: list[str]) -> str:
    raw = json.dumps([project, question, options], ensure_ascii=False, sort_keys=True)
    return "ld-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _context(store: Any, project: str, question: str, options: list[str]) -> dict[str, Any]:
    with store.read() as tx:
        row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
    root = Path(str(row["root"])) if row is not None else None
    goal = ""
    if root is not None:
        for relative in ("intent/goal.md", "GOAL.md", "ToDo/lead/STATE.md"):
            try:
                candidate = root / relative
                if candidate.is_file():
                    goal = candidate.read_text(encoding="utf-8")[:1800]
                    break
            except (OSError, UnicodeError):
                continue
    return {"project": project, "goal": goal, "question": question, "options": options}


def _model_decide(context: dict[str, Any], questions: dict[str, Any]
                  ) -> tuple[dict[str, Any] | None, str]:
    """Use overseer's already-configured free shared relay; bounded timeout, then Jev."""
    from . import overseer

    blocks = [{"type": "text", "text": json.dumps(context, ensure_ascii=False)}]
    try:
        # 15.11 живьём: холодный вызов gemini-decision-1 — 19,6 с, тёплый — 1,5 с. 25 с < exec_sync_budget 35.
        answers = overseer._relay_decide(blocks, questions, timeout_s=25)
        if (isinstance(answers, dict) and
                isinstance(answers.get("owner_only"), dict) and
                isinstance(answers.get("choice"), dict)):
            return answers, overseer.MODEL
    except Exception:  # noqa: BLE001 - decision service cannot prevent owner escalation
        pass
    try:
        answers = overseer._jev_decide(blocks, questions)
        if (isinstance(answers, dict) and
                isinstance(answers.get("owner_only"), dict) and
                isinstance(answers.get("choice"), dict)):
            return answers, "jev"
    except Exception:  # noqa: BLE001
        pass
    return None, ""


def _pick(answers: dict[str, Any] | None, choices: list[str]) -> int | None:
    if not isinstance(answers, dict):
        return None
    try:
        gate = float(answers["owner_only"]["noul"])
        probabilities = answers["choice"]["probabilities"]
        probs = [float(probabilities[f"v{i}"]) for i in range(len(choices))]
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None
    if not (0 <= gate <= 0.15) or any(not (0 <= p <= 1) for p in probs):
        return None
    rank = sorted(range(len(probs)), key=lambda i: probs[i], reverse=True)
    if probs[rank[0]] < _MIN_CONFIDENCE or probs[rank[0]] - probs[rank[1]] < _MIN_MARGIN:
        return None
    return rank[0]


def _questions(options: list[str]) -> dict[str, Any]:
    return {
        "owner_only": {
            "type": "noul",
            "instructions": (
                "Требует ли вопрос исключительно решения владельца: необратимый эффект, "
                "траты денег, новые аккаунты, публикация, архитектурное изменение или "
                "явная owner-decision? Сомневаешься — ответь true."
            ),
            "criteria": {
                "true_description": "решает только владелец",
                "false_description": "все варианты обратимы и в полномочиях ведущего",
            },
        },
        "choice": {
            "type": "choice",
            "instructions": (
                "Какой вариант лучше продвигает цель проекта при минимальном риске? "
                "Оцени только перечисленные варианты; ничего не исполняй."
            ),
            "criteria": {f"v{i}": option for i, option in enumerate(options)},
        },
    }


def _owner_pending(store: Any, agent: str, project: str, ident: str, question: str,
                   options: list[str], obligations: list[str], why: str) -> str:
    # The regular owner-obstacle projection produces actual owner-decision cards;
    # an event alone would be invisible to the owner.
    option = ("Предложение ведущего: " + options[0]) if options else "Выбрать безопасный вариант после проверки владельцем"
    note = ("Модель не имеет полномочий либо недоступна; вопрос передан владельцу. "
            "Существующие разрешения не дают модели права принять это решение. " + why)[:600]
    card = obstacles.declare(store, agent, project, {
        "type": "owner_action", "ask": question, "option": option,
        "not_covered": note, "obligations": obligations,
        "predicate": {"kind": "owner_answer"},
    })
    with store.tx() as tx:
        tx.record_event(kind="lead_decision_pending", worker_id=agent, project=project,
                        tz="TZ-LEAD", payload={"id": ident, "question": question,
                        "options": options, "obligations": obligations, "why": why})
    return f"{project}/{ident}: ждёт владельца ({why}). {card}"


def _existing(store: Any, ident: str, project: str) -> tuple[str, str] | None:
    with store.read() as tx:
        row = tx.one("SELECT text,source FROM decisions WHERE id=? AND scope=?",
                     (ident, f"lead:{project}"))
        if row is not None:
            withdrawn = tx.one(
                "SELECT 1 FROM events WHERE kind='lead_decision_undo' AND project=? "
                "AND json_extract(payload,'$.id')=? LIMIT 1", (project, ident))
            return ("undone" if withdrawn else "answered", str(row["text"]))
        row = tx.one("SELECT 1 FROM events WHERE kind='lead_decision_pending' AND project=? "
                     "AND json_extract(payload,'$.id')=? LIMIT 1", (project, ident))
        if row:
            return ("pending", "")
    return None


def ask(store: Any, agent: str, project: str, params: dict[str, Any]) -> str:
    question = " ".join(str(params.get("question") or params.get("ask") or "").split())
    raw_options = params.get("options") or []
    if not isinstance(raw_options, list):
        raise ValueError("ask: options — список вариантов")
    options = [" ".join(str(x).split()) for x in raw_options]
    if not options and re.search(r"\b(?:можно ли|стоит ли|допустимо ли|can we|should we)\b", question, re.I):
        options = ["Да, в пределах существующих полномочий", "Нет, оставить без изменения"]
    obligations = list(params.get("obligations") or [])
    if len(question) < 20 or len(question) > 600:
        raise ValueError("ask: вопрос от 20 до 600 символов")
    if len(options) > 6 or any(not 2 <= len(x) <= 300 for x in options):
        raise ValueError("ask: варианты до 6, каждый 2–300 символов")
    if len(set(options)) != len(options):
        raise ValueError("ask: повтор вариантов")
    if not obligations or not all(isinstance(x, str) and re.fullmatch(r"O-[\w.-]+", x)
                                  for x in obligations):
        raise ValueError("ask: obligations — список O-* из ToDo/lead/LEDGER.json")
    root = obstacles._root(store, project)
    ledger = {o["id"]: o for o in obstacles.ledger_obligations(root) or []}
    if any(o not in ledger for o in obligations):
        raise ValueError("ask: obligations отсутствуют в книге проекта")
    marked_owner = any(str(ledger[o].get("status") or "") == obstacles.OWNER_STATUS
                       for o in obligations)
    from . import outcome
    outcome.install_schema(store)
    ident = _id(project, question, options)
    previous = _existing(store, ident, project)
    if previous:
        state, answer = previous
        return (f"{project}/{ident}: ответ отменён владельцем, повторно модель не вызывается"
                if state == "undone" else
                f"{project}/{ident}: {answer}" if state == "answered" else
                f"{project}/{ident}: вопрос уже ждёт владельца")
    text = " ".join([question, *options])
    if (marked_owner or params.get("owner_decision") is True or
            params.get("reversible") is False or _OWNER_ONLY.search(text) or
            judgment.irreversible(text)):
        return _owner_pending(store, agent, project, ident, question, options,
                              obligations, "только владелец")
    if len(options) < 2:
        return _owner_pending(store, agent, project, ident, question, options,
                              obligations, "нет вариантов для decision-модели")
    answers, route = _model_decide(_context(store, project, question, options),
                                    _questions(options))
    chosen = _pick(answers, options)
    if chosen is None:
        return _owner_pending(store, agent, project, ident, question, options,
                              obligations, "модель недоступна или недостаточно уверена")
    result = options[chosen]
    answer = f"{question} → {result}"
    stamp = _stamp()
    message = (f"Быстрое решение {ident} ({route}) по {project}: {answer}. "
               f"Обратимо: fleet.cmd undo \"{project}/lead-decision:{ident}\". "
               "Владелец вправе отменить рекомендацию; выполненные действия откатываются отдельно. "
               "Это выбор, не разрешение на необратимый эффект.")
    mid = "m-" + hashlib.sha256(f"{ident}:{agent}".encode()).hexdigest()[:12]
    with store.tx() as tx:
        # One transaction: never record a decision without its next-call envelope.
        tx.changes([
            ("INSERT INTO decisions(id,scope,text,source,reversible,rollback,ts) "
             "VALUES(?,?,?,?,?,?,?)",
             (ident, f"lead:{project}", answer, f"model:{route}", 1,
              f'fleet.cmd undo "{project}/lead-decision:{ident}"', stamp)),
            ("INSERT INTO messages(id,dedup_key,to_worker,to_task,broadcast,from_kind,"
             "from_id,kind,body,created_at,delivered_at,acked_at) "
             "VALUES(?,?,?,NULL,0,'system','lead-decision','lead_decision',?,?,NULL,NULL)",
             (mid, f"lead-decision:{ident}:{agent}", agent, message, stamp)),
        ], event={"kind": "lead_decision_answered", "worker_id": agent, "project": project,
                  "tz": "TZ-LEAD", "payload": {"id": ident, "choice": chosen,
                  "source": f"model:{route}", "obligations": obligations}})
    return f"{project}/{ident}: {answer} (источник model:{route}; сообщение {mid})"


def undo(store: Any, project: str, ident: str) -> str:
    """Owner CLI undo, durable and idempotence-fenced; revoke the lead's envelope."""
    from . import outcome
    outcome.install_schema(store)
    with store.tx() as tx:
        decision = tx.one("SELECT text,source FROM decisions WHERE id=? AND scope=?",
                          (ident, f"lead:{project}"))
        if decision is None or not str(decision["source"]).startswith("model:"):
            raise ValueError(f"{project}/{ident}: нет модельного решения")
        if tx.one("SELECT 1 FROM events WHERE kind='lead_decision_undo' AND project=? "
                  "AND json_extract(payload,'$.id')=?", (project, ident)):
            raise ValueError(f"{project}/{ident}: уже отменено")
        event = tx.one("SELECT worker_id FROM events "
                       "WHERE kind='lead_decision_answered' AND project=? "
                       "AND json_extract(payload,'$.id')=? ORDER BY id DESC LIMIT 1",
                       (project, ident))
        worker = str(event["worker_id"] or "") if event else ""
        stamp = _stamp()
        statements: list[tuple[str, tuple[Any, ...]]] = []
        if worker:
            message = f"Владелец отменил решение {ident} ({decision['text']}). Не применять его."
            mid = "m-" + hashlib.sha256(f"undo:{ident}:{worker}".encode()).hexdigest()[:12]
            statements.append((
                "INSERT INTO messages(id,dedup_key,to_worker,to_task,broadcast,from_kind,"
                "from_id,kind,body,created_at,delivered_at,acked_at) "
                "VALUES(?,?,?,NULL,0,'owner','fleet.cmd','lead_decision',?,?,NULL,NULL)",
                (mid, f"lead-decision-undo:{ident}:{worker}", worker, message, stamp)))
        if statements:
            tx.changes(statements, event={"kind": "lead_decision_undo", "project": project,
                       "tz": "TZ-LEAD", "payload": {"id": ident, "by": "owner",
                       "worker": worker}})
        else:
            tx.record_event(kind="lead_decision_undo", project=project, tz="TZ-LEAD",
                            payload={"id": ident, "by": "owner"})
    suffix = "ведущему отправлено уведомление" if worker else "ведущий не найден"
    return f"{project}/{ident}: модельный выбор отменён владельцем; {suffix}"
