"""Уроки флота: отказ ревьюера при зелёной приёмке → класс дефекта → строка в памятке → проверка эффекта.

Зачем (07.10.2026, ToDo/2026-10-07-fleet-memory-tz.md). Флот много читал память и ничему не учился:
37% вердиктов — отказ при зелёной приёмке, каждый — дефект, который приёмка ТЗ пропустила, а пути
«опыт → правило» не было. Тормозило не отсутствие хранилища, а отсутствие замкнутого контура.

Устройство — один журнал, никаких счётчиков. Всё, что здесь «состояние», выводится из событий реестра:
  lesson_scan   — разбор одного отказа оракулом Jev: общий ли это класс (g), какой известный урок (lesson);
  lesson_add    — урок зарегистрирован (из файла `ToDo/lessons/*.md`, написанного агентом-майнером или
                  владельцем); lesson_retire — снят;
  lesson_shown  — какие уроки показаны в памятке задачи (по релевантности Jev).
Эффект урока (повторяется ли дефект у задач, которым он показан) считается запросом по этим событиям
(`effect`), поэтому ему нечему «разъезжаться».

Контур:
1. `scan` — каждый новый отказ задачи с приёмкой: Jev (один вызов, два вопроса) решает, СПОСОБ ли это
   ошибиться (g) и не известный ли это класс (choice по каталогу проекта). Известный — урок «сработал
   мимо» (дефект повторился, несмотря на строку в памятке); неизвестный и общий — в пул.
2. `open_mining` — пул проекта ≥ POOL_N → задача-суждение `lessons:<проект>` (judgment): агент
   кластеризует пул и пишет файл-урок. Кластеризация и формулировка — суждение LLM; оракул считает.
3. `register_files` — принятая triage → файлы уроков становятся событиями `lesson_add`.
4. `brief` — при выдаче задачи Jev отбирает ≤ SHOW_MAX уроков по релевантности; строка в памятке.
   Ответ инструмента — самое сильное указание агенту (tools._next_step), и сервер знает агента и проект:
   идентичность и домен выдачи решены конструкцией, а не фильтром поверх анонимной сессии прокси.
5. `effect` — доля повторов у показанных против непоказанных: «работает / не работает / неясно».
   «Не работает» — урок не лечится строкой памятки: его место в коде, тесте или шаблоне ТЗ (`fleet.cmd lessons`).
6. `export_candidates` — глобальные и эффективные уроки в формате memory-save для сна memory_service;
   в граф правило попадает только после подтверждения владельцем.

Всё необязательное: нет ключа Jev — контур спит (отказ не разбирается), сеть моргнула — повтор на
следующем тике; памятка без уроков — прежняя.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import jev

SCAN_BATCH = 12          # отказов за тик: ~0,4 с на вызов, фон не должен занимать минуты
NOTE_MIN = 80            # короче — не описание дефекта
GENERAL_MIN = 0.7        # P(«типовой механизм ошибки») — в пул (на 24 живых отказах отсекает ~половину)
KNOWN_MIN = 0.75         # уверенность choice, с которой отказ записывается в известный класс
RELEVANT_MIN = 0.7       # P(класс применим к задаче) — показывать; на 14 задачах Nova: 0,9+ у тех, где дефект был,
                         # 0,2–0,45 у посторонних, 0,5–0,7 — серая зона, в которую лучше промолчать
SCAN_TIMEOUT_S = 20.0    # холодный первый вызов Jev бывает ~11 с; фону спешить некуда
SHOW_MAX = 3
POOL_N = 5               # пул проекта, с которого стоит звать майнера
MINING_NOTES = 20        # отказов в одной задаче майнеру (по 700 знаков: ~14 тыс.)
MINING_COOLDOWN_H = 12.0
EXPOSED_MIN = 8          # вердиктов по показанным задачам, с которых эффект считается
WORKS_RATE = 0.10
FAILS_RATE = 0.25
MARK = "[fleet-lessons]"
TIERS = ("briefing", "code", "graph")
SCOPES = ("project", "global")

Ask = Callable[..., "dict[str, Any] | None"]

GENERAL_QUESTION = {
    "type": "noul",
    # Формулировка подобрана на 24 живых отказах (07.10.2026): мягкое «может ли повториться» давало
    # 0,8–0,9 почти всем (62 из 72) и не фильтровало ничего; перечень механизмов + явное «нет» раздвинули
    # оценки от 0,13 до 0,91.
    "instructions": (
        "Отказ ревьюера при зелёной приёмке. Дефект — следствие ТИПОВОЙ ошибки реализации, которую можно "
        "описать независимо от этой задачи: валидация входов/доменов, fail-open вместо fail-closed, код написан, "
        "но не подключён к боевому пути, проглоченные ошибки или очистка, проверка ослаблена ради зелёного, "
        "расхождение с контрактом/схемой, гонка/порядок операций, утечка приватных данных. Ответ НЕТ, если "
        "дефект — только то, что пункт ТЗ не выполнен или понят иначе, нужное не добавлено, или проблема "
        "в оформлении отчёта."
    ),
    "criteria": {"true": "типовой механизм ошибки", "false": "пункт ТЗ просто не выполнен / оформление"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _j(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


# ---- каталог -------------------------------------------------------------------------------------

def catalog(tx: Any, project: str | None = None) -> list[dict[str, Any]]:
    """Живые уроки, видимые проекту (свои + глобальные). Без project — все. Поздняя запись с тем же id
    заменяет раннюю; `_ev` — id события регистрации (по нему считается эффект и версия каталога)."""
    # Один проход в хронологии: add → retire → add оставляет урок живым (08.10.2026: раньше все retire
    # применялись после всех add, и повторная регистрация того же id молча исчезала).
    adds: dict[tuple[str, str], dict[str, Any]] = {}
    for row in tx.query("SELECT id,kind,project,payload FROM events "
                        "WHERE kind IN ('lesson_add','lesson_retire') ORDER BY id"):
        data = _j(row["payload"])
        key = (str(row["project"]), str(data.get("id")))
        if row["kind"] == "lesson_retire":
            adds.pop(key, None)
        elif data.get("id"):
            adds[key] = {**data, "_ev": int(row["id"]), "project": str(row["project"])}
    out = [item for item in adds.values()
           if project is None or item["project"] == project or item.get("scope") == "global"]
    return sorted(out, key=lambda item: item["_ev"])


# ---- 1. разбор отказов ---------------------------------------------------------------------------

def _known_question(lessons: list[dict[str, Any]]) -> dict[str, Any]:
    criteria: dict[str, str | None] = {str(item["id"]): str(item["class"]) for item in lessons}
    criteria["none"] = "ни один из перечисленных классов"
    return {
        "type": "choice",
        "instructions": "К какому из известных классов дефекта относится содержательный дефект из отказа ревьюера?",
        "criteria": criteria,
    }


def scan(store: Any, ask: Ask = jev.ask, *, limit: int = SCAN_BATCH) -> int:
    """Новые отказы задач с приёмкой → события lesson_scan. Нет ключа — ничего (контур спит);
    нет ответа от сети — стоп, повтор на следующем тике. Возвращает число разобранных."""
    if not jev.configured():
        return 0
    with store.read() as tx:
        mark = int(tx.scalar(
            "SELECT COALESCE(MAX(CAST(json_extract(payload,'$.ev') AS INTEGER)),0) FROM events "
            "WHERE kind='lesson_scan'") or 0)
        rows = [dict(r) for r in tx.query(
            "SELECT e.id,e.project,e.tz,e.worker_id,e.payload FROM events e "
            "JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
            "WHERE e.kind='reject' AND e.id>? AND e.tz NOT LIKE 'review:%' "
            "AND t.acceptance NOT IN ('[]','') ORDER BY e.id LIMIT ?", (mark, limit))]
        books = {p: catalog(tx, p) for p in {r["project"] for r in rows}}
    done = 0
    for row in rows:
        note = str(_j(row["payload"]).get("note") or "").strip()
        out: dict[str, Any] = {"ev": int(row["id"]), "g": None, "lesson": None, "p": None}
        if len(note) < NOTE_MIN:
            out["skip"] = "short"
        else:
            lessons = books[row["project"]]
            questions: dict[str, Any] = {"g": GENERAL_QUESTION}
            if lessons:
                questions["c"] = _known_question(lessons)
            answers = ask({"reject_note": note[:4000]}, questions, timeout_s=SCAN_TIMEOUT_S)
            g = jev.noul(answers, "g")
            if answers is None or g is None:
                break
            out["g"] = round(g, 3)
            picked = jev.choice(answers, "c") if lessons else None
            if picked and picked[0] != "none" and picked[1] >= KNOWN_MIN:
                out["lesson"], out["p"] = picked[0], round(picked[1], 3)
        with store.tx() as tx:
            tx.record_event(kind="lesson_scan", project=row["project"], tz=row["tz"],
                            worker_id=row["worker_id"], payload=out)
        done += 1
    return done


# ---- 2. пул и майнер -----------------------------------------------------------------------------

def _source_fingerprint(ids: Any) -> str:
    """Stable identity of original rejection events, independent of worker/review/notes."""
    if not isinstance(ids, (list, tuple)):
        return ""
    try:
        source = sorted({int(ev) for ev in ids})
    except (ValueError, TypeError):
        return ""
    if not source or source[0] <= 0:
        return ""
    return hashlib.sha256(",".join(map(str, source)).encode("ascii")).hexdigest()


def pool(tx: Any, project: str) -> list[dict[str, Any]]:
    """Отказы-«общие и неизвестные», которые ещё не отданы майнеру.

    Отдача — аренда, а не списание (08.10.2026): случай занят, пока задача-майнер открыта, и списан,
    когда её итог применён (`triage_applied`). Задача закрылась без применения — случаи вернулись в пул."""
    from . import judgment

    opens = [(int(r["id"]), str(r["tz"] or ""), _j(r["payload"])) for r in tx.query(
        "SELECT id,tz,payload FROM events WHERE kind='triage_open' AND project=? ORDER BY id", (project,))]
    applied = [(int(r["id"]), str(_j(r["payload"]).get("subject"))) for r in tx.query(
        "SELECT id,payload FROM events WHERE kind='triage_applied' AND project=?", (project,))]
    accepted = {str(r["tz"]) for r in tx.query(
        "SELECT tz FROM tasks WHERE project=? AND kind='triage' AND state='accepted'", (project,))}
    # A bootstrap/reimport can reset tasks.state; an accepted review's verdict
    # is a durable event, and its original rejection IDs must stay retired.
    verdicts = {(str(r["tz"]), int(r["id"])) for r in tx.query(
        "SELECT tz,id FROM events WHERE kind='accept' AND project=?", (project,))}
    open_now = {s for p, s in judgment._open_subjects(tx) if p == project}
    latest: dict[str, int] = {}
    for eid, _tz, data in opens:
        latest[str(data.get("subject"))] = eid
    taken: set[int] = set()
    for eid, tz, data in opens:
        subject = str(data.get("subject"))
        consumed = any(aid > eid and s == subject for aid, s in applied)
        leased = subject in open_now and latest.get(subject) == eid
        # An accepted miner has finished its assigned batch even while the
        # asynchronous triage_applied hook is pending.  Retire only the IDs
        # actually included in that batch: older unshown rejects still belong
        # to the pool and must be available to the next miner.
        accepted_batch = subject == f"lessons:{project}" and (
            tz in accepted or any(vtz == tz and vid > eid for vtz, vid in verdicts)
        )
        if consumed or leased or accepted_batch:
            taken.update(int(x) for x in data.get("pool") or [])
    items = []
    for row in tx.query("SELECT payload FROM events WHERE kind='lesson_scan' AND project=? ORDER BY id", (project,)):
        data = _j(row["payload"])
        g = data.get("g")
        if (data.get("lesson") or data.get("skip") or g is None or g < GENERAL_MIN
                or int(data["ev"]) in taken):
            continue
        items.append(data)
    return items


def open_mining(store: Any, *, now: datetime | None = None) -> list[str]:
    """Пул проекта достаточен → задача-суждение «выдели классы дефектов» (через общий механизм judgment)."""
    from . import judgment

    now = now or _now()
    opened: list[str] = []
    with store.read() as tx:
        roots = {str(r["name"]): str(r["root"]) for r in tx.query("SELECT name,root FROM projects WHERE active=1")}
    for project, root in roots.items():
        with store.tx() as tx:
            items = pool(tx, project)
            if len(items) < POOL_N:
                continue
            subject = f"lessons:{project}"
            if (project, subject) in judgment._open_subjects(tx):
                continue
            if judgment._spent_today(tx, project, now) >= judgment.DAILY_BUDGET:
                continue
            last = judgment._last_triage(tx, project, subject)
            if last is not None and now - judgment._parse(last["ts"]) < timedelta(hours=MINING_COOLDOWN_H):
                continue
            shown = items[-MINING_NOTES:]  # остальные останутся в пуле до следующего круга
            ids = [int(i["ev"]) for i in shown]
            fingerprint = _source_fingerprint(ids)
            # Extra protection for old registries that reimport accepted
            # triage tasks after a review: compare source IDs, not notes,
            # worker IDs or a freshly allocated triage number. Closed but
            # rejected/unfinished attempts are intentionally retryable.
            previous = tx.query(
                "SELECT e.id,e.tz,e.payload,t.state FROM events e "
                "LEFT JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
                "WHERE e.kind='triage_open' AND e.project=? ORDER BY e.id", (project,),
            )
            duplicate = False
            for entry in previous:
                data = _j(entry["payload"])
                if data.get("subject") != subject:
                    continue
                old_fp = str(data.get("pool_fingerprint") or "") or _source_fingerprint(data.get("pool"))
                if old_fp != fingerprint:
                    continue
                review_accepted = tx.one(
                    "SELECT 1 FROM events WHERE kind='accept' AND project=? AND tz=? AND id>? LIMIT 1",
                    (project, entry["tz"], entry["id"]),
                )
                if entry["state"] == "accepted" or review_accepted is not None:
                    duplicate = True
                    break
            if duplicate:
                continue
            notes = []
            for item in shown:
                row = tx.one("SELECT ts,tz,payload FROM events WHERE id=?", (item["ev"],))
                if row is not None:
                    notes.append(f"- [{item['ev']}] {row['ts'][:10]} {row['tz']}: "
                                 + str(_j(row["payload"]).get("note") or "")[:700])
            known = [f"- {x['id']}: {x['class']}" for x in catalog(tx, project)]
            Path(root, "ToDo", "lessons").mkdir(parents=True, exist_ok=True)
            body = "\n".join([
                f"Задача-суждение. В {project} ревьюер {len(items)} раз отклонил работу, которую штатная приёмка "
                "пропустила; оракул счёл эти отказы общими способами ошибиться, а не разовыми недоделками.",
                "", "Отказы (id события — задача — что нашёл ревьюер):", *notes, "",
                "Известные классы проекта (не дублируй):", *(known or ["- (пока нет)"]), "",
                "Вопрос: какие из отказов — один и тот же способ ошибиться (минимум два отказа на класс)? "
                "На каждый такой класс — ОДИН файл `ToDo/lessons/<id>.md` с блоком:",
                "```yaml",
                "id: латиницей-через-дефис   # имя класса, не задачи",
                "class: одна фраза — КАК именно здесь ошибаются, без привязки к конкретной задаче",
                "rule: что делать или не делать, 1–2 предложения; конкретное поведение, не «будь внимателен»",
                "check: как проверить себя за минуту до сдачи (команда, вопрос, контрпример)",
                "trigger: по каким признакам задачи класс вероятен (что она трогает)",
                "tier: briefing | code | graph",
                "scope: project | global",
                "evidence: [id событий из списка выше]",
                "```",
                "tier: code — если дефект закрывается проверкой в самом проекте (тест, линтер, шаблон ТЗ): "
                "строкой в памятке такой класс не лечится, и его заберёт архитектор; briefing — только "
                "поведение; graph — то же, но верно для любого проекта (поставь и scope: global).",
                "Одиночные отказы без повторяющегося класса в уроки не превращай. Если ни одного класса нет — "
                "один файл `ToDo/lessons/_none.md` с `class: none` и строкой why: почему.",
                "Файлы уроков вне `ToDo/lessons/` не пиши, код проекта и ТЗ не правь. В note сдачи — одно "
                "предложение: сколько классов выделено.",
            ])
            tz = judgment._insert(tx, project, subject, f"Суждение: классы дефектов, которые пропускает приёмка ({project})",
                                  body, [judgment._abs(root, "ToDo/lessons/**")], 2,
                                  extra={"pool": ids, "pool_fingerprint": fingerprint})
            opened.append(f"{project}/{tz}")
    return opened


# ---- 3. регистрация ------------------------------------------------------------------------------

_YAML = re.compile(r"```ya?ml\s*\n(.*?)\n```", re.S | re.I)
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,48}$")


def parse_lesson(path: Path) -> dict[str, Any]:
    """Файл урока → поля. Бросает ValueError с причиной: она уходит в результат triage."""
    text = path.read_text(encoding="utf-8")
    m = _YAML.search(text)
    fields: dict[str, str] = {}
    for line in (m.group(1) if m else text).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            fields[key.strip().casefold()] = re.sub(r"\s+#\s.*$", "", value).strip()
    if fields.get("class", "").casefold() == "none":
        return {}
    lesson = {k: fields.get(k, "") for k in ("id", "class", "rule", "check", "trigger", "tier", "scope")}
    lesson["tier"] = lesson["tier"] or "briefing"
    lesson["scope"] = lesson["scope"] or "project"
    lesson["evidence"] = [int(x) for x in re.findall(r"\d+", fields.get("evidence", ""))]
    problems = []
    if not _ID.match(lesson["id"]) or lesson["id"] == "none":
        problems.append("id: латиница/цифры/дефис, 3–49 знаков, не none")
    for key, size in (("class", 15), ("rule", 20), ("trigger", 10)):
        if len(lesson[key]) < size:
            problems.append(f"{key}: короче {size} знаков")
    # Текст урока читают все следующие агенты проекта: потолок длины ограничивает и шум, и вред от
    # плохо сформулированного (или подсунутого) урока. Видно владельцу — `fleet.cmd lessons`, снять — retire.
    for key, cap in (("class", 220), ("rule", 360), ("check", 240), ("trigger", 260)):
        if len(lesson[key]) > cap:
            problems.append(f"{key}: длиннее {cap} знаков — сожми до сути")
    if lesson["tier"] not in TIERS:
        problems.append(f"tier: один из {', '.join(TIERS)}")
    if lesson["scope"] not in SCOPES:
        problems.append(f"scope: один из {', '.join(SCOPES)}")
    if problems:
        raise ValueError("; ".join(problems))
    return lesson


def register(store: Any, project: str, lesson: dict[str, Any], source: str = "") -> None:
    with store.tx() as tx:
        tx.record_event(kind="lesson_add", project=project, tz=source, payload=lesson)


def register_files(store: Any, project: str, paths: list[Path], source: str = "") -> tuple[list[str], list[str]]:
    ids, errors = [], []
    for path in paths:
        try:
            lesson = parse_lesson(path)
            if lesson:
                register(store, project, lesson, source)
                ids.append(lesson["id"])
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}"[:300])
    return ids, errors


def retire(store: Any, project: str, lesson_id: str) -> bool:
    with store.tx() as tx:
        if not any(x["id"] == lesson_id and x["project"] == project for x in catalog(tx)):
            return False
        tx.record_event(kind="lesson_retire", project=project, payload={"id": lesson_id})
    return True


# ---- 4. доставка в памятку -----------------------------------------------------------------------

def _task_text(task: Any) -> str:
    return "\n".join([
        str(task.title), str(task.body or "")[:1800],
        "owns: " + ", ".join(task.owns[:8]), "acceptance: " + "; ".join(task.acceptance[:6]),
    ])


def _relevance_question(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": (
            f"Известный класс дефекта: «{item['class']}». Признак: {item['trigger']}. "
            "Относится ли задача к такой работе, где этот дефект возможен?"
        ),
        "criteria": {"true": "класс применим к задаче", "false": "задача про другое"},
    }


def _selection(store: Any, task: Any, worker_id: str | None, ask: Ask) -> list[str]:
    with store.read() as tx:
        lessons = [x for x in catalog(tx, task.project) if x.get("tier") != "code"]
        version = max((x["_ev"] for x in lessons), default=0)
        last = tx.one("SELECT payload FROM events WHERE kind='lesson_shown' AND project=? AND tz=? "
                      "ORDER BY id DESC LIMIT 1", (task.project, task.tz))
    if not lessons:
        return []
    cached = _j(last["payload"]) if last is not None else {}
    if cached.get("v") == version:
        return list(cached.get("lessons") or [])
    answers = ask({"task": _task_text(task)}, {str(x["id"]): _relevance_question(x) for x in lessons})
    scored = {str(x["id"]): jev.noul(answers, str(x["id"])) for x in lessons}
    if answers is None or all(p is None for p in scored.values()):
        # Оракул недоступен: лучше показать недавние уроки, чем ничего. Показ записываем (эффект должен его
        # видеть), но без версии каталога — кэшем он не станет, в следующий раз спросим снова.
        fallback = [str(x["id"]) for x in lessons[-2:]]
        with store.tx() as tx:
            tx.record_event(kind="lesson_shown", project=task.project, tz=task.tz, worker_id=worker_id,
                            payload={"v": None, "lessons": fallback, "fallback": True})
        return fallback
    chosen = sorted((i for i, p in scored.items() if p is not None and p >= RELEVANT_MIN), key=lambda i: -scored[i])
    chosen = chosen[:SHOW_MAX]
    with store.tx() as tx:
        tx.record_event(kind="lesson_shown", project=task.project, tz=task.tz, worker_id=worker_id,
                        payload={"v": version, "lessons": chosen,
                                 "p": {i: round(p, 3) for i, p in scored.items() if p is not None}})
    return chosen


def brief(store: Any, task: Any, worker_id: str | None = None, ask: Ask = jev.ask) -> str:
    """Блок «уроки проекта» для памятки. Любой сбой — пустая строка: урок не условие выдачи задачи."""
    if task is None or task.kind == "triage":
        return ""
    try:
        ids = _selection(store, task, worker_id, ask)
        if not ids:
            return ""
        with store.read() as tx:
            by_id = {str(x["id"]): x for x in catalog(tx, task.project)}
        head = ("Классы дефектов, которые приёмка в этом проекте пропускала, — проверь на них в первую очередь:"
                if task.kind == "review" else
                "Уроки проекта — классы дефектов, которые ревьюер уже находил при зелёной приёмке; "
                "проверь себя до сдачи (не про твою задачу — пропусти):")
        lines = [head]
        for i in ids:
            item = by_id.get(i)
            if item:
                check = f" Проверь: {item['check']}" if item.get("check") else ""
                lines.append(f"  - {item['class']}. {item['rule']}{check}")
        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception:  # noqa: BLE001
        return ""


# ---- 5. эффект -----------------------------------------------------------------------------------

def effect(tx: Any, item: dict[str, Any]) -> dict[str, Any]:
    """Повторяется ли дефект класса у задач, которым урок показан. Только по журналу, без счётчиков.

    Исход засчитывается «с показом», только если показ был РАНЬШЕ этого исхода у той же задачи; сравнение
    — внутри области урока (свой проект или все, если урок глобальный). 08.10.2026: раньше любой вердикт
    задачи, которой урок когда-либо показали, шёл в «с показом», а «без показа» смешивал чужие проекты."""
    since = int(item["_ev"])
    own = str(item["project"])
    is_global = item.get("scope") == "global"
    first_shown: dict[tuple[str, str], int] = {}
    for r in tx.query("SELECT id,project,tz,payload FROM events WHERE kind='lesson_shown' AND id>? ORDER BY id",
                      (since,)):
        if item["id"] in (_j(r["payload"]).get("lessons") or []):
            first_shown.setdefault((str(r["project"]), str(r["tz"])), int(r["id"]))

    def exposed(project: str, tz: str, at: int) -> bool:
        shown_at = first_shown.get((project, tz))
        return shown_at is not None and shown_at < at

    exp_n = unexp_n = exp_hit = unexp_hit = 0
    for r in tx.query("SELECT id,project,tz FROM events WHERE kind IN ('accept','reject') AND id>? "
                      "AND tz NOT LIKE 'review:%'", (since,)):
        if not (is_global or str(r["project"]) == own):
            continue
        if exposed(str(r["project"]), str(r["tz"]), int(r["id"])):
            exp_n += 1
        else:
            unexp_n += 1
    for r in tx.query("SELECT id,project,tz,payload FROM events WHERE kind='lesson_scan' AND id>?", (since,)):
        data = _j(r["payload"])
        if data.get("lesson") != item["id"] or not (is_global or str(r["project"]) == own):
            continue
        at = int(data.get("ev") or r["id"])  # момент самого отказа, а не его разбора
        if at <= since:
            continue  # отказ старше урока (разобран позже из бэклога) — не повтор, а исходный материал
        if exposed(str(r["project"]), str(r["tz"]), at):
            exp_hit += 1
        else:
            unexp_hit += 1
    rate = exp_hit / exp_n if exp_n else None
    if exp_n < EXPOSED_MIN:
        verdict = "мало данных"
    elif rate is not None and rate <= WORKS_RATE:
        verdict = "работает"
    elif rate is not None and rate >= FAILS_RATE:
        verdict = "не работает"
    else:
        verdict = "неясно"
    return {"shown": len(first_shown), "verdicts": exp_n, "repeats": exp_hit,
            "unshown_verdicts": unexp_n, "unshown_repeats": unexp_hit, "verdict": verdict}


class ConnTx:
    """Тот же интерфейс чтения (`query/one/scalar`), что у Txn, поверх готового sqlite3-соединения: пульт и
    хук SessionStart читают реестр напрямую, без Store."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def query(self, sql: str, params: tuple = ()) -> list[Any]:
        return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> Any:
        return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        row = self._conn.execute(sql, params).fetchone()
        return row[0] if row is not None else None


