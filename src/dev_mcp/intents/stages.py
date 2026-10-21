"""Stage playbooks and the server-side stage prover for intent projects.

spawn.py gives every stage task a real assignment (this playbook), an
acceptance command and the whole project as its zone. The prover closes the
loop that spawn left open: an ACCEPTED stage task is re-verified by the trusted
checker; pass -> server evidence -> outcome closed -> controller unfreezes the
next stage. Failure reopens the task with the checker's words, at most
MAX_REOPEN times; after that the stage is handed to the controller's generic
"plan:proof" research step (change the method), never retried blindly.
"""
from __future__ import annotations

from dev_mcp import machine

import json
import logging
import shutil
import re
import time
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from . import stage_check

LOG = logging.getLogger(__name__)
MAX_REOPEN = 3
PROVER_INTERVAL_S = 60.0
OWNER_POLL_INTERVAL_S = 300.0
LIVE_FLEET_DB = machine.FLEET_DB
CHECKER_REL = Path(".intent") / "check.py"


ProblemKind = Literal["invalid_result", "external_pending", "platform_error"]


@dataclass(frozen=True)
class StageProblem:
    """An author-repairable defect is not a service outage or a prover fault."""

    kind: ProblemKind
    detail: str
    digest: str = ""


def _last_external_event(store: Any, project: str, tz: str):
    with store.read() as tx:
        return tx.one(
            "SELECT kind,ts,payload FROM events WHERE kind IN "
            "('intent.stage_waiting_external','intent.stage_external_resumed') "
            "AND project=? AND tz=? ORDER BY id DESC LIMIT 1",
            (project, tz),
        )


def _external_resumed(store: Any, project: str, tz: str, digest: str) -> None:
    previous = _last_external_event(store, project, tz)
    if previous and previous["kind"] == "intent.stage_waiting_external":
        with store.tx() as tx:
            tx.record_event(
                kind="intent.stage_external_resumed", project=project, tz=tz,
                payload={"kind": "external_pending", "digest": digest,
                         "previous": json.loads(previous["payload"]).get("reason", "")},
            )


def _external_pending(store: Any, project: str, tz: str, digest: str,
                      service_state: str, reason: str) -> StageProblem:
    previous = _last_external_event(store, project, tz)
    attempt = 1
    if previous and previous["kind"] == "intent.stage_waiting_external":
        before = json.loads(previous["payload"])
        if before.get("digest") == digest and before.get("service_state") == service_state:
            attempt = min(16, int(before.get("attempt", 1)) + 1)
    with store.tx() as tx:
        tx.record_event(
            kind="intent.stage_waiting_external", project=project, tz=tz,
            payload={"kind": "external_pending", "digest": digest,
                     "service_state": service_state, "reason": reason[:500],
                     "attempt": attempt, "backoff_s": min(3600, 30 * 2 ** attempt)},
        )
    return StageProblem("external_pending", reason, digest)


def _platform_exception(store: Any, row: Any, exc: Exception, trace: str) -> None:
    """Persist one actionable platform repair per failure signature, never reopen author."""
    import hashlib

    project, tz = str(row["project"]), str(row["tz"])
    fingerprint = hashlib.sha256(
        (project + "\0" + tz + "\0" + type(exc).__name__ + "\0" + str(exc)
         + "\0" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        .encode("utf-8")).hexdigest()[:16]
    repair_tz = f"repair:stage-prover:{fingerprint}"
    with store.tx() as tx:
        from ..fleet import platform_manual
        if platform_manual.enabled():
            repair = platform_manual.enqueue(tx,fingerprint,title='Repair intent stage prover exception',
                                             detail=trace,source_project=project,source_tz=tz,
                                             owns=['src/dev_mcp/intents/stages.py','src/dev_mcp/intents/stage_check.py'],
                                             acceptance=['py -3 -m pytest -q tests/intents/test_stage_problem_kinds.py'])
            tx.record_event(kind='intent.stage_platform_error',project=project,tz=tz,
                            payload={'kind':'platform_error','repair':repair,'error':str(exc)})
            return
        existing = tx.one(
            "SELECT tz FROM tasks WHERE project='Fleet Platform' AND tz=?",
            (repair_tz,))
        if existing:
            return
        platform = tx.one("SELECT root FROM projects WHERE name='Fleet Platform'")
        if platform is not None:
            source = str(Path(platform["root"]) / "src" / "dev_mcp" / "intents" / "stages.py")
            test = str(Path(platform["root"]) / "tests/intents/test_stage_problem_kinds.py")
            checker = str(Path(platform["root"]) / "src/dev_mcp/intents/stage_check.py")
            now = _now()
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES('Fleet Platform',?,?,'ready',-10,'work','','','[]',?,'[]',?, ?,?,?)",
                (repair_tz, "Repair intent stage prover exception",
                 json.dumps([source, checker, test]),
                 json.dumps(["py -3 -m pytest -q tests/intents/test_stage_problem_kinds.py"]),
                 f"[stage-prover-repair] Failed stage: {project}/{tz}\n"
                 f"Exception: {type(exc).__name__}: {exc}\n"
                 f"Traceback:\n{trace[-12000:]}\n"
                 "Repair the server, not the stage author; prove recovery with tests.",
                 now, now),
                event={"kind": "intent.stage_repair_created", "project": "Fleet Platform",
                       "tz": repair_tz,
                       "payload": {"source_project": project, "source_tz": tz}},
            )
        tx.record_event(
            kind="intent.stage_platform_error", project=project, tz=tz,
            payload={"kind": "platform_error", "repair": repair_tz if platform else "",
                     "error": f"{type(exc).__name__}: {exc}"[:500], "traceback": trace[-12000:]},
        )

COMMON = """\
Ты ведёшь одну стадию интенции владельца. Текст интенции — `.intent/intent.md` (дословно).
Всё, что делаешь, пиши в папку проекта; служебные документы стадии — в `intent/`.
На стадии understood выбери `kind: build|change|investigate|ops` и
`target: new` (только build) либо абсолютный путь уже существующего проекта.
Запиши эти две строки ровно по одному разу в `intent/understood.md`.
Там же одной строкой `size: small|medium|large` — объём работы, а не важность:
small — понятно что делать и как проверить, одна-две правки или документ (интенция
пойдёт одной стадией «сделать и проверить»); medium — нужен выбор подхода; large —
новая система, несколько компонентов или неизвестный способ. Сомневаешься — меньший:
недооценку исправит следующая итерация, переоценка тратит часы флота.
Владельцу не задавай вопросов: неясное закрывай разумным обратимым умолчанием и запиши его
в «Неясности»; к владельцу идёт только то, что может он один (руки, вкус в необратимом,
деньги, доступ к личным данным) — это раздел «Нужно от владельца» в `intent/understood.md`.
Перед решениями сверяйся с доступной картой пульта `{map_path}`, `{stack_path}`,
`{decisions_path}` и подключённой памятью (recall), чтобы не изобретать то, что уже есть.
Приёмка стадии: `py -3 .intent/check.py {stage}` — сервер повторит ту же проверку сам.
"""

