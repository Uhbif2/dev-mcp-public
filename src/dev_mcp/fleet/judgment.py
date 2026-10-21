"""Задачи-суждения и умолчания решений (архитектура v2 §6–§7; одобрено владельцем 07.10.2026).

Код решает правила; вопрос, требующий суждения, код ставит задачей `kind=triage` в ту же очередь —
её берёт обычный агент, а не владелец. Прецедент — стык интегратора (integrate._open_styk).

Источники (не больше одной открытой triage на предмет, не больше DAILY_BUDGET на проект в сутки):
1. `rejects:<TZ>` — семья задачи получила REJECTS_N+ отказов → «почему круги не сходятся, что
   поменять в ТЗ или приёмке». Повторно — только на REJECTS_N новых отказов после прошлой triage.
2. `gates:<ворота>` — за одним набором ворот стоят GATE_TASKS_N+ задач → «какие на деле не зависят
   от ворот — отвяжи (`needs`) или предложи разрез».
3. `decide:<TZ>` — ворота ждут владельца дольше DECIDE_AFTER_H → агент формулирует вариант решения
   файлом `ToDo/decisions/<TZ>.md` (§7: «умолчание со сроком»).

Итог triage — правка файлов (ТЗ, файл решения) или заявка, не прозой: сдача без этого отклоняется
(queue._triage_submit_reason). После accept код применяет итог сам:
- изменённые ТЗ перерегистрируются (как `fleet.cmd bootstrap`), кроме взятых сейчас;
- файл решения становится предложением (событие `proposal`).

Умолчание (E). Обратимое действие над очередью (reactivate / close), предложенное одним агентом и
принятое другим, применяется СРАЗУ — §7: «обратимое и не катящееся снежным комом применяется без
ожидания»; владелец 07.10: «хочу систему, которая решает без меня». Событие `default_applied` с
прежним состоянием, строка в `fleet.cmd decisions`, откат — `fleet.cmd undo <Проект>/<TZ>`.
Предмет про деньги, аккаунты, публикацию, железо — НЕОБРАТИМЫЙ, что бы ни написал агент: ждёт
владельца, и тот видит одну фразу `owner_needs` — что именно сделать (решение №4, 26.09).
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .model import norm_path, paths_overlap, task_root

REJECTS_N = 3
GATE_TASKS_N = 5
DECIDE_AFTER_H = 2.0  # было 24: владелец хочет, чтобы система решала сама, а не ждала его сутки
DAILY_BUDGET = 6
GATE_COOLDOWN_H = 72.0
INTERVAL_S = 300.0
OPEN_STATES = ("draft", "ready", "taken", "submitted", "frozen")
MARK = "[fleet-triage]"
ACTIONS = ("reactivate", "close", "keep", "park")

# Деньги, аккаунты, публикация (решение владельца №4): такое не применяется по умолчанию.
# Железо убрано 18.10.2026 (владелец: «останавливать только реальные риски и траты денег»):
# у VPN-проекта телефон и устройство упоминает почти каждая задача, и любая разморозка
# становилась «ждёт владельца». Отсутствующий телефон/стенд — ресурс мира (world.py):
# очередь сама не выдаёт задачу без него и размораживает, когда он появился.
IRREVERSIBLE = re.compile(
    r"(spend|purchase|\bbuy\b|\bpay\b|paid|price|\busd\b|\beur\b|\$\s?\d|₽|\bруб|оплат|покуп|купит|"
    r"трат[аиыу]|деньг|сч[её]т на оплату|"
    r"чуж\w* аккаунт|нов\w* аккаунт|создать аккаунт|регистрац\w* аккаунт|sign.?up|"
    r"publish|публикац|опубликова|релиз в|store listing|sim\b|сим-карт)",
    re.I,
)

_CONDITIONAL = re.compile(r"\s*(если|когда|в случае|if|when|once)\b", re.I)

Decisions = Callable[[], dict[str, Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: Any) -> datetime:
    text = str(ts or "").strip().replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _json(value: Any) -> Any:
    try:
        return json.loads(value or "null")
    except (TypeError, ValueError):
        return None


def _granted(tx: Any, project: str, tz: str, root: str = "") -> list[str]:
    """Owner decisions (owner_grants.py: project files + registry) for the decide-triage body:
    a keep over something the owner already allowed is the 18.10 refreeze loop."""
    from .owner_grants import file_decisions, grants_for, render

    lines = render(grants_for(tx, project, tz), file_decisions(root))
    return ["", *lines, "Если решение выше снимает препятствие — action: reactivate, не keep."]


def irreversible(*texts: str) -> bool:
    return any(IRREVERSIBLE.search(t or "") for t in texts)


def pult_decisions() -> dict[str, Any]:
    """Очередь решений из пульта (`fleet.cmd decisions --json`): логика ворот живёт там одна."""
    repo = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run(
        [sys.executable, str(repo / "tools" / "fleet.py"), "decisions", "--json"],
        cwd=str(repo), env=env, capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b"").decode("utf-8", "replace")[-400:])
    return json.loads(proc.stdout.decode("utf-8"))


# ---- открытие triage ---------------------------------------------------------------------------

def _subject_of(body: str) -> str:
    m = re.search(re.escape(MARK) + r" subject=(\S+)", body or "")
    return m.group(1) if m else ""


def _open_subjects(tx: Any) -> set[tuple[str, str]]:
    rows = tx.query(
        "SELECT project,body FROM tasks WHERE kind='triage' AND state IN (%s)" % ",".join("?" * len(OPEN_STATES)),
        OPEN_STATES,
    )
    return {(str(r["project"]), _subject_of(str(r["body"] or ""))) for r in rows}


def _last_triage(tx: Any, project: str, subject: str) -> dict[str, Any] | None:
    for row in tx.query(
        "SELECT id,ts,payload FROM events WHERE kind='triage_open' AND project=? ORDER BY id DESC", (project,),
    ):
        if (_json(row["payload"]) or {}).get("subject") == subject:
            return dict(row)
    return None


def _spent_today(tx: Any, project: str, now: datetime) -> int:
    return int(tx.scalar(
        "SELECT COUNT(*) FROM events WHERE kind='triage_open' AND project=? AND ts>=?",
        (project, _iso(now - timedelta(hours=24))),
    ) or 0)


def _abs(root: str, rel: str) -> str:
    return norm_path(str(Path(root) / rel)) if rel else ""


def _insert(tx: Any, project: str, subject: str, title: str, body: str, owns: list[str], priority: int,
            extra: dict[str, Any] | None = None) -> str:
    n = int(tx.scalar("SELECT COUNT(*) FROM tasks WHERE project=? AND kind='triage'", (project,)) or 0)
    tz = f"triage:{n + 1}"
    while tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?", (project, tz)) is not None:
        n += 1
        tz = f"triage:{n + 1}"
    now = _iso(_now())
    tx.change(
        "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
        "resources,acceptance,author_worker,body,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (project, tz, title[:200], "ready", priority, "triage", "", "", "[]",
         json.dumps(sorted(set(o for o in owns if o)), ensure_ascii=False), "[]", "[]", "judgment",
         f"{MARK} subject={subject}\n{body}", now, now),
        event={"kind": "triage_open", "project": project, "tz": tz, "payload": {"subject": subject, **(extra or {})}},
    )
    return tz


def _verified_cluster_edges(
    project: str, root: str, gates: list[str], members: list[str],
    tasks: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, list[str]]:
    """Only *direct*, currently declared needs can be unlinked by gate triage.

    fleet.py's decisions().clusters represents TRANSITIVE blockers, including
    historical block reasons. Such a cluster is useful for the dashboard, but
    it cannot be turned into an unlink proposal for members without an edge.
    The source spec takes precedence over stale tasks.needs after an unlink.
    """
    from .spec import _header, _load_header, parse_spec

    selected: dict[str, list[str]] = {}
    for tz in dict.fromkeys(members):
        item = tasks.get((project, tz))
        if not item or item["state"] != "ready":
            continue
        registered = _json(item.get("needs"))
        if not isinstance(registered, list):
            continue
        needs = {str(x) for x in registered}
        spec_name = str(item.get("spec_path") or "")
        if spec_name:
            spec_path = Path(root) / spec_name
            # A spec-less synthetic task uses the registry. A real spec wins
            # over an out-of-date registry (and invalid specs fail closed).
            if spec_path.is_file():
                try:
                    source = parse_spec(str(spec_path.resolve()))
                    if source.tz != tz:
                        continue
                    if source.project != project:
                        # parse_spec defaults a missing `project` to the root
                        # directory name, which may differ from the registry
                        # name (e.g. dev-mcp-work vs Fleet Platform). Reject
                        # only an explicitly conflicting project declaration.
                        _, header_text = _header(spec_path)
                        declared = str(_load_header(spec_path, header_text).get("project") or "").strip()
                        if declared and declared != project:
                            continue
                    needs = set(source.needs)
                except (OSError, ValueError, TypeError):
                    continue
        direct = sorted(needs.intersection(gates))
        if direct:
            selected[tz] = direct
    return selected


def _cluster_signature(edges: dict[str, list[str]]) -> str:
    canonical = json.dumps(edges, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_stale_cluster(
    tx: Any, *, project: str, gates: list[str], reported: list[str],
    direct: dict[str, list[str]],
) -> None:
    """One diagnostic per distinct stale cluster (not one every polling tick)."""
    payload = {
        "gates": gates, "reported": sorted(reported), "direct": direct,
    }
    fingerprint = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    # A diagnostic is not a verdict or a new triage; it never changes needs.
    previous = tx.query(
        "SELECT payload FROM events WHERE kind='triage_cluster_stale' "
        "AND project=? ORDER BY id DESC LIMIT 80", (project,),
    )
    if any((_json(row["payload"]) or {}).get("fingerprint") == fingerprint
           for row in previous):
        return
    tx.record_event(kind="triage_cluster_stale", project=project,
                    payload={**payload, "fingerprint": fingerprint})


_RESULT_RULE = (
    "Итог — ТОЛЬКО правкой файлов зоны (ТЗ, файл решения) или заявкой request_note; сдача без этого "
    "отклоняется. Код задач не правь. В note сдачи — одно предложение: что поменял и почему."
)


def open_triages(store: Any, decisions: Decisions | None = None, *, now: datetime | None = None) -> list[str]:
    # Repair stale decision owns before allocating any new work (idempotent).
    migrate_legacy_decision_paths(store)
    now = now or _now()
    opened: list[str] = []
    data: dict[str, Any] | None = None
    with store.read() as tx:
        roots = {str(r["name"]): str(r["root"]) for r in tx.query("SELECT name,root FROM projects WHERE active=1")}

    # 2–3 нужны ворота пульта; считаем их вне транзакции (подпроцесс читает ту же базу).
    if decisions is not None:
        try:
            data = decisions()
        except Exception:  # noqa: BLE001 — нет пульта, нет и этих источников
            data = None
    data = data or {}
    items = {(it["project"], it["tz"]): it for it in data.get("items", [])}
    # Historical scans and filesystem preflight must not hold the global
    # writer gate. Under paging pressure these reads held every agent for >60s.
    with store.read() as tx:
        tasks = {(str(r["project"]), str(r["tz"])): dict(r) for r in tx.query(
            "SELECT t.project,t.tz,t.title,t.state,t.kind,t.spec_path,t.needs FROM tasks t "
            "JOIN projects p ON p.name=t.project WHERE p.active=1")}
        reject_rows = tx.query(
            "SELECT e.id,e.ts,e.project,e.tz,e.payload FROM events e "
            "JOIN projects p ON p.name=e.project WHERE e.kind='reject' AND p.active=1 ORDER BY e.id")
    clusters = sorted(data.get("clusters", []), key=lambda c: -len(c["tasks"]))
    preflight = {}
    for index, cluster in enumerate(clusters):
        project, gates, members = cluster["project"], list(cluster["gates"]), list(cluster["tasks"])
        if len(members) >= GATE_TASKS_N and project in roots and gates:
            preflight[index] = _verified_cluster_edges(project, roots[project], gates, members, tasks)

    def unchanged(tx: Any, project: str, tz: str) -> bool:
        previous = tasks.get((project, tz))
        current = tx.one("SELECT state,needs,spec_path FROM tasks WHERE project=? AND tz=?", (project, tz))
        return bool(previous and current and all(current[k] == previous[k]
                                                for k in ("state", "needs", "spec_path")))

    # Порядок — по пользе (07.10.2026): дневной бюджет Nova VPN ушёл на два разбора одних ворот,
    # а скопление в 31 задачу за TZ-61 ждало. Сначала большие скопления, потом ворота, потом отказы.
    with store.tx() as tx:
        busy = _open_subjects(tx)

        def allowed(project: str, subject: str) -> bool:
            return (project in roots and (project, subject) not in busy
                    and _spent_today(tx, project, now) < DAILY_BUDGET)

        for index, cluster in enumerate(clusters):
            project, gates, members = cluster["project"], list(cluster["gates"]), list(cluster["tasks"])
            if len(members) < GATE_TASKS_N or project not in roots or not gates:
                continue
            subject = "gates:" + ",".join(gates)
            edges = preflight[index]
            if not all(unchanged(tx, project, tz) for tz in members):
                continue  # A changed topology needs a fresh preflight next tick.
            # Stale/transitive cluster membership is not permission to propose
            # a no-op unlink of a gate which is absent from the current spec.
            if len(edges) != len(members):
                _record_stale_cluster(tx, project=project, gates=gates,
                                      reported=members, direct=edges)
            if len(edges) < GATE_TASKS_N:
                continue
            if not allowed(project, subject):
                continue
            last = _last_triage(tx, project, subject)
            signature = _cluster_signature(edges)
            if last is not None and (_json(last["payload"]) or {}).get("signature") == signature:
                continue  # Already adjudicated this exact topology. No new work.
            if last is not None and now - _parse(last["ts"]) < timedelta(hours=GATE_COOLDOWN_H):
                continue
            ready = sorted(edges)
            rel = f"ToDo/decisions/gates-{_safe_name(gates[0])}.md"
            lines = []
            for tz in ready[:40]:
                t = tasks[(project, tz)]
                lines.append(f"- {tz} «{t['title']}» direct needs={edges[tz]} ТЗ: {t['spec_path'] or '—'}")
            gate_lines = [f"- {g}: {items.get((project, g), {}).get('reason', '')[:400]}" for g in gates]
            body = "\n".join([
                f"Задача-суждение. В {project} {len(ready)} задач ПРЯМО зависят от ворот {', '.join(gates)}:",
                *gate_lines, "", "Стоящие задачи (ready):", *lines, "",
                "Вопрос: какие из них на деле НЕ зависят от этих ворот? Прочитай их ТЗ (read_file) и ответь "
                f"ОДНИМ файлом `{rel}` — сами ТЗ не правь (07.10.2026: правка 31 файла ТЗ держала все 31 задачу, "
                "пока шёл разбор). После принятия сервер сам уберёт зависимости из шапок и перерегистрирует ТЗ:",
                "```yaml", "unlink:", f"  TZ-NN: [{gates[0]}]   # у TZ-NN убрать эти needs", "why: почему они не зависят",
                "```",
                "Зависимые не упоминай. Задача зависит частично — в why опиши разрез, его сделает отдельная задача.",
                _RESULT_RULE,
            ])
            owns = [_abs(roots[project], rel)]
            opened.append(f"{project}/" + _insert(
                tx, project, subject, f"Суждение: что из {len(ready)} задач не ждёт {', '.join(gates)}",
                body, owns, 1, extra={"signature": signature, "edges": edges}))
            busy.add((project, subject))

        for it in sorted(data.get("items", []), key=lambda i: -int(i.get("needed_for") or 0)):
            project, tz = it["project"], it["tz"]
            if not it.get("gate") or not it.get("needed_for") or tz == "(гейт)":
                continue
            if not unchanged(tx, project, tz):
                continue
            if it.get("age_min", 0) < DECIDE_AFTER_H * 60:
                continue
            subject = f"decide:{tz}"
            if not allowed(project, subject):
                continue
            prop = _open_proposal(tx, project, tz)
            # Просьба к владельцу без понятной ему фразы — брак разбора: переспросить сразу, без паузы.
            # Условная просьба («если X — сделай Y») — тоже брак: это park, владельцу сейчас делать нечего.
            needs = str(prop["payload"].get("owner_needs") or "") if prop is not None else ""
            unclear = prop is not None and not prop["payload"].get("reversible") \
                and prop["payload"].get("action") != "park" \
                and (not needs or bool(_CONDITIONAL.match(needs)))
            parked_out = (prop is not None and prop["payload"].get("action") == "park"
                          and now - _parse(prop["ts"]) >= timedelta(hours=GATE_COOLDOWN_H))
            if prop is not None and not unclear and not parked_out:
                continue
            last = _last_triage(tx, project, subject)
            if not unclear and last is not None and now - _parse(last["ts"]) < timedelta(hours=GATE_COOLDOWN_H):
                continue
            rel = f"ToDo/decisions/{_safe_name(tz)}.md"
            body = "\n".join([
                f"Задача-суждение. {project}/{tz} «{it['title']}» ждёт владельца "
                f"{int(it['age_min'] // 60)} ч и держит {it['needed_for']} задач.",
                f"Причина остановки: {it['reason']}", "",
                f"Сформулируй вариант решения владельцу файлом `{rel}`:",
                "```yaml", f"tz: {tz}", "action: reactivate | close | keep | park",
                "why: одно-два предложения — почему этот вариант",
                "owner_needs: только для keep — ОДНА фраза владельцу простыми словами: что именно сделать "
                "(сумма и на что; какое устройство и что с ним; какой доступ) и что пойдёт после",
                "```",
                "reactivate — снять заморозку и дать задаче ещё круг (если препятствие ушло или обходимо);",
                "close — задача больше не нужна; keep — без владельца правда нельзя СЕЙЧАС;",
                "park — ждёт условия (свежей проверки, другой задачи), а не владельца: система перепроверит "
                "сама через 72 ч. Просьба к владельцу «если X — сделай Y» — это park, а не keep.",
                "Владелец не знает ТЗ и не читает код: keep без owner_needs, понятного без контекста, — брак.",
                "Обратимый вариант (reactivate/close) применится сразу после принятия. Деньги, аккаунты,"
                " публикация не применяются никогда — их решает владелец по owner_needs.",
                "Нет телефона/стенда/устройства — это не keep: очередь ждёт ресурс сама (park или reactivate).",
                *_granted(tx, project, tz, str(roots.get(project) or "")),
                _RESULT_RULE,
            ])
            opened.append(f"{project}/" + _insert(
                tx, project, subject, f"Суждение: вариант решения по {tz}", body,
                [_abs(roots[project], rel)], 2))
            busy.add((project, subject))

    with store.tx() as tx:
        busy = _open_subjects(tx)

        def allowed(project: str, subject: str) -> bool:
            return (project in roots and (project, subject) not in busy
                    and _spent_today(tx, project, now) < DAILY_BUDGET)

        # 1. Отказы
        rejects: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in reject_rows:
            rejects.setdefault((str(row["project"]), task_root(str(row["tz"]))), []).append(dict(row))
        for (project, root_tz), rows in rejects.items():
            root = tasks.get((project, root_tz))
            if root is None or root["state"] not in OPEN_STATES or root["kind"] not in ("work", "fix", "request"):
                continue
            if not unchanged(tx, project, root_tz):
                continue
            subject = f"rejects:{root_tz}"
            if not allowed(project, subject):
                continue
            last = _last_triage(tx, project, subject)
            fresh = [r for r in rows if last is None or int(r["id"]) > int(last["id"])]
            if len(fresh) < REJECTS_N:
                continue
            notes = "\n".join(
                f"- {r['ts'][:16]}: " + str((_json(r["payload"]) or {}).get("note") or "")[:700] for r in fresh[-3:]
            )
            spec = str(root["spec_path"] or "")
            body = "\n".join([
                f"Задача-суждение. {project}/{root_tz} «{root['title']}» отклонена {len(fresh)} раз.",
                f"ТЗ: {spec or '(файла нет — только заявкой)'}",
                "", "Последние отказы:", notes, "",
                "Вопрос: почему круги не сходятся — ТЗ неясно или противоречиво, приёмка не проверяет то, "
                "что требует ревьюер, зона узка, задача слишком велика? Поправь ТЗ (разделы, owns, acceptance, "
                "needs) так, чтобы следующий круг сошёлся, или разрежь задачу.",
                _RESULT_RULE,
            ])
            opened.append(f"{project}/" + _insert(
                tx, project, subject, f"Суждение: почему {root_tz} отклоняют раз за разом", body,
                [_abs(roots[project], spec)], 1))
            busy.add((project, subject))
    return opened


# ---- применение итогов -------------------------------------------------------------------------

def _safe_name(tz: str) -> str:
    """Имя файла решения без `:` — на Windows двоеточие в пути читается как поток NTFS, и страж
    его отклоняет (09.10.2026: triage:10 по fix:TZ-441 не смог записать вердикт). Цель решения
    берётся из поля `tz:` внутри файла, поэтому имя только безопасное, не обратимое."""
    return tz.replace(":", "__")


def migrate_legacy_decision_paths(store: Any) -> list[str]:
    """Repair only old decide-triages whose one owned decision filename contains ':'.

    A taken triage may also have an active claim_paths snapshot; update it in
    the SAME transaction, or the file stays blocked even after tasks.owns is fixed.
    Old decisions for accepted/completed tasks and all non-decision owns are untouched.
    """
    changed: list[str] = []
    with store.tx() as tx:
        rows = tx.query(
            "SELECT t.project,t.tz,t.body,t.owns,p.root FROM tasks t "
            "JOIN projects p ON p.name=t.project "
            "WHERE t.kind='triage' AND t.state IN ('draft','ready','taken','submitted','frozen')"
        )
        for row in rows:
            project, task_tz = str(row["project"]), str(row["tz"])
            body = str(row["body"] or "")
            subject = _subject_of(body)
            if not subject.startswith("decide:"):
                continue
            target = subject[len("decide:"):]
            # Only migrate an unambiguous legacy decision owned by this task.
            if (not target or ":" not in target or "/" in target or "\\" in target
                    or target in (".", "..") or "\n" in target):
                continue
            old_rel = f"ToDo/decisions/{target}.md"
            new_rel = f"ToDo/decisions/{_safe_name(target)}.md"
            old_path = _abs(str(row["root"]), old_rel)
            new_path = _abs(str(row["root"]), new_rel)
            try:
                owns = json.loads(str(row["owns"] or "[]"))
            except (TypeError, ValueError):
                continue
            if (owns != [old_path] or body.count(old_rel) != 1
                    or f"tz: {target}" not in body):
                continue

            active_claims = tx.query(
                "SELECT id FROM claims WHERE project=? AND tz=? AND state='active'",
                (project, task_tz),
            )
            claim_ids = [int(r["id"]) for r in active_claims]
            # A live claim needs exactly its old path, not another worker's path.
            if any(
                [str(p["path_norm"]) for p in tx.query(
                    "SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim_id,)
                )].count(old_path) != 1
                for claim_id in claim_ids
            ):
                continue
            foreign = tx.query(
                "SELECT cp.path_norm FROM claim_paths cp JOIN claims c ON c.id=cp.claim_id "
                "WHERE c.state='active' AND NOT (c.project=? AND c.tz=?)",
                (project, task_tz),
            )
            if any(paths_overlap(new_path, str(p["path_norm"])) for p in foreign):
                continue

            statements = [
                (
                    "UPDATE tasks SET owns=?,body=?,updated_at=? WHERE project=? AND tz=?",
                    (json.dumps([new_path], ensure_ascii=False),
                     body.replace(old_rel, new_rel), _iso(_now()), project, task_tz),
                )
            ]
            statements.extend(
                (
                    "UPDATE claim_paths SET path_norm=? WHERE claim_id=? AND path_norm=?",
                    (new_path, claim_id, old_path),
                )
                for claim_id in claim_ids
            )
            tx.changes(statements, event={
                "kind": "triage_decision_path_migrate",
                "project": project,
                "tz": task_tz,
                "payload": {"from": old_rel, "to": new_rel, "active_claims": len(claim_ids)},
            })
            changed.append(f"{project}/{task_tz}")
    return changed


_TZ_FILE = re.compile(r"/todo/tz/(tz-[^/]+)\.md$", re.I)
_DECISION_FILE = re.compile(r"/todo/decisions/([^/]+)\.md$", re.I)
_LESSON_FILE = re.compile(r"/todo/lessons/([^/_][^/]*)\.md$", re.I)  # `_none.md` — «классов нет», не урок
_LESSON_REPORT = re.compile(r"/todo/lessons/\d{4}-\d{2}-\d{2}-[^/]*\.md$", re.I)  # датированные отчёты не YAML-уроки
_YAML = re.compile(r"```ya?ml\s*\n(.*?)\n```", re.S | re.I)


def _read_decision(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    m = _YAML.search(text)
    block = m.group(1) if m else text
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            out[key.strip().casefold()] = value.strip()
    return out


def _decision_problem(tx: Any, project: str, name: str, fields: dict[str, str]) -> str:
    """Validate the same decision fields consumed by _propose, without side effects."""
    target = fields.get("tz") or name
    action = fields.get("action", "").casefold()
    if not action:
        return f"{target}: пустой action"
    if action not in ACTIONS:
        return f"{target}: action «{action}» не из {ACTIONS}"
    if action == "park" and not (fields.get("until", "").strip() or fields.get("why", "").strip()):
        return f"{target}: park требует until или why"
    task = tx.one("SELECT state,acceptance FROM tasks WHERE project=? AND tz=?", (project, target))
    if task is None:
        return f"{target}: задачи нет"
    if str(task["state"]) == "taken" and action in ("reactivate", "close", "park"):
        return f"{target}: задача взята, действие {action} небезопасно"
    until = fields.get("until", "").strip()
    if until:
        acceptance = _json(task["acceptance"])
        if isinstance(acceptance, list) and any(
            " ".join(until.split()).casefold() == " ".join(str(cmd).split()).casefold()
            for cmd in acceptance
        ):
            return f"{target}: self-until повторяет собственную acceptance"
    return ""


def _preflight_unlink(store: Any, project: str, root: Path, path: Path) -> str:
    """Dry-run gates unlink against YAML headers; never write target specs."""
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
        match = _YAML.search(text)
        data = yaml.safe_load(match.group(1) if match else text) or {}
        unlink = data.get("unlink") or {}
        if not isinstance(unlink, dict):
            return f"{path.name}: unlink — не словарь TZ → [needs]"
        with store.read() as tx:
            specs = {str(r["tz"]): str(r["spec_path"] or "") for r in tx.query(
                "SELECT tz,spec_path FROM tasks WHERE project=?", (project,))}
            taken = {str(r["tz"]) for r in tx.query(
                "SELECT tz FROM tasks WHERE project=? AND state='taken'", (project,))}
        for tz, drop in unlink.items():
            tz = str(tz)
            if tz in taken:
                continue  # executor also defers an active target
            if not specs.get(tz):
                return f"{tz}: нет файла ТЗ"
            body = (root / specs[tz]).read_text(encoding="utf-8")
            header = _YAML.search(body)
            if header is None:
                return f"{tz}: отсутствует YAML-шапка"
            gates = {str(d) for d in (drop if isinstance(drop, list) else [drop])}
            _unlink_needs_header(header.group(1), gates)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
        return f"{path.name}: {exc}"
    return ""


def triage_preflight(
    store: Any, project: str, tz: str, worker_id: str, started_at: str,
    *, checkout_root: Path | None = None,
) -> str:
    """Read-only gate: inspect this claim's decisions before review and Git sealing."""
    with store.read() as tx:
        home = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        paths = [str(r["path_norm"]) for r in tx.query(
            "SELECT DISTINCT path_norm FROM events WHERE kind='write' AND worker_id=? "
            "AND project=? AND tz=? AND ts>=?",
            (worker_id, project, tz, started_at),
        )]
    if home is None:
        return f"{project}: проект отсутствует"
    root = Path(checkout_root) if checkout_root is not None else Path(str(home["root"]))
    for p in sorted(paths):
        match = _DECISION_FILE.search(p.replace("\\", "/"))
        if match is None:
            continue
        name = match.group(1)
        if name.casefold().startswith("watchdog-"):
            continue
        path = root / "ToDo" / "decisions" / f"{name}.md"
        if name.casefold().startswith("gates-"):
            error = _preflight_unlink(store, project, root, path)
        else:
            try:
                fields = _read_decision(path)
            except (OSError, UnicodeError, ValueError) as exc:
                return f"{name}: файл решения не читается: {exc}"
            with store.read() as tx:
                error = _decision_problem(tx, project, name, fields)
        if error:
            return error
    return ""


