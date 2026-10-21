"""Сторож прогресса: система сама замечает, что работа идёт по кругу или стоит.

13.10.2026 владелец дважды сам находил то, что должна была найти система: 18 агентов Main Hub
часами крутили одни и те же финальные ревью; TZ-311/312 четыре часа «делались», хотя одна
была ложно отклонена (снимок ревьюера), а другая принята, но не попала в ствол. Счётчики
«можно выдать N» при этом выглядели нормально — смотреть надо на прогресс, а не на очередь.

Каждый детектор — факт из реестра за окно, без догадок. Находка → тикет сторожа в Fleet
Platform (watchdog._ticket, дедуп по отпечатку 6 ч): разобрать причину и починить платформу.
"""
from __future__ import annotations

import collections
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

LOOP_TASK_TAKES = 4          # взята столько раз за окно без приёмки — цикл
LOOP_TASK_WINDOW_H = 3
LOOP_PROJECT_TAKES = 20      # проект: выдач за час не меньше…
LOOP_PROJECT_YIELD = 0.15    # …а приёмок на выдачу меньше этой доли
REFUSAL_STORM = 300          # отказов выдачи за час с одной причиной
INTENT_STALL_H = 3           # стадия интенции без движения
UNINTEGRATED_MIN = 15        # принятая изолированная работа без интеграции


def _since(now: datetime, **delta: float) -> str:
    return (now - timedelta(**delta)).isoformat()


def _reason(payload: Any) -> str:
    try:
        data = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return ""
    return str(data.get("reason") or data.get("why") or data.get("note") or "")[:160]


def _progress_at(raw: Any, now: datetime) -> datetime | None:
    """Only comparable past UTC instants can advance the front clock."""
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    value = value.astimezone(now.tzinfo)
    return value if value <= now else None