def attention(tx: Any) -> list[str]:
    """Что из уроков требует архитектора: класс не лечится строкой памятки или сам просится в код.
    Владельцу здесь решать нечего (в граф урок идёт через повестку сна)."""
    out = []
    for x in catalog(tx):
        s = effect(tx, x)
        if x.get("tier") == "code":
            out.append(f"урок в код: {x['project']}/{x['id']} — {x['class']}")
        elif s["verdict"] == "не работает":
            out.append(f"урок не работает: {x['project']}/{x['id']} (повторов {s['repeats']} из {s['verdicts']}) — "
                       "вынести в код/тест/шаблон ТЗ")
    return out


def report(store: Any) -> list[str]:
    with store.read() as tx:
        return report_tx(tx)


def report_tx(tx: Any) -> list[str]:
    """Отчёт поверх любого читающего tx: пульт открывает реестр только на чтение (`ConnTx`), потому что
    `Store.open` гоняет миграции под замком записи и падает `database is locked` при живом флоте."""
    items = catalog(tx)
    stats = {(x["project"], x["id"]): effect(tx, x) for x in items}
    pools = {str(r["name"]): len(pool(tx, str(r["name"]))) for r in tx.query("SELECT name FROM projects WHERE active=1")}
    scanned = int(tx.scalar("SELECT COUNT(*) FROM events WHERE kind='lesson_scan'") or 0)
    out = [f"уроки: разобрано отказов {scanned}, в пуле " + (", ".join(f"{p} {n}" for p, n in pools.items() if n) or "пусто")
           + ("" if jev.configured() else " — ключа Jev нет, контур спит")]
    for x in items:
        s = stats[(x["project"], x["id"])]
        where = "код" if x.get("tier") == "code" else "памятка"
        out.append(
            f"  {x['project']}/{x['id']} [{where}, {x.get('scope')}] {s['verdict']}: показан {s['shown']} задачам, "
            f"повторов {s['repeats']} из {s['verdicts']} вердиктов (у непоказанных {s['unshown_repeats']} из "
            f"{s['unshown_verdicts']}) — {x['class']}"
            + (" → НЕ ЛЕЧИТСЯ СТРОКОЙ: вынести в код/тест/шаблон ТЗ" if s["verdict"] == "не работает" else ""))
    return out


