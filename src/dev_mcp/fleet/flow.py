"""Метрики потока флота v2 — эксперименты E1–E5 и здоровье ствола одним вызовом (27.09.2026).

Зачем. Архитектура v2 (ToDo/2026-09-26-fleet-architecture-v2.md §11–12) ставит эксперименты,
но считать их приходилось разовыми скриптами по реестру и журналу вызовов — и каждый раз
заново. Здесь все определения в одном месте, `fleet.cmd flow` печатает их, тесты фиксируют
смысл каждой цифры.

Определения:
- ход — непрерывная серия вызовов одного агента; пауза больше TURN_GAP_S = новый ход
  (как в §1 архитектуры). Конец хода классифицируется по последнему инструменту.
- утечка приёмки — отказ ревьюера по задаче, у которой есть acceptance. Сдача без зелёной
  приёмки невозможна (queue.submit), значит каждый такой отказ — дефект, который приёмка ТЗ
  пропустила.
- первый проход — доля base-задач (не fix/review), чей ПЕРВЫЙ вердикт — accept. Считать по
  принятым нельзя: после отказа принимается fix, а сама base-задача — никогда.
- будка подтверждена — событие `wake_ack` (агент пришёл с токеном будки); до токена —
  «вызов в течение WAKE_ANSWER_S», это не доказательство, агент мог работать и так.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

TURN_GAP_S = 180
WAKE_ANSWER_S = 180

_FLEET_WAIT = {"fleet_take"}
_BUILD = {"run", "run_status", "run_task"}
_HANDOFF = {"fleet_finish", "fleet_submit", "fleet_review", "fleet_block"}


def _ts(raw: Any) -> datetime | None:
    try:
        text = str(raw or "").strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _payload(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


# ---------------------------------------------------------------- ходы (журнал вызовов)

def turn_class(tool: str) -> str:
    if tool in _FLEET_WAIT:
        return "пустая очередь / парковка"
    if tool in _BUILD:
        return "после запуска команды"
    if tool in _HANDOFF:
        return "после сдачи/вердикта"
    if tool.startswith("fleet_"):
        return "после служебного вызова"
    return "посреди работы"


def turns(entries: Iterable[dict[str, Any]], since: str = "") -> dict[str, Any]:
    """Ходы агентов по журналу вызовов: сколько, чем кончаются, длина."""
    calls: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for e in entries:
        agent = str(e.get("agent") or "")
        ts = _ts(e.get("ts"))
        if not agent or ts is None or (since and str(e.get("ts")) < since):
            continue
        calls[agent].append((ts, str(e.get("tool") or "")))
    ends: Counter[str] = Counter()
    lengths: list[float] = []
    counts: list[int] = []
    for seq in calls.values():
        seq.sort()
        start = 0
        for i in range(1, len(seq) + 1):
            if i == len(seq) or (seq[i][0] - seq[i - 1][0]).total_seconds() > TURN_GAP_S:
                ends[turn_class(seq[i - 1][1])] += 1
                lengths.append((seq[i - 1][0] - seq[start][0]).total_seconds() / 60)
                counts.append(i - start)
                start = i
    total = sum(ends.values())
    return {
        "n": total,
        "ends": {k: v for k, v in ends.most_common()},
        "median_min": _median(lengths),
        "median_calls": _median([float(c) for c in counts]),
    }


# ---------------------------------------------------------------- реестр

def _rows(conn: Any, sql: str, args: tuple = ()) -> list[Any]:
    return list(conn.execute(sql, args).fetchall())


def agent_states(store: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Единственный счётчик состояний воркеров для CLI, MCP и UI.

    Свежий MCP и краткоживущее наблюдение генерации UI считаются отдельно.
    Давность MCP сама по себе не доказывает, что модель продолжает думать.
    Без захвата свежий worker ожидает новую задачу; забытые беззадачные
    разговоры не считаются ни ожидающими, ни работающими.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sql = (
        "SELECT w.last_seen, EXISTS("
        "SELECT 1 FROM claims c WHERE c.worker_id=w.id AND c.state='active'"
        ") AS claimed,w.id FROM workers w WHERE w.revoked_at IS NULL"
    )
    def observations(tx):
        if not tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_activity'"):
            return {}
        return {str(r['worker_id']):dict(r) for r in tx.query(
            "SELECT a.* FROM worker_activity a JOIN claims c ON c.id=a.claim_id WHERE c.state='active'")}
    activity={}
    if hasattr(store, "read"):
        with store.read() as tx:
            rows = tx.query(sql)
            activity=observations(tx)
    elif hasattr(store,'query'):
        rows = store.query(sql)
        activity=observations(store)
    else:
        rows = _rows(store, sql)
        if store.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_activity'").fetchone():
            activity={r[0]:{'state':r[1],'stall_s':r[2],'seen_at':r[3]} for r in store.execute(
                "SELECT a.worker_id,a.state,a.stall_s,a.seen_at FROM worker_activity a JOIN claims c "
                "ON c.id=a.claim_id WHERE c.state='active'")}

    result = {"working": 0, "thinking": 0, "silent": 0, "waiting": 0,
              # 17.10: «не подтверждены» по причинам. Вкладка закрывается через секунды после
              # начала ответа, поэтому молчащий агент чаще всего просто думает без вкладки.
              "silent_answering": 0, "silent_ended": 0, "silent_unknown": 0}
    for row in rows:
        seen = _ts(row[0])
        age = max(0.0, (current - seen).total_seconds()) if seen else float("inf")
        if not row[1]:
            if age <= 120:
                result["waiting"] += 1
        elif age <= 120:
            result["working"] += 1
        elif (str(row[2]) in activity and activity[str(row[2])]['state']=='generating' and
              activity[str(row[2])]['stall_s']<180 and
              (seen_ui:=_ts(activity[str(row[2])]['seen_at'])) is not None and
              0<=(current-seen_ui).total_seconds()<=90):
            result["thinking"] += 1
        else:
            result["silent"] += 1
            seen_activity = activity.get(str(row[2])) or {}
            seen_ui = _ts(seen_activity.get('seen_at'))
            ui_age = (current - seen_ui).total_seconds() if seen_ui else float("inf")
            if seen_activity.get('state') == 'generating' and ui_age <= 30 * 60:
                result["silent_answering"] += 1
            elif seen_activity.get('state') in ('idle', 'error') and ui_age <= 10 * 60:
                result["silent_ended"] += 1
            else:
                result["silent_unknown"] += 1
    return result


def format_silent_breakdown(counts: dict[str, int]) -> str:
    """Причины «не подтверждены»: отдельная строка, чтобы не ломать контракт основной."""
    return (f"агенты-молчание: отвечают без вкладки {counts.get('silent_answering', 0)} · "
            f"ход окончен {counts.get('silent_ended', 0)} · без следов {counts.get('silent_unknown', 0)}")


def format_agent_states(counts: dict[str, int]) -> str:
    """Стабильный контракт строки fleet_list и fleet.cmd для расширения."""
    return (f"агенты: MCP≤2мин {counts['working']} · генерация UI {counts['thinking']} · "
            f"не подтверждены {counts['silent']} · ждут {counts['waiting']}")


def parking(conn: Any, since: str) -> dict[str, Any]:
    """E1: сколько парковок отдали задачу без будки."""
    outcomes: Counter[str] = Counter()
    for (payload,) in _rows(conn, "SELECT payload FROM events WHERE kind='park' AND ts>=?", (since,)):
        outcomes[str(_payload(payload).get("outcome") or "?")] += 1
    return {"n": sum(outcomes.values()), "outcomes": dict(outcomes)}


def wakes(conn: Any, since: str) -> dict[str, Any]:
    """E3: приказы будильника, подтверждённые токеном, и ответ вызовом (без доказательства)."""
    orders = _rows(conn, "SELECT id,ts,worker_id,payload FROM events WHERE kind='wake_order' AND ts>=?", (since,))
    wake_tokens = {str(_payload(payload).get("wake")) for _, _, _, payload in orders}
    generating_tokens = {str(_payload(payload).get("wake")) for (payload,) in _rows(
        conn, "SELECT payload FROM events WHERE kind='wake_state' AND ts>=?", (since,)
    ) if _payload(payload).get("state") == "generating"}
    acked = {str(_payload(p).get("wake")) for (p,) in _rows(
        conn, "SELECT payload FROM events WHERE kind='wake_ack' AND ts>=?", (since,))}
    answered = confirmed = 0
    delays: list[float] = []
    for oid, ts, worker, payload in orders:
        wake_id = str(_payload(payload).get("wake") or "")
        if wake_id and wake_id in acked:
            confirmed += 1
        t0 = _ts(ts)
        nxt = conn.execute(
            "SELECT ts FROM events WHERE worker_id=? AND id>? AND kind NOT IN ('wake_order','park') "
            "ORDER BY id LIMIT 1", (worker, oid)).fetchone()
        t1 = _ts(nxt[0]) if nxt else None
        if t0 and t1 and (t1 - t0).total_seconds() <= WAKE_ANSWER_S:
            answered += 1
            delays.append((t1 - t0).total_seconds())
    gave_up = _rows(conn, "SELECT payload FROM events WHERE kind='requeue' AND ts>=?", (since,))
    return {
        "orders": len(orders),
        "generating": len(wake_tokens & generating_tokens),
        "confirmed": confirmed,
        "answered": answered,
        "median_answer_s": _median(delays),
        "gave_up": sum(1 for (p,) in gave_up if "будильник" in str(_payload(p).get("why", ""))),
    }


def verdicts(conn: Any, since: str) -> dict[str, Any]:
    """Принято в день, первый проход, утечка приёмки."""
    tasks = {(r[0], r[1]): r for r in _rows(conn, "SELECT project,tz,kind,acceptance FROM tasks")}
    per_day: Counter[str] = Counter()
    first_verdict: dict[tuple[str, str], tuple[str, str]] = {}
    with_acc = rej_with_acc = 0
    for kind, ts, project, tz in _rows(
        conn, "SELECT kind,ts,project,tz FROM events WHERE kind IN ('accept','reject') ORDER BY id"):
        tz = str(tz)
        if tz.startswith("review:"):
            continue
        if not tz.startswith("fix:"):
            first_verdict.setdefault((project, tz), (str(ts), kind))
        if str(ts) < since:
            continue
        row = tasks.get((project, tz))
        if row and json.loads(row[3] or "[]"):
            with_acc += 1
            rej_with_acc += kind == "reject"
        if kind == "accept":
            per_day[str(ts)[:10]] += 1
    firsts = [kind for ts, kind in first_verdict.values() if ts >= since]
    return {
        "accepted_per_day": dict(sorted(per_day.items())),
        "first_pass": {"n": firsts.count("accept"), "N": len(firsts)},
        "leak": {"n": rej_with_acc, "N": with_acc},
    }


def gates(conn: Any, since: str) -> dict[str, Any]:
    """E4: прогоны гейта после приёмок, красные, стыки, возраст последнего зелёного."""
    rows = _rows(conn, "SELECT ts,project,rc,payload FROM events WHERE kind='gate' ORDER BY id")
    per: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    for ts, project, rc, payload in rows:
        item = per.setdefault(project, {"runs": 0, "red": 0, "durs": [], "last_green": None, "last_rc": None})
        item["last_rc"] = rc
        if rc == 0:
            item["last_green"] = ts
        if str(ts) < since:
            continue
        item["runs"] += 1
        item["red"] += rc != 0
        dur = _payload(payload).get("dur_s")
        if isinstance(dur, (int, float)):
            item["durs"].append(float(dur))
    styk = _rows(conn, "SELECT project FROM events WHERE kind='styk_open' AND ts>=?", (since,))
    out: dict[str, Any] = {}
    for project, item in per.items():
        green = _ts(item["last_green"])
        out[project] = {
            "runs": item["runs"],
            "red": item["red"],
            "median_s": _median(item["durs"]),
            "max_s": max(item["durs"]) if item["durs"] else None,
            "trunk_green": item["last_rc"] == 0,
            "green_age_h": (now - green).total_seconds() / 3600 if green else None,
            "styk": sum(1 for (p,) in styk if p == project),
        }
    return out


def bites(conn: Any, since: str) -> dict[str, Any]:
    """E5: вердикты кусающей проверки по последнему событию задачи и повторные fix (раунд 2+)."""
    last: dict[tuple[str, str], str] = {}
    for project, tz, payload in _rows(conn, "SELECT project,tz,payload FROM events WHERE kind='bite' AND ts>=? "
                                            "ORDER BY id", (since,)):
        last[(project, tz)] = str(_payload(payload).get("verdict") or "?")
    fixes = _rows(conn, "SELECT DISTINCT project,tz FROM events WHERE kind='submit' AND tz LIKE 'fix:%' AND ts>=?",
                  (since,))
    repeat = sum(1 for _, tz in fixes if str(tz).count(":") >= 2 and str(tz).rsplit(":", 1)[-1].isdigit())
    verdict_counts: dict[str, int] = {}
    for verdict in last.values():
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
    return {"fixes": len(fixes), "checked": len(last), "verdicts": verdict_counts, "repeat_fixes": repeat}


# Инструменты, добавленные после 27.09: ChatGPT берёт список инструментов один раз при подключении
# коннектора, и до переподключения агенты их не видят. Ноль вызовов при живом флоте — сигнал
# «коннектор не переподключён», а не «агенты не хотят».
NEW_TOOLS = ("apply_patch", "fleet_checkpoint")


def tool_drift(entries: Iterable[dict[str, Any]], since: str = "") -> dict[str, Any]:
    used = {name: 0 for name in NEW_TOOLS}
    calls = wake_tokens = 0
    for e in entries:
        if since and str(e.get("ts", "")) < since:
            continue
        tool = str(e.get("tool") or "")
        if not tool:
            continue
        calls += 1
        if tool in used:
            used[tool] += 1
        if tool == "fleet_resume" and (e.get("args") or {}).get("wake"):
            wake_tokens += 1
    return {"calls": calls, "used": used, "wake_tokens": wake_tokens}


def intervention_counts(conn: Any, *, now: datetime | None = None, days: int = 7) -> dict[str, Any]:
    """TZ-48: read-only intervention metrics; safe before the ledger is installed."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not _rows(conn, "SELECT name FROM sqlite_master "
                     "WHERE type='table' AND name='intervention_log'"):
        rows: list[Any] = []
    else:
        since = (now.date() - timedelta(days=max(1, days) - 1)).isoformat()
        rows = _rows(conn, "SELECT ts,class FROM intervention_log WHERE ts>=?", (since,))
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for ts, category in rows:
        parsed = _ts(ts)
        if parsed is not None:
            grouped[parsed.date().isoformat()][str(category)] += 1
    dates = [(now.date() - timedelta(days=delta)).isoformat()
             for delta in range(max(1, days) - 1, -1, -1)]
    by_day = {date: dict(sorted(grouped.get(date, {}).items())) for date in dates}
    today = sum(by_day[dates[-1]].values())
    yesterday = sum(by_day[dates[-2]].values()) if len(dates) >= 2 else 0
    by_class: Counter[str] = Counter()
    for counts in by_day.values():
        by_class.update(counts)
    return {"goal": 0, "today": today, "yesterday": yesterday,
            "rising": today > yesterday, "by_day": by_day,
            "by_class": dict(sorted(by_class.items()))}