PLAYBOOK = {
    "understood": """\
Цель: понять, что именно нужно, и не строить то, что уже есть.
1. Найди готовые решения (открытый код, продукты, статьи) — не меньше трёх, со ссылками и
   выводом «берём / берём частично / нет, потому что». Есть Deep Research — используй.
2. Найди, что уже есть в арсенале владельца (`MAP.md`, `stack.json`, доступные проекты
   в {workspaces}) и что можно переиспользовать.
3. Ограничения: решения владельца, правила пульта, фактически доступные ресурсы машины.
Документ `intent/understood.md`, разделы: «## Интенция» (своими словами + что считается
успехом), «## Готовые решения», «## Что уже есть», «## Ограничения», «## Неясности»
(каждая с принятым умолчанием), «## Нужно от владельца» (или «ничего»).
Плюс `intent/owner-brief.md` — для владельца в TickTick, читается через недели без контекста:
«## Коротко» (2–4 предложения: что это, зачем ему, что получит — как другу, без терминов, путей,
номеров задач и англ. жаргона) и «## Вопросы» (на каждый пункт «Нужно от владельца»:
«- <пункт дословно>: что это значит в жизни; если «Да» — …; если «Нет» — …», или «нет»).
Проверка формы: `py -3 .intent/check.py owner_brief`.""",
    "executable_goal": """\
Цель: превратить интенцию в проверяемые сценарии ДО кода.
`intent/goal.md`: «## Результат» (что видит владелец, когда всё готово), «## Сценарии»
(5–15 сценариев: основной путь, крайние случаи, негативные, нефункциональные — скорость,
память, визуал, если есть интерфейс), «## Вне рамок».
`intent/scenarios.py` — исполняемая проверка сценариев (код возврата 0 — все зелёные).
Сейчас кода нет, поэтому она обязана ПАДАТЬ: приёмка проверяет именно красную базу.
Нужны данные — сделай тестовый корпус в проекте, не жди данных владельца.""",
    "approach_selected": """\
Цель: выбрать способ по данным, а не по мнению.
Опиши 2–3 существенно разных варианта; там, где они расходятся в главном, сделай спайки в
`spikes/<вариант>/` и прогони на сценариях из `intent/scenarios.py` (хотя бы на части).
`intent/approaches.md`: «## Варианты», «## Замер» (таблица: вариант × сценарий/метрика),
«## Выбор» (с основанием), «## Откат» (что делать, если выбор не оправдается).""",
    "executable_plan": """\
Цель: план, который флот исполнит без вопросов.
`intent/plan.md`: «## Работы», «## Зависимости», «## Приёмка».
Каждую работу оформи черновиком ТЗ флота `ToDo/tz-drafts/TZ-<n>-<slug>.md` с yaml-шапкой
(tz, owns, needs, acceptance) — сервер сам проверит и зарегистрирует черновики, перенеся их в
`ToDo/tz/`; в owns ТЗ не включай `intent/` и `ToDo/tz-drafts/`. Работы — с непересекающимися owns, чтобы шли
параллельно; приёмка каждой — команда, а не слова. Интерфейс → отдельная работа на визуал
и анимации; установка и README «с нуля» → отдельная работа.""",
    "implemented": """\
Цель: все сценарии зелёные. Работы плана выполняет флот по своим ТЗ; эта стадия — довести
`intent/scenarios.py` до кода возврата 0 на текущем коде проекта. Красное — разберись,
чья вина, и чини (или заведи fix-ТЗ). Подгонять сценарии под код нельзя: менять
`intent/scenarios.py` можно только с записью причины в `intent/goal.md`.""",
    "clean_start": """\
Цель: доказать, что проект работает с нуля, а не только на этой машине.
Возьми чистую копию — `git clone` проекта (или копию без venv/node_modules/кэшей) в
`.clean-start/<claim_id>` ВНУТРИ текущего проекта. Не запускай clone из общего корня рабочих проектов:
его файлы принадлежат владельцу. Из корня проекта допустим
`git clone --local --no-hardlinks . .clean-start/<claim_id>`; используй только committed HEAD,
не подмешивай dirty/untracked файлы. Затем новое окружение с нуля, установка СТРОГО по README,
затем `intent/scenarios.py` из этой копии. Чего не хватило в README — черновик fix-ТЗ в `ToDo/tz-drafts/` (README — зона работ плана), повтор с нуля после его приёмки.
Квитанция `intent/clean-start.md`: где, какая версия (коммит), команды, вывод и строка
`result: pass`. Копию после квитанции удали.""",
    "quality": """\
Цель: найти то, что «в плане не учитывалось». Смотри против ИНТЕНЦИИ, а не против плана.
`intent/lenses.md` — по разделу на каждую линзу: «## визуал» (и анимации, если есть
интерфейс: сними скриншоты и опиши), «## пустые и ошибочные состояния», «## первый запуск»,
«## ресурсы», «## безопасность», «## документация», «## восстановление».
Каждый дефект — строка с уровнем P0–P3. P0/P1 исправь (или fix-ТЗ) и отметь «закрыт»;
открытый P0/P1 не пропустит приёмку. P2/P3 — в бэклог проекта.""",
    "delivered": """\
Цель: установить туда, где этим пользуются (MCP-клиенты, автозапуск, телефон, облако),
с возможностью отката. Настройки безопасности ОС не меняй — подготовь команды владельцу.
`intent/delivery.md`: «## Где» (куда установлено и как это проверить), «## Откат».
`intent/smoke.py` — проверка с того места, где пользуются (код 0 — работает).""",
    "handed_over": """\
Цель: одно сообщение владельцу. `intent/handover.md`: «## Готово» (что работает и как
этим пользоваться — до 15 строк), «## От владельца» (каждое действие: что сделать, вариант
по умолчанию, почему система не смогла сама; или «ничего»). Сервер отправит его владельцу.""",
    "hypotheses_plan": """\
Это исследование с доступом к коду и системе, не обзор внешних источников.
`intent/hypotheses.md`: «## Гипотезы», «## Эксперименты», «## Метрики»,
«## Воспроизведение» и «## Риски». Для каждого опыта назови измеряемый
критерий, контроль и команду; данные владельца не отправляй наружу.
Сравни три независимых варианта и оформи свод разногласий.
Критику Gemini запрашивает СЕРВЕР после твоей сдачи и пишет в `intent/consensus/<стадия>-gemini.md`; ты её не добываешь и задачу из-за неё не блокируешь — сдавай, когда своя приёмка зелёная. Вердикт revise вернётся к тебе текстом.""",
    "experiments": """\
Проведи реальные пробы (прототипы, замеры, тесты, установки в безопасной песочнице).
Каждый опыт — `experiments/<id>/README.md` с разделами «## Гипотеза»,
«## Команда», «## Результат», «## Среда». Сохрани наблюдения и артефакты.
Команды должны быть повторяемыми; разрушительные проверки — только после согласования.""",
    "verified": """\
Другой агент повторяет ключевые эксперименты. `intent/verification.md`: «## Повторы»,
«## Независимый агент», «## Критика Gemini» (ссылка на серверный файл), «## Расхождения».
Критику Gemini запрашивает СЕРВЕР после твоей сдачи и пишет в `intent/consensus/<стадия>-gemini.md`; ты её не добываешь и задачу из-за неё не блокируешь — сдавай, когда своя приёмка зелёная. Вердикт revise вернётся к тебе текстом.""",
    "report": """\
`intent/report.md`: «## Вывод», «## Эксперименты», «## Уверенность»,
«## Что дальше». Для каждого вывода ссылка на воспроизводимый опыт;
по итогам составь готовые ТЗ/интенции на дальнейшую реализацию, если нужны.""",
    "rollback_plan": """\
`intent/rollback-plan.md`: «## План», «## Риски», «## Откат»,
«## Проверка», «## Нужно от владельца». До отдельного факта одобрения
владельцем никаких действий на машине не исполнять.""",
    "owner_approved": """\
Только явное действие владельца даёт одобрение, а не записанный агентом файл.
Отправь вариант по умолчанию и последствия; дождись доверенного события
`intent.ops_approved` в реестре. Без него стадия остаётся заблокирована.""",
    "executed": """\
После проверенного одобрения выполни строго согласованные операции.
`intent/execution.md`: «## Команды», «## Результат», «## Откат»,
«## Проверка»; `result: pass` только после реальной проверки.
Если что-либо разошлось с планом, остановись и выполни откат.""",
}


