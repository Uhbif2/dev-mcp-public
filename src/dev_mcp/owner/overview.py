"""Read-only, privacy-minimal, versioned owner dashboard projection.

The dashboard receives a *projection*, never raw intent text, draft, scope,
credential, evidence.ref, worker logs or private model observations.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dev_mcp.fleet.interventions import InterventionLedger
from dev_mcp.fleet.store import Store

_SAFE_ID = re.compile(r"^[a-zA-Z0-9_.:#-]{1,128}$")
_STAGE_KINDS = frozenset({"run", "measure", "screen", "review", "external"})
_WORK_STATES = frozenset({"ready", "taken", "submitted", "accepted", "frozen", "rejected"})
_CAUSE_LABELS = {
    "path_forbidden": "Требуется разрешённый путь",
    "resource_down": "Недоступен внешний ресурс",
    "gate_red_foreign": "Ожидает исправления смежного компонента",
    "gate_red_own": "Ожидает исправления текущей работы",
    "needs_file_outside_owns": "Требуется отдельная задача на файл",
    "acceptance_impossible": "Проверка пока неисполнима",
    "procedure_unknown": "Идёт исследование способа",
    "apply_failed_schema": "Требуется исправить формат применения",
    "apply_failed_transient": "Ожидается восстановление сервиса",
    "producer_failed": "Поставщик результата сообщил ошибку",
    "owner_physical": "Требуется физическое действие",
    "owner_taste": "Ожидается выбор визуального варианта",
    "owner_money": "Требуется решение о расходе",
    "owner_private_data": "Требуется действие с личными данными",
}
_MAX_OUTCOMES = 2000
_INTERVENTION_DAYS = 7
_HUMAN_STAGES = (
    ("understood", "Понято", "Разбирается, что нужно получить", "understood.md"),
    ("executable_goal", "Цель", "Проверяются критерии результата", "goal.md"),
    ("approach_selected", "Способ", "Выбирается способ достижения", "approaches.md"),
    ("executable_plan", "План", "Распределяются работы", "plan.md"),
    ("implemented", "Сделано", "Выполняются и проверяются работы", "implementation.md"),
    ("clean_start", "С нуля", "Проверяется запуск с чистого окружения", "clean-start.md"),
    ("quality", "Качество", "Проверяется качество результата", "quality.md"),
    ("delivered", "Поставлено", "Результат готовится к поставке", "delivery.md"),
    ("handed_over", "Передано", "Готовится передача результата", "handover.md"),
)
_STAGE_INFO = {key: (label, description, filename)
               for key, label, description, filename in _HUMAN_STAGES}
_STAGE_INFO.update({
    "hypotheses_plan": ("Гипотезы", "Готовится план различающих экспериментов", "hypotheses.md"),
    "experiments": ("Эксперименты", "Выполняются воспроизводимые эксперименты", "../experiments"),
    "verified": ("Проверено", "Проверяется независимое воспроизведение", "verification.md"),
    "report": ("Отчёт", "Выводы связываются с наблюдениями", "report.md"),
    "rollback_plan": ("Откат", "Проверяется план операции и отката", "rollback-plan.md"),
    "owner_approved": ("Решение", "Ожидается ваше решение по конкретному плану", "rollback-plan.md"),
    "executed": ("Исполнено", "Исполняется согласованная операция", "execution.md"),
})


def _opaque(value: Any) -> str:
    text = str(value or "")
    if _SAFE_ID.fullmatch(text):
        return text
    return "ref-" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _title(row: Any) -> str:
    """Titles are opt-in public; never echo unclassified owner input."""
    try:
        spec = json.loads(row["spec"])
    except (TypeError, ValueError):
        spec = {}
    if not isinstance(spec, dict) or spec.get("dashboard_visibility") != "public":
        return "Результат " + _opaque(row["id"])[:30]
    text = str(row["title"] or "")
    if not text or len(text) > 160 or any(ord(char) < 32 for char in text):
        return "Результат " + _opaque(row["id"])[:30]
    return text  # Client MUST use textContent, never innerHTML.


def _version(row: Any) -> str:
    try:
        spec = json.loads(row["evidence_spec"])
    except (TypeError, ValueError):
        return f"rev:{row['rev']}"
    if isinstance(spec, dict):
        value = spec.get("subject_version")
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            return value
    return f"rev:{row['rev']}"


def _revision(data: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _empty(status: str, message: str, *, interventions: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "schema": 1, "status": status, "message": message,
        "intents": [], "issues": [], "revision": "0" * 64,
        "human": {"actions": [], "projects": [], "done": [],
                  "fleet": {"agents": 0, "queue": 0, "release": None, "overloads": 0}},
    }
    if interventions is not None:
        data["interventions"] = interventions
        data["revision"] = _revision({key: value for key, value in data.items() if key != "revision"})
    return data


def _human_line(text: str, *, limit: int = 240) -> str:
    """One visible plain-text line; never pass a markdown document to the UI."""
    line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", text.strip())
    line = re.sub(r"\s+", " ", line)
    return line[:limit] if line and not any(ord(ch) < 32 for ch in line) else ""


def _section_lines(root: Path, document: str, heading: str) -> list[str]:
    """Read bounded, verified stage artifacts only inside a registered project."""
    try:
        if not root.is_dir() or root.is_symlink():
            return []
        folder = root / "intent"
        file = folder / document
        if folder.is_symlink() or file.is_symlink() or not file.is_file():
            return []
        if file.stat().st_size > 48_000:
            return []
        lines = file.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError, ValueError):
        return []
    result: list[str] = []
    active = False
    for line in lines:
        if line.startswith("## "):
            if active:
                break
            active = line[3:].strip().casefold() == heading.casefold()
            continue
        if active:
            clean = _human_line(line)
            if clean:
                result.append(clean)
            if len(result) >= 12:
                break
    return result


def _human_projection(rows: list[Any], evidence: dict[str, list[Any]],
                      roots: dict[str, str], fleet: dict[str, Any],
                      completed: set[tuple[str, str]],
                      owner_requests: dict[str, list[str]] | None = None,
                      owner_planning: set[str] | None = None) -> dict[str, Any]:
    """A single compact owner-facing card per intent project, not per outcome id."""
    from dev_mcp.intents.template import selected_stages

    grouped: dict[str, dict[str, Any]] = {}
    live_roots = {str(row["id"]) for row in rows
                  if row["parent_id"] is None and row["state"] in {"open", "done"}}
    for row in rows:
        try:
            spec = json.loads(row["spec"])
        except (TypeError, ValueError):
            continue
        if not isinstance(spec, dict) or "stage" in spec and spec["stage"] not in _STAGE_INFO:
            continue
        project = str(row["project"])
        if (project not in roots or row["state"] == "dropped"
                or row["parent_id"] is not None and str(row["parent_id"]) not in live_roots):
            continue
        try:
            chosen = selected_stages(spec.get("kind", "build"), spec.get("size", "large"))
        except (KeyError, TypeError):
            continue
        item = grouped.setdefault(project, {"stages": {}, "started": str(row["created_ts"]),
                                            "chosen": chosen})
        item["chosen"] = chosen
        item["structured"] = item.get("structured", False) or "source_id" in spec or "intent_ref" in spec
        stage = spec.get("stage")
        if stage in _STAGE_INFO:
            expected = json.loads(row["evidence_spec"])
            matching = [proof for proof in evidence[str(row["id"])]
                        if proof["subject_version"] == _version(row)
                        and (not expected.get("kind") or proof["kind"] == expected["kind"])]
            item["stages"][stage] = (row["state"] == "done"
                                     and any(p["verdict"] == "pass" for p in matching))

    projects: list[dict[str, Any]] = []
    actions: list[dict[str, str]] = []
    finished: list[dict[str, str]] = []
    for project, item in sorted(grouped.items()):
        root = Path(roots[project])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            continue
        name = (_section_lines(root, "understood.md", "Интенция") or
                [_human_line(project.replace("_", " ").replace("-", " "))])[0]
        stages = [
            {"label": label, "done": item["stages"].get(key, False)}
            for key in item["chosen"]
            for label in [_STAGE_INFO[key][0]]
        ]
        current = next((key for key in item["chosen"]
                        if not item["stages"].get(key, False)), "handed_over")
        label, description, filename = _STAGE_INFO[current]
        card = {"title": name or "Интенция без названия", "project": project,
                "stages": stages, "current": label,
                "description": ("Результат передан" if all(s["done"] for s in stages)
                                else description),
                "started": item["started"], "folder": root.as_uri(),
                "document": (root / "intent" / filename).as_uri()}
        if project in (owner_planning or set()) and current == 'owner_approved':
            card['description'] = 'Готовится и проверяется конкретное предложение'
        # Versioned intents carry future constraints in understood.md too.
        # Only an open typed owner obstacle (or the proved ops approval below)
        # means that the owner is required NOW. Legacy documents retain their
        # existing explicit action contract until migrated.
        requests = (list((owner_requests or {}).get(project, [])) if item.get("structured")
                    else _section_lines(root, "understood.md", "Нужно от владельца"))
        if item.get("structured") and not current_ops_approval_requested(root):
            requests = [r for r in requests if r.split(';',1)[0].strip() != "Одобрить операционный план с откатом"]
        if ("owner_approved" in item["chosen"] and item["stages"].get("rollback_plan")
                and not item["stages"].get("owner_approved")):
            if not item.get("structured"):
                requests += _section_lines(root, "rollback-plan.md", "Нужно от владельца")
        for line in dict.fromkeys(requests):
            # The cap is global, not per project. A break after append alone
            # still leaks one more action for every subsequent intent project.
            if len(actions) >= 30:
                break
            if line.casefold().strip(" .") == "ничего":
                continue
            chunks = [part.strip() for part in line.split(";") if part.strip()]
            action_text = chunks[0] if chunks else line
            default = next((part.partition(":")[2].strip() for part in chunks[1:]
                            if part.casefold().startswith("по умолчанию:")),
                           "Оставить без подтверждения")
            reason = next((part.partition(":")[2].strip() for part in chunks[1:]
                           if part.casefold().startswith("почему:")),
                          "Для этого шага необходимо действие владельца")
            action_id = hashlib.sha256(
                (project + "\n" + action_text).encode("utf-8")).hexdigest()[:20]
            if (project, action_id) in completed:
                continue
            actions.append({
                "project": project, "title": name,
                "action_id": action_id,
                "action": action_text, "default": default,
                "reason": reason,
            })
            if len(actions) >= 30:
                break
        if item["stages"].get("handed_over"):
            note = _section_lines(root, "handover.md", "Готово")
            finished.append({"project": project, "title": name,
                             "text": " ".join(note)[:2000] if note else "Готово"})
        else:
            projects.append(card)
    return {"actions": actions, "projects": projects, "done": finished, "fleet": fleet}


def current_ops_approval_requested(root: Path) -> bool:
    """An explicit 'nothing for this document now' is not an approval request.

    This never grants approval or unblocks execution. Future permissions remain
    constraints until a concrete execution plan is actually requested.
    """
    lines = _section_lines(root,"rollback-plan.md","Нужно от владельца")
    first = lines[0].replace("*","").replace("`","").casefold() if lines else ""
    return not re.match(r"^для текущ(?:его|ей) (?:документа|этапа|шага|стадии)\s*[—–:-]\s*ничего\b",first)


def _fleet_summary(tx: Any) -> dict[str, Any]:
    from dev_mcp.fleet.flow import agent_states
    states = agent_states(tx)
    from dev_mcp.fleet.owner_notices import attention
    release = tx.one(
        "SELECT ts,kind FROM events WHERE kind IN ('platform_deployed','platform_deploy') "
        "ORDER BY id DESC LIMIT 1"
    )
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    return {
        "agents": states['working'],
        "agent_states": states,
        "queue": int(tx.scalar("SELECT COUNT(*) FROM tasks t JOIN projects p ON p.name=t.project "
                               "WHERE t.state='ready' AND p.active=1") or 0),
        "release": ({"at": str(release["ts"]),
                     "success": release["kind"] == "platform_deployed"} if release else None),
        "overloads": int(tx.scalar("SELECT COUNT(*) FROM events WHERE "
                                   "lower(kind) LIKE '%overload%' AND ts>=?", (since,)) or 0),
        "pending_notices": _pending_notices(tx),
        "system_alerts": attention(tx),
    }


def _pending_notices(tx: Any) -> int:
    """Transport failures only; API acceptance never means seen or approved."""
    if tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_notices'"):
        return int(tx.scalar("SELECT COUNT(*) FROM owner_notices WHERE state='active' "
                             "AND delivery_state NOT IN ('api_accepted','clicked','browser_attention')") or 0)
    latest: dict[tuple[str, str, str], Any] = {}
    for row in tx.query(
        "SELECT e.project,e.kind,e.payload FROM events e JOIN projects p ON p.name=e.project "
        "WHERE p.active=1 AND e.kind IN "
        "('owner_channel.sent','owner_channel.toast_attempt','owner.done') ORDER BY e.id"
    ):
        try:
            data = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get("sha256"):
            continue
        key = (str(row["project"]), str(data.get("kind")), str(data["sha256"]))
        latest[key] = None if row["kind"] == "owner.done" else data.get("toast_accepted")
    return sum(value is False for value in latest.values())


def _intervention_summary(tx: Any, *, project: str | None = None) -> dict[str, Any]:
    """Seven calendar days of ledger metrics in the SAME read transaction as outcomes.

    InterventionLedger.__init__ creates tables and audit events even if they
    already exist. A dashboard poll must never invoke that mutating constructor.
    Its metrics() method only needs a store.read(), project and root, so use a
    read-only view of the ongoing snapshot instead.
    """
    now = datetime.now(timezone.utc)
    dates = [(now.date() - timedelta(days=offset)).isoformat()
             for offset in range(_INTERVENTION_DAYS - 1, -1, -1)]
    daily: dict[str, dict[str, int]] = {date: {} for date in dates}
    exists = tx.scalar(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='intervention_log'"
    )
    if exists:
        query = (
            "SELECT DISTINCT l.project AS name, COALESCE(p.root,'') AS root "
            "FROM intervention_log l LEFT JOIN projects p ON p.name=l.project"
        )
        args: tuple[str, ...] = ()
        if project is not None:
            query += " WHERE l.project=?"
            args = (project,)
        for row in tx.query(query + " ORDER BY l.project", args):
            # The producer owns schema creation. This adapter cannot mutate
            # SQLite, and a nested Store.read would break our read snapshot.
            ledger = object.__new__(InterventionLedger)
            ledger.store = SimpleNamespace(read=lambda: nullcontext(tx))
            ledger.project = str(row["name"])
            ledger.root = Path(str(row["root"]))
            metrics = ledger.metrics(now=now, days=_INTERVENTION_DAYS)
            for day, classes in metrics["by_day"].items():
                if day not in daily:
                    continue
                for category, count in classes.items():
                    label = _opaque(category)
                    daily[day][label] = daily[day].get(label, 0) + int(count)
    daily = {day: dict(sorted(counts.items())) for day, counts in daily.items()}
    by_class: dict[str, int] = {}
    for counts in daily.values():
        for category, count in counts.items():
            by_class[category] = by_class.get(category, 0) + count
    today, yesterday = dates[-1], dates[-2]
    today_count = sum(daily[today].values())
    yesterday_count = sum(daily[yesterday].values())
    return {
        "goal": 0, "today": today_count, "yesterday": yesterday_count,
        "rising": today_count > yesterday_count, "available": bool(exists),
        "by_day": daily,
        "by_class": dict(sorted(by_class.items())),
    }


def snapshot(store: Store, *, project: str | None = None) -> dict[str, Any]:
    """Return one consistent read transaction of applied/proven/delivered state.

    No database mutation and no network I/O. The revision changes only when the
    public projection changes; clients may use it for conditional redraws.
    """
    if project is not None and (not isinstance(project, str) or
                                len(project) > 256 or not project.strip()):
        return _empty("error", "Некорректный фильтр проекта")
    try:
        with store.read() as tx:
            interventions = _intervention_summary(tx, project=project)
            fleet = _fleet_summary(tx)
            present = tx.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='outcomes'")
            if not present:
                empty = _empty("empty", "Пока нет зарегистрированных результатов",
                               interventions=interventions)
                empty["human"]["fleet"] = fleet
                empty["revision"] = _revision({k: v for k, v in empty.items() if k != "revision"})
                return empty
            where = " WHERE project=?" if project is not None else ""
            args: tuple[str, ...] = (project,) if project is not None else ()
            rows = tx.query("SELECT * FROM outcomes" + where + " ORDER BY intent_id,created_ts,id", args)
            if len(rows) > _MAX_OUTCOMES:
                return _empty("error", "Слишком много результатов для одного снимка")
            if not rows:
                empty = _empty("empty", "Пока нет зарегистрированных результатов",
                               interventions=interventions)
                empty["human"]["fleet"] = fleet
                empty["revision"] = _revision({k: v for k, v in empty.items() if k != "revision"})
                return empty
            project_roots = {str(r["name"]): str(r["root"]) for r in tx.query(
                "SELECT name,root FROM projects WHERE active=1")}
            completed: set[tuple[str, str]] = set()
            for acknowledged in tx.query("SELECT project,payload FROM events WHERE kind='owner.done'"):
                try:
                    receipt = json.loads(str(acknowledged["payload"]))
                except (ValueError, TypeError):
                    continue
                if isinstance(receipt, dict) and isinstance(receipt.get("action_id"), str):
                    completed.add((str(acknowledged["project"]), receipt["action_id"]))
            ids = {str(row["id"]) for row in rows}
            evidence = defaultdict(list)
            work = defaultdict(list)
            obstacles = defaultdict(list)
            owner_requests: dict[str, list[str]] = defaultdict(list)
            owner_planning: set[str] = set()
            open_projects = {str(row["id"]): str(row["project"]) for row in rows if row["state"] == "open"}
            # Query all in the SAME read transaction, filter to selected outcomes
            # before forming client output, and never include untrusted free text.
            for item in tx.query(
                "SELECT outcome_id,kind,subject_version,env_version,verdict,ts FROM evidence"
            ):
                if item["outcome_id"] in ids:
                    evidence[str(item["outcome_id"])].append(item)
            if tx.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='outcome_work'"):
                for item in tx.query(
                    "SELECT w.outcome_id,w.project,w.tz,t.state FROM outcome_work w "
                    "JOIN tasks t ON t.project=w.project AND t.tz=w.tz"
                ):
                    if item["outcome_id"] in ids:
                        work[str(item["outcome_id"])].append(item)
            for item in tx.query(
                "SELECT outcome_id,type,cause_code,change_source,owner_action FROM obstacles "
                "WHERE state='open' ORDER BY ts,id"
            ):
                if item["outcome_id"] in ids:
                    obstacles[str(item["outcome_id"])].append(item)
                    if item["type"] == "owner_action" and item["outcome_id"] in open_projects:
                        if str(item['owner_action'] or '').startswith('Одобрить операционный план с откатом'):
                            from dev_mcp.fleet.owner_notices import ops_refinement_id
                            pending = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?",
                                (open_projects[item['outcome_id']],ops_refinement_id(str(item['outcome_id']))))
                            if pending and pending['state'] != 'accepted':
                                owner_planning.add(open_projects[item['outcome_id']])
                                continue  # submitted/refined text is not a reviewed owner proposal
                        owner_requests[open_projects[item["outcome_id"]]].append(
                            _human_line(str(item["owner_action"] or "")))
    except Exception:
        # Never leak a SQLite exception (it may contain source material/paths).
        return _empty("error", "Не удалось прочитать состояние результатов")

    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        oid = str(row["id"])
        subject_version = _version(row)
        matching = [ev for ev in evidence[oid] if ev["subject_version"] == subject_version]
        passed = [ev for ev in matching if ev["verdict"] == "pass"]
        current_work = [
            {"tz": _opaque(w["tz"]), "state": str(w["state"])}
            for w in sorted(work[oid], key=lambda w: (w["project"], w["tz"]))
            if w["state"] in _WORK_STATES
        ]
        applied = any(w["state"] == "accepted" for w in current_work) or any(
            item["kind"] == "review" for item in passed
        )
        proven = any(item["kind"] in _STAGE_KINDS for item in passed)
        delivered = proven and any(item["kind"] == "delivery" for item in passed)
        nodes[oid] = {
            "id": _opaque(oid), "parent_id": _opaque(row["parent_id"]) if row["parent_id"] else None,
            "intent_id": _opaque(row["intent_id"]), "project": _opaque(row["project"]),
            "title": _title(row), "state": str(row["state"]), "outcome_rev": row["rev"],
            "subject_version": subject_version,
            "stage": {"applied": applied, "proven": proven, "delivered": delivered},
            "evidence": sorted([
                {"kind": str(e["kind"]), "subject_version": str(e["subject_version"]),
                 "env_version": _opaque(e["env_version"]), "verdict": str(e["verdict"])}
                for e in matching
            ], key=lambda e: (e["kind"], e["subject_version"], e["env_version"], e["verdict"])),
            "work": current_work,
            "waiting": [
                {"type": str(ob["type"]), "cause_code": str(ob["cause_code"]),
                 "reason": _CAUSE_LABELS.get(str(ob["cause_code"]), "Ожидание устранения препятствия")}
                for ob in obstacles[oid]
            ],
            "children": [],
        }
        if row["state"] == "open" and not current_work and not obstacles[oid]:
            nodes[oid]["waiting"].append({
                "type": "unknown", "cause_code": "no_next_step",
                "reason": "Не назначен следующий шаг — требуется работа контроллера",
            })

    issues: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    def parent_is_valid(oid: str) -> bool:
        """Reject orphan and cyclic parent chains without recursive rendering."""
        seen: set[str] = set()
        pos = oid
        while pos in nodes:
            if len(seen) >= 96:
                issues.append("Дерево результатов превышает допустимую глубину")
                return False
            if pos in seen:
                issues.append("Обнаружен цикл в дереве результатов")
                return False
            seen.add(pos)
            parent = nodes[pos]["parent_id"]
            if parent is None:
                return True
            if parent not in nodes:
                issues.append("Результат с отсутствующим родителем")
                return False
            if nodes[parent]["intent_id"] != nodes[pos]["intent_id"]:
                issues.append("Несовместимые интенции в родительской связи")
                return False
            pos = parent
        return False

    valid = {oid: parent_is_valid(oid) for oid in nodes}
    for oid, node in nodes.items():
        parent = node["parent_id"]
        if parent and valid[oid] and valid.get(parent):
            nodes[parent]["children"].append(node)
        else:
            grouped[node["intent_id"]].append(node)
    def sort_children(node: dict[str, Any]) -> None:
        pending = [node]
        while pending:
            item = pending.pop()
            item["children"].sort(key=lambda n: n["id"])
            pending.extend(item["children"])
    intents = []
    for intent_id, roots in sorted(grouped.items()):
        roots.sort(key=lambda n: n["id"])
        for node in roots:
            sort_children(node)
        intents.append({"id": intent_id, "results": roots})
    data = {
        "schema": 1, "status": "error" if issues else "ready", "message": (
            "Часть связей результатов некорректна" if issues else ""
        ), "intents": intents, "issues": list(dict.fromkeys(issues)),
        "interventions": interventions,
        "human": _human_projection(rows, evidence, project_roots, fleet, completed, owner_requests, owner_planning),
    }
    data["revision"] = _revision(data)
    return data