def _frozen_triage_repair_authorized(tx: Any, project: str, tz: str) -> bool:
    """Recover a failed gate unlink only after an independently accepted fix.

    A frozen triage is never sufficient authority by itself. The accepted fix
    must have written the gates decision before approval, the old apply must
    have failed *after* that approval, and an agent must have blocked this
    exact triage after the failure. A later reject, owner freeze or unreviewed
    gates edit vetoes the replay. If replay fails, its new error is after the
    block and automatically disables further attempts.
    """
    fix = f"fix:{tz}"
    task = tx.one(
        "SELECT state,kind FROM tasks WHERE project=? AND tz=?", (project, fix),
    )
    if task is None or str(task["state"]) != "accepted" or str(task["kind"]) != "fix":
        return False
    approved = int(tx.scalar(
        "SELECT COALESCE(MAX(id),0) FROM events WHERE kind='accept' AND project=? AND tz=?",
        (project, fix),
    ) or 0)
    approved_paths = {
        str(row["path_norm"] or "").replace("\\", "/").casefold()
        for row in tx.query(
            "SELECT DISTINCT path_norm FROM events WHERE kind='write' AND project=? AND tz=? "
            "AND id<? AND LOWER(REPLACE(path_norm, CHAR(92), '/')) "
            "LIKE '%/todo/decisions/gates-%.md'",
            (project, fix, approved),
        )
    } if approved else set()
    # The accepted fix must correspond to this triage's decision, not merely
    # any gates file written by the same fix family.
    original_paths = {
        str(row["path_norm"] or "").replace("\\", "/").casefold()
        for row in tx.query(
            "SELECT DISTINCT path_norm FROM events WHERE kind='write' AND project=? AND tz=? "
            "AND id<? AND LOWER(REPLACE(path_norm, CHAR(92), '/')) "
            "LIKE '%/todo/decisions/gates-%.md'",
            (project, tz, approved),
        )
    } if approved else set()
    approved_paths.intersection_update(original_paths)
    if not approved_paths:
        return False
    # An earlier accepted repair must not authorize a newly rejected decision,
    # an owner freeze, or a later unreviewed change to the gates file.
    if tx.one(
        "SELECT 1 FROM events WHERE project=? AND id>? AND "
        "((tz IN (?,?) AND kind IN ('reject','freeze')) OR "
        "(tz IN (?,?) AND kind='write' AND "
        "LOWER(REPLACE(path_norm, CHAR(92), '/')) LIKE '%/todo/decisions/gates-%.md')) "
        "LIMIT 1", (project, approved, tz, fix, tz, fix),
    ) is not None:
        return False
    # Another task's post-accept write to that same decision path invalidates
    # the accepted event chain too; do not trust just the original tz.
    for row in tx.query(
        "SELECT path_norm FROM events WHERE project=? AND id>? AND kind='write' "
        "AND LOWER(REPLACE(path_norm, CHAR(92), '/')) LIKE '%/todo/decisions/gates-%.md'",
        (project, approved),
    ):
        if str(row["path_norm"] or "").replace("\\", "/").casefold() in approved_paths:
            return False
    failed = int(tx.scalar(
        "SELECT COALESCE(MAX(id),0) FROM events WHERE kind='triage_apply_error' "
        "AND project=? AND tz=?", (project, tz),
    ) or 0)
    blocked = int(tx.scalar(
        "SELECT COALESCE(MAX(id),0) FROM events WHERE kind='block' AND project=? AND tz=?",
        (project, tz),
    ) or 0)
    return approved < failed < blocked