# Зона стадии — только её документы. 13.10: зона «вся папка проекта» сделала каждую задачу,
# которую планировщик ставит флоту, «пересечением с чужим владельцем» — регистрация ТЗ
# стадии executable_plan отклонялась (foreign_owner_overlap), стадия замерзала.
STAGE_OWNS = {
    # До плана стадия одна и пишет в intent/ свободно (сценарии, фикстуры).
    "understood": ("intent",),
    "executable_goal": ("intent",),
    "approach_selected": ("intent", "spikes"),
    # С плана — только свои документы: ТЗ плана законно трогают intent/ (драйвер сценариев
    # и т.п.), и зона «intent» у замороженных стадий держала их регистрацию (13.10, TZ-05).
    # Не весь ToDo: зарегистрированное ТЗ владеет своим файлом в ToDo/tz.
    "executable_plan": ("intent/plan.md", "ToDo/tz-drafts"),
    "implemented": ("intent/implemented.md",),
    "clean_start": ("intent/clean-start.md", "ToDo/tz-drafts", ".clean-start"),
    "quality": ("intent/lenses.md", "ToDo/tz-drafts"),
    "delivered": ("intent/delivery.md", "intent/smoke.py"),
    "handed_over": ("intent/handover.md",),
    "hypotheses_plan": ("intent/hypotheses.md", "spikes"),
    "experiments": ("experiments",),
    "verified": ("intent/verification.md",),
    "report": ("intent/report.md", "ToDo/tz-drafts"),
    "rollback_plan": ("intent/rollback-plan.md",),
    "owner_approved": ("intent/owner-approval.md",),
    "executed": ("intent/execution.md",),
}


def stage_owns(directory: Path, stage: str) -> list[str]:
    return [str(Path(directory) / part) for part in STAGE_OWNS.get(stage, ("intent",))]


def triple_stage(stage: str) -> bool:
    return stage in stage_check.TRIPLE_STAGES


def candidate_tz(tree_id: str, stage: str, index: int) -> str:
    return f"{tree_id}-{stage}-candidate-{index}"


def candidate_body(stage: str, index: int) -> str:
    return (f"[intent-candidate] stage={stage} index={index}\n"
            "НЕ читай разработки других кандидатов. Независимо исследуй задачу; "
            "докажи свою версию, предложи эксперименты. Пиши ТОЛЬКО в "
            f"intent/candidates/{stage}/{index}.md: "
            "разделы ## Анализ, ## Доказательства, ## Вывод (не менее 400 знаков).\n")


def narrow_stage_owns(store: Any) -> int:
    """Self-heal stage tasks whose zone differs from STAGE_OWNS (older spawns)."""
    changed = 0
    with store.tx() as tx:
        rows = tx.query("""
            SELECT t.project, t.tz, t.owns, t.body, o.title, p.root FROM tasks t
              JOIN outcome_work w ON w.project=t.project AND w.tz=t.tz
              JOIN outcomes o ON o.id=w.outcome_id
              JOIN projects p ON p.name=t.project
             WHERE t.body LIKE '[intent-template]%' AND t.state IN ('ready','frozen')
               AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.project=t.project AND c.tz=t.tz AND c.state='active')""")
        for row in rows:
            try:
                owns = json.loads(row["owns"] or "[]")
            except ValueError:
                continue
            wanted = stage_owns(Path(row["root"]), row["title"])
            body = str(row['body'])
            if row['title'] == 'clean_start':
                body = body.replace('C:\\hub\\.clean-start\\<проект>-<дата>',
                                    '.clean-start/<claim_id> внутри текущего проекта')
            if owns == wanted and body == row['body']:
                continue
            tx.change("UPDATE tasks SET owns=?,body=? WHERE project=? AND tz=?",
                      (json.dumps(wanted, ensure_ascii=False), body,
                       row["project"], row["tz"]),
                      event={"kind": "intent.stage_owns_narrowed", "project": row["project"],
                             "tz": row["tz"], "payload": {}})
            changed += 1
    return changed


def task_body(outcome_id: str, stage: str, required: str, *, triple: bool = False) -> str:
    return (f"[intent-template] outcome={outcome_id}\n"
            f"Стадия: {stage}\nНужно доказать: {required}\n\n"
            + ("Три независимых кандидата + свод: проверь версии, разногласия,"
               " выполни проверочный спайк. Критику Gemini запрашивает СЕРВЕР после твоей сдачи и пишет в `intent/consensus/<стадия>-gemini.md`; ты её не добываешь и задачу из-за неё не блокируешь — сдавай, когда своя приёмка зелёная. Вердикт revise вернётся к тебе текстом."
               " Только свод принимается как доказательство стадии.\n" if triple and triple_stage(stage) else "")
            + COMMON.format(stage=stage, map_path=(machine.HUB / 'MAP.md').as_posix(),
                            stack_path=machine.STACK_JSON.as_posix(),
                            decisions_path=(machine.HUB / '_decisions').as_posix())
            + "\n" + PLAYBOOK[stage].replace('{workspaces}',
                ', '.join(f'`{path.as_posix()}`' for path in machine.WORKSPACES)) + "\n")


def acceptance(stage: str) -> list[str]:
    return [f"py -3 {CHECKER_REL.as_posix()} {stage}"]


def materialize(directory: Path, raw_intent: str, *, triple: bool = False,
                initialize_git: bool = True) -> None:
    """Idempotent: intent text and the checker copy inside the project."""
    target = directory / ".intent"
    target.mkdir(exist_ok=True)
    (target / "intent.md").write_text(raw_intent, encoding="utf-8")
    shutil.copyfile(Path(stage_check.__file__), directory / CHECKER_REL)
    (target / "options.json").write_text(json.dumps({"triple": triple}), encoding="utf-8")
    if initialize_git:
        ensure_git(directory)


# Серверные копии прежних версий stage_check.py (CRLF, как пишет сервер). Пополнять при
# каждой правке stage_check.py, иначе копии в проектах застревают на старой (18.10: 12 из 13
# копий были версиями eef008a/1e9413f/3d495c7 и не обновлялись).
LEGACY_CHECKER_SHA256 = {
    'c50d613046bba9c3d401b4d79b38cf42514e7e8429074106005179e1c3b4660f',  # 77944c0
    '2d5f21730aee412fbad8915ee32a7d97123ac145a07bcf230acac1ab1f094bac',  # eef008a
    'df4d1dbbe5db67c6bff8576e08aad31662124799d12bacc96c84e905df2452a7',  # 1e9413f
    'd26ace2f8aded63ab6cf115a21b859ce76156e03f0adb81949ba03d8ef769f49',  # 3d495c7
    '5763e0b113430cad7a44ae41fc5bc3db0d21956836515063580835f87d653bcf',  # a27ec14
    'ef8dc53160c2a4e9c3d69f10d20559763081f516b6b85f4d22c070ff6e693910',  # 86889f0, до owner_brief
}


