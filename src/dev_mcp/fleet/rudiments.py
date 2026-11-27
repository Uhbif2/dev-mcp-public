"""Задачи выведенных генераторов снимает платформа, а не владелец (решение владельца, 08.11).

Дважды одно и то же: 35 `revalidate:*` Beta App/Nova (04.11) и 67 (08.11) висели неделями,
и оба раза агент спросил владельца, хотя ответ уже был: «флот разбирает сам» (аудит 05.11,
`_todo/42`). Правило: генератор выведен → его незавершённые задачи — рудимент; снимаются здесь,
со снимком «до» (data/fleet/retired/*.json) и событием `task_retired` (не `reject`: это не отказ
работе, разбор уроков по отказам их не видит). Взятая задача и активный захват не трогаются.

Новый класс добавляется сюда в том же коммите, где выводится его генератор, — иначе рудимент
появится в третий раз.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dev_mcp.json_book import write_json

OPEN = ("ready", "frozen", "submitted")

# (имя, условие на tasks t, почему генератор выведен)
CLASSES: tuple[tuple[str, str, str], ...] = (
    ("revalidate",
     "(t.tz LIKE 'revalidate:%' OR t.tz LIKE '%:revalidate:%')",  # и fix:/review:fix:revalidate
     "LEAD-SYSTEM-DESIGN 1.3 (07.11): печать = снимок; ревалидация сдач без печати не нужна"),
    ("lead_fix_round",
     "(t.tz LIKE 'fix:TZ-LEAD%' OR t.tz LIKE 'review:fix:TZ-LEAD%')",
     "LEAD-SYSTEM-DESIGN 3.1 S6 (08.11): отказ вехи — попытка устава, раундов fix:TZ-LEAD нет"),
    ("stage_history",
     "(t.tz LIKE 'intent-%' AND EXISTS (SELECT 1 FROM events e WHERE e.kind='intent.stages_to_history'"
     " AND e.project=t.project))",
     "LEAD-SYSTEM-DESIGN 2.1 (08.11): дерево стадий проекта с ведущим — история"),
    # №48 B1 (09.11): ревью, открытое циклом обслуживания сдаче-прогрессу ведущего за секунду до
    # серверного accept (lead-progress). Источник уже принят и переоткрыт — решить такое ревью
    # нельзя («expected submitted task»), а его отказ замораживал ведущего. Генератор закрыт
    # в queue.reopen_orphan_reviews (прогресс идёт через open_review → progress_accept).
    ("lead_orphan_review",
     "(t.kind='review' AND t.tz LIKE 'review:TZ-LEAD:%' AND NOT EXISTS (SELECT 1 FROM tasks s "
     "WHERE s.project=t.project AND s.tz='TZ-LEAD' AND s.state='submitted') AND EXISTS (SELECT 1 "
     "FROM events o WHERE o.kind='review_open' AND o.project=t.project AND o.tz=t.tz AND EXISTS "
     "(SELECT 1 FROM events a WHERE a.project=t.project AND a.tz='TZ-LEAD' AND a.kind='accept' "
     "AND a.id>o.id AND json_extract(a.payload,'$.producer')='lead-progress')))",
     "№48 B1 (09.11): сдачу-прогресс ведущего принял сервер; ревью без источника — сирота гонки"),
    # 15.11: owner disabled all task producers except explicit lead/owner issue and
    # primary independent review. Include reviews/fixes OF generated work, but
    # never match an ordinary review of a lead/owner task.
    ("intent_generated",
     "(t.tz LIKE 'intent-%' OR t.tz LIKE 'review:intent-%' OR t.tz LIKE 'fix:intent-%')",
     "15.11: intent stages/candidates are no longer autonomous fleet work"),
    ("integrator_generated",
     "(t.tz LIKE 'styk:%' OR t.tz LIKE 'review:styk:%' OR t.tz LIKE 'fix:styk:%')",
     "15.11: integration reports stay operational, automatic styk tasks stopped"),
    ("watchdog_generated",
     "(t.tz LIKE 'watch:%' OR t.tz LIKE 'review:watch:%' OR t.tz LIKE 'fix:watch:%')",
     "15.11: watchdog cannot issue fleet work"),
    ("judgment_generated",
     "(t.tz LIKE 'triage:%' OR t.tz LIKE 'review:triage:%' OR t.tz LIKE 'fix:triage:%')",
     "15.11: triage of automatic judgments disabled"),
    ("queue_source_generated",
     "(t.tz LIKE 'fix:queue-source:%' OR t.tz LIKE 'review:fix:queue-source:%')",
     "15.11: queue intake only reports defects, does not issue fixes"),
    ("draft_generated",
     "(t.tz LIKE 'fix:draft:%' OR t.tz LIKE 'review:fix:draft:%')",
     "15.11: draft auto-repair disabled"),
    ("guard_generated",
     "(t.tz LIKE 'fix:guard-watch:%' OR t.tz LIKE 'review:fix:guard-watch:%')",
     "15.11: guard watcher no longer issues repair work"),
    ("claim_maintenance_generated",
     "(t.tz LIKE 'repair:claim-maintenance:%' OR t.tz LIKE 'review:repair:claim-maintenance:%')",
     "15.11: claim maintenance diagnoses do not generate tasks"),
    ("request_generated",
     "(t.tz LIKE 'request:%' OR t.tz LIKE 'review:request:%' OR t.tz LIKE 'fix:request:%') "
     "AND (t.author_worker IN ('judgment','integrator','guard_watch','planner','watchdog') "
     "OR t.body LIKE '[fleet-request]%' OR t.body LIKE '[fleet-queue-source]%')",
     "15.11: automatic requests and follow-ups no longer issued"),
    ("review_fix_rounds",
     "(t.tz LIKE 'fix:%' AND t.tz NOT LIKE 'fix:TZ-LEAD%' "
     "AND t.tz NOT LIKE 'fix:queue-source:%' AND t.tz NOT LIKE 'fix:draft:%' "
     "AND t.tz NOT LIKE 'fix:guard-watch:%')",
     "15.11: reject is recorded but cannot automatically produce further fix rounds"),
    ("lessons_generated",
     "(t.tz LIKE 'lessons:%' OR t.tz LIKE 'lesson:%')",
     "15.11: lesson miners cannot issue fleet work"),
    ("regate_generated",
     "(t.tz LIKE 'regate:%' OR t.tz LIKE 'drill:%')",
     "15.11: regate and drills cannot issue fleet work"),
)

# Leave already-submitted results and their independent review intact, even
# when the underlying producer is now disabled.  The owner requested a queue
# dry-run, not cancellation of work that has already reached acceptance.
NEW_GENERATOR_CLASSES = frozenset({
    "intent_generated", "integrator_generated", "watchdog_generated",
    "judgment_generated", "queue_source_generated", "draft_generated",
    "guard_generated", "claim_maintenance_generated", "request_generated",
    "review_fix_rounds", "lessons_generated", "regate_generated",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def candidates(tx: Any, names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, where, why in CLASSES:
        if names and name not in names:
            continue
        if name in NEW_GENERATOR_CLASSES:
            where = f"({where}) AND t.kind<>'review' AND t.state IN ('ready','frozen')"
        rows = tx.query(
            f"SELECT t.project,t.tz,t.state,t.updated_at FROM tasks t WHERE {where} "
            f"AND t.state IN ({','.join('?' * len(OPEN))}) AND NOT EXISTS (SELECT 1 FROM claims c "
            "WHERE c.project=t.project AND c.tz=t.tz AND c.state='active') ORDER BY t.project,t.tz", OPEN)
        out.extend({**dict(r), "class": name, "reason": why} for r in rows)
    return out


def retire_lead_orphan(store: Any, project: str, review_tz: str, reviewer: str) -> bool:
    """Снять ревью-сироту ведущего, которое держит ревьюер (№48 B1): у ревьюера нет инструмента
    отпустить захват, а ревью без источника решить нельзя. Условие — то же, что у класса."""
    where = next(w for name, w, _ in CLASSES if name == "lead_orphan_review")
    stamp = _now()
    with store.tx() as tx:
        row = tx.one(f"SELECT t.project,t.tz,t.state FROM tasks t WHERE t.project=? AND t.tz=? "
                     f"AND {where} AND t.state IN ('ready','taken','frozen','submitted')",
                     (project, review_tz))
        if row is None:
            return False
        tx.change("UPDATE claims SET state='released',released_at=? WHERE project=? AND tz=? "
                  "AND state='active'", (stamp, project, review_tz), event="lead_orphan_review_released")
        tx.change("UPDATE tasks SET state='rejected',updated_at=? WHERE project=? AND tz=?",
                  (stamp, project, review_tz),
                  event={"kind": "task_retired", "project": project, "tz": review_tz,
                         "worker_id": reviewer,
                         "payload": {"class": "lead_orphan_review", "from": str(row["state"]),
                                     "reason": "№48 B1: источник уже принят lead-progress",
                                     "detected_by": "sealed_review"}})
    return True


def retire(store: Any, *, names: tuple[str, ...] | None = None, dry: bool = False,
           preimage_dir: Path | None = None) -> dict[str, Any]:
    """Снять рудименты. Возвращает {class: count, preimage: путь}; dry — только счёт."""
    with store.read() as tx:
        found = candidates(tx, names)
    summary: dict[str, Any] = {}
    for item in found:
        summary[item["class"]] = summary.get(item["class"], 0) + 1
    if dry or not found:
        return summary
    base = preimage_dir or Path(str(getattr(store, "path", "") or ".")).parent / "retired"
    base.mkdir(parents=True, exist_ok=True)
    stamp = _now()
    pre = base / f"retired-{stamp[:19].replace(':', '')}.json"
    write_json(pre, found, indent=1)
    done = 0
    charters: set[str] = set()
    with store.tx() as tx:
        for item in found:
            cur = tx.change(
                "UPDATE tasks SET state='rejected',updated_at=? WHERE project=? AND tz=? AND state=? "
                "AND NOT EXISTS (SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active')",
                (stamp, item["project"], item["tz"], item["state"], item["project"], item["tz"]),
                event={"kind": "task_retired", "project": item["project"], "tz": item["tz"],
                       "payload": {"class": item["class"], "from": item["state"],
                                   "reason": item["reason"], "preimage": str(pre)}})
            done += int(cur.rowcount or 0)
            if item["class"] == "lead_fix_round":
                charters.add(item["project"])
        for project in sorted(charters):
            # Устав ждал снятый раунд в submitted — без этого он не вернулся бы никогда.
            tx.change("UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz='TZ-LEAD' "
                      "AND state='submitted' AND NOT EXISTS (SELECT 1 FROM tasks WHERE project=? AND "
                      "state IN ('ready','taken','submitted','frozen') AND (tz LIKE 'fix:TZ-LEAD%' OR "
                      "tz LIKE 'review:TZ-LEAD%' OR tz LIKE 'review:fix:TZ-LEAD%'))",
                      (stamp, project, project),
                      event={"kind": "lead_charter_returned", "project": project, "tz": "TZ-LEAD",
                             "payload": {"why": "снят раунд fix:TZ-LEAD (рудимент S6)"}})
    summary["retired"] = done
    summary["preimage"] = str(pre)
    return summary