# ---- 6. кандидаты в граф -------------------------------------------------------------------------

def candidates(store: Any) -> list[dict[str, Any]]:
    """Уроки для сна memory_service в формате memory-save: глобальные или помеченные graph, не провалившиеся.
    Подтверждает владелец; в граф сервер сам ничего не пишет."""
    out = []
    with store.read() as tx:
        for x in catalog(tx):
            if x.get("tier") == "code" or not (x.get("scope") == "global" or x.get("tier") == "graph"):
                continue
            s = effect(tx, x)
            if s["verdict"] == "не работает":
                continue
            out.append({
                "fleet_lesson": f"{x['project']}/{x['id']}",
                "name": x["class"][:120],
                "behavior": x["rule"],
                "reason_incident": (f"Флот dev-mcp, проект {x['project']}: ревьюер находил этот класс дефекта при зелёной "
                                    f"приёмке (отказы-доказательства: события {x.get('evidence') or '—'}); "
                                    f"после показа в памятке: {s['verdict']} ({s['repeats']} повторов из {s['verdicts']})."),
                "trigger": x["trigger"],
                "scope": "Агенты флота и любые сессии, правящие код проекта; "
                         + ("любой проект." if x.get("scope") == "global" else f"проект {x['project']}."),
                "effect": s,
            })
    return out


def export_candidates(store: Any, path: Path) -> bool:
    """Записывает файл, только если содержимое изменилось. Возвращает, менялся ли файл."""
    text = json.dumps(candidates(store), ensure_ascii=False, indent=1)
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


# ---- фон -----------------------------------------------------------------------------------------

def tick(store: Any, *, ask: Ask = jev.ask) -> dict[str, Any]:
    """Один проход фона (зовётся из judgment.tick): разбор отказов → майнер → файл кандидатов."""
    result: dict[str, Any] = {}
    for name, fn in (
        ("scanned", lambda: scan(store, ask)),
        ("mining", lambda: open_mining(store)),
        ("exported", lambda: export_candidates(store, Path(store.path).parent / "lesson-candidates.json")),
    ):
        try:
            result[name] = fn()
        except Exception as exc:  # noqa: BLE001 — фон обязан жить
            result[name] = f"error: {type(exc).__name__}: {exc}"[:200]
    return result