def refresh_managed_checkers(store: Any) -> list[str]:
    """Update only byte-identical known server copies, preserving the old file."""
    import hashlib
    current = Path(stage_check.__file__).read_bytes()
    current_sha = hashlib.sha256(current).hexdigest()
    with store.read() as tx:
        projects = tx.query("SELECT DISTINCT p.name,p.root FROM projects p JOIN outcomes o "
                            "ON o.project=p.name WHERE p.active=1 AND o.parent_id IS NULL AND o.state='open'")
    refreshed = []
    for project in projects:
        root = Path(project['root'])
        target = root/CHECKER_REL
        if (not target.is_file() or target.is_symlink() or root.resolve() != root.absolute()
                or target.resolve() != target.absolute()):
            continue
        original = target.read_bytes()
        old_sha = hashlib.sha256(original).hexdigest()
        if old_sha == current_sha or old_sha not in LEGACY_CHECKER_SHA256:
            continue
        backup = root/'.intent/check-backups'/f'{old_sha}.py'
        if backup.resolve() != backup.absolute():
            continue
        backup.parent.mkdir(exist_ok=True)
        if not backup.exists():
            backup.write_bytes(original)
        if backup.read_bytes() != original:
            continue
        pending = target.with_name(f'.checker-{uuid.uuid4().hex}.tmp')
        with pending.open('xb') as stream:
            stream.write(current)
        pending.replace(target)
        with store.tx() as tx:
            tx.record_event(kind='intent.checker_refreshed', project=project['name'],
                            payload={'before':old_sha,'after':current_sha,'backup':str(backup)})
        refreshed.append(project['name'])
    return refreshed


def ensure_git(directory: Path) -> None:
    """Свой репозиторий у проекта интенции: принятая работа коммитится (goals.project_info),
    стадия clean_start клонирует, откат возможен. Идемпотентно."""
    import subprocess
    if (directory / ".git").exists():
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    run = lambda *args: subprocess.run(["git", *args], cwd=directory, capture_output=True,
                                       timeout=60, creationflags=flags, check=False)
    run("init", "-q")
    (directory / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.venv/\nnode_modules/\n.env\n*.secret*\n", encoding="utf-8")
    run("add", "-A")
    run("-c", "user.name=Fleet", "-c", "user.email=fleet@localhost",
        "commit", "-q", "-m", "Проект интенции: начальное состояние")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _triple_workers(store: Any, project: str, root_id: str, stage: str,
                    *, consensus: bool = False) -> tuple[list[str], list[str]]:
    """Require three completed candidate claims from distinct real workers."""
    problems: list[str] = []
    workers: list[str] = []
    with store.read() as tx:
        for index in range(1, 4):
            tz = candidate_tz(root_id, stage, index)
            task = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
            claim = tx.one("SELECT worker_id FROM claims WHERE project=? AND tz=? "
                           "AND state='released' ORDER BY id DESC LIMIT 1", (project, tz))
            if not task or task["state"] != "accepted" or not claim:
                problems.append(f"нет принятой независимой версии {index} с историей claim")
                continue
            workers.append(claim["worker_id"])
        if len(workers) != 3 or len(set(workers)) != 3:
            problems.append("три версии должны сделать три разных агента")
        if consensus:
            tz = f"{root_id}-{stage}"
            own = tx.one("SELECT worker_id FROM claims WHERE project=? AND tz=? "
                         "ORDER BY id DESC LIMIT 1", (project, tz))
            if not own or own["worker_id"] in workers:
                problems.append("свод обязан делать четвёртый, независимый агент")
    return workers, problems


def _redo_duplicate_candidates(store: Any, project: str, root_id: str, stage: str) -> int:
    """All variants accepted, but one worker made several: reopen the duplicates.

    14.10.2026: fast-jev-compaction-fixes understood — W3203 made all three variants,
    the consensus waited for three distinct workers for 13 h and nobody saw it. The
    take guard (queue._triple_sibling_conflict) keeps the same worker off the redo.
    """
    seen: set[str] = set()
    redo: list[str] = []
    with store.read() as tx:
        for index in range(1, 4):
            tz = candidate_tz(root_id, stage, index)
            task = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
            claim = tx.one("SELECT worker_id FROM claims WHERE project=? AND tz=? "
                           "AND state='released' ORDER BY id DESC LIMIT 1", (project, tz))
            if not task or task["state"] != "accepted" or not claim:
                return 0  # still in progress — nothing to redo yet
            if claim["worker_id"] in seen:
                redo.append(tz)
            seen.add(claim["worker_id"])
    changed = 0
    for tz in redo:
        with store.tx() as tx:
            result = tx.change(
                "UPDATE tasks SET state='ready',updated_at=? "
                "WHERE project=? AND tz=? AND state='accepted'",
                (_now(), project, tz),
                event={"kind": "intent.triple_redo", "project": project, "tz": tz,
                       "payload": {"why": "вариант делал тот же агент, что и соседний — "
                                          "нужна независимая версия другого агента"}})
            changed += int(result.rowcount)
    return changed


def advance_triples(store: Any) -> int:
    """Ready only the fourth task after all three independently claimed variants."""
    with store.read() as tx:
        rows = tx.query("SELECT o.id,o.title,o.project FROM outcomes o "
                        "JOIN outcome_work w ON w.outcome_id=o.id "
                        "JOIN tasks t ON t.project=w.project AND t.tz=w.tz "
                        "JOIN projects p ON p.name=o.project "
                        "WHERE o.state='open' AND t.state='frozen' "
                        "AND p.active=1 "
                        "AND o.title IN ('understood','approach_selected','hypotheses_plan') "
                        "AND t.body LIKE '[intent-template]%'")
    changed = 0
    for row in rows:
        with store.read() as tx:
            spec = tx.one("SELECT spec FROM outcomes WHERE id=?", (row["id"],))
        if not spec or not json.loads(spec["spec"]).get("triple"):
            continue
        root_id = row["id"].rsplit(":", 1)[0]
        _, problems = _triple_workers(store, row["project"], root_id, row["title"])
        if problems:
            changed += _redo_duplicate_candidates(store, row["project"], root_id, row["title"])
            continue
        with store.tx() as tx:
            result = tx.change("UPDATE tasks SET state='ready',updated_at=? "
                               "WHERE project=? AND tz=? AND state='frozen'",
                               (_now(), row["project"], f"{root_id}-{row['title']}"),
                               event="intent.triple_consensus_ready")
            changed += int(result.rowcount)
    return changed


CRITIQUE_TIMEOUT_S = 90


def _clip(text: str, limit: int) -> str:
    """14.10: hard [:3500]/[:7000] cut sentences; Gemini answered revise "text stops
    mid-sentence" and the understood stage of ai-relay-upgrade looped."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut if cut > limit // 2 else limit] + "\n[обрезано сервером]\n"


def _gemini_critique(store: Any, project: str, stage: str, root: Path,
                    tz: str, *, single: bool = False) -> list[StageProblem]:
    """Cross-model critique is server-observed, never the author's declaration."""
    import hashlib
    from dev_mcp.fleet import gemini
    folder = root / "intent"
    try:
        if stage == "verified":
            content = _clip((folder / "hypotheses.md").read_text(encoding="utf-8"), 20000)
            content += "\n## НЕЗАВИСИМАЯ ПРОВЕРКА\n" + _clip((
                folder / "verification.md").read_text(encoding="utf-8"), 30000)
            for report in sorted((root / "experiments").glob("*/README.md"))[:12]:
                content += "\n## ОПЫТ " + report.parent.name + "\n"
                content += _clip(report.read_text(encoding="utf-8"), 6000)
        elif single:
            document = {'understood':'understood.md','approach_selected':'approaches.md',
                        'hypotheses_plan':'hypotheses.md','report':'report.md'}[stage]
            content = _clip((folder/document).read_text(encoding='utf-8'),30000)
        else:
            content = "\n".join(_clip((folder / "candidates" / stage / f"{i}.md").read_text(
                encoding="utf-8"), 15000) for i in range(1, 4))
            content += "\n## СВОД\n" + _clip((folder / "consensus" / f"{stage}.md").read_text(
                encoding="utf-8"), 30000)
    except OSError:
        return [StageProblem("invalid_result", "нет документов для независимой критики")]
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with store.read() as tx:
        history = tx.query("SELECT payload FROM events WHERE kind='intent.gemini_critique' "
                           "AND project=? AND tz=? ORDER BY id DESC LIMIT 20", (project, tz))
    for old in history:
        data = json.loads(old["payload"])
        if data.get("digest") == digest:
            _external_resumed(store, project, tz, digest)
            if data.get("verdict") == "approve":
                return []
            return [StageProblem("invalid_result", "Gemini потребовал пересмотра: "
                                 + str(data.get("critique", ""))[:800])]
    # A pending submission is held by its actual artifact digest and observed
    # relay state, not by a new author claim or by every prover timer tick.
    # An altered artifact or relay state change bypasses backoff immediately.
    service_state = "up" if gemini._listening() else "down"
    previous = _last_external_event(store, project, tz)
    if previous and previous["kind"] == "intent.stage_waiting_external":
        data = json.loads(previous["payload"])
        if data.get("digest") == digest and data.get("service_state") == service_state:
            delay = min(3600, 30 * 2 ** min(16, max(1, int(data.get("attempt", 1)))))
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(previous["ts"])).total_seconds()
            if age < delay:
                return [StageProblem("external_pending", str(data.get("reason", "")), digest)]
    schema = {"type": "OBJECT", "properties": {
        "verdict": {"type": "STRING", "enum": ["approve", "revise"]},
        "critique": {"type": "STRING"}}, "required": ["verdict", "critique"]}
    answer = gemini.ask(
        ("Ты независимый критик. Проверь одиночный документ и доказательства. " if single else
         "Ты независимый критик предпродакшена. Сравни три версии и свод. ") +
        "Найди ошибки, несогласия, недоказанные утверждения. "
        "Ответь revise, если спайки не разрешили спорные вопросы. "
        "Пометка «[обрезано сервером]» — ограничение пересылки, не дефект документа.\n" + content,
        # gemini-auto-flash answers in ~20 s even on 'Say ok' (13.10.2026): 25 s made
        # every critique fail and the understood stage of ai-relay-upgrade loop.
        schema=schema, timeout=CRITIQUE_TIMEOUT_S)
    if not isinstance(answer, dict) or answer.get("verdict") not in ("approve", "revise") or not (
            isinstance(answer.get("critique"), str) and answer["critique"].strip()):
        reason = ("недоступна независимая критика Gemini (сервер, не агент: "
                  + str(gemini.last_error() or "нет ответа")[:200] + ")")
        return [_external_pending(store, project, tz, digest, service_state, reason)]
    _external_resumed(store, project, tz, digest)
    with store.tx() as tx:
        tx.record_event(kind="intent.gemini_critique", project=project, tz=tz,
                        payload={"digest": digest, "verdict": answer["verdict"],
                                 "critique": answer["critique"][:3000]})
    critique = folder / "consensus" / f"{stage}-gemini.md"
    critique.parent.mkdir(parents=True, exist_ok=True)
    critique.write_text("model: gemini\n## Критика\n" + answer["critique"][:3000]
                        + "\n## Вердикт\n" + answer["verdict"] + "\n", encoding="utf-8")
    if answer["verdict"] != "approve":
        return [StageProblem("invalid_result",
                             "Gemini потребовал пересмотра: " + answer["critique"][:800])]
    return []


