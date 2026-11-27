"""Ведущий проекта: одна долгая задача `TZ-LEAD*`, а не очередь ТЗ (владелец, 28.10.2026).

Такая задача не «доводится до сдачи»: будильник зовёт продолжать цикл, а сданная веха после
окончательной интеграции сама возвращается в очередь — ведущий продолжает с `STATE.md`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

# 06.11 (LEAD-SYSTEM-DESIGN фаза 1.2, Ш2): веха = заявленный переход O-*. Строка в note сдачи:
#   веха: O-android-update L1→L3      (или milestone: …; уровень «откуда» можно опустить)
#   забытое: письма/обещания/начатое за окно — куда записано (или «нет»)
# Сдача без такой строки — checkpoint, а НЕ accept/review/commit.
_MILESTONE = re.compile(
    r"(?im)(?:^\s*|(?<=[.;] ))(?:веха|milestone)\s*:\s*(O-[\w.\-]+)\s*(?:(L[0-4])\s*(?:→|->)\s*)?(L[0-4])\b")
_FORGOTTEN = re.compile(r"(?im)^\s*(?:забытое|forgotten)\s*:\s*(\S.{1,})$")
_O_ID = re.compile(r"^O-[\w.\-]+$")
PROGRESS_PRODUCER = "lead-progress"


def milestone_claim(note: str) -> dict[str, str] | None:
    match = _MILESTONE.search(str(note or ""))
    if match is None:
        return None
    forgotten = _FORGOTTEN.search(str(note or ""))
    return {"obligation": match.group(1), "from": match.group(2) or "", "to": match.group(3),
            "forgotten": forgotten.group(1).strip()[:1500] if forgotten else ""}


def _ledger_form(root: Any) -> str:
    """Форма книги ведущего (то, что раньше проверял ledger_check проекта): "" — в порядке."""
    from pathlib import Path
    path = Path(str(root)) / "ToDo" / "lead" / "LEDGER.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"не разбирается как JSON ({type(exc).__name__}: {str(exc)[:160]})"
    obligations = data.get("obligations") if isinstance(data, dict) else None
    if isinstance(obligations, dict):
        ids = list(obligations)
    elif isinstance(obligations, list):
        if not all(isinstance(o, dict) for o in obligations):
            return "obligations: каждый пункт — объект с id"
        ids = [o.get("id") for o in obligations]
    else:
        return "нет списка obligations"
    bad = [str(i) for i in ids if not (isinstance(i, str) and _O_ID.match(i))]
    if bad:
        return f"id обязательств не вида O-…: {', '.join(bad[:5])}"
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        return f"повторяются id: {', '.join(dupes[:5])}"
    return ""


def submit_refusal(tx: Any, claim: dict[str, Any], note: str) -> str:
    """Only milestones face product acceptance gates; progress is a checkpoint."""
    if not is_lead(str(claim.get("tz") or "")):
        return ""
    claim_ = milestone_claim(note)
    if claim_ is None:
        return ""  # checkpoint is not a product acceptance decision
    row = tx.one("SELECT root FROM projects WHERE name=?", (claim["project"],))
    if row is None or not row["root"]:
        return ""
    form = _ledger_form(row["root"])
    if form:
        return f"Сдача ведущего не принята: книга ToDo/lead/LEDGER.json — {form}. Исправь и сдай снова."
    from pathlib import Path
    from .instructions import ledger_ids
    if claim_["obligation"] not in ledger_ids(Path(str(row["root"]))):
        return f"Веха: {claim_['obligation']} нет в книге — веха это переход существующего O-*."
    from . import lead_stages
    if claim_["from"] and claim_["from"] >= claim_["to"]:
        return f"Веха: переход {claim_['from']}→{claim_['to']} не повышает уровень."
    counted = accepted_levels(tx, claim["project"]).get(claim_["obligation"])
    if counted and claim_["to"] <= counted[0]:
        return (f"Веха: {claim_['obligation']} уже засчитан на {counted[0]} ({counted[1][:16]}). "
                "Вехой будет только следующий уровень этого O-* или переход другого; эта работа — "
                "прогресс: сдай без строки «веха:».")
    if not claim_["forgotten"]:
        return ("Веха без строки «забытое: …»: перечисли письма, обещания, побочные находки и начатое "
                "за окно и куда каждое записано (или «забытое: нет»). Это часть вехи, а не вежливость.")
    verdict = lead_stages.validate_transition(
        claim_["from"] or "L0", claim_["to"],
        lead_stages.receipts(tx, claim["project"], claim_["obligation"]))
    if not verdict.ok:
        return (f"Веха {claim_['obligation']} → {claim_['to']}: {verdict.reason}. "
                "Недостающую поставку не заменяй accept или зелёным unit-тестом.")
    return ""


def accepted_levels(tx: Any, project: str) -> dict[str, tuple[str, str]]:
    """O-* → (уровень, время) по принятым вехам: заявка, за которой первым вердиктом шёл accept.

    06–07.11: засчитанный переход нигде не хранился — O-resource-benchmark «L1→L2» принят
    дважды (06.11 18:05 и 07.11 07:39), книга при этом осталась `unknown`."""
    lead_tz = "(UPPER(tz) LIKE 'TZ-LEAD%' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%')"
    claims = tx.query("SELECT id,payload FROM events WHERE kind='lead_milestone_claim' AND project=? "
                      "ORDER BY id", (project,))
    out: dict[str, tuple[str, str]] = {}
    verified: dict[str, str] = {}
    bounds = [int(r["id"]) for r in claims[1:]] + [2**62]
    for row, bound in zip(claims, bounds):
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        verdict = tx.one(f"SELECT kind,ts FROM events WHERE project=? AND id>? AND {lead_tz} AND "
                         "(kind='reject' OR (kind='accept' AND "
                         "COALESCE(json_extract(payload,'$.progress'),0)=0)) AND id<? ORDER BY id LIMIT 1",
                         (project, int(row["id"]), bound))
        obligation, level = str(data.get("obligation") or ""), str(data.get("to") or "")
        if verdict is None or verdict["kind"] != "accept" or not obligation or not level:
            continue
        from . import lead_stages
        if obligation not in verified:
            verified[obligation] = lead_stages.verified_stage(tx, project, obligation).name
        if verified[obligation] < level:
            continue
        if obligation not in out or level > out[obligation][0]:
            out[obligation] = (level, str(verdict["ts"]))
    return out


def progress_accept(store: Any, project: str, tz: str) -> str | None:
    """Handoff progress to a checkpoint and ready state in ONE transaction.

    No accept, review or commit. The submission already exists (queue.submit),
    and orphan-review maintenance routes it here as well.
    """
    if not is_lead(tz):
        return None
    import hashlib
    with store.tx() as tx:
        current = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
        if current is None or current["state"] != "submitted":
            return None
        sub = tx.one("SELECT id,payload FROM events WHERE kind='submit' AND project=? AND tz=? "
                     "ORDER BY id DESC LIMIT 1", (project, tz))
        try:
            note = str(json.loads(sub["payload"] or "{}").get("note") or "") if sub else ""
        except (ValueError, TypeError):
            note = ""
        claim = milestone_claim(note)
        if claim is not None:
            submit_id = int(sub["id"]) if sub else None
            prior = tx.one("SELECT 1 FROM events WHERE kind='lead_milestone_claim' "
                           "AND project=? AND tz=? AND "
                           "json_extract(payload,'$.submit_id')=? LIMIT 1",
                           (project, tz, submit_id))
            if prior is None:
                tx.record_event(kind="lead_milestone_claim", project=project, tz=tz,
                                payload={**claim, "submit_id": submit_id})
            return None
        normalised = " ".join(note.split())
        fingerprint = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
        previous = tx.one("SELECT payload FROM events WHERE kind='lead_checkpoint' AND project=? "
                          "AND tz=? ORDER BY id DESC LIMIT 1", (project, tz))
        try:
            same = (previous is not None and
                    json.loads(previous["payload"] or "{}").get("fingerprint") == fingerprint)
        except (TypeError, ValueError):
            same = False
        now = datetime.now(timezone.utc).isoformat()
        # 'ready' is a continuation of the standing charter, not a product
        # verdict. Atomic with the checkpoint/dedup event, so no orphan review.
        tx.change("UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=? "
                  "AND state='submitted'", (now, project, tz),
                  event={"kind": "lead_checkpoint_repeat" if same else "lead_checkpoint",
                         "worker_id": "server-lead-progress", "project": project, "tz": tz,
                         "payload": {"producer": PROGRESS_PRODUCER,
                                     "fingerprint": fingerprint,
                                     "submit_id": int(sub["id"]) if sub else None,
                                     "note": normalised[:1200]}})
    return "server-progress:" + tz


def is_progress_submission(tx: Any, project: str, tz: str) -> bool:
    """Последняя сдача ведущего — прогресс (без строки «веха:»): её принимает сервер, не ревью.

    09.11 (№48 B1): цикл обслуживания `reopen_orphan_reviews` видел TZ-LEAD в `submitted` в окне
    между submit и `progress_accept` и открывал LLM-ревью напрямую. Через секунду сервер принимал
    ту же сдачу как прогресс, ревью оставалось без источника (deep-research 211, Nova 165,
    TSPU 9, lnb 220), ревьюер получал `submitted source missing`, а заявка оператору замораживала
    самого ведущего. Событие submit пишется в одной транзакции со state='submitted'."""
    if not is_lead(tz):
        return False
    sub = tx.one("SELECT payload FROM events WHERE kind='submit' AND project=? AND tz=? "
                 "ORDER BY id DESC LIMIT 1", (project, tz))
    if sub is None:
        return False
    try:
        note = str(json.loads(sub["payload"] or "{}").get("note") or "")
    except ValueError:
        note = ""
    return milestone_claim(note) is None


def milestone_lines(store: Any, project: str) -> list[str]:
    """Ревьюеру вехи: заявленный переход, «забытое», диапазон с прошлой вехи, приборы."""
    with store.read() as tx:
        claim_row = tx.one("SELECT id,payload FROM events WHERE kind='lead_milestone_claim' AND project=? "
                           "ORDER BY id DESC LIMIT 1", (project,))
        if claim_row is None:
            return []
        previous = tx.one(
            "SELECT id,ts FROM events WHERE kind='accept' AND project=? AND id<? AND "
            "(UPPER(tz) LIKE 'TZ-LEAD%' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%') AND "
            "COALESCE(json_extract(payload,'$.progress'),0)=0 ORDER BY id DESC LIMIT 1",
            (project, int(claim_row["id"])))
        since = int(previous["id"]) if previous is not None else 0
        writes = tx.query("SELECT DISTINCT path_norm FROM events WHERE kind='write' AND project=? AND id>? "
                          "AND (UPPER(tz) LIKE 'TZ-LEAD%' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%') AND path_norm<>''",
                          (project, since))
        receipts = tx.query("SELECT payload FROM events WHERE kind='witness_result' AND project=? AND id>? "
                            "ORDER BY id", (project, since))
    claim = json.loads(claim_row["payload"] or "{}")
    shas: dict[str, set[str]] = {}
    for r in receipts:
        data = json.loads(r["payload"] or "{}")
        if data.get("obligation") == claim.get("obligation") and data.get("instrument_sha"):
            shas.setdefault(str(data.get("id")), set()).add(str(data["instrument_sha"])[:12])
    changed = {k: v for k, v in shas.items() if len(v) > 1}
    files = [str(w["path_norm"]) for w in writes]
    lines = [
        f"Веха — заявленный переход: {claim.get('obligation')} {claim.get('from') or '?'}→{claim.get('to')}.",
        "  Прими, только если переход виден на пути человека (свидетель/кадр/лог), а не в тестах. "
        "Нет перехода — reject с причиной. Смотри весь диапазон с прошлой вехи, не последнюю правку.",
        f"  Диапазон: {len(files)} файлов ведущего с прошлой вехи"
        + (f" ({str(previous['ts'])[:16]})" if previous is not None else "") + ": "
        + ", ".join(files[:12]) + (" …" if len(files) > 12 else ""),
        f"  Забытое (ведущий): {claim.get('forgotten') or '—'} — проверь, что каждое записано.",
    ]
    if changed:
        lines.append("  ⚠ Прибор этого O-* менялся в диапазоне (" + "; ".join(
            f"{k}: {', '.join(sorted(v))}" for k, v in changed.items()) + ") — переход на изменённом "
            "приборе не засчитывается без его приёмки с отрицательным контролем.")
    return lines


def is_lead(tz: str) -> bool:
    """`TZ-LEAD*` и его исправление `fix:TZ-LEAD*[:N]` — один ведущий; ревью — нет."""
    value = str(tz or "").upper()
    if value.startswith("FIX:"):
        value = value[4:]
    return value.startswith("TZ-LEAD")


# 07.11.2026 (владелец): ведущие заводили карточки на разрешения, которые владелец уже давал
# в чатах (deep-research — трижды; корпус local-library подключён ровно для поиска), а в
# канон проекта их никто не записал. Разрешения владельца живут у оператора
# (fleet-projects.toml → owner_grants), ведущий их не правит и видит в каждом брифе.
GENERAL_GRANTS = (
    "Ресурс, который владелец подключил проекту (корпус, MCP, аккаунт, устройство, сеть), — "
    "разрешение пользоваться им по назначению проекта, в том числе живые проверки на реальных "
    "данных только для чтения. Отдельно не спрашивай.",
    "Спрашивать владельца стоит только про: трату денег; необратимое внешнее действие от его "
    "имени (публикация, отправка людям, удаление чужих данных); новый аккаунт или учётные данные; "
    "юридическое решение. Остальное решай сам и записывай решение в STATE.md.",
    "Не изобретай аттестаций, «подтверждений оператора» и ограничений, которых нет в этих "
    "разрешениях и каноне владельца. Строгий запрет в AGENTS.md проекта, перекрытый разрешением "
    "ниже, — устаревший текст, а не препятствие.",
)


def owner_grants(project: str) -> list[str]:
    try:
        from .goals import project_info
        grants = project_info(project).get("owner_grants") or []
    except Exception:  # noqa: BLE001 — бриф не падает из-за конфига
        grants = []
    return [*GENERAL_GRANTS, *(str(g).strip() for g in grants if str(g).strip())]


def grants_lines(project: str) -> list[str]:
    return ["Разрешения владельца (действуют; карточка владельцу на покрытое ими — отказ):",
            *(f"  - {g}" for g in owner_grants(project))]


def focus_lines(project: str, store: Any = None) -> list[str]:
    from .goals import project_info

    focus = project_info(project).get("lead_focus")
    if not isinstance(focus, dict) or not focus.get("action") or not focus.get("evidence"):
        return []
    from . import lead_focus

    if lead_focus.continuous(project) and store is not None:
        with store.read() as tx:
            status, reason = lead_focus.state(tx, project)
            if status:
                return [f"Текущий результат проекта — {reason}"]
            epoch = lead_focus._epoch(tx, project, focus)
            basis = lead_focus._basis(tx, project, epoch) if epoch else None
            if basis and not lead_focus._owner_instruction(basis, focus):
                payload = json.loads(basis["payload"])
                if payload.get("text"):
                    return [f"Поручение владельца — {payload['text']}",
                            f"Проверка результата — {payload.get('done_when') or 'см. поручение'}"]
                return ["Выбери следующий результат по цели проекта, открытым поручениям и фактам."]
    return [f"Ближайший результат для пользователя — {focus['action']}",
            f"Проверка результата — {focus['evidence']}"]


def window_lines(store: Any, project: str) -> list[str]:
    """First-screen brief: one O-ID, verified stage, blocker/owner, one action."""
    from . import lead_stages
    oid = ""
    action = ""
    blocker = ""
    with store.read() as tx:
        row = tx.one("SELECT payload FROM events WHERE kind IN ('lead_checkpoint','checkpoint') "
                     "AND project=? AND (UPPER(tz) LIKE 'TZ-LEAD%' OR "
                     "UPPER(tz) LIKE 'FIX:TZ-LEAD%') ORDER BY id DESC LIMIT 1", (project,))
        if row:
            try:
                data = json.loads(row["payload"] or "{}")
                match = re.search(r"\bO-[\w.\-]+\b", str(data.get("note") or data.get("done") or ""))
                oid = match.group() if match else ""
                action = str(data.get("next") or "")
            except (ValueError, TypeError, AttributeError):
                pass
        if not oid:
            row = tx.one("SELECT payload FROM events WHERE kind='lead_milestone_claim' "
                         "AND project=? ORDER BY id DESC LIMIT 1", (project,))
            if row:
                try:
                    oid = str(json.loads(row["payload"] or "{}").get("obligation") or "")
                except (ValueError, TypeError):
                    pass
        row = tx.one("SELECT payload FROM events WHERE kind='lead_parked' AND project=? "
                     "ORDER BY id DESC LIMIT 1", (project,))
        if row:
            try:
                blocker = str(json.loads(row["payload"] or "{}").get("reason") or "")
            except (ValueError, TypeError):
                pass
        if not oid:
            root = tx.scalar("SELECT root FROM projects WHERE name=?", (project,))
            if root:
                from pathlib import Path
                from .instructions import ledger_ids
                ids = ledger_ids(Path(str(root)))
                oid = sorted(ids)[0] if ids else ""
        stage = lead_stages.verified_stage(tx, project, oid).name if oid else "NOT_MEASURED"
    if not action:
        try:
            from .goals import project_info
            action = str((project_info(project).get("lead_focus") or {}).get("action") or "")
        except Exception:  # noqa: BLE001
            pass
    owner = "оператор/владелец разрешения" if blocker else "ведущий"
    return [
        f"Фокус: {oid or 'O-ID не выбран'}; последняя проверенная стадия: {stage}.",
        f"Блокер: {blocker[:360] or 'нет свежей проверенной записи'}; владелец: {owner}.",
        f"Одно действие: {(action or 'проверить свидетельство следующей стадии поставки')[:380]}.",
        "Подробности: ToDo/lead/STATE.md, LEDGER.json, fleet_checkpoint, fleet_lead. "
        "Сдача прогресса = checkpoint, не accept; L3 signed/published, "
        "L4 installed/native/negative control.",
    ]



def progress_lines(store: Any, project: str, *, hours: int = 3) -> list[str]:
    """Show delivery and live evidence separately from accepted micro-fixes.

    05–06.11: hundreds of accepted validation tweaks, unchanged live witnesses and
    no milestone commits. The next chat inherited 'another synthetic defect'.
    This is feedback for judgment, not a quota or a new automatic task generator.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with store.read() as tx:
        rows = tx.query(
            "SELECT id,kind,tz,payload FROM events WHERE project=? AND ts>=? "
            "AND kind IN ('accept','reject','integrate','witness_result','lead_checkpoint') "
            "ORDER BY id", (project, since))
        previous = tx.query(
            "SELECT payload FROM events WHERE project=? AND kind='witness_result' AND ts<? ORDER BY id",
            (project, since))
    accepted, committed = set(), set()
    rejected = measured = new_live = progress = 0
    states: dict[str, str] = {}
    for row in [*previous, *rows]:
        try:
            data = json.loads(row['payload'] or '{}')
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        # Previous rows are witness baseline only; no progress is inferred from them.
        kind = row['kind'] if 'kind' in row.keys() else 'baseline'
        if kind in ('baseline', 'witness_result'):
            key, verdict = data.get('id'), data.get('verdict')
            if isinstance(key, str) and isinstance(verdict, str):
                if kind == 'witness_result':
                    measured += 1
                    new_live += verdict == 'works_live' and states.get(key) != 'works_live'
                states[key] = verdict
        elif is_lead(str(row['tz'] or '')):
            if kind == 'lead_checkpoint' or (kind == 'accept' and data.get('progress')):
                progress += 1  # historical progress-accept remains readable
            elif kind == 'accept':
                accepted.add(int(row['id']))
            elif kind == 'reject':
                rejected += 1
            elif data.get('commit'):
                committed.add(data.get('accept_id'))
    if not accepted and not rejected and not progress:
        return levels_lines(store, project) + _duties(store, project) + delivery_lines(store, project) + live_lines(store, project)
    lines = [f"Результат за {hours} ч: прогресс-сдач {progress}, принято вех {len(accepted)}, отклонено {rejected}; "
             f"из принятых есть коммит у {len(accepted & committed)}; замеров свидетелей {measured}, "
             f"новых переходов в works_live {new_live}."]
    if (accepted or progress) and states and not new_live:
        lines += [
            "  Живое подтверждение за это окно не продвинулось. Начни с причины и next_step свидетеля: "
            "устрани разрыв в продукте или выполни разрешённый замер на пути пользователя.",
            "  Если этот путь закрыт полномочиями/ресурсом, запиши конкретную зависимость и готовый "
            "вариант в OWNER.md с одной строкой-меткой рядом — без неё владелец вопроса не увидит: "
            "<!-- owner-decision: <проект>/<O-id> | since: ГГГГ-ММ-ДД | ask: <вопрос да/нет или "
            "выбор> | cost: <что стоит ожидание> -->. Затем возьми другое обязательство с доступным шагом. "
            "Следующий synthetic-дефект того же проверяющего скрипта не является целью по умолчанию.",
        ]
    if accepted and not (accepted & committed):
        lines.append("  Приёмка ещё не доставлена в основную ветку; это наблюдение об интеграторе, "
                     "не указание повторно сдавать те же байты или обходить его защиту.")
    return lines + levels_lines(store, project) + _duties(store, project) + delivery_lines(store, project) + live_lines(store, project)