def flow(conn: Any, entries: Iterable[dict[str, Any]], since: str) -> dict[str, Any]:
    entries = list(entries)
    from . import account_telemetry
    return {
        "account_limit": account_telemetry.metrics(conn),
        "bites": bites(conn, since),
        "tools": tool_drift(entries, since),
        "since": since,
        "verdicts": verdicts(conn, since),
        "turns": turns(entries, since),
        "parking": parking(conn, since),
        "wakes": wakes(conn, since),
        "gates": gates(conn, since),
        "interventions": intervention_counts(conn),
    }


# ---------------------------------------------------------------- печать

def _pct(n: int, total: int) -> str:
    return f"{n}/{total} ({n / total:.0%})" if total else f"{n}/0"


def _num(value: Any, unit: str = "") -> str:
    return "—" if value is None else f"{value:.1f}{unit}" if isinstance(value, float) else f"{value}{unit}"


def format_flow(data: dict[str, Any]) -> list[str]:
    v, t, p, w = data["verdicts"], data["turns"], data["parking"], data["wakes"]
    lines = [f"поток с {data['since']}"]
    account = data.get("account_limit")
    if account is not None:
        lines.append("лимит аккаунта: перегрузок " + str(account["overloads"]) +
                     " за сутки, медиана паузы " + _num(account["median_pause_min"], " мин") +
                     ", чатов за 3 ч до перегрузки (медиана) " +
                     _num(account["median_chats_3h"]))
    lines.append("утечка приёмки (отказ при зелёной приёмке / вердикты с приёмкой): "
                 + _pct(v["leak"]["n"], v["leak"]["N"]))
    lines.append("первый проход (первый вердикт base-задачи — accept): "
                 + _pct(v["first_pass"]["n"], v["first_pass"]["N"]))
    days = " · ".join(f"{d[5:]} {n}" for d, n in v["accepted_per_day"].items()) or "—"
    lines.append(f"принято по дням: {days}")
    interventions = data.get("interventions") or {}
    if interventions:
        mix = " · ".join(f"{category} {n}" for category, n
                         in interventions["by_class"].items()) or "—"
        last_days = " · ".join(f"{day[5:]} {sum(counts.values())}"
                               for day, counts in interventions["by_day"].items())
        warning = " · РОСТ" if interventions["rising"] else ""
        lines.append(f"ручных вмешательств сегодня {interventions['today']}, вчера "
                     f"{interventions['yesterday']} (цель 0{warning}); "
                     f"по классам: {mix}; 7д: {last_days}")
    ends = " · ".join(f"{k} {_pct(n, t['n']).split(' ')[1]}" for k, n in t["ends"].items()) or "—"
    lines.append(f"ходов {t['n']} (медиана {_num(t['median_min'], ' мин')}, {_num(t['median_calls'])} вызовов); "
                 f"конец хода: {ends}")
    outcomes = p["outcomes"]
    lines.append(f"E1 парковка: {p['n']} ожиданий, выдали задачу {_pct(outcomes.get('task', 0), p['n'])}, "
                 f"сообщение {outcomes.get('mail', 0)}, серия исчерпана {outcomes.get('streak_end', 0)}")
    lines.append(f"E3 будки: приказов {w['orders']}, подтверждено токеном {w['confirmed']}, "
                 f"вызов ≤{WAKE_ANSWER_S // 60} мин {w['answered']} (медиана {_num(w['median_answer_s'], ' с')}), "
                 f"задач возвращено {w['gave_up']}")
    lines.append(f"будильник: приказов {w['orders']}, из них при generating {w.get('generating', 0)}, "
                 f"отборов {w['gave_up']}")
    for project, g in sorted(data["gates"].items()):
        state = "зелёный" if g["trunk_green"] else "КРАСНЫЙ"
        lines.append(f"E4 {project}: ствол {state} (зелёный {_num(g['green_age_h'], ' ч')} назад), "
                     f"гейтов {g['runs']}, красных {g['red']}, стыков {g['styk']}, "
                     f"длительность медиана {_num(g['median_s'], ' с')} / макс {_num(g['max_s'], ' с')}")
    b = data.get("bites")
    if b:
        mix = " · ".join(f"{k} {n}" for k, n in sorted(b["verdicts"].items())) or "—"
        lines.append(f"E5 кусающий тест: fix сдано {b['fixes']}, проверено {b['checked']} ({mix}); "
                     f"повторных fix (раунд 2+) {b['repeat_fixes']}")
    tl = data.get("tools")
    if tl and tl["calls"]:
        unused = [k for k, n in tl["used"].items() if not n]
        tail = (f" — НЕ ВИДНЫ агентам: {', '.join(unused)} (переподключить коннектор в ChatGPT)"
                if unused and tl["calls"] > 200 else "")
        lines.append("инструменты v2: " + " · ".join(f"{k} {n}" for k, n in tl["used"].items())
                     + f", токен будки {tl['wake_tokens']} из {tl['calls']} вызовов{tail}")
    return lines


def since_default(hours: float = 24.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