EXECUTABLE_SERVER_STAGES = frozenset((
    "executable_goal", "implemented", "delivered", "executable_plan"))
DOCUMENT_SERVER_STAGES = frozenset((
    "understood", "approach_selected", "hypotheses_plan", "report"))


STAGE_PROVER_WAKE = threading.Event()

def request_server_stage_check(store: Any, project: str, tz: str) -> str | None:
    """Keep slow checkers off the MCP response path; the prover owns delivery."""
    with store.read() as tx:
        source = tx.one('SELECT body FROM tasks WHERE project=? AND tz=?',(project,tz))
        if source is None or not str(source['body']).startswith(('[intent-template]','[intent-candidate]')):
            return None
        row = tx.one("SELECT t.body,t.state,t.updated_at,o.title FROM tasks t "
                     "LEFT JOIN outcome_work w ON w.project=t.project AND w.tz=t.tz "
                     "LEFT JOIN outcomes o ON o.id=w.outcome_id WHERE t.project=? AND t.tz=?",(project,tz))
    if row is None or row['state'] != 'submitted': return None
    candidate = str(row['body']).startswith('[intent-candidate]')
    if not (candidate or (str(row['body']).startswith('[intent-template]') and
                         row['title'] in EXECUTABLE_SERVER_STAGES | DOCUMENT_SERVER_STAGES)):
        return None
    with store.tx() as tx:
        if not tx.one("SELECT 1 FROM events WHERE kind='intent.stage_check_requested' AND project=? AND tz=? "
                      "AND json_extract(payload,'$.version')=? LIMIT 1",(project,tz,row['updated_at'])):
            tx.record_event(kind='intent.stage_check_requested',project=project,tz=tz,
                            payload={'version':row['updated_at']})
    STAGE_PROVER_WAKE.set()
    return 'server-pending:' + tz


def check_submitted_stages(store: Any) -> None:
    with store.read() as tx:
        rows = tx.query("SELECT t.project,t.tz,t.updated_at FROM tasks t JOIN projects p ON p.name=t.project "
                        "WHERE p.active=1 AND t.state='submitted' AND "
                        "(t.body LIKE '[intent-template]%' OR t.body LIKE '[intent-candidate]%') ORDER BY t.updated_at")
    for row in rows:
        try:
            accept_submitted_stage(store,row['project'],row['tz'])
        except Exception as exc:
            _platform_exception(store,row,exc,traceback.format_exc())