def levels_lines(store: Any, project: str) -> list[str]:
    """Засчитанные уровни O-*: ведущий не заявляет повторно уже принятый переход (07.11)."""
    with store.read() as tx:
        levels = accepted_levels(tx, project)
    if not levels:
        return []
    items = sorted(levels.items(), key=lambda kv: kv[1][1], reverse=True)
    return ["Засчитанные вехи (уровень O-* по принятым переходам; повтор того же уровня — отказ): "
            + ", ".join(f"{o} {lv}" for o, (lv, _) in items[:20]) + (" …" if len(items) > 20 else "")]


def _duties(store: Any, project: str) -> list[str]:
    """Обязанности, которые проверяет платформа (lead_admin.duty_lines)."""
    try:
        from . import lead_admin
        return lead_admin.duty_lines(store, project)
    except Exception:  # noqa: BLE001 — бриф не падает из-за обязанностей
        return []


def delivery_lines(store: Any, project: str, *, hours: int = 6) -> list[str]:
    """Почему принятое не в истории — задача ведущего с фактами, а не оператора (06.11.2026).

    Nova VPN неделю копил 336 незакоммиченных файлов: причины (окончания строк в своём
    ledger_check, замороженные ревью своих задач, непринятые остатки) были проектными, но
    видел их только оператор. Здесь ведущий получает последнее препятствие каждого
    недоставленного принятия своего проекта и остаток последней сводки.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with store.read() as tx:
        rows = tx.query("SELECT tz,payload FROM events WHERE kind='integrate' AND project=? AND ts>=? "
                        "ORDER BY id", (project, since))
        sweep = tx.one("SELECT ts,payload FROM events WHERE kind='integrate_sweep' AND project=? "
                       "ORDER BY id DESC LIMIT 1", (project,))
    last: dict[Any, tuple[str, dict]] = {}
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        last[data.get("accept_id")] = (str(row["tz"] or ""), data)
    open_: dict[str, list[str]] = {}
    for tz, data in last.values():
        if data.get("commit") or data.get("skipped") or data.get("superseded"):
            continue
        reason = str(data.get("reason_code") or data.get("reason") or "?")[:90]
        who = ",".join(str(x) for x in (data.get("waiting_for") or [])[:4])
        open_.setdefault(reason + (f" (ждёт {who})" if who else ""), []).append(tz)
    lines: list[str] = []
    if open_:
        lines.append(f"Доставка: {sum(len(v) for v in open_.values())} принятых не в истории проекта:")
        for reason, tzs in sorted(open_.items(), key=lambda kv: -len(kv[1]))[:4]:
            lines.append(f"  {len(tzs)} × {reason}: {', '.join(tzs[:5])}")
    if sweep is not None:
        data = json.loads(sweep["payload"] or "{}")
        left = data.get("left_paths") or []
        if not data.get("ready"):
            lines.append(f"  Сводка принятого не прошла проверку ({str(sweep['ts'])[:16]}): "
                         f"{str(data.get('reason') or '')[:160]}")
        if left:
            lines.append(f"  Без принятых байтов {len(left)} файлов: {', '.join(left[:8])}")
    if lines:
        lines.append("  Это твоя работа, если причина в проекте: сдай или верни непринятые файлы, "
                     "почини проверку, закрой или пересдай застрявшие задачи проекта. Дефект "
                     "интегратора платформы — fleet_request к оператору с этой строкой.")
    return lines


def live_lines(store: Any, project: str) -> list[str]:
    """Живая поверхность проекта — его продукт, а не чужой блокер (06.11.2026).

    local-library: ведущий сутки писал «оператору восстановить library_status» о сервере
    своего же репозитория. Бриф говорит, какая ревизия работает вживую и что выкатка — штатный
    шаг после коммита (`live_deploy`), а сбой своего маршрута — веха ведущего.
    """
    try:
        from . import goals
        live = goals.project_info(project).get("live")
    except Exception:  # noqa: BLE001
        live = None
    if not isinstance(live, dict) or not live.get("instance"):
        return []
    with store.read() as tx:
        row = tx.one("SELECT ts,payload FROM events WHERE kind='live_deployed' AND project=? "
                     "ORDER BY id DESC LIMIT 1", (project,))
    head = (f"Живая поверхность: экземпляр {live['instance']} :{live.get('port')} "
            f"(серверы проекта, без memory_service/obsidian).")
    if row is None:
        state = "выкаток ещё не было — живой процесс может исполнять старый код."
    else:
        data = json.loads(row["payload"] or "{}")
        rev = str(data.get("rev") or "")[:10]
        state = (f"последняя выкатка {str(row['ts'])[:16]} UTC, ревизия {rev}: "
                 + ("работает." if data.get("ok") else f"ПРОВАЛ — {data.get('detail')}"))
    return [head + " " + state,
            "  Интегратор выкатывает HEAD сам (не чаще раза в 10 мин). Сбой или медленный ответ "
            "своего живого маршрута — твоя продуктовая веха: воспроизведи на этом порту, исправь, "
            "дождись live_deployed и проверь вызовом. В OWNER.md — только чужие полномочия."]


def resubmit_reason(tx: Any, claim: dict[str, Any]) -> str:
    """Веха ведущего без единой правки после последнего вердикта — не веха (устав: «повтор тех
    же CLI без предметного изменения — не веха»).

    05.11 Nova VPN: Android-веху install-job backoff сдали четыре раза (04.11 18:14, 05.11
    05:10, принята 05:58, снова 06:39). Последний чат лишь прогнал acceptance по контрольной
    точке предыдущего — общий страж пустой сдачи такое пропускает: зелёная приёмка = доказательство."""
    if not is_lead(str(claim.get("tz") or "")):
        return ""
    project = claim["project"]
    family = ("(UPPER(tz) LIKE 'TZ-LEAD%' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%')")
    verdict = tx.one(f"SELECT id,ts,kind,tz FROM events WHERE project=? AND kind IN ('accept','reject') "
                     f"AND {family} ORDER BY id DESC LIMIT 1", (project,))
    if verdict is None:
        return ""
    wrote = tx.one(f"SELECT 1 FROM events WHERE project=? AND kind='write' AND {family} AND id>? "
                   "AND path_norm<>'' AND LOWER(path_norm) NOT LIKE '%/todo/lead/%' LIMIT 1",
                   (project, int(verdict["id"])))
    if wrote:
        return ""
    deployed = tx.one(
        "SELECT id,json_extract(payload,'$.rev') AS rev FROM events "
        "WHERE project=? AND worker_id=? AND kind='live_deployed' AND id>? "
        "AND json_extract(payload,'$.ok')=1 AND COALESCE(json_extract(payload,'$.rev'),'')<>'' "
        "ORDER BY id LIMIT 1",
        (project, claim.get("worker_id"), int(verdict["id"])),
    )
    if deployed:
        previous = tx.one(
            "SELECT json_extract(payload,'$.ok') AS ok,json_extract(payload,'$.rev') AS rev "
            "FROM events WHERE project=? AND kind='live_deployed' AND id<? ORDER BY id DESC LIMIT 1",
            (project, deployed["id"]),
        )
        if not previous or not previous["ok"] or previous["rev"] != deployed["rev"]:
            return ""
    word = "принята" if verdict["kind"] == "accept" else "отклонена"
    return (f"После последнего вердикта ({verdict['tz']} {word} {str(verdict['ts'])[:16]}) в продукте "
            "не изменено ни одного файла и нет квитанции новой выкатки. Запиши новый замер "
            "в fleet_checkpoint, затем проверь ближайший результат в используемом клиенте "
            "или устрани первое доказанное препятствие. Записи в книге и журнале не требуют сдачи.")


def verdict_lines(store: Any, project: str, *, size: int = 3) -> list[str]:
    """Чем кончились последние вехи ведущего — от ревьюера, а не из его STATE.md.

    05.11 Nova VPN: Android-веху install-job backoff сдавали 04.11 18:14 и 05.11 05:10, оба
    раза ревью отклонило её за книгу; в 06:01 третий чат по «Далее» в STATE.md снова собрался её
    сдавать. Свежий чат вердиктов не видел."""
    with store.read() as tx:
        rows = [dict(r) for r in tx.query(
            "SELECT ts,kind,tz,payload FROM events WHERE project=? AND kind IN ('accept','reject') "
            "AND (UPPER(tz) LIKE 'TZ-LEAD%' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%') "
            "AND COALESCE(json_extract(payload,'$.progress'),0)=0 "
            "ORDER BY id DESC LIMIT ?", (project, size))]
    if not rows:
        return []
    lines = ["Вердикты ревью твоих последних вех (новые сверху; отклонённое повторно не сдавай — "
             "исправь названное):"]
    for row in rows:
        try:
            note = str(json.loads(row["payload"] or "{}").get("note") or "")
        except ValueError:
            note = ""
        note = " ".join(note.split())
        if note.startswith("claim_id="):
            note = note.split(".", 1)[-1].strip()
        mark = "✗ отклонена" if row["kind"] == "reject" else "✓ принята"
        lines.append(f"  {mark} {row['tz']} [{str(row['ts'])[:16]}]: {note[:260]}")
    return lines


def owns_drift_lines(store: Any, project: str, tz: str) -> list[str]:
    """Зона в файле ТЗ разошлась с реестром — сказать ведущему, какая действует.

    28.10: ремонт источника очереди вырезал из файла TZ-LEAD `client`/`app` и дописал запрет;
    в реестре зона осталась прежней. Ведущий слушался файла, сервер — реестра: пять дней тупика.
    """
    from .spec import parse_spec
    from .model import paths_overlap
    base = str(tz or "")
    while base.lower().startswith("fix:"):
        base = base[4:]
    base = base.split(":", 1)[0]
    with store.read() as tx:
        row = tx.one("SELECT t.owns,t.spec_path,p.root FROM tasks t JOIN projects p ON p.name=t.project "
                     "WHERE t.project=? AND t.tz=?", (project, tz))
        spec = tx.one("SELECT spec_path FROM tasks WHERE project=? AND tz=?", (project, base))
    if row is None:
        return []
    path = (spec["spec_path"] if spec and spec["spec_path"] else row["spec_path"]) or ""
    if not path:
        return []
    from pathlib import Path
    if not Path(path).is_absolute():
        path = str(Path(str(row["root"] or "")) / path)
    try:
        registered = [str(x) for x in json.loads(row["owns"] or "[]")]
        declared = list(parse_spec(path).owns)
    except Exception:
        return []
    missing = [r for r in registered
               if not any(paths_overlap(r, d) for d in declared)]
    if not missing:
        return []
    root = str(row["root"] or "").replace("\\", "/").lower().rstrip("/") + "/"
    short = [m[len(root):] if m.startswith(root) else m for m in missing]
    return [
        "Зона записи: в файле ТЗ нет " + ", ".join(short[:8]) + ", а в реестре есть.",
        "  Действует реестр — это твоя зона, правь её сам; не проси для неё отдельную задачу.",
        "  Расхождение — дефект источника, оператор о нём знает.",
    ]


# 08.11 («восприятие начинающего»): автор и его рассказ сдвигают ревьюера к его выводу (curse of
# knowledge; просьба «посмотри свежим глазом» почти не работает — работает порядок входа). Сначала
# свой вердикт по артефакту, потом рассказ ведущего. Строка «холодно:» в note считает арбитр.
COLD_REVIEW = [
    "Холодное ревью — два шага, в этом порядке:",
    "  1) НЕ читая «Итог»/заметку сдачи и сводку вехи ниже: открой дифф вехи, критерий заявленного O-*",
    "     в книге и вердикты приборов; запиши в note строку «холодно: <продвинут ли O-* и чем это видно>».",
    "  2) Потом прочитай отчёт ведущего и ищи, что он обошёл молчанием или назвал без доказательства;",
    "     строка «после отчёта: …». Расхождение шага 1 и 2 — главный материал ревью.",
]


def review_lines(store: Any, project: str, tz: str, *, size: int = 6) -> list[str]:
    """Ревью вехи ведущего: книгу обязательств проверить по существу, а не по rc сторожа.

    03.11: ревьюер вехи засчитал книгу по `ledger_check rc=0`, а в ней одно общее
    обязательство стояло на 44% разделов канона. Даём ему случайную выборку «раздел → привязка».
    """
    import random
    from pathlib import Path
    base = str(tz or "")
    if not base.lower().startswith("review:"):
        return []
    base = base[len("review:"):]
    if not is_lead(base):
        return []
    with store.read() as tx:
        row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
    if row is None:
        return []
    path = Path(str(row["root"])) / "ToDo" / "lead" / "LEDGER.json"
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pairs = [(f, h, v) for f, e in (ledger.get("sources") or {}).items() if isinstance(e, dict)
             for h, v in (e.get("sections") or {}).items()]
    if not pairs:
        return []
    from collections import Counter
    used = Counter(o for _, _, v in pairs if isinstance(v, list) for o in v)
    # 04.11 (аудит R4): зерно — от содержания книги, а не от числа разделов: переписанная книга
    # того же размера давала ревьюеру ту же выборку.
    import hashlib
    digest = hashlib.sha256(json.dumps(ledger, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    rng = random.Random(f"{project}/{tz}/{digest}")
    picked = rng.sample(pairs, min(size, len(pairs)))
    lines = [
        *COLD_REVIEW,
        "Ревью вехи ведущего — книга обязательств (ToDo/lead/LEDGER.json):",
        "  Сначала проверь связь вехи с обещанием O-* и путём использования. Зелёный новый тест "
        "не доказывает выбранный приоритет. Приборы (ops/witness, ledger_check) ведущий не правит — "
        "страж отказывает; правка прибора в вехе ведущего — reject.",
        "  `ledger_check` проверяет только форму. Открой каждый раздел ниже в его файле и реши: следует ли",
        "  из него названное обязательство и не потеряно ли конкретное обещание человеку. Шаблонная",
        "  или общая привязка в выборке — повод для reject с примерами.",
        "  Вид окна проверяй замером: `py -3 ops/eyes/capture_windows.py --tag review-<твой id> --states off on",
        "  offline` и `py -3 ops/eyes/check_window_contract.py`, кадр смотри сам.",
        "  Визуальную пару «наш кадр — эталон» требуй только у вехи, меняющей вид окна, и только против",
        "  раскладки, выбранной владельцем. Нет выбора — это не повод отклонять",
        "  веху Android, логики или книги (04.11: так пять часов готовых правок не могли дойти до ревью).",
        "  Дисциплина доказательств (CLAUDE.md проекта, 04.11) — reject с примером, если: works_live или «сам»",
        "  без свидетеля или с ручным звеном в цепочке; итог называет меньше FAIL, чем вывод ворот; ворота",
        "  ослаблены (xfail, выключенная блокировка) без решения владельца со сроком; новая проверка без",
        "  отрицательного контроля; ручная починка одной машины выдана за исправление класса.",
    ]
    for rel, heading, verdict in picked:
        target = ", ".join(verdict) if isinstance(verdict, list) else str(verdict)
        lines.append(f"  - {rel} → «{heading[:70]}» → {target[:120]}")
    if used:
        top, count = used.most_common(1)[0]
        share = count * 100 // len(pairs)
        if share >= 25:
            lines.append(f"  Внимание: {top} привязано к {count} из {len(pairs)} разделов ({share}%) — "
                         "проверь, не универсальная ли это заглушка.")
    try:
        lines += milestone_lines(store, project)
    except Exception:  # noqa: BLE001 — ревью не падает из-за сводки вехи
        pass
    try:
        from . import instructions
        lines += instructions.review_lines(store, project)
    except Exception:  # noqa: BLE001 — ревью не падает из-за реестра поручений
        pass
    return lines


def _final(payload: dict) -> bool:
    """Для ведущего веха окончательна и тогда, когда проект не коммитит сам (03.11: Nova VPN
    без `commit = true` — ведущий после первой принятой вехи не вернулся никогда)."""
    from .integrate import Integrator
    if str(payload.get("skipped") or "").startswith("коммит не разрешён") and not payload.get("deferred"):
        return True
    return Integrator._final_integration(payload)


def _held_by_others(payload: dict) -> bool:
    """Коммит вехи ждёт чужую незаконченную задачу — ведущему ждать нечего.

    04.11 19:38 – 05.11 05:00 Nova VPN: принятый fix:TZ-LEAD:5 откладывался 11 раз «unfinished
    foreign writes» (TZ-170 frozen, TZ-262 submitted с 19.10, request:dd5faeaf ждёт телефон), и
    устав не возвращался — ведущий простоял ночь. Интеграция повторяется сама; его следующая
    веха, переписавшая те же файлы, заменяет эту (`_superseded_by_later_accept`)."""
    return bool(payload.get("deferred") and payload.get("waiting_for")
                and str(payload.get("reason") or "").startswith("unfinished foreign writes"))


def _base(tz: str) -> str:
    """`fix:TZ-LEAD:3` → `TZ-LEAD`: задача-устав ведущего, от которой пошли исправления."""
    value = str(tz or "")
    while value.lower().startswith("fix:"):
        value = value[4:]
    return value.split(":", 1)[0]


def _ready_or_park(store: Any, project: str, target: str) -> bool:
    """Устав возвращается по готовности, а не сразу после приёмки (LEAD-SYSTEM-DESIGN 1.1, Ш3).

    05–06.11: все O-* ждали владельца, а устав возвращался каждые 2–3 минуты — и ведущий
    находил «чем заняться»: 150 правок своего свидетеля. Нечего продвинуть — парковка; владелец
    уже получил карточку на каждое препятствие (obstacles.write_cards)."""
    try:
        from . import obstacles
        ok, why = obstacles.readiness(store, project)
    except Exception:  # noqa: BLE001 — сбой проверки готовности ведущего не держит
        return True
    with store.read() as tx:
        from .lead_focus import hold_reason
        if hold_reason(tx, project, target):
            return False
        last = tx.one("SELECT kind,payload FROM events WHERE project=? AND kind IN "
                      "('lead_parked','lead_unparked') ORDER BY id DESC LIMIT 1", (project,))
    parked = last is not None and last["kind"] == "lead_parked"
    if ok:
        if parked:
            with store.tx() as tx:
                tx.record_event(kind="lead_unparked", project=project, tz=target, payload={"reason": why})
        return True
    previous = json.loads(last["payload"] or "{}").get("reason") if parked else None
    if previous != why:
        with store.tx() as tx:
            tx.record_event(kind="lead_parked", project=project, tz=target, payload={"reason": why})
    return False


def reopen(store: Any, *, project: str | None = None) -> list[str]:
    """Принятую задачу ведущего вернуть в `ready` (с 05.11 — не дожидаясь интеграции).

    Принятый fix ведущего возвращает не себя, а устав `TZ-LEAD` — и ровно один раз на приёмку.
    03.11 15:00 – 04.11 07:00 Nova VPN: fix:TZ-LEAD:3 возвращался сам 266 раз подряд — тело
    fix звало чинить уже исправленное, агент писал «уже интегрировано», ревью принимало, и
    снова. Исправление закончено приёмкой; продолжение — следующий пункт книги по уставу.
    """
    reopened: list[str] = []
    with store.read() as tx:
        rows = [dict(r) for r in tx.query(
            "SELECT project,tz FROM tasks WHERE state='accepted' AND (UPPER(tz) LIKE 'TZ-LEAD%' "
            "OR UPPER(tz) LIKE 'FIX:TZ-LEAD%')" + (" AND project=?" if project else ""),
            (project,) if project else ())]
    for row in rows:
        project, tz = row["project"], row["tz"]
        with store.read() as tx:
            accept = tx.one("SELECT id FROM events WHERE kind='accept' AND project=? AND tz=? "
                            "ORDER BY id DESC LIMIT 1", (project, tz))
            if accept is None:
                continue
            # 05.11: устав возвращается сразу после приёмки, не после коммита и гейта. Ожидание
            # интеграции стоило ведущему Nova 9 ч простоя (04–05.11), а у Beta App гейт идёт
            # 13–87 мин на каждую веху. Ждать было незачем: печать сдачи с областью отделяет байты
            # принятой вехи от его новых правок, а запись в её файлы — «незаконченная работа»,
            # которую заменит следующая веха (_superseded_by_later_accept).
            done = True
            target = tz
            if done and str(tz).lower().startswith("fix:"):
                base = _base(tz)
                settled = tx.one(
                    "SELECT 1 FROM events WHERE kind='lead_fix_settled' AND project=? AND tz=? "
                    "AND json_extract(payload,'$.accept_id')=? LIMIT 1", (project, tz, accept["id"]))
                charter = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, base))
                if settled:
                    continue
                if charter is not None:
                    active = tx.one(
                        "SELECT tz FROM tasks WHERE project=? AND state IN ('ready','taken','submitted') "
                        "AND (UPPER(tz) LIKE 'TZ-LEAD%' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%') LIMIT 1",
                        (project,))
                    target = "" if active or charter["state"] not in ("accepted", "rejected") else base
        if not done:
            continue
        if target and not _ready_or_park(store, project, target):
            continue
        now = datetime.now(timezone.utc).isoformat()
        with store.tx() as tx:
            current = tx.one(
                "SELECT e.id FROM events e JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
                "WHERE e.kind='accept' AND e.project=? AND e.tz=? AND t.state='accepted' "
                "ORDER BY e.id DESC LIMIT 1", (project, tz))
            if current is None or current["id"] != accept["id"]:
                continue
            if target != tz:
                settled = tx.one(
                    "SELECT 1 FROM events WHERE kind='lead_fix_settled' AND project=? AND tz=? "
                    "AND json_extract(payload,'$.accept_id')=? LIMIT 1", (project, tz, accept["id"]))
                if settled is not None:
                    continue
                charter = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, target))
                if charter is None or charter["state"] not in ("accepted", "rejected"):
                    target = ""
                tx.record_event(kind="lead_fix_settled", project=project, tz=tz,
                                payload={"accept_id": accept["id"], "charter": target or None})
            if target:
                tx.change("UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=? "
                          "AND state IN ('accepted','rejected')", (now, project, target),
                          event={"kind": "reactivate", "project": project, "tz": target,
                                 "payload": {"reason": "lead_milestone_integrated",
                                             "by": "lead.reopen", "from": tz}})
        if target:
            reopened.append(f"{project}/{target}")
    return reopened
