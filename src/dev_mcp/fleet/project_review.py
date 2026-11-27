"""Turn independently confirmed project-review findings into ordinary Fleet work."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .lead_admin import OWNER_CANON, criteria_block
from .model import norm_path, paths_overlap
from .project_identity import require_available

TASK_ID = r"TZ-RV-[0-9a-f]{16}"
EVIDENCE = re.compile(r"([^:\r\n]+):([1-9]\d*)")
ACTIVE_STATES = "('ready','taken','submitted','frozen')"
ARCHIVE_PROTECTED = {
    "readme.md", "roadmap.md", "status.md", "docs/readme.md",
    "docs/roadmap.md", "docs/status.md", "research/readme.md",
}


def task_id(tz: str) -> bool:
    return bool(re.fullmatch(rf"(?:fix:)?{TASK_ID}(?::\d+)?", tz, re.I))


def reviewer_instruction(mode: str) -> str:
    if mode == "gap":
        return (
            "Сверь обещанный пользователю результат с рабочим путём в коде, поставкой и доступными "
            "живыми свидетельствами. Начни с требований в данном блоке. Найди недостающий сценарий, "
            "свяжи его с точным путём реализации и объясни наблюдаемое расхождение. "
            "Тесты и документация сами по себе не доказывают работу продукта. "
            "Неизвестное состояние поставки или выбор владельца укажи в disputed."
        )
    return (
        "Прочитай указанный блок документов этой папки и сверяй с соседними файлами, "
        "актуальным каноном, кодом и открытой работой. Ищи документы для переноса в архив, "
        "слияния с действующим источником или постановки ещё нужной работы. "
        "Для архива и слияния покажи, куда уже перенесено актуальное или что требуется перенести, "
        "какие ссылки проверить и что хранится как история. Возраст файла сам по себе не улика. "
        "Живые ТЗ, договоры, лицензии и канон оставляй на месте. "
        "Пиши отдельную находку issue на каждую подтверждённую невыполненную работу из документа."
    )


def schema(mode: str) -> str:
    actions = "issue" if mode == "gap" else "archive|consolidate|issue"
    return (
        '{"findings":[{"severity":"critical|high|medium|low","kind":"' + actions
        + '","key":"стабильный-slug-сценария","location":"файл:строка реализации или документа",'
        '"source":"файл:строка требования или актуального источника",'
        '"summary":"результат пользователю или полезное изменение документа",'
        '"evidence":"наблюдаемый факт и почему текущая работа его не закрывает",'
        '"verification":"какой наблюдаемый результат и каким способом проверить",'
        '"owns":["конкретный/существующий/файл"]}],"checked":[],"disputed":[]}'
    )


def context(db: Path, project: str) -> str:
    from .goals import goal_block

    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT tz,title,state,owns FROM tasks WHERE project=? AND state IN {ACTIVE_STATES} "
            "ORDER BY CASE state WHEN 'taken' THEN 0 WHEN 'submitted' THEN 1 ELSE 2 END,"
            " priority,updated_at DESC LIMIT 25", (project,),
        ).fetchall()
    current = [{"tz": row["tz"], "title": row["title"], "state": row["state"],
                "owns": json.loads(row["owns"] or "[]")[:4]} for row in rows]
    return goal_block(project)[:2500] + "\nЖивая очередь, до 25 строк:\n" + json.dumps(
        current, ensure_ascii=False)[:5000]


def candidate_fields(item: dict[str, Any], revision: str) -> dict[str, Any]:
    """Keep provenance from the scheduler and never accept a supplied command."""
    owns = item.get("owns")
    return {
        "key": str(item.get("key") or "")[:90],
        "source": str(item.get("source") or "")[:240],
        "verification": str(item.get("verification") or "")[:600],
        "owns": owns if isinstance(owns, list) and len(owns) <= 6
        and all(isinstance(p, str) and len(p) <= 240 for p in owns) else [],
        "revision": revision,
    }


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or "\\" in value or ":" in value or path.is_absolute()
            or path.as_posix() != value or ".." in path.parts
            or any(part.endswith((" ", ".")) or part.startswith("-") for part in path.parts)):
        raise ValueError(f"недопустимый путь {value!r}")
    return value


def _anchor(value: Any) -> str:
    match = EVIDENCE.fullmatch(str(value))
    if not match:
        raise ValueError("улика требует точный путь:строку")
    return _relative(match[1])


def _text(item: dict[str, Any], key: str) -> str:
    value = " ".join(str(item.get(key) or "").split())
    if len(value) < 12 or re.search(r"\[(?:fleet-|lead-)", value, re.I):
        raise ValueError(f"некорректное поле {key}")
    return value


def _paths(root: Path, mode: str, item: dict[str, Any]) -> tuple[list[str], list[str]]:
    from .nightly_review import ARCHIVE_DIRS, _document, _line_count, _safe

    source, location = _anchor(item["source"]), _anchor(item["location"])
    action = item["kind"]
    owns = list(dict.fromkeys(_relative(p) for p in item["owns"]))
    references = list(dict.fromkeys([source, location, *owns]))
    if not owns or source == location:
        raise ValueError("нужны разные источники и конкретные файлы зоны записи")
    if action == "issue":
        if not _document(source) or _document(location):
            raise ValueError("нужны документ с требованием и реализация, интерфейс или поставка")
    elif (mode != "docs" or action not in {"archive", "consolidate"} or location not in owns
          or not all(_document(p) for p in references)):
        raise ValueError("архив и объединение требуют конкретные документы и актуальный источник")
    if action == "archive" and source in owns:
        raise ValueError("для изменения актуального источника используй consolidate")
    if action == "consolidate" and source not in owns:
        raise ValueError("для объединения включи актуальный источник в owns")
    if action != "issue" and location.casefold() in ARCHIVE_PROTECTED:
        raise ValueError("действующий указатель проекта защищён от архивации")
    for rel in references:
        file = _safe(root, rel)
        if file is None or file.resolve() != root / rel:
            raise ValueError(f"файл отсутствует или является ссылкой: {rel}")
    for reference in (item["source"], item["location"]):
        rel, _, line = reference.rpartition(":")
        if int(line) > _line_count(root / rel):
            raise ValueError(f"строка свидетельства отсутствует: {reference}")
    for rel in owns:
        low = rel.casefold()
        if any(low == prefix.rstrip("/") or low.startswith(prefix) for prefix in OWNER_CANON):
            raise ValueError(f"файл входит в защищённый канон: {rel}")
        parts = low.split("/")
        if parts[0] in {".github", ".codex", ".claude"} or parts[:2] in (
                ["todo", "tz"], ["todo", "tz-drafts"], ["todo", "fleet"], ["todo", "lead"]):
            raise ValueError(f"служебный файл: {rel}")
        if action != "issue" and any(marker in Path(low).name for marker in
                                     ("license", "licence", "third-party-notice", "changelog")):
            raise ValueError(f"сохраняемый документ: {rel}")
        if set(parts[:-1]) & ARCHIVE_DIRS:
            raise ValueError(f"файл уже находится в архиве: {rel}")
    if action in {"archive", "consolidate"}:
        archive = "docs/archive/review/" + location
        target = root / archive
        if target.exists() or target.resolve() != target:
            raise ValueError("место в архиве занято или проходит через ссылку")
        owns.append(archive)
    return owns, references


def _publish_one(store: Any, project: str, root: Path, mode: str,
                 item: dict[str, Any]) -> tuple[str, str]:
    from .nightly_review import _git

    action = item["kind"]
    key = str(item["key"]).casefold()
    if not re.fullmatch(r"[\w-]{4,80}", key) or not re.fullmatch(r"[a-f0-9]{40,64}", item["revision"]):
        raise ValueError("нужен стабильный ключ и Git-ревизия наблюдения")
    summary, evidence, verification = (_text(item, key) for key in
                                       ("summary", "evidence", "verification"))
    anchor = _anchor(item["location"] if action != "issue" else item["source"])
    # For cleanup one source document has a single durable identity. A product
    # obligation can have several independent gaps in the same requirements file.
    fingerprint = hashlib.sha256(
        json.dumps([anchor.casefold(), key if action == "issue" else "cleanup"],
                   ensure_ascii=False).encode()
    ).hexdigest()[:16]
    tz = f"TZ-RV-{fingerprint}"
    with store.read() as tx:
        if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?", (project, tz)):
            return tz, "already_registered"
    owns, references = _paths(root, mode, item)
    literal = [":(literal)" + path for path in references]
    tracked = set(_git(root, "ls-files", "-z", "--", *literal).split("\0"))
    if not set(references) <= tracked:
        raise ValueError("улики и зоны записи должны быть зарегистрированы в Git")
    if _git(root, "diff", "--no-ext-diff", "--name-only", "-z",
            item["revision"], "--", *literal):
        raise ValueError("файлы изменились после наблюдения; нужен повторный проход")
    absolute = [norm_path(str(root / rel)) for rel in owns]
    criteria = [verification]
    body = (
        f"[fleet-project-review] {mode} / {item['id']}\n{summary}\n\n"
        f"Наблюдение на {item['revision']}: {item['location']}. "
        f"Источник: {item['source']}.\n{evidence}\n\n"
        "Перед правкой проверь улику на текущей версии, открытые ТЗ и канон. "
        "При сдаче приложи наблюдение до и после и точную версию проверки."
    )
    if action in {"archive", "consolidate"}:
        body += (
            f"\nПеренеси оригинал {anchor} в {owns[-1]} с сохранением байтов. "
            f"При объединении сначала перенеси актуальное в {_anchor(item['source'])}. "
            "Перепроверь обратные ссылки, укажи новый путь, действующий источник и причину переноса в сдаче. "
            "Если осталась нужная работа без ТЗ, поставь её через ведущего до переноса и приложи ID. "
            "Если перенос сломает ссылку вне owns, передай конфликт ведущему до перемещения."
        )
        criteria.append("Сверь хеш оригинала с архивом, проверь ссылочную навигацию и ID оставшейся работы.")
    else:
        criteria.append("Воспроизведи пользовательский сценарий через штатный вход, укажи ревизию и наблюдаемый эффект.")
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        require_available(tx, project, str(root))
        if not tx.one("SELECT 1 FROM projects WHERE name=? AND active=1", (project,)):
            raise ValueError("проект больше не активен")
        if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?", (project, tz)):
            return tz, "already_registered"
        for claim in tx.query(
            "SELECT c.project,c.tz,p.path_norm FROM claims c JOIN claim_paths p ON p.claim_id=c.id "
            "WHERE c.state IN ('active','preparing')"
        ):
            if any(paths_overlap(a, claim["path_norm"]) for a in absolute):
                return f"{claim['project']}/{claim['tz']}", "overlap"
        for row in tx.query(
            f"SELECT project,tz,owns FROM tasks WHERE state IN {ACTIVE_STATES} AND kind<>'review'"
        ):
            if any(paths_overlap(a, b) for a in absolute for b in json.loads(row["owns"] or "[]")):
                return f"{row['project']}/{row['tz']}", "overlap"
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, tz, summary[:200], "ready", 4 if item["severity"] in {"high", "critical"} else 6,
             "work", "", "", "[]", json.dumps(absolute, ensure_ascii=False), "[]", "[]",
             body + criteria_block(criteria), stamp, stamp),
            event={"kind": "project_review_issue", "project": project, "tz": tz,
                   "payload": {"mode": mode, "candidate": item["id"],
                               "source_quick": item["source_quick"], "revision": item["revision"],
                               "owns": owns}},
        )
    return tz, "registered"


def publish(store: Any, project: str, root: Path, mode: str, candidates: list[dict[str, Any]],
            approved: Any) -> list[dict[str, str]]:
    if project == "dev-mcp-self" or root == Path(__file__).resolve().parents[3]:
        return []
    if not isinstance(approved, list):
        return []
    ids = {item for item in approved if isinstance(item, str)}
    outcomes = []
    for row in candidates:
        if row["id"] not in ids:
            continue
        try:
            result, status = _publish_one(store, project, root, mode, row)
        except (KeyError, ValueError, subprocess.CalledProcessError) as exc:
            result, status = str(exc), "needs_evidence"
        outcomes.append({"id": row["id"], "status": status, "result": result})
    return outcomes