def apply_accepted(store: Any, *, sync: Callable[..., dict] | None = None) -> list[dict[str, Any]]:
    """Apply accepted triages, marking success only after all effects validate."""
    if sync is None:
        from .spec import sync_selected as sync
    done: list[dict[str, Any]] = []
    with store.read() as tx:
        rows = [dict(r) for r in tx.query(
            "SELECT t.project,t.tz,t.body,t.state,p.root FROM tasks t JOIN projects p ON p.name=t.project "
            "WHERE t.kind='triage' AND t.state IN ('accepted','frozen') AND "
            "(SELECT COALESCE(MAX(id),0) FROM events e WHERE e.kind='triage_applied' "
            "AND e.project=t.project AND e.tz=t.tz) <= "
            "(SELECT COALESCE(MAX(id),0) FROM events e WHERE e.kind='triage_apply_reopen' "
            "AND e.project=t.project AND e.tz=t.tz) AND "
            "(t.state='frozen' OR "
            "(SELECT COALESCE(MAX(id),0) FROM events e WHERE e.kind='triage_apply_error' "
            "AND e.project=t.project AND e.tz=t.tz) <= "
            "(SELECT COALESCE(MAX(id),0) FROM events e WHERE e.kind='triage_apply_reopen' "
            "AND e.project=t.project AND e.tz=t.tz))"
        )]
        rows = [r for r in rows if r["state"] == "accepted" or
                _frozen_triage_repair_authorized(tx, str(r["project"]), str(r["tz"]))]
        wrote: dict[tuple[str, str], set[str]] = {}
        for r in rows:
            chain = [str(x["tz"]) for x in tx.query("SELECT tz FROM tasks WHERE project=?", (r["project"],))
                     if task_root(str(x["tz"])) == r["tz"]]
            marks = ",".join("?" * len(chain))
            wrote[(r["project"], r["tz"])] = {
                str(x["path_norm"]) for x in tx.query(
                    f"SELECT DISTINCT path_norm FROM events WHERE kind='write' AND project=? AND tz IN ({marks})",
                    (r["project"], *chain))
            }
    for r in rows:
        project, tz, root = str(r["project"]), str(r["tz"]), str(r["root"])
        paths = wrote[(project, tz)]
        result: dict[str, Any] = {"subject": _subject_of(str(r["body"] or ""))}
        if r["state"] == "frozen":
            result["recovered_frozen"] = True
        tzs = []
        for p in sorted(paths):
            m = _TZ_FILE.search(p)
            if m:
                try:
                    from .spec import parse_spec
                    tzs.append(parse_spec(str(Path(root) / "ToDo" / "tz" / (Path(p).name))).tz)
                except Exception as exc:  # noqa: BLE001
                    result.setdefault("errors", []).append(f"{Path(p).name}: {exc}"[:300])
        with store.read() as tx:
            taken = {str(x["tz"]) for x in tx.query("SELECT tz FROM tasks WHERE project=? AND state='taken'", (project,))}
        for p in sorted(paths):
            m = _DECISION_FILE.search(p)
            if m and m.group(1).casefold().startswith("gates-"):
                decision = Path(root) / "ToDo" / "decisions" / f"{m.group(1)}.md"
                result.setdefault("decisions", []).append(str(decision))
                edited, skipped, errors = _apply_unlink(store, project, root, decision, taken)
                tzs.extend(edited)
                # A prior worker may have written the corrected YAML and died
                # before registering it. Reconcile that stale registry edge
                # without rewriting the spec or losing the accepted decision.
                stale = [t for t in _stale_unlink_registry_targets(
                    store, project, root, decision, taken) if t not in edited]
                tzs.extend(stale)
                result["unlinked"] = edited
                if stale:
                    result["registry_resync"] = stale
                if skipped:
                    result["skipped_taken"] = skipped
                if errors:
                    result.setdefault("errors", []).extend(errors)
        lesson_files = [Path(p) for p in sorted(paths)
                        if _LESSON_FILE.search(p) and not _LESSON_REPORT.search(p)]
        if lesson_files:
            from . import lessons
            try:
                ids, errors = lessons.register_files(store, project, lesson_files, tz)
                result["lessons"] = ids
                if errors:
                    result.setdefault("errors", []).extend(errors)
            except Exception as exc:  # noqa: BLE001 — transient Store/filesystem failure
                result.setdefault("errors", []).append(f"lesson registration: {exc}"[:300])
        resync = [t for t in tzs if t not in taken]
        if resync:
            try:
                out = sync(store, project, root, resync)
                result["synced"] = resync
                if out.get("errors"):
                    result.setdefault("errors", []).extend(f"{k}: {v}"[:300] for k, v in out["errors"].items())
            except Exception as exc:  # noqa: BLE001
                result.setdefault("errors", []).append(f"sync: {exc}"[:300])
        if [t for t in tzs if t in taken]:
            result["skipped_taken"] = [t for t in tzs if t in taken]
        for p in sorted(paths):
            m = _DECISION_FILE.search(p)
            # Watchdog reports are observations, not owner action proposals.
            # Parsing them as decisions produces an empty action and reopens
            # an already accepted triage through triage_apply_error.
            if m and not m.group(1).casefold().startswith(("gates-", "watchdog-")):
                proposed = _propose(store, project, root, m.group(1))
                result.setdefault("proposals", []).append(proposed)
                if re.search(
                    r"(?i)action «|пустой action|park требует|self-until|небезопасно|задачи нет|"
                    r"no such file|permission denied|отказано", proposed,
                ):
                    result.setdefault("errors", []).append(f"decision: {proposed}"[:300])
        with store.tx() as tx:
            errors = result.get("errors") or []
            if errors:
                accepted = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?",
                                  (project, tz))
                if accepted is not None and accepted["state"] == "accepted":
                    from .terminal import apply_failure_decision

                    version = str(tx.scalar(
                        "SELECT COALESCE(MAX(id),0) FROM events WHERE kind='accept' "
                        "AND project=? AND tz=?", (project, tz),
                    ) or 0)
                    plan = apply_failure_decision(
                        state="accepted", project=project, tz=tz, errors=errors,
                        accepted_version=version,
                    )
                    repair = plan.repair
                    if repair is not None:
                        from . import platform_manual
                        if platform_manual.enabled():
                            result['repair'] = platform_manual.enqueue(
                                tx,repair.dedup_key,title=repair.title,
                                detail=json.dumps(repair.evidence,ensure_ascii=False),
                                source_project=project,source_tz=tz,
                                owns=['src/dev_mcp/fleet/judgment.py','src/dev_mcp/fleet/tools.py'],
                                acceptance=['py -3 -m pytest -q tests/fleet/test_triage_decision_validation.py'])
                        platform = tx.one("SELECT root FROM projects WHERE name=?",
                                          (repair.project,))
                        if platform is not None and not platform_manual.enabled():
                            platform_root = Path(str(platform["root"]))
                            owns = [
                                str(platform_root / "src/dev_mcp/fleet/judgment.py"),
                                str(platform_root / "src/dev_mcp/fleet/tools.py"),
                                str(platform_root / "tests/fleet/test_triage_decision_validation.py"),
                            ]
                            body = (
                                f"Устранить ошибку применения принятой triage "
                                f"{project}/{tz}; принятую задачу не возвращать в ready.\n"
                                f"Fingerprint: {repair.dedup_key}\n"
                                f"Evidence: {json.dumps(repair.evidence, ensure_ascii=False)}\n"
                                "Воспроизвести отказ отдельным кусающим тестом; исправить "
                                "исполнитель/preflight и доказать применение до сдачи."
                            )
                            now = _iso(_now())
                            if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?",
                                      (repair.project, repair.tz)) is None:
                                tx.change(
                                    "INSERT INTO tasks(project,tz,title,state,priority,kind,"
                                    "spec_path,spec_sha,needs,owns,resources,acceptance,body,"
                                    "created_at,updated_at) VALUES(?,?,?,'ready',1,'work','','',"
                                    "'[]',?,'[]',?,?,?,?)",
                                    (repair.project, repair.tz, repair.title,
                                     json.dumps(owns, ensure_ascii=False),
                                     json.dumps(["py -3 -m pytest -q "
                                                 "tests/fleet/test_triage_decision_validation.py"]),
                                     body, now, now),
                                    event={"kind": "triage_apply_repair_open",
                                           "project": repair.project, "tz": repair.tz,
                                           "payload": {"fingerprint": repair.dedup_key,
                                                       **repair.evidence}},
                                )
                            result["repair"] = f"{repair.project}/{repair.tz}"
                        result["fingerprint"] = repair.dedup_key
                tx.record_event(
                    kind="triage_apply_error", project=project, tz=tz,
                    payload={"errors": errors, "fingerprint": result.get("fingerprint", ""),
                             "repair": result.get("repair", "")},
                )
                result["status"] = "repair"
            else:
                tx.record_event(kind="triage_applied", project=project, tz=tz, payload=result)
        done.append({"project": project, "tz": tz, **result})
    return done