def accept_submitted_stage(store: Any, project: str, tz: str) -> str | None:
    """Replace agent review only for server-provable intent submissions.

    No user-controlled body by itself authorizes this path: regular stages
    must belong to a registered outcome; independent candidates need the
    server-generated typed marker and the project's .intent directory.
    The checker is the imported trusted copy, never the editable project copy.
    """
    import re

    with store.read() as tx:
        row = tx.one("""
            SELECT t.*, p.root, o.id AS outcome_id, o.title AS outcome_stage,
                   o.spec AS outcome_spec, o.authority AS outcome_authority
              FROM tasks t
              JOIN projects p ON p.name=t.project AND p.active=1
              LEFT JOIN outcome_work w ON w.project=t.project AND w.tz=t.tz
              LEFT JOIN outcomes o ON o.id=w.outcome_id
             WHERE t.project=? AND t.tz=?
        """, (project, tz))
    if row is None or row["state"] != "submitted":
        return None
    body = str(row["body"] or "")
    root = Path(str(row["root"]))
    stage = str(row["outcome_stage"] or "")
    candidate = re.match(r"^\[intent-candidate\] stage=(\w+) index=([123])\b", body)
    from dev_mcp.fleet import evidence
    artifact_paths = json.loads(row['owns'] or '[]')
    artifact_before = evidence.tree_hash(artifact_paths)
    version = row['updated_at']
    if candidate:
        # Do not confuse a regular project TZ or a spoofed body with a
        # candidate: this must be the generated candidate task identifier.
        stage, index = candidate.groups()
        root_id = tz[: -len(f"-{stage}-candidate-{index}")]
        with store.read() as tx:
            registered = tx.one(
                "SELECT id FROM outcomes WHERE id=? AND project=? AND parent_id IS NULL",
                (root_id, project),
            )
        if (stage not in stage_check.TRIPLE_STAGES
                or not tz.endswith(f"-{stage}-candidate-{index}")
                or not registered
                or not (root / ".intent" / "intent.md").is_file()):
            return None
        problems = stage_check.check(root, f"candidate:{stage}:{index}")
    elif (body.startswith("[intent-template]") and row["outcome_id"]
          and stage in EXECUTABLE_SERVER_STAGES | DOCUMENT_SERVER_STAGES):
        spec = json.loads(row["outcome_spec"] or "{}")
        is_triple = bool(spec.get("triple")) and triple_stage(stage)
        problems = stage_check.check(root, stage, strict=bool(spec.get("triple")),
                                     triple=is_triple)
        if stage == "understood" and not problems:
            try:
                grants = json.loads(row["outcome_authority"] or "{}").get("project_roots", ())
                kind, _ = stage_check.routing(
                    root, allowed_roots=tuple(Path(p) for p in grants))
                if kind != "build" and not grants:
                    raise ValueError("отсутствуют доверенные project_roots")
            except (ValueError, OSError) as exc:
                problems.append(f"target не разрешён: {exc}")
        if is_triple:
            _, errors = _triple_workers(store, project,
                                        str(row["outcome_id"]).rsplit(":", 1)[0], stage,
                                        consensus=True)
            problems.extend(errors)
        if stage == "executable_plan" and not problems:
            problems.extend(_plan_registered(store, project, root))
        if stage in DOCUMENT_SERVER_STAGES and not problems:
            problems.extend(_gemini_critique(store, project, stage, root, tz,
                                             single=not is_triple))
    else:
        return None

    problems = [p if isinstance(p,StageProblem) else StageProblem('invalid_result',p) for p in problems]
    # A changed artifact or registry submission invalidates the checked result.
    if artifact_before != evidence.tree_hash(artifact_paths):
        return 'server-stale:' + tz
    # No Gemini answer is NOT an adverse verdict.  Leave submitted, with
    # a durable event; the retry/wait controller (TZ-337) owns this case.
    if any(p.kind in ("external_pending","platform_error") for p in problems):
        with store.tx() as tx:
            tx.record_event(kind="intent.stage_critique_wait", project=project, tz=tz,
                            payload={"reason": [p.detail for p in problems[:3]]})
        return "server-wait:" + tz

    now = _now()
    if problems:
        note = ("\n\n## Серверная проверка не прошла (" + now[:16] + ")\n"
                + "\n".join("- " + p.detail for p in problems[:12]) + "\n")
        with store.tx() as tx:
            current = tx.one(
                "SELECT state,updated_at FROM tasks WHERE project=? AND tz=?", (project, tz))
            if current is None or current["state"] != "submitted" or current["updated_at"] != version:
                return "server-stale:" + tz
            tx.change(
                "UPDATE tasks SET state='ready',body=body||?,updated_at=? "
                "WHERE project=? AND tz=? AND state='submitted'",
                (note, now, project, tz),
                event={"kind": "intent.stage_reopened", "project": project, "tz": tz,
                       "payload": {"problems": [p.detail for p in problems[:8]], "producer": "server-stage-check"}},
            )
            _retire_stage_reviews(tx,project,tz,now)
        return "server-revise:" + tz

    with store.tx() as tx:
        current = tx.one(
            "SELECT state,updated_at FROM tasks WHERE project=? AND tz=?", (project, tz))
        if current is None or current["state"] != "submitted" or current["updated_at"] != version:
            return "server-stale:" + tz
        tx.change(
            "UPDATE tasks SET state='accepted',updated_at=? "
            "WHERE project=? AND tz=? AND state='submitted'",
            (now, project, tz),
            event={"kind": "accept", "worker_id": "server-stage-check",
                   "project": project, "tz": tz,
                   "payload": {"producer": "server-stage-check", "stage": stage}},
        )
        _retire_stage_reviews(tx,project,tz,now)
    return "server-accepted:" + tz


def _retire_stage_reviews(tx: Any, project: str, tz: str, now: str) -> None:
    """Only reviews of this exact server-checked submission become obsolete."""
    from dev_mcp.fleet.review import _review_target
    for row in tx.query("SELECT tz,body FROM tasks WHERE project=? AND kind='review' "
                        "AND state IN ('ready','taken','submitted','frozen') AND tz LIKE ?",
                        (project,'review:'+tz+'%')):
        try:
            target = _review_target(row['tz'],row['body'],project)
        except ValueError:
            continue
        if target != (project,tz): continue
        tx.change("UPDATE claims SET state='released',released_at=? WHERE project=? AND tz=? "
                  "AND state IN ('active','preparing')",(now,project,row['tz']),event='server_stage_review_release')
        tx.change("UPDATE tasks SET state='accepted',updated_at=? WHERE project=? AND tz=?",
                  (now,project,row['tz']),event={'kind':'server_stage_review_retired','project':project,
                  'tz':row['tz'],'payload':{'source':tz,'producer':'server-stage-check'}})


def _research_independence(store: Any, project: str, root_id: str) -> list[str]:
    """Research verification must be performed by a different claimed agent."""
    with store.read() as tx:
        authors = []
        for stage in ("experiments", "verified"):
            row = tx.one("SELECT worker_id FROM claims WHERE project=? AND tz=? "
                         "AND state='released' ORDER BY id DESC LIMIT 1",
                         (project, f"{root_id}-{stage}"))
            authors.append(row["worker_id"] if row else None)
    if not all(authors) or authors[0] == authors[1]:
        return ["повтор опытов должен доказать другой агент, не автор экспериментов"]
    return []


def _ops_owner_approval(store: Any, project: str, root: Path) -> list[str]:
    """Agent documents cannot satisfy owner-only consent."""
    import hashlib
    plan = root / "intent" / "rollback-plan.md"
    if not plan.is_file():
        return ["отсутствует план операции"]
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    with store.read() as tx:
        history = tx.query("SELECT payload FROM events WHERE kind='intent.ops_approved' "
                           "AND project=? ORDER BY id DESC LIMIT 20", (project,))
    for row in history:
        data = json.loads(row["payload"])
        if data.get("plan_sha256") == digest and data.get("approved_by") == "owner":
            return []
    return ["нет доверенного одобрения владельца именно этой ревизии плана"]


