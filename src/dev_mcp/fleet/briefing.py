from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .model import Task


_BLIND_MARKER = "[fleet-blind]"
_BLIND_BATCH = re.compile(r"batch-[a-z0-9]{6,40}\Z")


def _current_blind_digest(root: str, batch: str, attempt_id: str | None = None) -> str | None:
    """Recompute the server's seal over the current candidate blind.json bytes.

    The manifest only selects candidate IDs; its contents never authorize an
    unsealed reviewer to see candidate material. Return None on unsafe paths or
    incomplete artifacts, so a stale seal cannot unlock the next phase.
    """
    if not root or not _BLIND_BATCH.fullmatch(batch):
        return None
    base = Path(root)
    manifest = base / "data" / "batches" / "out" / batch / "MANIFEST.md"
    if manifest.is_symlink() or not manifest.is_file():
        return None
    raw = manifest.read_bytes()
    if not raw or len(raw) > 1024 * 1024:
        return None
    text = raw.decode("utf-8")
    start, end = "<!-- MILL_MANIFEST_JSON", "MILL_MANIFEST_JSON -->"
    if text.count(start) != 1 or text.count(end) != 1:
        return None
    data = json.loads(text.split(start, 1)[1].split(end, 1)[0])
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return None
    candidates = sorted(
        cid for cid, entry in entries.items()
        if isinstance(cid, str) and isinstance(entry, dict)
        and entry.get("outcome") == "candidate"
    )
    if not candidates:
        return None
    review = base / "data" / "reviews" / batch
    if attempt_id is not None:
        if not re.fullmatch(r"claim-[1-9][0-9]*", attempt_id):
            return None
        review = review / "attempts" / attempt_id
    if review.is_symlink() or not review.is_dir():
        return None
    digests: list[tuple[str, str]] = []
    for cid in candidates:
        safe = (cid if re.fullmatch(r"[A-Za-z0-9._-]{1,120}", cid) and cid not in (".", "..")
                else "case-" + hashlib.sha256(cid.encode("utf-8")).hexdigest()[:24])
        case_dir = review / safe
        if case_dir.is_symlink():
            return None
        blind = case_dir / "blind.json"
        if blind.is_symlink() or not blind.is_file() or blind.stat().st_size > 128 * 1024:
            return None
        digests.append((cid, hashlib.sha256(blind.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(digests, ensure_ascii=False).encode("utf-8")).hexdigest()


def _blind_batch_marker(task: Task) -> str | None:
    """Only an explicit, machine-shaped marker opts into a sealed blind brief.

    None means ordinary task. An empty string is a malformed blind marker,
    which must fail closed rather than exposing the rest of task.body.
    Never infer a batch from rejection prose or an arbitrary source path.
    """
    if task.project != "Mill Works" or not task.body:
        return None
    first = task.body.splitlines()[0].strip()
    if not first.startswith(_BLIND_MARKER):
        return None
    try:
        marker = json.loads(first[len(_BLIND_MARKER):].strip())
    except (ValueError, TypeError):
        return ""
    if not isinstance(marker, dict) or set(marker) - {"batch_id", "phase"}:
        return ""
    if marker.get("phase") not in (None, "case_only"):
        return ""
    batch = marker.get("batch_id")
    return batch if isinstance(batch, str) and _BLIND_BATCH.fullmatch(batch) else ""


def _blind_attempt_id(store: Any, worker_id: str, task: Task) -> str | None:
    """A retry's namespace is derived from the active server claim, never its body."""
    if task.kind != "fix" or not task.tz.startswith("fix:TZ-MR-batch-"):
        return None
    with store.read() as tx:
        claim = tx.one(
            "SELECT id FROM claims WHERE worker_id=? AND project=? AND tz=? "
            "AND state='active' ORDER BY id DESC LIMIT 1",
            (worker_id, task.project, task.tz),
        )
    return f"claim-{int(claim['id'])}" if claim is not None else None


def _blind_is_sealed(store: Any, worker_id: str, task: Task, batch: str) -> bool:
    """A trusted server seal must bind this worker, batch and current claim.

    The marker/body, a local blind.json or the agent's assertion cannot advance
    the phase. The controller owns emitting the ``blind_seal`` event after
    verifying that the fresh blind artifact preceded candidate access.
    """
    if not worker_id or not batch:
        return False
    try:
        with store.read() as tx:
            claim = tx.one(
                "SELECT id FROM claims WHERE worker_id=? AND project=? AND tz=? "
                "AND state='active' ORDER BY id DESC LIMIT 1",
                (worker_id, task.project, task.tz),
            )
            if claim is None:
                return False
            events = tx.query(
                "SELECT payload FROM events WHERE kind='blind_seal' AND "
                "worker_id=? AND project=? AND tz=? ORDER BY id DESC LIMIT 10",
                (worker_id, task.project, task.tz),
            )
        attempt_id = (f"claim-{int(claim['id'])}"
                      if task.kind == "fix" and task.tz.startswith("fix:TZ-MR-batch-")
                      else None)
        current_digest = _current_blind_digest(
            _project_root(store, task.project), batch, attempt_id)
        if current_digest is None:
            return False
        for event in events:
            try:
                payload = json.loads(event["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if (payload.get("claim_id") == int(claim["id"])
                    and payload.get("batch_id") == batch
                    and payload.get("attempt_id") == attempt_id):
                # The latest server seal wins. Rolling back blind bytes cannot
                # resurrect an older seal after a later attempt was recorded.
                digest = payload.get("seal_sha256")
                return (isinstance(digest, str)
                        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
                        and digest == current_digest)
    except Exception:
        return False
    return False


def _blind_brief(store: Any, worker_id: str, task: Task, batch: str) -> str:
    """Never render original body, checkpoints, neighbours or old review notes."""
    sealed = _blind_is_sealed(store, worker_id, task, batch)
    root = _project_root(store, task.project)
    lines = [
        f"worker: {worker_id}",
        f"задача: {task.project}/{task.tz} [{task.kind}/{task.state}]",
        "Режим: серверное слепое ревью — только структурная памятка.",
        f"Правила проекта: {_project_rules(root)}",
    ]
    if not batch:
        lines.extend([
            "Ошибка: некорректный структурный blind-маркер; доступ к данным не подтверждён.",
            "Не открывай исходные Case, candidate, MANIFEST или старые вердикты; "
            "оформи заявку на исправление выдачи задачи.",
        ])
        return "\n".join(lines)
    cases = Path(root) / "data" / "batches" / "cases" / batch
    review = Path(root) / "data" / "reviews" / batch
    attempt_id = _blind_attempt_id(store, worker_id, task)
    if attempt_id:
        review = review / "attempts" / attempt_id
    lines.extend([
        f"batch_id: {batch}",
        f"attempt_id: {attempt_id or 'initial'}",
        f"Первичные обезличенные Case (чтение): {cases}",
        f"Папка новых blind/verdict-артефактов (запись по claim): {review}",
        "Прежде всего самостоятельно выведи границы каждого Case и запечатай "
        "три hidden replay (ровно один negative) для каждого candidate.",
    ])
    if sealed:
        lines.extend([
            "Статус: fresh blind сервером запечатан для текущего claim.",
            f"Теперь допустимо читать MANIFEST: {Path(root) / 'data' / 'batches' / 'out' / batch / 'MANIFEST.md'}",
            "Сравни candidate с ранее запечатанными blind-запросами и составь verdict; "
            "старые reject/чужие blind не используй как подсказки.",
        ])
    else:
        lines.extend([
            "Статус: blind ещё НЕ запечатан сервером — фаза case_only.",
            "MANIFEST, candidate, старые blind/verdict и reject/evidence запрещены до "
            "подтверждённого server seal. Наличие файла blind.json само по себе не seal.",
            "Раскладка: отдельный файл на каждый Case — data/reviews/<batch>/<case>/blind.json "
            "(как в canon/REVIEWER.md); один общий blind.json партии verifier не принимает.",
            'Вызови fleet_blind_seal(agent). Если cached toolset не содержит его, '
            'используй run(root=<проект>, argv=["fleet.cmd", "blind-seal"], agent=<worker_id>); '
            'это тот же серверный verifier. Не выполняй следующий этап без server seal.',
        ])
    return "\n".join(lines)


def _json_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    return tuple(str(x) for x in value) if isinstance(value, (list, tuple)) else ()


def _task_from_row(row: Any) -> Task:
    return Task(
        project=str(row["project"]),
        tz=str(row["tz"]),
        state=str(row["state"]),
        priority=int(row["priority"]),
        kind=str(row["kind"]),
        spec_path=str(row["spec_path"] or ""),
        spec_sha=str(row["spec_sha"] or ""),
        needs=_json_list(row["needs"]),
        owns=_json_list(row["owns"]),
        resources=_json_list(row["resources"]),
        acceptance=_json_list(row["acceptance"]),
        title=str(row["title"] or ""),
        body=str(row["body"] or ""),
    )


def _project_root(store: Any, project: str) -> str:
    with store.read() as tx:
        row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
    return str(row["root"]) if row is not None else ""


def _project_rules(root: str) -> str:
    if not root:
        return "AGENTS.md"
    base = Path(root)
    for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
        candidate = base / name
        if candidate.is_file():
            return str(candidate)
    return str(base / "AGENTS.md")


def _active_task(store: Any, worker_id: str) -> Task | None:
    with store.read() as tx:
        row = tx.one(
            "SELECT t.* FROM claims c "
            "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.worker_id=? AND c.state='active' "
            "ORDER BY c.id DESC LIMIT 1",
            (worker_id,),
        )
    return _task_from_row(row) if row is not None else None


def _segments(path: str) -> tuple[str, ...]:
    text = str(path).replace("\\", "/").strip().strip("/")
    if text.endswith("/**"):
        text = text[:-3].rstrip("/")
    return tuple(part.casefold() for part in text.split("/") if part)


def _nearby(a: str, b: str) -> bool:
    sa, sb = _segments(a), _segments(b)
    if not sa or not sb:
        return False
    common = 0
    for left, right in zip(sa, sb):
        if left != right:
            break
        common += 1
    return common >= min(2, len(sa), len(sb))


def _neighbors(store: Any, task: Task) -> list[str]:
    if not task.owns:
        return []
    with store.read() as tx:
        rows = tx.query(
            "SELECT tz,state,kind,owns FROM tasks "
            "WHERE project=? AND tz<>? AND state NOT IN ('accepted','frozen') "
            "ORDER BY priority,created_at,tz",
            (task.project, task.tz),
        )
    out: list[str] = []
    for row in rows:
        owns = _json_list(row["owns"])
        if not owns:
            continue
        if any(_nearby(mine, other) for mine in task.owns for other in owns):
            shown = ", ".join(owns[:3])
            if len(owns) > 3:
                shown += f" (+{len(owns) - 3})"
            out.append(f"{row['tz']} [{row['kind']}/{row['state']}] — {shown}")
    return out


def _lines(title: str, values: tuple[str, ...] | list[str]) -> list[str]:
    return [f"{title}:"] + ([f"  - {value}" for value in values] if values else ["  (нет)"])


def _dependency_lines(store: Any, task: Task) -> list[str]:
    """Show recorded prerequisites, so an old pending note cannot hide an accept."""
    from .world import dep_tz
    if not task.needs:
        return []
    lines = ["Зависимости — живой реестр (accepted не означает live/product PASS):"]
    with store.read() as tx:
        for reference in task.needs[:16]:
            tz = dep_tz(reference)
            row = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (task.project, tz))
            accepted = tx.one("SELECT id,worker_id,ts FROM events WHERE project=? AND tz=? "
                              "AND kind='accept' ORDER BY id DESC LIMIT 1", (task.project, tz))
            detail = (f"; accept event {accepted['id']}, reviewer {accepted['worker_id']}, {accepted['ts']}"
                      if accepted else "; квитанции accept нет")
            lines.append(f"  - {reference}: {row['state'] if row else 'не зарегистрирована'}{detail}")
    lines.append('Подробный результат: run argv=["fleet.cmd", "result", "<проект>/<TZ>"].')
    return lines


def briefing(store: Any, worker_id: str, task: Task | None) -> str:
    """Render deterministic task context without copying semantic project rules."""
    if task is None:
        task = _active_task(store, worker_id)
    if task is None:
        return (
            f"worker: {worker_id}\n"
            "Активной задачи нет. Возьми одну через fleet_take; после выдачи памятка "
            "покажет границы, приёмку и ссылку на регламент проекта."
        )

    blind_batch = _blind_batch_marker(task)
    if blind_batch is not None:
        return _blind_brief(store, worker_id, task, blind_batch)

    root = _project_root(store, task.project)
    rules = _project_rules(root)
    spec = str(Path(root) / task.spec_path) if root and task.spec_path else task.spec_path
    neighbors = _neighbors(store, task)

    lines = [
        f"worker: {worker_id}",
        f"задача: {task.project}/{task.tz} [{task.kind}/{task.state}]",
        "",
        "Прочитать:",
        f"  - {rules} — действующий корневой регламент проекта; правила здесь намеренно не пересказываются.",
    ]
    if spec and task.owns:
        lines.append(
            f"  - {spec} — ТЗ и раздел «Итог». Файл входит в твою зону: сюда же строка "
            "в момент расхождения и точка продолжения."
        )
    elif spec:
        lines.append(f"  - {spec} — ТЗ и раздел «Итог».")
    lines.extend(["", *_lines("owns", task.owns)])
    lines.extend(_lines("resources", task.resources))
    lines.extend(_lines("acceptance", task.acceptance))
    lines.extend(_dependency_lines(store, task))
    try:
        from .owner_grants import RULE, file_decisions, grants_for, render

        with store.read() as tx:
            granted = render(grants_for(tx, task.project, task.tz), file_decisions(root))
    except Exception:
        granted = list(RULE)
    if granted:
        lines.extend(["", *granted])
    lines.extend(["", *_lines("Соседи по путям", neighbors)])
    lines.extend(
        [
            "",
            "Рабочая памятка:",
            f"  - ориентируйся на {Path(rules).name} для git, чужих зон/заявок и закрытия по rc;",
            "  - детерминированные границы выше — это данные очереди, а не копия регламента.",
        ]
    )
    try:
        from .review import _gate_commands

        gated = _gate_commands(task.project, task.acceptance)
    except Exception:
        gated = set()
    if gated:
        lines.append(
            "  - гейт проекта в acceptance ("
            + "; ".join(sorted(gated))
            + "): пока ствол красный, сервер откладывает его до интеграции (её гейт идёт после "
            "accept). Красный гейт по тестам вне твоей зоны — не повод для block/reject; "
            "остальные команды acceptance — обязательны."
        )
    try:
        from . import checkpoint

        cp = checkpoint.latest(store, task.project, task.tz)
    except Exception:
        cp = None
    if cp:
        lines.extend(["", checkpoint.render(cp, "Контрольная точка — продолжай с неё")])
    if task.body:
        lines.extend(["", "Тело задачи:", task.body])
    if task.kind == "review":
        lines.extend(["", *_review_lines(store, task)])
    return "\n".join(lines)


def _review_source_lines(store: Any, task: Task) -> list[str]:
    """Resolve the server-owned review target, including already-created reviews.

    Requests and triages often have no spec file. Their stored body is the
    contract; a review marker and an empty owns list cannot replace it.
    This adds read context only and grants no write or acceptance authority.
    """
    from .review import _review_target

    project, source_tz = _review_target(task.tz, task.body or "", task.project)
    with store.read() as tx:
        source = tx.one("SELECT * FROM tasks WHERE project=? AND tz=?", (project, source_tz))
        submitted = tx.one(
            "SELECT ts,payload FROM events WHERE project=? AND tz=? AND kind='submit' "
            "ORDER BY id DESC LIMIT 1", (project, source_tz))
    if source is None:
        return [f"Исходная задача {project}/{source_tz} отсутствует в реестре; нужна починка контекста."]
    if _blind_batch_marker(_task_from_row(source)) is not None:
        return []
    lines = [f"Исходное поручение: {project}/{source_tz}",
             "Зона исходной задачи (только чтение):"]
    lines.extend(f"  - {path}" for path in _json_list(source["owns"]))
    lines.extend(_dependency_lines(store, _task_from_row(source)))
    if source["body"]:
        lines.extend(["Текст исходного поручения из реестра:", str(source["body"])])
    if submitted:
        try:
            payload = json.loads(submitted["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        note = payload.get("note") if isinstance(payload, dict) else None
        if note:
            lines.extend([f"Отчёт автора при сдаче ({submitted['ts']}; требует проверки):", str(note)])
    return lines


def _review_lines(store: Any, task: Task) -> list[str]:
    """Что сделал автор и по каким правилам его принимать."""
    lines = [
        "Правила ревью:",
        "  - во время ревью файлы задачи НЕ правь: любая запись после твоего прогона",
        "    обесценивает его, accept засчитывает только прогоны по текущему состоянию;",
        "  - rc=0 не доказывает, что тесты проверяют то же, что раньше, — смотри сигналы и diff;",
        "  - гейт проекта в acceptance при красном стволе сервер откладывает до интеграции:",
        "    красный гейт по тестам вне зоны задачи — не повод для block/reject/escalate;",
        "  - нельзя проверить здесь (визуал, живой запуск, решение владельца) —",
        "    fleet_review(agent, 'escalate', note = что именно и почему), а не accept;",
        "  - reject — только за содержание: пункт «Что сделать» не сделан или сделан неправильно",
        "    (в note — какой и чем проверено). Форма отчёта, нет сопоставления в «Итог» — не повод:",
        "    сам проверил, что всё сделано, — accept, недочёт формы одной строкой в note.",
        '  - кадры UI: fleet_look; если инструмента нет в схеме клиента, вызови run',
        '    argv=["fleet.cmd", "look", "docs/screens/...png", "--rubric", "критерии"].',
        "    Это тот же серверный просмотр проекта через Gemini; наличие PNG не является оценкой.",
    ]
    lines.extend(["", *_review_source_lines(store, task)])
    try:
        from . import evidence
        from .review import _review_target

        project, source_tz = _review_target(task.tz, task.body or "", task.project)
        section = evidence.review_section(store, project, source_tz)
    except Exception:
        section = ""
    if section:
        lines.extend(["", section])
    try:
        from . import bite

        bite_line = bite.review_line(store, project, source_tz)
    except Exception:
        bite_line = ""
    if bite_line:
        lines.extend(["", bite_line, "  (КУСАЕТ — тест ловит отклонённый дефект; НЕ КУСАЕТ/не проверено — "
                      "проверь тест сам: fix без защиты ствола от того же дефекта — повод для reject)"])
    return lines


def resume(store: Any, worker_id: str) -> str:
    """Render active-task briefing plus durable evidence after a client/session restart."""
    task = _active_task(store, worker_id)
    if task is None:
        return f"worker: {worker_id}\nАктивной задачи для возобновления нет."
    # The durable summary can contain past reject prose, write paths and
    # messages which reveal candidates before the fresh blind seal. It stays
    # hidden even after seal; the independent replay consumes Case + MANIFEST.
    if _blind_batch_marker(task) is not None:
        return briefing(store, worker_id, task)
    return briefing(store, worker_id, task) + "\n\n" + resume_summary(store, worker_id, task)


def _payload_label(payload: str) -> str:
    if not payload:
        return ""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if not isinstance(data, dict):
        return str(data)
    for key in ("task", "command", "cmd"):
        if data.get(key):
            return str(data[key])
    if data.get("argv"):
        value = data["argv"]
        if isinstance(value, list):
            return " ".join(str(x) for x in value)
        return str(value)
    return ""


def _claim_start(store: Any, worker_id: str, task: Task) -> str:
    with store.read() as tx:
        row = tx.one(
            "SELECT started_at FROM claims WHERE worker_id=? AND project=? AND tz=? "
            "ORDER BY id DESC LIMIT 1",
            (worker_id, task.project, task.tz),
        )
    return str(row["started_at"]) if row is not None else ""


def resume_summary(store: Any, worker_id: str, task: Task) -> str:
    """Summarize durable evidence only for non-blind tasks.

    Keep this guard even though resume() skips the summary: direct callers
    must not surface old rejects, messages or candidate paths in blind phase.
    """
    if _blind_batch_marker(task) is not None:
        return "Blind-review: история попыток скрыта; используй структурную памятку briefing()."
    started_at = _claim_start(store, worker_id, task)
    with store.read() as tx:
        if started_at:
            events = tx.query(
                "SELECT kind,tool,path_norm,rc,payload,ts FROM events "
                "WHERE worker_id=? AND ts>=? ORDER BY id",
                (worker_id, started_at),
            )
        else:
            events = tx.query(
                "SELECT kind,tool,path_norm,rc,payload,ts FROM events "
                "WHERE worker_id=? ORDER BY id",
                (worker_id,),
            )
        requests = tx.query(
            "SELECT tz,state,body FROM tasks WHERE kind='request' AND author_worker=? "
            "ORDER BY created_at,tz",
            (worker_id,),
        )
        address = f"{task.project}/{task.tz}"
        messages = tx.query(
            "SELECT id,kind,from_kind,from_id,body,delivered_at FROM messages "
            "WHERE acked_at IS NULL AND (to_worker=? OR to_task=?) "
            "ORDER BY created_at,id",
            (worker_id, address),
        )

    writes: dict[str, int] = {}
    runs: list[str] = []
    request_events: set[str] = set()
    for event in events:
        kind = str(event["kind"])
        if kind == "write":
            path = str(event["path_norm"] or "(путь не записан)")
            writes[path] = writes.get(path, 0) + 1
        elif kind == "run":
            label = _payload_label(str(event["payload"] or ""))
            tool = str(event["tool"] or "run")
            rc = event["rc"]
            suffix = f" — {label}" if label else ""
            runs.append(f"{tool}{suffix}; rc={rc if rc is not None else '?'}")
        elif kind == "request_draft":
            request_events.add(str(event["payload"] or ""))

    lines = [
        f"Возобновление: {task.project}/{task.tz}, worker {worker_id}",
        f"Последняя попытка начата: {started_at or '(claim не найден)'}",
        "",
        "Уже написано:",
    ]
    if writes:
        lines.extend(f"  - {path} ({count} write)" for path, count in writes.items())
    else:
        lines.append("  (нет write-событий)")

    lines.extend(["", "Прогоны:"])
    lines.extend(f"  - {item}" for item in runs) if runs else lines.append("  (нет run-событий)")

    lines.extend(["", "Заявки:"])
    if requests:
        for row in requests:
            first = str(row["body"] or "").splitlines()[0] if row["body"] else ""
            lines.append(f"  - {row['tz']} [{row['state']}] {first}".rstrip())
    elif request_events:
        lines.extend(f"  - {item}" for item in sorted(request_events))
    else:
        lines.append("  (нет)")

    lines.extend(["", "Сообщения без ack:"])
    if messages:
        for row in messages:
            sender = f"{row['from_kind']}:{row['from_id']}" if row["from_id"] else str(row["from_kind"])
            delivery = "доставлялось" if row["delivered_at"] else "ещё не доставлялось"
            lines.append(
                f"  - {row['id']} [{row['kind']}] от {sender}, {delivery}: {row['body']}"
            )
    else:
        lines.append("  (нет)")

    return "\n".join(lines)


__all__ = ["briefing", "resume", "resume_summary"]