_NEEDS_BLOCK_ITEM = re.compile(r"^[ \t]+-[ \t]+([^\r\n]*?)(?:[ \t]+#.*)?(?:\r?\n)?$")


def _unlink_needs_header(head: str, drop: set[str]) -> str:
    """Remove specified YAML needs without re-serializing or reformatting the header.

    Both flow ([TZ-1]) and block (- TZ-1) sequences occur in real specs.
    A missing needs key or an already-removed gate is an idempotent no-op.
    Unsupported/malformed needs remains a real error instead of a silent success.
    """
    import yaml

    data = yaml.safe_load(head) or {}
    if not isinstance(data, dict):
        raise ValueError("шапка YAML — не mapping")
    needs = data.get("needs", [])
    if needs is None and "needs" in data:
        raise ValueError("needs: ожидался список")
    if not isinstance(needs, list):
        raise ValueError("needs: ожидался список")
    if not drop.intersection(str(item).split("#", 1)[0].strip() for item in needs):
        return head

    # YAML node marks point at the original tokens (including quotes and multiline flow).
    # Edit only the token spans: serializing YAML would rewrite comments and the rest of
    # the spec header, while comma-splitting loses quoted/line-wrapped flow scalars.
    tree = yaml.compose(head)
    if isinstance(tree, yaml.nodes.MappingNode):
        for key, sequence in tree.value:
            if not (isinstance(key, yaml.nodes.ScalarNode) and key.value == "needs"):
                continue
            if not (isinstance(sequence, yaml.nodes.SequenceNode) and sequence.flow_style):
                break
            values = sequence.value
            removed = {
                index for index, element in enumerate(values)
                if isinstance(element, yaml.nodes.ScalarNode)
                and element.value.split("#", 1)[0].strip() in drop
            }
            if not removed:
                raise ValueError("needs: не удалось найти элемент в flow-списке")
            if len(removed) == len(values):
                left = sequence.start_mark.index + 1
                right = sequence.end_mark.index - 1
                return head[:left] + head[right:]
            spans: list[tuple[int, int]] = []
            index = 0
            while index < len(values):
                if index not in removed:
                    index += 1
                    continue
                first = index
                while index + 1 < len(values) and index + 1 in removed:
                    index += 1
                last = index
                if first == 0:
                    left = values[first].start_mark.index
                    right = values[last + 1].start_mark.index
                else:
                    # A flow comment between a kept item and the removed item
                    # belongs to the kept item. Keep its comma and comment;
                    # a trailing comma is valid YAML if the removed item was last.
                    gap = head[values[first - 1].end_mark.index:values[first].start_mark.index]
                    if "#" in gap:
                        left = values[first].start_mark.index
                        right = (
                            values[last + 1].start_mark.index
                            if last + 1 < len(values) else values[last].end_mark.index
                        )
                    else:
                        left = values[first - 1].end_mark.index
                        right = values[last].end_mark.index
                spans.append((left, right))
                index += 1
            updated = head
            for left, right in reversed(spans):
                updated = updated[:left] + updated[right:]
            return updated

    lines = head.splitlines(keepends=True)
    for index, raw in enumerate(lines):
        if not raw.startswith("needs:"):
            continue
        # Only a YAML block sequence is supported here; the flow form was
        # handled above. Do not replace unrelated YAML or the Markdown body.
        if raw[len("needs:"):].strip() not in ("",) and not raw[len("needs:"):].lstrip().startswith("#"):
            raise ValueError("needs: неподдерживаемый формат")
        end = index + 1
        while end < len(lines) and (lines[end][:1] in (" ", "\t") or not lines[end].strip()):
            end += 1
        retained: list[str] = []
        removed = 0
        for item_line in lines[index + 1:end]:
            if not item_line.strip() or item_line.lstrip().startswith("#"):
                retained.append(item_line)
                continue
            item = _NEEDS_BLOCK_ITEM.match(item_line)
            if item is None:
                raise ValueError("needs: ожидался YAML block-список")
            value = str(yaml.safe_load(item.group(1).strip())).split("#", 1)[0].strip()
            if value in drop:
                removed += 1
            else:
                retained.append(item_line)
        if not removed:
            raise ValueError("needs: не удалось найти элемент в block-списке")
        if not any(_NEEDS_BLOCK_ITEM.match(row) for row in retained):
            # Empty YAML block sequences are null, not []. Keep future unlinks
            # idempotent and spec parsers happy after the final gate is removed.
            raw_key = lines[index]
            newline = "\r\n" if raw_key.endswith("\r\n") else "\n" if raw_key.endswith("\n") else ""
            content = raw_key[:-len(newline)] if newline else raw_key
            key, sep, comment = content.partition("#")
            lines[index] = key.rstrip() + " []" + ("  #" + comment if sep else "") + newline
        return "".join(lines[:index + 1] + retained + lines[end:])
    raise ValueError("needs: не удалось найти ключ в шапке")