def advance_ops_approvals(store: Any) -> int:
    """Trusted owner event, not a task claim, advances the operations consent."""
    with store.read() as tx:
        rows = tx.query("""
            SELECT o.id, o.project, w.tz, p.root
              FROM outcomes o
              JOIN outcome_work w ON w.outcome_id=o.id
              JOIN tasks t ON t.project=w.project AND t.tz=w.tz
              JOIN projects p ON p.name=o.project
             WHERE o.title='owner_approved' AND o.state='open' AND t.state='frozen'
               AND p.active=1
        """)
    changed = 0
    for row in rows:
        root = Path(row["root"])
        if _ops_owner_approval(store, row["project"], root):
            continue
        prior_id = row["id"].rsplit(":", 1)[0] + ":rollback_plan"
        with store.read() as tx:
            prior = tx.one("SELECT state,evidence_spec FROM outcomes WHERE id=?", (prior_id,))
            proved = bool(prior and prior["state"] == "done" and tx.scalar(
                "SELECT COUNT(*) FROM evidence WHERE outcome_id=? AND verdict='pass' "
                "AND kind=? AND subject_version=?",
                (prior_id, json.loads(prior["evidence_spec"])["kind"],
                 json.loads(prior["evidence_spec"])["subject_version"])))
        if not proved:
            continue
        receipt = root / "intent" / "owner-approval.md"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text("Одобрено владельцем: контроль доверенного события\nresult: pass\n",
                           encoding="utf-8")
        with store.tx() as tx:
            changed += int(tx.change(
                "UPDATE tasks SET state='accepted',updated_at=? "
                "WHERE project=? AND tz=? AND state='frozen'",
                (_now(), row["project"], row["tz"]),
                event="intent.owner_approved_by_receipt").rowcount)
            tx.change("UPDATE obstacles SET state='resolved' "
                      "WHERE outcome_id=? AND type='owner_action' AND state='open'",
                      (row["id"],), event="intent.owner_action_resolved")
    return changed


def _stage_problems(store: Any, row: Any) -> list[StageProblem]:
    stage, tz = row["title"], row["tz"]
    spec_data = json.loads(row["spec"])
    is_triple = bool(spec_data.get("triple")) and triple_stage(stage)
    problems = [
        StageProblem("invalid_result", detail)
        for detail in stage_check.check(Path(row["root"]), stage,
                                        strict=bool(spec_data.get("triple")),
                                        triple=is_triple)
    ]
    if stage == "understood" and not problems:
        try:
            grants = json.loads(row["authority"]).get("project_roots", ())
            stage_check.routing(Path(row["root"]),
                                allowed_roots=tuple(Path(path) for path in grants))
        except (ValueError, OSError) as exc:
            problems.append(StageProblem("invalid_result", f"target не разрешён: {exc}"))
    if is_triple:
        _, errors = _triple_workers(store, row["project"],
                                    row["id"].rsplit(":", 1)[0], stage, consensus=True)
        problems.extend(StageProblem("invalid_result", error) for error in errors)
        if not problems:
            problems.extend(_gemini_critique(store, row["project"], stage,
                                             Path(row["root"]), tz))
    if stage == "verified" and spec_data.get("kind") == "investigate":
        problems.extend(StageProblem("invalid_result", error) for error in
                        _research_independence(store, row["project"],
                                               row["id"].rsplit(":", 1)[0]))
        if not problems:
            problems.extend(_gemini_critique(store, row["project"], stage,
                                             Path(row["root"]), tz))
    if stage == "owner_approved":
        problems.extend(StageProblem("invalid_result", error) for error in
                        _ops_owner_approval(store, row["project"], Path(row["root"])))
    if not problems and stage == "executable_plan":
        problems.extend(StageProblem("invalid_result", error) for error in
                        _plan_registered(store, row["project"], Path(row["root"])))
    return problems


def _trusted_owner_store(store: Any) -> bool:
    """Synthetic test stores may prove stages, but must never notify a real owner."""
    try:
        return Path(store.path).resolve() == LIVE_FLEET_DB.resolve()
    except (TypeError, ValueError, OSError):
        return False


def _owner_stage_notice(stage: str, path: Path) -> tuple[str, str] | None:
    """Extract only the server-proven section, never an agent-written command."""
    heading = ("Нужно от владельца" if stage == "understood"
               else "Готово" if stage == "handed_over" else None)
    if heading is None or not path.is_file():
        return None
    data = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r"(?mi)^##\s+" + re.escape(heading) + r"\s*$\n?(.*?)(?=^##\s+|\Z)",
        data, re.S | re.M,
    )
    if match is None:
        return None
    text = match.group(1).strip()
    if not text or text.casefold().strip(" .!\t\r\n") == "ничего":
        return None
    return ("needs" if stage == "understood" else "done", text[:4096])
def cancel_pending_intents(store: Any) -> int:
    """Acknowledge durable cancellations only after their trees are stopped.

    A rename produces cancelled(old identity) + created(new identity). Match
    the exact source/external/revision from the immutable root specification,
    not the project name or shared target directory. No other intent is paused.
    """
    from .sources.registry import IntentRegistry

    IntentRegistry(store._connection())  # creates source schema on a new DB
    with store.read() as tx:
        pending = tx.query(
            "SELECT event_id,source_id,external_id,revision "
            "FROM intent_source_events WHERE kind='cancelled' AND acknowledged=0 "
            "ORDER BY event_id LIMIT 100")
    stopped = 0
    for event in pending:
        with store.tx() as tx:
            roots = tx.query(
                "SELECT id,project,spec FROM outcomes "
                "WHERE parent_id IS NULL AND spec LIKE '%intent_ref%'")
            for root in roots:
                try:
                    spec = json.loads(root["spec"])
                except (ValueError, TypeError):
                    continue
                if (spec.get("source_id"), spec.get("external_id"), spec.get("revision")) != (
                        event["source_id"], event["external_id"], event["revision"]):
                    continue
                project, root_id = root["project"], root["id"]
                # Revoke claims too: a task becoming frozen is insufficient to
                # stop its already-running worker from writing to the checkout.
                tx.change(
                    "UPDATE claims SET state='released',released_at=? "
                    "WHERE state='active' AND project=? AND tz IN "
                    "(SELECT tz FROM tasks WHERE project=? AND "
                    "(tz LIKE ? OR tz LIKE ?))",
                    (_now(), project, project, root_id + "-%", "%" + root_id + "-%"),
                    event="intent.cancel_claims")
                result = tx.change(
                    "UPDATE tasks SET state='frozen',updated_at=?, "
                    "body=body||? WHERE project=? "
                    "AND (tz LIKE ? OR tz LIKE ?) "
                    "AND state IN ('ready','taken','submitted','frozen') "
                    "AND body NOT LIKE ?",
                    (_now(), "\n\nИнтенция отменена владельцем.\n", project,
                     root_id + "-%", "%" + root_id + "-%",
                     "%Интенция отменена владельцем%"),
                    event="intent.cancel_tasks")
                stopped += result.rowcount
                tx.change(
                    "UPDATE outcomes SET state='dropped',rev=rev+1 "
                    "WHERE (id=? OR parent_id=?) AND state='open'",
                    (root_id, root_id), event="intent.cancel_tree")
            # Never later spawn a stale created/updated event for the same
            # cancelled revision. The new filename has a DIFFERENT external_id.
            tx.change(
                "UPDATE intent_source_events SET acknowledged=1 "
                "WHERE source_id=? AND external_id=? AND revision=? "
                "AND kind IN ('created','updated','cancelled') AND acknowledged=0",
                (event["source_id"], event["external_id"], event["revision"]),
                event="intent.cancel_ack")
    return stopped