def front_snapshot(tx: Any, now: datetime) -> list[dict[str, Any]]:
    """One primary open stage per intent; structural proof, not event activity.

    The detector stays read-only. The supervising watchdog must persist the
    returned rows via write_front_report outside its read transaction.
    """
    if now.tzinfo is None:
        raise ValueError("front snapshot requires aware time")
    required = {"outcomes", "outcome_work", "obstacles", "evidence"}
    tables = {r["name"] for r in tx.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "name IN ('outcomes','outcome_work','obstacles','evidence')")}
    if tables != required:
        return []  # the versioned outcome schema has not been installed

    stages = tx.query(
        "SELECT o.id,o.project,o.intent_id,o.title AS stage,o.state AS outcome_state,"
        "o.created_ts,o.closed_ts,t.tz,t.state AS task_state,t.created_at "
        "FROM outcomes o JOIN outcome_work w ON w.outcome_id=o.id "
        "JOIN tasks t ON t.project=w.project AND t.tz=w.tz "
        "WHERE t.body LIKE '[intent-template]%' AND EXISTS (SELECT 1 FROM outcomes parent "
        "WHERE parent.id=o.parent_id AND parent.state='open') "
        "ORDER BY o.project,o.intent_id,o.created_ts,o.id")
    if not stages:
        return []
    groups: dict[tuple[str, str], list[Any]] = collections.defaultdict(list)
    for stage in stages:
        groups[(stage["project"], stage["intent_id"])].append(stage)

    # Durable transitions only. An event labelled accept counts only when the
    # corresponding stage currently remains accepted. Reopening removes it.
    progress: dict[tuple[str, str], datetime] = {}
    def mark(key: tuple[str, str], value: Any) -> None:
        when = _progress_at(value, now)
        if when is not None and (key not in progress or when > progress[key]):
            progress[key] = when

    for r in tx.query("SELECT project,intent_id,closed_ts FROM outcomes "
                      "WHERE state='done' AND closed_ts IS NOT NULL"):
        mark((r["project"], r["intent_id"]), r["closed_ts"])
    for r in tx.query(
        "SELECT o.project,o.intent_id,MIN(e.ts) AS ts FROM evidence e "
        "JOIN outcomes o ON o.id=e.outcome_id WHERE e.verdict='pass' "
        "AND (json_extract(o.evidence_spec,'$.subject_version') IS NULL "
        "OR json_extract(o.evidence_spec,'$.subject_version')=e.subject_version) "
        "GROUP BY e.outcome_id,e.kind,e.subject_version,e.env_version"):
        mark((r["project"], r["intent_id"]), r["ts"])

    for r in tx.query(
        "SELECT o.project,o.intent_id,MIN(e.ts) AS ts "
        "FROM events e JOIN outcome_work w ON w.project=e.project AND w.tz=e.tz "
        "JOIN outcomes o ON o.id=w.outcome_id "
        "JOIN tasks t ON t.project=w.project AND t.tz=w.tz "
        "WHERE e.kind='accept' AND t.state='accepted' "
        "AND t.body LIKE '[intent-template]%' "
        "GROUP BY o.project,o.intent_id,w.tz"):
        mark((r["project"], r["intent_id"]), r["ts"])

    resolved = {
        r["id"]: (r["project"], r["intent_id"])
        for r in tx.query(
            "SELECT b.id,o.project,o.intent_id FROM obstacles b "
            "JOIN outcomes o ON o.id=b.outcome_id WHERE b.state='resolved'")
    }
    if resolved:
        for r in tx.query(
            "SELECT ts,payload FROM events WHERE kind='outcome.obstacle_resolved'"):
            try:
                ident = json.loads(r["payload"] or "{}").get("id")
            except (TypeError, ValueError, AttributeError):
                continue
            if ident in resolved:
                mark(resolved[ident], r["ts"])

    # Delivery/integration is progress only when it refers to work linked to
    # this intent. Unrelated project integrations cannot unstick another front.
    for r in tx.query(
        "SELECT o.project,o.intent_id,e.ts FROM events e "
        "JOIN outcome_work w ON w.project=e.project AND w.tz=e.tz "
        "JOIN outcomes o ON o.id=w.outcome_id "
        "WHERE e.kind IN ('claim_integrated','integrate')"):
        mark((r["project"], r["intent_id"]), r["ts"])

    open_obstacles: dict[str, list[str]] = collections.defaultdict(list)
    for r in tx.query(
        "SELECT outcome_id,type,cause_code FROM obstacles WHERE state='open' "
        "ORDER BY ts,id"):
        open_obstacles[r["outcome_id"]].append(f"{r['type']}:{r['cause_code']}")

    fronts: list[dict[str, Any]] = []
    for key, items in sorted(groups.items()):
        primary = next((r for r in items if r["outcome_state"] == "open"), None)
        if primary is None:
            continue
        # Start the clock when the first stage was created. A frozen future
        # task's updated_at never makes a valid reset.
        born = _progress_at(items[0]["created_at"], now)
        if born is None:
            continue
        latest = max(born, progress.get(key, born))
        minutes = max(0, int((now - latest).total_seconds() // 60))
        reason = (", ".join(open_obstacles[primary["id"]][:3])
                  or f"stage_state:{primary['task_state']}")
        state = primary['task_state']
        if state == 'accepted':
            waiting = tx.one(
                "SELECT kind,ts,payload FROM events WHERE project=? AND tz=? "
                "AND kind IN ('intent.stage_waiting_external','intent.stage_external_resumed',"
                "'intent.stage_platform_error','intent.stage_reopened') ORDER BY id DESC LIMIT 1",
                (primary['project'], primary['tz']))
            if waiting and waiting['kind'] in {'intent.stage_waiting_external',
                                               'intent.stage_platform_error'}:
                data = json.loads(waiting['payload'])
                state = 'waiting_external' if waiting['kind'].endswith('waiting_external') else 'platform_error'
                reason = str(data.get('reason') or data.get('error') or data.get('repair') or state)
        fronts.append({
            "project": key[0], "intent": key[1], "stage": primary["stage"],
            "tz": primary["tz"], "outcome_id": primary["id"],
            "state": state,
            "minutes_without_progress": minutes, "last_progress_at": latest.isoformat(),
            "reason": reason, "stalled": minutes >= INTENT_STALL_H * 60,
        })
    return fronts


def write_front_report(path: str | Path, now: datetime,
                       fronts: list[dict[str, Any]]) -> None:
    """Atomically replace one bounded, inspectable trusted-sensor report."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema": 1, "checked_at": now.isoformat(), "fronts": fronts},
                         ensure_ascii=False, indent=2) + "\n"
    temp: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                         dir=target.parent, prefix=".progress-front-",
                                         suffix=".tmp", delete=False) as file:
            temp = file.name
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, target)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def findings(tx: Any, now: datetime, issue: Any) -> list[Any]:
    out: list[Any] = []
    fronts = front_snapshot(tx, now)
    front_projects = {r["project"] for r in fronts}

    # 1. Задача по кругу: много выдач, ни одной приёмки.
    since = _since(now, hours=LOOP_TASK_WINDOW_H)
    takes = collections.Counter()
    for r in tx.query("SELECT project,tz FROM events WHERE kind='take' AND ts>?", (since,)):
        takes[(r["project"], r["tz"])] += 1
    for (project, tz), n in takes.items():
        if n < LOOP_TASK_TAKES or not project or not tz:
            continue
        # A looping stage belongs to the single intent-front alarm below.
        # Reporting both makes one causal blockage look like several outages.
        if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=? "
                  "AND body LIKE '[intent-template]%' LIMIT 1", (project, tz)):
            continue
        if tx.scalar("SELECT COUNT(*) FROM events WHERE kind='accept' AND project=? AND tz=? "
                     "AND ts>?", (project, tz, since)):
            continue
        last = tx.query("SELECT kind,payload FROM events WHERE project=? AND tz=? AND kind IN "
                        "('reject','requeue','block','claim_take_refused') ORDER BY id DESC LIMIT 3",
                        (project, tz))
        why = "; ".join(f"{r['kind']}: {_reason(r['payload'])}" for r in last)
        out.append(issue("loop_task", "Fleet Platform", f"{project}/{tz}",
                         f"{project}/{tz} взята {n} раз за {LOOP_TASK_WINDOW_H} ч без приёмки. "
                         f"Последнее: {why}", "observability",
                         fingerprint_key=f"{project}/{tz}"))

    # 2. Проект по кругу: выдачи идут, приёмок почти нет.
    hour = _since(now, hours=1)
    per = collections.defaultdict(collections.Counter)
    for r in tx.query("SELECT project,kind FROM events WHERE ts>? AND kind IN ('take','accept')",
                      (hour,)):
        per[r["project"]][r["kind"]] += 1
    for project, cnt in per.items():
        if project in front_projects:
            continue  # summarize the primary stage, not incidental queue churn
        if cnt["take"] >= LOOP_PROJECT_TAKES and cnt["accept"] < cnt["take"] * LOOP_PROJECT_YIELD:
            out.append(issue("loop_project", "Fleet Platform", str(project),
                             f"{project}: за час выдач {cnt['take']}, приёмок {cnt['accept']} — "
                             "работа идёт по кругу; найди общую причину отказов/возвратов",
                             "observability", fingerprint_key=str(project)))

    # 3. Шторм отказов выдачи с одной причиной (замок, грязный ствол…).
    reasons = collections.Counter(
        _reason(r["payload"])[:90] for r in tx.query(
            "SELECT payload FROM events WHERE kind='claim_take_refused' AND ts>?", (hour,)))
    for text, n in reasons.most_common(1):
        if n >= REFUSAL_STORM:
            out.append(issue("refusal_storm", "Fleet Platform", text[:60],
                             f"{n} отказов выдачи за час: «{text}»", "observability",
                             fingerprint_key=text[:60]))

    # 4. One actual front, not nine future stages. Activity is not proof.
    for r in fronts:
        if not r["stalled"]:
            continue
        out.append(issue(
            "intent_stall", "Fleet Platform", f"{r['project']}/{r['tz']}",
            f"интенция {r['project']} ({r['intent']}): стадия {r['stage']} "
            f"стоит {r['minutes_without_progress']} мин; причина: {r['reason']}",
            "observability", fingerprint_key=f"{r['project']}/{r['intent']}"))

    # 5. Принятая изолированная работа не попала в ствол.
    window = _since(now, hours=6)
    late = _since(now, minutes=UNINTEGRATED_MIN)
    for r in tx.query("SELECT e.project,e.tz,e.ts FROM events e WHERE e.kind='claim_accept' "
                      "AND e.ts>? AND e.ts<?", (window, late)):
        if tx.scalar("SELECT COUNT(*) FROM events WHERE project=? AND tz=? AND ts>=? AND kind IN "
                     "('claim_integrated','claim_integration_obstacle','integrate')",
                     (r["project"], r["tz"], r["ts"])):
            continue
        out.append(issue("accepted_not_integrated", "Fleet Platform", f"{r['project']}/{r['tz']}",
                         f"{r['project']}/{r['tz']} принята в {r['ts'][:16]}, но интеграции нет "
                         f"{UNINTEGRATED_MIN}+ мин — работа лежит только в дереве захвата",
                         "observability", fingerprint_key=f"{r['project']}/{r['tz']}"))
    return out