def resync_skipped(store: Any, *, sync: Callable[..., dict] | None = None) -> list[str]:
    """ТЗ, пропущенные при применении triage, потому что задача была взята, — перерегистрировать,
    как только захват кончится (07.10.2026: правка TZ-104 из triage:4 лежала на диске незаметной)."""
    if sync is None:
        from .spec import sync_selected as sync
    done: list[str] = []
    with store.read() as tx:
        rows = [dict(r) for r in tx.query(
            "SELECT e.id,e.project,e.tz,e.payload,p.root FROM events e JOIN projects p ON p.name=e.project "
            "WHERE e.kind='triage_applied' AND e.payload LIKE '%skipped_taken%' AND NOT EXISTS ("
            "SELECT 1 FROM events r WHERE r.kind='triage_resync' AND r.project=e.project AND r.tz=e.tz)")]
        states = {(str(r["project"]), str(r["tz"])): str(r["state"]) for r in tx.query("SELECT project,tz,state FROM tasks")}
    for r in rows:
        project = str(r["project"])
        waiting = [str(t) for t in (_json(r["payload"]) or {}).get("skipped_taken") or []]
        if any(states.get((project, t)) == "taken" for t in waiting):
            continue
        result: dict[str, Any] = {"synced": waiting}
        for decision in (_json(r["payload"]) or {}).get("decisions") or []:
            # Взятым задачам unlink файл не правил — применить сейчас (уже снятые needs не меняются).
            _edited, _skipped, errors = _apply_unlink(store, project, str(r["root"]), Path(decision), set())
            if errors:
                result.setdefault("errors", []).extend(errors)
        try:
            out = sync(store, project, str(r["root"]), waiting)
            if out.get("errors"):
                result["errors"] = [f"{k}: {v}"[:300] for k, v in out["errors"].items()]
        except Exception as exc:  # noqa: BLE001
            result["errors"] = [f"sync: {exc}"[:300]]
        with store.tx() as tx:
            tx.record_event(kind="triage_resync", project=project, tz=str(r["tz"]), payload=result)
        done.append(f"{project}/{r['tz']}")
    return done