def prove_once(store: Any) -> int:
    """Check accepted stages. Only invalid_result reopens an author's claim."""
    from dev_mcp.fleet import outcome

    cancel_pending_intents(store)
    check_submitted_stages(store)
    advance_triples(store)
    advance_ops_approvals(store)
    with store.read() as tx:
        rows = tx.query("""
            SELECT o.id, o.title, o.spec, o.authority, o.evidence_spec, w.project, w.tz, p.root
              FROM outcomes o
              JOIN outcome_work w ON w.outcome_id = o.id
              JOIN tasks t ON t.project = w.project AND t.tz = w.tz
              JOIN projects p ON p.name = w.project
             WHERE o.state = 'open' AND o.parent_id IS NOT NULL AND p.active=1
               AND t.state = 'accepted' AND t.body LIKE '[intent-template]%'""")
    closed = 0
    for row in rows:
        stage, tz = row["title"], row["tz"]
        if stage not in PLAYBOOK:
            continue
        try:
            problems = _stage_problems(store, row)
            if not problems:
                if _trusted_owner_store(store) and stage in {"understood", "handed_over"}:
                    from .owner_channel import notify_owner
                    filename = ("understood.md" if stage == "understood"
                                else "handover.md")
                    notice = _owner_stage_notice(stage, Path(row["root"]) / "intent" / filename)
                    if notice is not None:
                        try:
                            notify_owner(store, row["project"], *notice)
                        except (OSError, ValueError, RuntimeError):
                            LOG.exception("owner channel deferred proven stage %s", stage)
                            # Retry on a later pass instead of closing a proven
                            # outcome while its required owner notice was lost.
                            continue
                spec = json.loads(row["evidence_spec"])
                outcome.add_evidence(
                    store, writer=outcome.server_evidence_writer(store),
                    outcome_id=row["id"], kind=spec["kind"],
                    ref=f"stage-check://{row['project']}/{tz}",
                    subject_version=spec["subject_version"], env_version="stage-check-v1",
                    verdict="pass", producer="intent_stage_prover",
                )
                outcome.close_outcome(store, row["id"])
                closed += 1
                continue
            if any(p.kind == "external_pending" for p in problems):
                # The submission stays accepted; only the SERVER can resume proof.
                continue
            details = [p.detail for p in problems]
            with store.tx() as tx:
                reopened = int(tx.scalar(
                    "SELECT COUNT(*) FROM events WHERE kind='intent.stage_reopened' "
                    "AND project=? AND tz=?", (row["project"], tz)) or 0)
                if reopened >= MAX_REOPEN:
                    if not tx.scalar("SELECT COUNT(*) FROM events WHERE kind='intent.stage_stuck' "
                                     "AND project=? AND tz=?", (row["project"], tz)):
                        tx.record_event(kind="intent.stage_stuck", project=row["project"],
                                        tz=tz, payload={"problems": details[:8]})
                    continue
                note = "\n\n## Серверная проверка не прошла (" + _now()[:16] + ")\n" + \
                    "\n".join(f"- {p}" for p in details[:12]) + "\n"
                tx.change(
                    "UPDATE tasks SET state='ready', body=body||?, updated_at=? "
                    "WHERE project=? AND tz=? AND state='accepted'",
                    (note, _now(), row["project"], tz),
                    event={"kind": "intent.stage_reopened", "project": row["project"],
                           "tz": tz, "payload": {"kind": "invalid_result",
                                                "problems": details[:8]}})
        except Exception as exc:
            trace = traceback.format_exc()
            LOG.exception("stage prover platform error: %s/%s", row["project"], tz)
            try:
                _platform_exception(store, row, exc, trace)
            except Exception:
                LOG.exception("unable to persist platform repair for %s/%s", row["project"], tz)
    return closed


def _plan_registered(store: Any, project: str, root: Path) -> list[str]:
    """План доказан, только когда его ТЗ стоят в очереди флота, а не лежат файлами.

    13.10: deep-research положил ТЗ сразу в ToDo/tz, стадия прошла проверку файлов, а
    ни одной работы во флоте не появилось — стадия «сделано» ждала бы вечно.
    """
    import re
    drafts = sorted((root / "ToDo" / "tz-drafts").glob("TZ-*.md"))
    if drafts:
        return [f"черновики ещё не зарегистрированы сервером: {', '.join(p.name for p in drafts[:6])}"]
    wanted = {m.group(1).upper() for p in (root / "ToDo" / "tz").glob("TZ-*.md")
              if (m := re.match(r"(TZ-\d+)", p.name, re.I))}
    with store.read() as tx:
        have = {str(r["tz"]).upper() for r in tx.query(
            "SELECT tz FROM tasks WHERE project=? AND tz LIKE 'TZ-%'", (project,))}
    missing = sorted(wanted - have)
    return [f"ТЗ не зарегистрированы во флоте: {', '.join(missing)}"] if missing else []


def reconcile_all(store: Any) -> int:
    """Полная сверка всех открытых деревьев интенций каждый проход.

    13.10: стадия delivered local-library осталась frozen, хотя все её поставщики были
    доказаны — разморозка шла только по курсору событий и это событие пропустила.
    """
    from dev_mcp.fleet.controller import Controller
    from .spawn import reconcile_tree
    with store.read() as tx:
        roots = [r["id"] for r in tx.query(
            "SELECT o.id FROM outcomes o JOIN projects p ON p.name=o.project "
            "WHERE o.parent_id IS NULL AND o.state='open' AND p.active=1 "
            "AND o.spec LIKE '%intent_ref%'")]
    helper = Controller(store, maintenance=lambda: None)
    moved = 0
    for root in roots:
        tree = helper._intent_tree(root)
        if tree is not None:
            moved += len(reconcile_tree(store, tree))
    return moved


def is_stuck(tx: Any, project: str, tz: str) -> bool:
    return bool(tx.scalar("SELECT COUNT(*) FROM events WHERE kind='intent.stage_stuck' "
                          "AND project=? AND tz=?", (project, tz)))


def run_stage_prover(store: Any, stop_event: threading.Event,
                     interval_s: float = PROVER_INTERVAL_S) -> None:
    """Own thread: a slow scenario run must never stall the 30 s controller tick."""
    if _trusted_owner_store(store):
        from .owner_channel import run_notice_retries
        threading.Thread(target=run_notice_retries,args=(store,stop_event),
                         name='dev-mcp-owner-notices',daemon=True).start()
    try:
        narrow_stage_owns(store)
    except Exception:
        LOG.exception("stage owns migration failed")
    while not stop_event.is_set():
        try:
            STAGE_PROVER_WAKE.clear()
            refresh_managed_checkers(store)
            from .progress_recovery import resume_implementation_plans
            resume_implementation_plans(store)
            prove_once(store)
            reconcile_all(store)
            from .progress_recovery import retire_routed_placeholders
            retire_routed_placeholders(store)
            if _trusted_owner_store(store):
                from .owner_channel import notify_ready_ops_plans
                notify_ready_ops_plans(store)
            try:
                from . import remote_repo
                remote_repo.schedule_retry(store)
            except Exception:
                LOG.exception('intent delivery scheduler deferred')
        except Exception as exc:
            LOG.exception("intent stage prover failed")
            try:
                with store.tx() as tx:
                    tx.record_event(kind="intent.prover_error",
                                    payload={"error": type(exc).__name__, "id": uuid.uuid4().hex[:8]})
            except Exception:
                LOG.exception("prover error could not be recorded")
        deadline = time.monotonic() + interval_s
        while not stop_event.is_set() and not STAGE_PROVER_WAKE.is_set() and time.monotonic() < deadline:
            stop_event.wait(min(1.0,max(0.0,deadline-time.monotonic())))
