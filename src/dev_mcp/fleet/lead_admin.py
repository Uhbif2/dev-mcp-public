"""Полномочия ведущего в своём проекте и обязанности, которые проверяет платформа (06.11.2026).

Владелец 06.11: постоянное администрирование проекта переходит от оператора к ведущему.
Права (инструмент `fleet_lead`) — только у держателя активного захвата устава проекта
(`TZ-LEAD` / `fix:TZ-LEAD:*`) и только для задач этого проекта:
- freeze / reactivate / priority — очередь своего проекта (24 замороженных ревью Nova
  держали вехи ночь, снять их мог лишь оператор);
- issue — подзадача рабочему с непересекающейся зоной записи (параллельность внутри проекта);
- redeploy — выкатить живую поверхность проекта сейчас, не ждать интегратора;
- discovery — отметка разведки неучтённого (см. `discovery_lines`).
Конфигурация проекта — `ops/lead/fleet.toml` (`project_config.py`), не этот модуль.

Обязанности в брифе (`duty_lines`) проверяет платформа по файлам и реестру, а не по слову:
- поручения владельца и соседей к ведущему, не отмеченные в его каноне (05.11 владелец
  отправил ведущему Nova ~15 поручений; все доставлены, ни одно не попало в STATE/LEDGER —
  каждый новый чат вехи о них не знал);
- документы проекта, не разобранные в книге по текущему sha256 (у двух новых ведущих
  единственным источником был intent/goal.md — README, docs, research не читались);
- разведка того, что важно человеку, но ещё не стало обязательством.
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

OWNER_CANON = ("product/", "goal.md", "intent/", ".intent/", "claude.md", "agents.md",
               "todo/tz/", "todo/decisions/", "todo/fleet/", ".git/")
# Parallel lanes are bounded by exclusive paths/resources, never a global task count.
CANON_DEFAULT = ("GOAL.md", "README.md", "intent/**/*.md", "product/**/*.md", "docs/**/*.md",
                 "research/**/*.md", "ToDo/requests/*.md")
CANON_MAX = 400
DISCOVERY_EVERY_H = 24


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def lead_project(tx: Any, agent: str) -> str | None:
    row = tx.one("SELECT project FROM claims WHERE worker_id=? AND state='active' AND "
                 "(UPPER(tz)='TZ-LEAD' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%') ORDER BY id DESC LIMIT 1",
                 (agent,))
    return str(row["project"]) if row is not None else None


def _is_lead_family(tz: str) -> bool:
    up = tz.upper()
    return up == "TZ-LEAD" or up.startswith("FIX:TZ-LEAD") or up.startswith("REVIEW:TZ-LEAD")


def act(store: Any, agent: str, action: str, params: dict[str, Any]) -> str:
    params = dict(params or {})
    with store.read() as tx:
        project = lead_project(tx, agent)
    if project is None:
        raise ValueError("fleet_lead: только держатель активного захвата устава TZ-LEAD своего проекта")
    if action in ("freeze", "reactivate", "priority"):
        return _queue_op(store, agent, project, action, params)
    if action == "issue":
        return _issue(store, agent, project, params)
    if action == "ask":
        from . import lead_decision
        return lead_decision.ask(store, agent, project, params)
    if action == "redeploy":
        return _redeploy(store, agent, project)
    if action in {"choose", "park", "wait", "result"}:
        from . import lead_focus
        return lead_focus.settle(store, agent, project, action, params)
    if action == "instruction":
        from . import instructions
        return instructions.set_status(store, agent, project, params)
    if action == "obstacle":
        from . import obstacles
        return obstacles.declare(store, agent, project, params)
    if action == "obstacle_close":
        from . import obstacles
        return obstacles.withdraw(store, agent, project, params)
    if action == "discovery":
        summary = str(params.get("summary") or "").strip()
        if len(summary) < 40:
            raise ValueError("discovery: summary — что проверено и что найдено (не меньше 40 знаков)")
        with store.tx() as tx:
            tx.record_event(kind="lead_discovery", worker_id=agent, project=project, payload={
                "summary": summary[:4000], "new": [str(x) for x in params.get("new") or []][:40]})
        return f"{project}: разведка записана"
    raise ValueError("fleet_lead: action ∈ freeze, reactivate, priority, issue, redeploy, discovery, "
                     "instruction, obstacle, obstacle_close, choose, park, wait, result, ask")


def _queue_op(store: Any, agent: str, project: str, action: str, params: dict[str, Any]) -> str:
    tz = str(params.get("tz") or "").strip()
    reason = str(params.get("reason") or "").strip()
    if not tz:
        raise ValueError(f"{action}: нужен tz задачи своего проекта")
    if _is_lead_family(tz):
        raise ValueError(f"{action}: устав ведущего и его раунды так не управляются")
    if action != "priority" and len(reason) < 10:
        raise ValueError(f"{action}: нужна причина (что препятствие и почему оно ушло/держит)")
    with store.tx() as tx:
        row = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
        if row is None:
            raise ValueError(f"{project}/{tz}: задача не найдена")
        state = str(row["state"])
        busy = tx.one("SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active' LIMIT 1",
                      (project, tz))
        if action == "priority":
            value = int(params.get("value"))
            if not 0 <= value <= 9:
                raise ValueError("priority: 0..9")
            tx.change("UPDATE tasks SET priority=?,updated_at=? WHERE project=? AND tz=?",
                      (value, _now(), project, tz),
                      event={"kind": "lead_priority", "worker_id": agent, "project": project, "tz": tz,
                             "payload": {"value": value, "reason": reason}})
            return f"{project}/{tz}: priority {value}"
        if busy is not None:
            raise ValueError(f"{project}/{tz}: задачу держит активный захват")
        if action == "freeze":
            if state in ("accepted", "taken", "frozen"):
                raise ValueError(f"{project}/{tz}: freeze из {state} нельзя")
            target = "frozen"
        else:
            if state not in ("frozen", "rejected"):
                raise ValueError(f"{project}/{tz}: reactivate только из frozen/rejected, сейчас {state}")
            target = "ready"
        tx.change("UPDATE tasks SET state=?,updated_at=? WHERE project=? AND tz=?",
                  (target, _now(), project, tz),
                  event={"kind": "lead_" + action, "worker_id": agent, "project": project, "tz": tz,
                         "payload": {"previous_state": state, "reason": reason}})
    return f"{project}/{tz}: {state} → {target} — {reason}"


def _root(tx: Any, project: str) -> Path:
    row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
    if row is None or not row["root"]:
        raise ValueError(f"{project}: нет корня проекта")
    return Path(str(row["root"]))


REVIEW_CRITERIA = "[lead-review-criteria]"


def criteria_block(criteria: list[str]) -> str:
    return "\n" + REVIEW_CRITERIA + " " + json.dumps(criteria, ensure_ascii=False) if criteria else ""


def review_criteria(tz: str, body: str) -> list[str]:
    from .project_review import TASK_ID

    if not re.fullmatch(rf"(?:fix:)?(?:sub:LEAD-\d+|{TASK_ID})(?::\d+)?", tz):
        return []
    _, marker, encoded = body.rpartition("\n" + REVIEW_CRITERIA + " ")
    if not marker:
        return []
    try:
        criteria = json.loads(encoded)
    except ValueError:
        return []
    return [item for item in criteria if isinstance(item, str)] if isinstance(criteria, list) else []


def _has_launcher(command: str, root: Path) -> bool:
    from .exec_preflight import parse_command

    try:
        argv = parse_command(command)
    except ValueError:
        return False
    launcher = argv[0]
    if any(arg in {"&&", "||", "|", ">", "<", ";"} for arg in argv):
        return False
    if "/" in launcher or "\\" in launcher:
        path = (root / launcher).resolve()
        return path.is_relative_to(root.resolve()) and path.is_file()
    return (shutil.which(launcher) is not None
            or shutil.which(launcher, path=str(Path(sys.executable).parent)) is not None
            or (root / launcher).is_file())


def _issue(store: Any, agent: str, project: str, params: dict[str, Any]) -> str:
    from .model import norm_path, paths_overlap
    title = str(params.get("title") or "").strip()
    body = str(params.get("body") or "").strip()
    owns = [str(x).strip().replace("\\", "/").strip("/") for x in params.get("owns") or [] if str(x).strip()]
    for name in ("acceptance", "criteria"):
        values = params.get(name)
        if values is not None and (not isinstance(values, list)
                                   or not all(isinstance(x, str) for x in values)):
            raise ValueError(f"issue: {name} должен быть списком строк")
    acceptance = [str(x).strip() for x in params.get("acceptance") or [] if str(x).strip()]
    criteria = [str(x).strip() for x in params.get("criteria") or [] if str(x).strip()]
    if not title or len(body) < 80 or not owns or not (acceptance or criteria):
        raise ValueError("issue: нужны title, body (от 80 знаков), owns и acceptance или criteria")
    if REVIEW_CRITERIA in body:
        raise ValueError("issue: критерии передаются отдельным полем criteria")
    for rel in owns:
        low = rel.casefold()
        if rel.startswith("..") or ":" in rel or any(low == c.rstrip("/") or low.startswith(c) for c in OWNER_CANON):
            raise ValueError(f"issue: {rel} — вне зоны ведущего (канон владельца или служебное флота)")
    with store.tx() as tx:
        root = _root(tx, project)
        from .lead_focus import issue_refusal

        refusal = issue_refusal(tx, project)
        if refusal:
            raise ValueError(refusal)
        for command in acceptance:
            if not _has_launcher(command, root):
                raise ValueError(f"issue: acceptance требует исполняемую команду: {command!r}. "
                                 "Условия для независимой проверки передавай в criteria.")
        absolute = [norm_path(str(root / rel)) for rel in owns]
        resources = [str(x).strip() for x in params.get("resources") or [] if str(x).strip()]
        exclusive = {r.casefold() for r in resources}
        # An independently owned track can start even when many other tracks are
        # open. Frozen tracks still reserve their paths/resources on reactivation.
        for r in tx.query("SELECT t.tz,t.owns,t.resources FROM tasks t WHERE t.project=? "
                          "AND t.state IN ('ready','taken','submitted','frozen') "
                          "AND t.tz LIKE 'sub:LEAD-%'", (project,)):
            for other in json.loads(r["owns"] or "[]"):
                if any(paths_overlap(a, str(other)) for a in absolute):
                    raise ValueError(f"issue: зона пересекается с {r['tz']} ({other})")
            busy = exclusive & {str(x).strip().casefold()
                                for x in json.loads(r["resources"] or "[]")}
            if busy:
                raise ValueError(f"issue: ресурс пересекается с {r['tz']} ({', '.join(sorted(busy))})")
        n = 1 + int(tx.scalar("SELECT COUNT(*) FROM tasks WHERE project=? AND tz LIKE 'sub:LEAD-%'",
                              (project,)) or 0)
        tz = f"sub:LEAD-{n}"
        stamp = _now()
        text = (f"[fleet-lead-sub] {project}\nВыдал ведущий ({agent}). {title}\n\n{body}\n\n"
                "Зона записи — только owns. Сдача — fleet_submit с зелёной acceptance; "
                "ревью и интеграция — штатные. Вопросы — fleet_send ведущему "
                f"{project}/TZ-LEAD.") + criteria_block(criteria)
        tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                  "resources,acceptance,author_worker,body,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (project, tz, title[:200], "ready", int(params.get("priority") or 3), "work", "", "",
                   "[]", json.dumps(absolute, ensure_ascii=False),
                   json.dumps(resources, ensure_ascii=False),
                   json.dumps(acceptance, ensure_ascii=False), agent, text, stamp, stamp),
                  event={"kind": "lead_issue", "worker_id": agent, "project": project, "tz": tz,
                         "payload": {"owns": owns, "acceptance": acceptance, "criteria": criteria}})
    return (f"{project}/{tz}: подзадача выдана (ready). Пока она не принята, её owns не правь — "
            "твоя зона ей уступает.")


def _redeploy(store: Any, agent: str, project: str) -> str:
    from . import goals, live_deploy
    live = goals.project_info(project).get("live")
    if not isinstance(live, dict) or not live.get("instance"):
        raise ValueError(f"{project}: живая поверхность не объявлена ([projects.\"{project}\".live])")
    with store.read() as tx:
        last = tx.one("SELECT ts FROM events WHERE kind='live_deployed' AND project=? ORDER BY id DESC LIMIT 1",
                      (project,))
        root = _root(tx, project)
    if last is not None:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(last["ts"]))
        if age < timedelta(minutes=2):
            raise ValueError("redeploy: прошлая выкатка меньше 2 мин назад")
    rev, _ = live_deploy._git_head(str(root))
    result = live_deploy.deploy(str(live["instance"]), int(live["port"]), str(live.get("probe") or project))
    with store.tx() as tx:
        tx.record_event(kind="live_deployed", worker_id=agent, project=project, payload={
            "instance": live["instance"], "port": int(live["port"]), "rev": rev, "by": "lead", **result})
    return (f"{project}: выкачено {rev[:10]} в {live['instance']} — "
            + ("работает" if result.get("ok") else f"ПРОВАЛ: {result.get('detail')}"))


# ---------------------------------------------------------------- обязанности в брифе

_CANON_CACHE: dict[str, tuple[float, str]] = {}


def _sha(path: Path) -> str:
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    hit = _CANON_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _CANON_CACHE[key] = (mtime, digest)
    return digest


def _lead_texts(root: Path) -> str:
    parts = []
    for rel in ("ToDo/lead/LEDGER.json", "ToDo/lead/STATE.md", "ToDo/lead/OWNER.md"):
        try:
            parts.append((root / rel).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    for path in sorted(glob.glob(str(root / "ToDo" / "lead" / "log" / "*.md")))[-60:]:
        try:
            parts.append(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)


def owner_lines(store: Any, project: str, *, days: int = 14) -> list[str]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store.read() as tx:
        root = _root(tx, project)
        rows = tx.query(
            "SELECT id,from_kind,kind,body,created_at,delivered_at FROM messages WHERE created_at>=? "
            "AND (from_kind='owner' OR kind='contract') AND (to_task=? OR to_task LIKE ? OR to_task LIKE ?) "
            "ORDER BY created_at", (since, f"{project}/TZ-LEAD", f"{project}/fix:TZ-LEAD%",
                                    f"{project}/TZ-LEAD:%"))
    text = _lead_texts(root)
    open_ = [r for r in rows if str(r["id"]) not in text]
    if not open_:
        return []
    lines = [f"Поручения владельца/контракты к ведущему без отметки в твоём каноне: {len(open_)}. "
             "Каждое — в книгу (LEDGER: owner_reports/обязательство с полем message=<id>) или в "
             "OWNER.md с причиной, почему не делаешь. Письмо читает один чат; не записал — "
             "следующий о нём не знает."]
    for r in open_[-6:]:
        body = re.sub(r"\s+", " ", str(r["body"] or ""))[:420]
        mark = "" if r["delivered_at"] else " (НЕ ДОСТАВЛЕНО: адресовано закрытому раунду)"
        lines.append(f"  [{r['id']}] {str(r['created_at'])[:16]}{mark}: {body}")
    return lines


def canon_lines(store: Any, project: str, info: dict[str, Any]) -> list[str]:
    with store.read() as tx:
        root = _root(tx, project)
    patterns = info.get("canon") or CANON_DEFAULT
    files: list[str] = []
    for pattern in patterns:
        for path in glob.glob(str(root / pattern), recursive=True):
            rel = Path(path).relative_to(root).as_posix()
            if Path(path).is_file() and rel not in files and "/log/" not in rel:
                files.append(rel)
        if len(files) >= CANON_MAX:
            break
    try:
        ledger = json.loads((root / "ToDo/lead/LEDGER.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = {}
    sources = ledger.get("sources") if isinstance(ledger.get("sources"), dict) else {}
    # Группа однотипных файлов без обещаний (журналы, фикстуры) разбирается маской с причиной:
    # «docs/journal/**»: {"reason": "..."} — без sha, но причина обязательна.
    groups = [k for k, v in sources.items() if "*" in k and isinstance(v, dict)
              and len(str(v.get("reason") or "")) >= 10]
    missing, changed = [], []
    for rel in files[:CANON_MAX]:
        entry = sources.get(rel)
        if entry is None and any(fnmatch.fnmatch(rel, g) for g in groups):
            continue
        if not isinstance(entry, dict):
            missing.append(rel)
        elif entry.get("sha256") != _sha(root / rel):
            changed.append(rel)
    if not missing and not changed:
        return []
    return [f"Документы проекта без разбора в книге: новых {len(missing)}, изменённых {len(changed)} "
            f"(из {len(files)}). Прочитай каждый и запиши в LEDGER.sources[путь] = "
            "{sha256, obligations: [O-*] или reason: почему обещаний человеку нет}; однотипную "
            "группу без обещаний — маской: \"docs/journal/**\": {reason}. "
            "Непрочитанный документ — непроверенное обещание.",
            "  " + ", ".join((changed + missing)[:10])]


def discovery_lines(store: Any, project: str) -> list[str]:
    since = (datetime.now(timezone.utc) - timedelta(hours=DISCOVERY_EVERY_H)).isoformat()
    with store.read() as tx:
        last = tx.one("SELECT ts FROM events WHERE kind='lead_discovery' AND project=? AND ts>=? "
                      "ORDER BY id DESC LIMIT 1", (project, since))
    if last is not None:
        return []
    return ["Разведка неучтённого (раз в сутки, отдельной вехой): пройди путь человека целиком — "
            "узнал, поставил, первый запуск, каждый день, сломалось, обновилось, заплатил, ушёл — "
            "и сравни с тем, что делают лучшие аналоги. Что важно человеку, но нет ни в каноне, ни "
            "в книге? Выводимое из канона — новое O-*; требующее решения владельца — вариант в "
            "OWNER.md с меткой. Итог — fleet_lead(action='discovery', summary=…, new=[O-*])."]


def _instruction_lines(store: Any, project: str) -> list[str]:
    """06.11 (фаза 0.3): поручения — объекты реестра со статусом; старый поиск id в тексте — запасной."""
    try:
        from . import instructions
        return instructions.lines(store, project)
    except Exception:  # noqa: BLE001
        return owner_lines(store, project)


def _obstacle_lines(store: Any, project: str) -> list[str]:
    """06.11 (фаза 1.1): препятствия ведущего и карточки владельцу."""
    try:
        from . import obstacles
        return obstacles.lines(store, project)
    except Exception:  # noqa: BLE001
        return []


def duty_lines(store: Any, project: str) -> list[str]:
    from . import goals
    try:
        info = goals.project_info(project)
    except Exception:  # noqa: BLE001
        info = {}
    out: list[str] = []
    for fn in (lambda: _instruction_lines(store, project), lambda: canon_lines(store, project, info),
               lambda: discovery_lines(store, project), lambda: _obstacle_lines(store, project)):
        try:
            out += fn()
        except (OSError, ValueError, KeyError):
            continue
    return out