def _apply_unlink(store: Any, project: str, root: str, path: Path, taken: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Файл `gates-*.md` с `unlink: {TZ-NN: [ворота]}` → правка строки `needs:` в шапках ТЗ.

    Правит сервер, а не агент: агент, державший файлы ТЗ всех задач за воротами, держал и сами
    задачи — разбор, призванный их пустить, их же и стопорил (07.10.2026, triage:4 Nova VPN)."""
    import yaml

    edited: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
        m = _YAML.search(text)
        data = yaml.safe_load(m.group(1) if m else text) or {}
    except Exception as exc:  # noqa: BLE001
        return [], [], [f"{path.name}: {exc}"[:300]]
    unlink = data.get("unlink") or {}
    if not isinstance(unlink, dict):
        return [], [], [f"{path.name}: unlink — не словарь TZ → [needs]"]
    with store.read() as tx:
        specs = {str(r["tz"]): str(r["spec_path"] or "") for r in tx.query(
            "SELECT tz,spec_path FROM tasks WHERE project=?", (project,))}
    for tz, drop in unlink.items():
        tz = str(tz)
        drop = {str(d) for d in (drop if isinstance(drop, list) else [drop])}
        if tz in taken:
            skipped.append(tz)
            continue
        if not specs.get(tz):
            errors.append(f"{tz}: нет файла ТЗ")
            continue
        spec = Path(root) / specs[tz]
        try:
            body = spec.read_bytes().decode("utf-8")
            yaml_head = _YAML.search(body)
            if yaml_head is None:
                errors.append(f"{tz}: отсутствует YAML-шапка")
                continue
            head = yaml_head.group(1)
            try:
                updated_head = _unlink_needs_header(head, drop)
            except (ValueError, yaml.YAMLError) as exc:
                errors.append(f"{tz}: {exc}"[:300])
                continue
            if updated_head == head:
                continue
            updated = body[:yaml_head.start(1)] + updated_head + body[yaml_head.end(1):]
            spec.write_bytes(updated.encode("utf-8"))
            edited.append(tz)
        except OSError as exc:
            errors.append(f"{tz}: {exc}"[:300])
    return edited, skipped, errors


def _stale_unlink_registry_targets(
    store: Any, project: str, root: str, decision: Path, taken: set[str],
) -> list[str]:
    """Resync needs already removed from YAML but still present in the registry.

    The source spec is authoritative. Never infer an unlink from a stale
    dashboard cluster, and never touch a taken task.
    """
    import yaml
    from .spec import parse_spec

    try:
        text = decision.read_text(encoding="utf-8")
        match = _YAML.search(text)
        proposed = yaml.safe_load(match.group(1) if match else text) or {}
        unlink = proposed.get("unlink") or {}
        if not isinstance(unlink, dict):
            return []  # _apply_unlink reports the malformed decision
    except (OSError, ValueError, yaml.YAMLError, AttributeError):
        return []
    with store.read() as tx:
        rows = {str(r["tz"]): dict(r) for r in tx.query(
            "SELECT tz,spec_path,needs FROM tasks WHERE project=?", (project,))}
    stale: list[str] = []
    for tz, gates in unlink.items():
        tz = str(tz)
        row = rows.get(tz)
        if tz in taken or not row or not row["spec_path"]:
            continue
        drop = {str(g) for g in (gates if isinstance(gates, list) else [gates])}
        old = _json(row["needs"])
        if not isinstance(old, list) or not drop.intersection(map(str, old)):
            continue
        try:
            spec = parse_spec(str(Path(root) / str(row["spec_path"])))
        except (OSError, ValueError, TypeError):
            continue  # only an independently valid source can override the DB
        if spec.tz == tz and not drop.intersection(spec.needs):
            stale.append(tz)
    return stale


def _propose(store: Any, project: str, root: str, name: str) -> str:
    path = Path(root) / "ToDo" / "decisions" / f"{name}.md"
    try:
        fields = _read_decision(path)
    except OSError as exc:
        return f"{name}: {exc}"
    target = fields.get("tz") or name
    action = fields.get("action", "").casefold()
    now = _now()
    with store.tx() as tx:
        error = _decision_problem(tx, project, name, fields)
        if error:
            return error
        task = tx.one("SELECT title,state FROM tasks WHERE project=? AND tz=?", (project, target))
        reasons = " ".join(str((_json(r["payload"]) or {}).get(k) or "") for r in tx.query(
            "SELECT payload FROM events WHERE project=? AND tz=? AND kind IN ('block','freeze','escalate','reject')",
            (project, target)) for k in ("note", "reason"))
        why = fields.get("why", "")
        hard = action == "keep" or (action != "park" and irreversible(str(task["title"]), reasons, why))
        tx.record_event(kind="proposal", project=project, tz=target, payload={
            "action": action, "why": why[:600], "reversible": not hard,
            "until": fields.get("until", "")[:400],
            "owner_needs": fields.get("owner_needs", "")[:400] if hard else "",
            "title": str(task["title"])[:160], "file": str(path), "state_at": str(task["state"]),
        })
    return f"{target}: {action}" + ("" if not hard else " (ждёт владельца)")


# ---- умолчания ---------------------------------------------------------------------------------

_OWNER_KINDS = ("thaw", "freeze", "reactivate", "approve", "owner_decide", "owner_grant",
                "default_undo", "accept", "reject")


def _open_proposal(tx: Any, project: str, tz: str) -> dict[str, Any] | None:
    row = tx.one(
        "SELECT id,ts,payload FROM events WHERE kind='proposal' AND project=? AND tz=? ORDER BY id DESC LIMIT 1",
        (project, tz),
    )
    if row is None:
        return None
    closed = tx.one(
        "SELECT 1 FROM events WHERE kind IN ('default_applied','proposal_void') AND project=? AND tz=? AND id>?",
        (project, tz, int(row["id"])),
    )
    return None if closed is not None else {**dict(row), "payload": _json(row["payload"]) or {}}


def apply_defaults(store: Any, *, now: datetime | None = None) -> list[str]:
    now = now or _now()
    applied: list[str] = []
    with store.tx() as tx:
        props = tx.query(
            "SELECT DISTINCT project,tz FROM events WHERE kind='proposal'"
        )
        for row in props:
            project, tz = str(row["project"]), str(row["tz"])
            prop = _open_proposal(tx, project, tz)
            if prop is None:
                continue
            payload = prop["payload"]
            task = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
            # Задача сдвинулась сама или владелец сказал своё — предложение больше не о текущем.
            moved = task is None or str(task["state"]) != payload.get("state_at") or tx.one(
                "SELECT 1 FROM events WHERE project=? AND tz=? AND id>? AND kind IN (%s)"
                % ",".join("?" * len(_OWNER_KINDS)), (project, tz, int(prop["id"]), *_OWNER_KINDS),
            ) is not None
            if moved:
                tx.record_event(kind="proposal_void", project=project, tz=tz,
                                payload={"why": "задача или владелец сдвинули её после предложения"})
                continue
            if not payload.get("reversible"):
                continue
            action = payload.get("action")
            new_state = {"reactivate": "ready", "close": "rejected", "park": "frozen"}.get(str(action))
            if new_state is None:
                continue
            prev = str(task["state"])
            if new_state == prev:
                continue
            tx.change(
                "UPDATE tasks SET state=?,updated_at=? WHERE project=? AND tz=?",
                (new_state, _iso(now), project, tz),
                event={"kind": "default_applied", "project": project, "tz": tz, "payload": {
                    "action": action, "from": prev, "to": new_state, "proposal_id": prop["id"],
                    "why": payload.get("why", ""), "undo": f'fleet.cmd undo "{project}/{tz}"',
                }},
            )
            applied.append(f"{project}/{tz}: {action}")
    return applied


def undo_default(store: Any, project: str, tz: str) -> str:
    with store.tx() as tx:
        row = tx.one(
            "SELECT id,payload FROM events WHERE kind='default_applied' AND project=? AND tz=? ORDER BY id DESC LIMIT 1",
            (project, tz),
        )
        if row is None:
            raise ValueError(f"{project}/{tz}: умолчаний не применялось")
        if tx.one("SELECT 1 FROM events WHERE kind='default_undo' AND project=? AND tz=? AND id>?",
                  (project, tz, int(row["id"]))) is not None:
            raise ValueError(f"{project}/{tz}: уже откачено")
        payload = _json(row["payload"]) or {}
        prev = str(payload.get("from") or "frozen")
        tx.change(
            "UPDATE tasks SET state=?,updated_at=? WHERE project=? AND tz=? AND state<>'taken'",
            (prev, _iso(_now()), project, tz),
            event={"kind": "default_undo", "project": project, "tz": tz,
                   "payload": {"restored": prev, "applied_id": row["id"]}},
        )
    return f"{project}/{tz}: умолчание «{payload.get('action')}» откачено, состояние снова {prev}"


def tick(store: Any, decisions: Decisions | None = pult_decisions) -> dict[str, Any]:
    from . import lessons

    return {
        "applied": apply_accepted(store),
        "resynced": resync_skipped(store),
        "defaults": apply_defaults(store),
        "opened": open_triages(store, decisions),
        "lessons": lessons.tick(store),
    }


def run_judgment(db_path: str | Path, stop_event: threading.Event, *, interval_s: float = INTERVAL_S) -> None:
    from .store import Store

    store = Store.open(str(db_path))
    stop_event.wait(60)  # не мешать холодному старту сервера
    while not stop_event.is_set():
        try:
            tick(store)
        except Exception as exc:  # noqa: BLE001 — фон обязан жить
            try:
                with store.tx() as tx:
                    tx.record_event(kind="judgment_error", payload={"error": f"{type(exc).__name__}: {exc}"[:500]})
            except Exception:
                pass
        stop_event.wait(interval_s)
