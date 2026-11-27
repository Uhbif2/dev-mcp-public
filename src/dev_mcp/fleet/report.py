"""Read-only owner reports for the fleet coordination registry."""
from __future__ import annotations

import json
import math
import os
import sqlite3
from fnmatch import fnmatchcase
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterator, Mapping, Sequence

SILENCE_MIN = 30
PENDING_ACK_MIN = 30
REVIEW_WAIT_MIN = 30
# Начатую работу maintenance сохраняет и возвращает в очередь после длительного
# молчания. Порог отчёта обязан совпадать с фактическим recovery в fleet.tools:
# иначе claim уже должен был вернуться в ready, а owner-report ещё считает его нормальным.
ABANDONED_WITH_WORK_MIN = 30
_REQUIRED_TABLES = {"projects", "tasks", "workers", "claims", "messages", "events"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        value = raw
    else:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _minutes_since(raw: Any, now: datetime | None = None) -> float:
    value = _dt(raw)
    return max(0.0, ((now or _utcnow()) - value).total_seconds() / 60.0) if value else 0.0


def _age_label(minutes: float) -> str:
    total = max(0, int(minutes))
    if total < 1:
        return "<1 мин"
    if total < 60:
        return f"{total} мин"
    hours, mins = divmod(total, 60)
    if hours < 24:
        return f"{hours}ч{mins:02d}м" if mins else f"{hours}ч"
    days, hours = divmod(hours, 24)
    return f"{days}д{hours:02d}ч" if hours else f"{days}д"


def _duration_label(start: Any, end: Any | None = None, now: datetime | None = None) -> str:
    begin, finish = _dt(start), (_dt(end) if end else (now or _utcnow()))
    if not begin or not finish:
        return "—"
    return f"{max(0.0, (finish - begin).total_seconds() / 3600.0):.1f}ч"


def _rows(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(r) for r in rows]
    names = [item[0] for item in (getattr(cursor, "description", None) or [])]
    return [dict(zip(names, row)) for row in rows]


@contextmanager
def _executor(store: Any) -> Iterator[Any]:
    candidates = [store]
    for name in ("connection", "conn", "_connection", "_conn", "db", "_db"):
        obj = getattr(store, name, None)
        if obj is not None:
            candidates.append(obj)
    for obj in candidates:
        if callable(getattr(obj, "execute", None)):
            yield obj
            return

    for name in ("path", "db_path", "_path", "_db_path"):
        raw = getattr(store, name, None)
        if raw:
            conn = sqlite3.connect(os.fspath(raw))
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()
            return

    tx = getattr(store, "tx", None)
    if callable(tx):
        with tx() as txn:
            candidates = [txn]
            for name in ("connection", "conn", "_connection", "_conn", "db", "_db"):
                obj = getattr(txn, name, None)
                if obj is not None:
                    candidates.append(obj)
            for obj in candidates:
                if callable(getattr(obj, "execute", None)):
                    yield obj
                    return
    raise TypeError("Store does not expose a readable SQLite connection/path/transaction")


def _query(store: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    with _executor(store) as db:
        return _rows(db.execute(sql, tuple(params)))


def _scalar(store: Any, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
    rows = _query(store, sql, params)
    return next(iter(rows[0].values()), default) if rows else default


def _schema_ready(store: Any) -> bool:
    try:
        names = {str(r["name"]) for r in _query(store, "SELECT name FROM sqlite_master WHERE type='table'")}
    except (sqlite3.Error, TypeError, KeyError):
        return False
    return _REQUIRED_TABLES.issubset(names)


def _payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def claim_write_status(
    event: Mapping[str, Any], claim: Mapping[str, Any] | None,
    owns: Sequence[str],
) -> str:
    """Provenance of one write against a particular task take (never just worker_id).

    The claim's captured claim_paths are authoritative; current tasks.owns can change
    between takes. Missing scope is explicitly *unverified*, not a silent grant.
    """
    if claim is None or any(
        str(event.get(key) or "") != str(claim.get(key) or "")
        for key in ("worker_id", "project", "tz")
    ):
        return "no_claim"
    occurred, started, released = (_dt(event.get("ts")), _dt(claim.get("started_at")),
                                   _dt(claim.get("released_at")))
    if not occurred or not started or occurred < started or (released and occurred > released):
        return "no_claim"
    payload = _payload(event.get("payload"))
    linked = payload.get("claim_id", payload.get("source_claim_id"))
    if linked is not None and str(linked) != str(claim.get("id")):
        return "no_claim"
    if not owns:
        return "unknown_owns"
    path = str(event.get("path_norm") or "").replace("\\", "/").casefold()
    for pattern in owns:
        if fnmatchcase(path, str(pattern).replace("\\", "/").casefold()):
            return "owned"
    return "outside_owns"


def _claim_owns(store: Any, claim_id: int) -> list[str]:
    return [str(r["path_norm"]) for r in _query(
        store, "SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim_id,)
    )]


def _foreign(row: Mapping[str, Any]) -> bool:
    data = _payload(row.get("payload"))
    code = str(data.get("code") or data.get("reason_code") or "")
    zone_codes = ("FLEET_FOREIGN_PATH", "FLEET_OUT_OF_CLAIM")
    return code in zone_codes or any(c in str(row.get("payload") or "") for c in zone_codes)


def _quick_operator_scope(store: Any, row: Mapping[str, Any]) -> bool:
    """A *recorded denial* was inside a Claude quick job's explicit operator scope.

    This is not a successful write: the fleet guard can still deny a conflicting
    claim even when policy.DELEGATED_ROOTS allowed the operator's path.
    """
    try:
        from . import quick
        from .model import norm_path
        worker = str(row.get("worker_id") or "")
        if not quick.is_quick_agent(worker):
            return False
        db_path = str(getattr(store, "path", "") or "")
        if not db_path:
            db_path = next((str(r["file"]) for r in _query(store, "PRAGMA database_list")
                            if str(r.get("name") or "") == "main"), "")
        if not db_path:
            return False
        job = quick.lookup(quick.Book(Path(db_path).parent), worker)
        if not job or not str(job.get("owner") or "").startswith("claude") or not job.get("scope"):
            return False
        occurred, created, closed = (_dt(row.get("ts")), _dt(job.get("created")),
                                     _dt(job.get("closed_at")))
        if not occurred or not created or occurred < created or (closed and occurred >= closed):
            return False
        return not quick.scope_denial(job, norm_path(str(row.get("path_norm") or "")))
    except (KeyError, OSError, ValueError, sqlite3.Error, TypeError):
        return False


def _run_name(row: Mapping[str, Any]) -> str:
    if str(row.get("tool") or "").strip():
        return str(row["tool"]).strip()
    data = _payload(row.get("payload"))
    value = data.get("command") or data.get("cmd") or data.get("argv") or data.get("acceptance")
    if isinstance(value, list):
        return " ".join(str(x) for x in value[:3])
    return str(value or "run")


def _display_path(raw: Any, root: Any) -> str:
    text = str(raw or "")
    base = str(root or "")
    if not text:
        return text
    try:
        if base and os.path.isabs(text):
            rel = os.path.relpath(text, base)
            if rel != ".." and not rel.startswith(".." + os.sep):
                text = rel
    except (OSError, ValueError):
        pass
    return text.replace("\\", "/")


def _result_nonempty(root: str, spec_path: str) -> bool:
    if not spec_path:
        return False
    path = Path(spec_path)
    if not path.is_absolute():
        path = Path(root) / path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    start = next((i + 1 for i, line in enumerate(lines)
                  if line.strip().casefold() in {"## итог", "## результат"}), None)
    if start is None:
        return False
    body = []
    for line in lines[start:]:
        if line.lstrip().startswith("## "):
            break
        stripped = line.strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        body.append(line)
    result = "\n".join(body).strip()
    if not result:
        return False
    normalized = result.casefold().strip(" \t\r\n`*_.,:;!?—-()[]{}")
    placeholders = {
        "заполняется исполнителем",
        "заполнить после выполнения",
        "todo",
        "tbd",
        "не заполнено",
    }
    return normalized not in placeholders


def _claim_imported(store: Any, claim: Mapping[str, Any]) -> bool:
    rows = _query(
        store,
        "SELECT payload FROM events "
        "WHERE kind='take' AND worker_id=? AND project=? AND tz=? "
        "ORDER BY id DESC LIMIT 1",
        (claim["worker_id"], claim["project"], claim["tz"]),
    )
    return bool(rows and _payload(rows[0].get("payload")).get("imported") is True)


def claimed_vs_done(store: Any, project: str | None = None) -> str:
    """Show latest claim per task against observed writes, runs, and the spec result."""
    if not _schema_ready(store):
        return "реестр fleet пуст или недоступен"
    where, params = ("WHERE c.project = ?", [project]) if project is not None else ("", [])
    claims = _query(store, f"""
        SELECT c.id, c.project, c.tz, c.worker_id, c.state, c.started_at, c.released_at,
               w.last_seen, t.spec_path, p.root
          FROM claims c
          JOIN workers w ON w.id=c.worker_id
          JOIN tasks t ON t.project=c.project AND t.tz=c.tz
          JOIN projects p ON p.name=c.project
          JOIN (SELECT project,tz,MAX(id) id FROM claims GROUP BY project,tz) x ON x.id=c.id
          {where}
         ORDER BY c.project,c.tz
    """, params)
    if not claims:
        return "заявок нет"

    now, lines = _utcnow(), []
    single_project = len({str(c["project"]) for c in claims}) == 1
    for claim in claims:
        events = _query(store, """
            SELECT id,ts,kind,worker_id,project,tz,tool,path_norm,rc,payload
              FROM events
             WHERE worker_id=? AND project=? AND tz=? AND ts>=?
               AND (? IS NULL OR ts<=?)
             ORDER BY id
        """, (claim["worker_id"], claim["project"], claim["tz"], claim["started_at"],
              claim["released_at"], claim["released_at"]))
        owns = _claim_owns(store, int(claim["id"]))
        paths: list[str] = []
        outside: list[str] = []
        unverified = False
        foreign_n = 0
        for row in events:
            if _foreign(row):
                foreign_n += 1
            if row.get("kind") != "write" or not row.get("path_norm"):
                continue
            status = claim_write_status(row, claim, owns)
            path = _display_path(row["path_norm"], claim.get("root"))
            if status == "outside_owns" or status == "no_claim":
                if path not in outside:
                    outside.append(path)
                if not _foreign(row):
                    foreign_n += 1
            else:
                if path not in paths:
                    paths.append(path)
                if status == "unknown_owns":
                    unverified = True
        written = "—" if not paths else paths[0] + (f"(+{len(paths)-1})" if len(paths) > 1 else "")
        if unverified:
            written += " [owns не подтверждены]"
        foreign_text = "—" if not foreign_n else f"{foreign_n} (!)"
        if outside:
            foreign_text += " [вне owns: " + outside[0] + (f" (+{len(outside)-1})" if len(outside) > 1 else "") + "]"
        runs = [row for row in events if row.get("kind") == "run" and row.get("rc") is not None]
        run_text = "—" if not runs else f"{_run_name(runs[-1])} rc={runs[-1]['rc']}"
        has_result = _result_nonempty(str(claim.get("root") or ""), str(claim.get("spec_path") or ""))
        silence = _minutes_since(claim.get("last_seen"), now)
        duration = _duration_label(claim.get("started_at"), claim.get("released_at"), now)
        marker = "  ←" if claim.get("state") == "active" and silence >= SILENCE_MIN and not paths else ""
        label = str(claim["tz"]) if single_project else f"{claim['project']}/{claim['tz']}"
        lines.append(
            f"{label}  {claim['worker_id']}  {duration}  "
            f"писал: {written}  вне зоны: {foreign_text}  run: {run_text}  "
            f"Итог: {'есть' if has_result else 'пуст'}  молчит {_age_label(silence)}{marker}"
        )
    return "\n".join(lines)


def exceptions(store: Any) -> list[str]:
    """Return owner-attention conditions only; [] is the normal result."""
    return [text for text, _ts in exception_items(store)]


UNCOMMITTED_ACCEPT_MIN = 60


def _integration_class(payload: dict[str, Any]) -> str:
    code = str(payload.get('reason_code') or '')
    reason = str(payload.get('reason') or payload.get('error') or '')
    if 'WinError 206' in reason or 'filename or extension is too long' in reason.lower():
        return 'winerror206'
    if code in {'accepted_candidate_bytes_changed', 'accepted_candidate_bytes_changed_by_unfinished'}:
        return 'foreign' if code.endswith('_by_unfinished') else 'drift'
    if 'unfinished foreign writes' in reason:
        return 'foreign'
    if code in {'resource_busy'} or payload.get('gate_wait') or payload.get('resource_wait'):
        return 'busy'
    if code == 'validation_timeout' or 'timeout' in reason.lower() or 'timed out' in reason.lower():
        return 'timeout'
    if code.startswith('missing_acceptance_script') or "can't open file" in reason:
        return 'missing_script'
    if code == 'missing_candidate_dependency' or 'modulenotfounderror' in reason.lower():
        return 'missing_import'
    if code in {'candidate_paths_already_staged'}:
        return 'staged'
    if 'seal' in code or 'запечатан' in reason:
        return 'unsealed'
    if code in {'launch_error'} or 'gate_launch_failed' in reason:
        return 'gate_launch_failed'
    if payload.get('error'):
        return 'git_error'
    return 'validation' if payload.get('deferred') else 'other'


def _integration_status(payload: dict[str, Any], now: datetime) -> str:
    if payload.get('operator_required') or _integration_class(payload) in {'drift', 'staged'}:
        return 'ждёт оператора'
    if payload.get('retry_exhausted') or (payload.get('skipped') and not payload.get('deferred')):
        return 'окончательно'
    retry = payload.get('retry') or {}
    transient = (_integration_class(payload) in {'busy', 'timeout', 'foreign', 'git_error', 'gate_launch_failed'}
                 or str(payload.get('reason') or '') in {
                     'candidate changed during validation', 'candidate changed during staging'})
    if retry or transient:
        try:
            at = datetime.fromtimestamp(float(retry.get('next_at') or now.timestamp())).astimezone()
            return 'повтор в ' + at.strftime('%H:%M')
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return 'ждёт оператора' if payload.get('deferred') else 'окончательно'


def uncommitted_accepts(store: Any, *, now: datetime | None = None,
                        commit_projects: set[str] | None = None) -> list[str]:
    """Accepted task chains without a final integration receipt on commit-enabled projects."""
    if commit_projects is None:
        import tomllib
        try:
            config = Path(__file__).resolve().parents[3] / "fleet-projects.toml"
            with config.open("rb") as stream:
                projects = tomllib.load(stream).get("projects", {})
            commit_projects = {name for name, flags in projects.items()
                               if isinstance(flags, dict) and flags.get("commit") is True}
        except (OSError, ValueError, TypeError):
            return []  # Unknown configuration is not evidence of a required commit.
    if not commit_projects:
        return []
    now = now or _utcnow()
    # Match the integrator's retry horizon. Older historical accepts can predate
    # integration entirely or have been committed manually without any receipt.
    from datetime import timedelta
    since = (now - timedelta(days=14)).isoformat()
    pending: dict[str, list[str]] = {}
    for project in sorted(commit_projects):
        accepts = _query(store, """
            SELECT e.id,e.tz,e.ts FROM events e
            JOIN (SELECT tz,MAX(id) id FROM events
                  WHERE project=? AND kind='accept' AND ts>=? GROUP BY tz) last ON last.id=e.id
            WHERE e.project=? ORDER BY e.id
        """, (project, since, project))
        if not accepts:
            continue
        integrations = _query(store, """
            SELECT e.tz,e.payload,e.ts FROM events e
            JOIN (SELECT tz,MAX(id) id FROM events
                  WHERE project=? AND kind='integrate' GROUP BY tz) last ON last.id=e.id
            WHERE e.project=?
        """, (project, project))
        latest = {str(row["tz"]): row for row in integrations}
        for accepted in accepts:
            age = _minutes_since(accepted["ts"], now)
            if age < UNCOMMITTED_ACCEPT_MIN:
                continue
            receipt = latest.get(str(accepted["tz"]))
            # A final receipt must belong to this acceptance, not an earlier round.
            current_receipt = bool(
                receipt and _dt(receipt["ts"]) and
                _dt(receipt["ts"]) >= _dt(accepted["ts"]))
            payload = _payload(receipt["payload"]) if current_receipt else {}
            skip = str(payload.get('skipped') or '')
            final = bool(payload.get('commit') or payload.get('superseded') is True
                         or (skip and ('уже в истории' in skip or
                                       'переписаны более поздними' in skip or
                                       'несут более поздние' in skip)))
            if final:
                continue
            reason = str(payload.get("reason") or payload.get("error") or
                         payload.get("skipped") or "интеграция не запускалась")
            reason = " ".join(reason.split())[:140]
            kind = _integration_class(payload)
            status = _integration_status(payload, now)
            pending.setdefault(project, []).append(
                f"{accepted['tz']} ({_age_label(age)}; {kind}; {status}; {reason})")
    # The registry includes pre-integrator history; never drown recent refusals
    # in hundreds of old tasks. Preserve the full total and show the newest first.
    return [f"ПРИНЯТО, НО НЕ ЗАКОММИЧЕНО {project}: {len(items)} задач — " +
            "; ".join(items[-30:][::-1]) +
            (f"; … ещё {len(items)-30} старых" if len(items) > 30 else "")
            for project, items in pending.items()]


def exception_items(store: Any) -> list[tuple[str, str | None]]:
    """То же, что exceptions(), но с моментом события, на котором держится тревога.

    ts=None — тревога про текущее состояние (живой claim, пустая очередь, ждущая приёмки
    сдача): она не стареет. Остальные (чужой путь, красная приёмка, сообщение без ack)
    опираются на конкретное событие, и пульт прячет их, когда событию больше суток:
    27.09.2026 из ~100 строк «!» свежими были единицы, сигнал тонул в истории.
    """
    if not _schema_ready(store):
        return [("реестр fleet пуст или недоступен", None)]
    now, out = _utcnow(), []

    active = _query(store, """
        SELECT c.id,c.project,c.tz,c.worker_id,c.started_at,w.last_seen
          FROM claims c JOIN workers w ON w.id=c.worker_id
         WHERE c.state='active' ORDER BY c.id
    """)
    for claim in active:
        if _claim_imported(store, claim):
            continue
        silence = _minutes_since(claim.get("last_seen"), now)
        writes = int(_scalar(store, """
            SELECT COUNT(*) n FROM events
             WHERE kind='write' AND worker_id=? AND (project IS NULL OR project=?)
               AND (tz IS NULL OR tz=?) AND ts>=?
        """, (claim["worker_id"], claim["project"], claim["tz"], claim["started_at"])))
        if silence >= SILENCE_MIN and writes == 0:
            out.append((
                f"МОЛЧАНИЕ {claim['project']}/{claim['tz']} {claim['worker_id']}: "
                f"{_age_label(silence)}, записей 0", None
            ))
        elif silence >= ABANDONED_WITH_WORK_MIN and writes > 0:
            out.append((
                f"БРОШЕНО {claim['project']}/{claim['tz']} {claim['worker_id']}: "
                f"{_age_label(silence)}, записей {writes} — maintenance должен сохранить работу, "
                "отозвать старого worker и вернуть задачу в ready", None
            ))

    # Only deny/write can be a guard violation. Without kind the query scans
    # the entire multi-million-row events table (including read payloads that
    # merely quote the code), even though events_kind_id is indexed.
    # Keep all history here for fleet.cmd --all; the CLI applies its own TTL.
    foreign_rows: dict[tuple[str, str, str, str], tuple[dict[str, Any], int]] = {}
    for row in _query(store, """
        SELECT id,ts,worker_id,project,tz,path_norm,payload FROM events
         WHERE kind IN ('deny','write')
           AND (payload LIKE '%FLEET_FOREIGN_PATH%' OR payload LIKE '%FLEET_OUT_OF_CLAIM%')
         ORDER BY id
    """):
        if _foreign(row):
            target = "/".join(x for x in (str(row.get("project") or ""), str(row.get("tz") or "")) if x) or "?"
            code = "outside" if "FLEET_OUT_OF_CLAIM" in str(row.get("payload") or "") else "foreign"
            key = (code, target, str(row.get("worker_id") or "?"), str(row.get("path_norm") or ""))
            _, n = foreign_rows.get(key, (row, 0))
            foreign_rows[key] = (row, n + 1)
    from .quick import is_quick_agent
    for (code, target, worker, path), (row, count) in foreign_rows.items():
        label = ("ВНЕ ЗОНЫ" if code == "outside"
                 else "ОТКАЗ QUICK В SCOPE" if _quick_operator_scope(store, row)
                 else "ЧУЖОЙ ПУТЬ")
        repeat = f" (повторов {count})" if count > 1 else ""
        target_text = "" if target == "?" and is_quick_agent(worker) else target + " "
        out.append((f"{label} {target_text}{worker}: {path or 'путь не указан'}{repeat}",
                    row.get("ts")))

    latest_runs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _query(store, """
        SELECT id,ts,project,tz,rc FROM events
         WHERE kind='run' AND rc IS NOT NULL AND project IS NOT NULL AND tz IS NOT NULL
         ORDER BY id DESC
    """):
        key = (str(row["project"]), str(row["tz"]))
        bucket = latest_runs.setdefault(key, [])
        if len(bucket) < 2:
            bucket.append(row)
    for (project, tz), rows in latest_runs.items():
        if len(rows) == 2 and all(int(row["rc"]) != 0 for row in rows):
            out.append((f"ПРИЁМКА КРАСНАЯ ДВАЖДЫ {project}/{tz}: rc={rows[1]['rc']}, затем rc={rows[0]['rc']}",
                        rows[0].get("ts")))

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    # Последнее событие каждой задачи — по индексу (project,tz,kind), без обхода всех payload.
    for row in _query(store, """
        SELECT e.id,e.ts,e.kind,e.project,e.tz FROM events e
          JOIN (SELECT MAX(id) id FROM events
                 WHERE project IS NOT NULL AND tz IS NOT NULL GROUP BY project,tz) m ON e.id=m.id
    """):
        latest.setdefault((str(row["project"]), str(row["tz"])), row)
    states = {(str(r["project"]), str(r["tz"])): str(r["state"])
              for r in _query(store, "SELECT project,tz,state FROM tasks")}
    for key, row in latest.items():
        if row.get("kind") == "requeue" and states.get(key) == "ready":
            out.append((f"ВЕРНУЛАСЬ В ОЧЕРЕДЬ {key[0]}/{key[1]}", row.get("ts")))
        elif row.get("kind") == "escalate" and states.get(key) == "draft":
            # Legacy-состояние. В нормальном контуре fleet_list/take уже должен
            # auto-promote такой final review/fix; это сигнал автоматики, не задача человеку.
            out.append((
                f"АВТОМАТИКА НЕ ПРОДВИНУЛА LEGACY DRAFT {key[0]}/{key[1]} — "
                "maintenance поднимет его при следующем fleet_list/fleet_take", row.get("ts")
            ))

    # Accepted is not the same as committed. Keep this current-state alarm visible
    # even when the integration refusal itself is older than the normal alarm TTL.
    for line in uncommitted_accepts(store, now=now):
        out.append((line, None))

    # Воркер без активной задачи ack уже не пришлёт: вкладка отработала и закрыта.
    # Его непрочитанное — не исключение для владельца, а мусор рассылки.
    holding = {str(r["worker_id"]) for r in _query(store, "SELECT worker_id FROM claims WHERE state='active'")}
    noacks: dict[tuple[str, str], list[tuple[dict[str, Any], float]]] = {}
    for msg in _query(store, """
        SELECT id,to_worker,to_task,broadcast,kind,created_at
          FROM messages WHERE acked_at IS NULL ORDER BY created_at
    """):
        if msg.get("to_worker") and str(msg["to_worker"]) not in holding:
            continue
        # Заметка сданной задаче тоже не будет прочитана: следующий держатель
        # возьмёт review, не исходную submitted-задачу.
        if msg.get("to_task"):
            project, _, tz = str(msg["to_task"]).partition("/")
            if states.get((project, tz)) in (None, "accepted", "rejected", "frozen", "submitted"):
                continue
        age = _minutes_since(msg.get("created_at"), now)
        if age >= PENDING_ACK_MIN:
            target = msg.get("to_worker") or msg.get("to_task") or ("всем" if msg.get("broadcast") else "?")
            noacks.setdefault((str(target), str(msg.get("kind") or "")), []).append((msg, age))
    for (target, kind), group in noacks.items():
        last, last_age = group[-1]
        if len(group) == 1:
            out.append((f"БЕЗ ACK {last['id']} → {target}: {_age_label(last_age)}",
                        last.get("created_at")))
        else:
            oldest_age = max(age for _msg, age in group)
            out.append((f"БЕЗ ACK {len(group)} сообщений ({kind}) → {target}: "
                        f"старейшее {_age_label(oldest_age)}, последнее {_age_label(last_age)}",
                        last.get("created_at")))

    free = [
        row for row in _query(store, """
            SELECT w.id,w.last_seen FROM workers w
             WHERE w.revoked_at IS NULL
               AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.worker_id=w.id AND c.state='active')
        """)
        if _minutes_since(row.get("last_seen"), now) <= SILENCE_MIN
    ]
    ready_count = int(_scalar(store, "SELECT COUNT(*) n FROM tasks WHERE state='ready'"))
    if free and ready_count == 0:
        ids = ", ".join(str(row["id"]) for row in free[:6])
        suffix = "" if len(free) <= 6 else f" (+{len(free)-6})"
        out.append((f"ОЧЕРЕДЬ ПУСТА: свободны {ids}{suffix}", None))

    # All reviewer routes are in tasks already. A correlated LIKE review lookup
    # per submitted task repeatedly scans the tasks table on real fleet DBs.
    reviews: dict[str, list[tuple[str, str]]] = {}
    for review in _query(store, "SELECT project,tz,state FROM tasks WHERE kind='review'"):
        reviews.setdefault(str(review["project"]), []).append(
            (str(review["tz"]).casefold(), str(review["state"])))
    priority = {"ready": 0, "taken": 1, "submitted": 2, "draft": 3, "frozen": 4}
    for rows in reviews.values():
        rows.sort(key=lambda item: priority.get(item[1], 5))
    unrouted: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for task in _query(store, """
        SELECT t.project,t.tz,t.kind,t.updated_at,
               (SELECT MAX(e.ts) FROM events e
                 WHERE e.project=t.project AND e.tz=t.tz AND e.kind='submit') submitted_at
          FROM tasks t WHERE t.state='submitted'
           -- отклонённая сдача ждёт не приёмки, а своего fix: он уже в очереди или в работе
           AND NOT EXISTS (
               SELECT 1 FROM tasks f
                WHERE f.project=t.project AND f.kind='fix'
                  AND (f.tz='fix:' || t.tz OR f.tz LIKE 'fix:' || t.tz || ':%')
                  AND f.state IN ('ready','taken','submitted','draft'))
    """):
        age = _minutes_since(task.get("submitted_at") or task.get("updated_at"), now)
        if age >= REVIEW_WAIT_MIN:
            expected = ("review:" + str(task["tz"])).casefold()
            review_state = next((state for tz, state in reviews.get(str(task["project"]), ())
                                 if tz == expected or tz.startswith(expected + ":")), None)
            if review_state in ("ready", "taken", "submitted", "draft"):
                out.append((f"ЖДЁТ ПРИЁМКИ {task['project']}/{task['tz']}: {_age_label(age)}", None))
            else:
                # A frozen review cannot be taken; a submitted request without
                # a review has no receiver at all. Neither is a live review queue.
                reason = ("ПРИЁМКА ЗАМОРОЖЕНА" if review_state == "frozen"
                          else "СДАЧА БЕЗ МАРШРУТА ПРИЁМКИ")
                unrouted.setdefault((str(task["project"]), reason), []).append(
                    (str(task["tz"]), age))
    for (project, reason), tasks in sorted(unrouted.items()):
        examples = "; ".join(f"{tz} ({_age_label(age)})" for tz, age in tasks[:4])
        suffix = f"; ещё {len(tasks)-4}" if len(tasks) > 4 else ""
        out.append((f"{reason} {project}: {len(tasks)} — {examples}{suffix}", None))
    return out


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _ratio(n: int, N: int) -> dict[str, Any]:
    return {"n": n, "N": N, "ratio": (n / N if N else None), "text": f"{n}/{N}"}


def metrics(store: Any, since: str) -> dict[str, Any]:
    """Measure fleet workflow since an ISO date/time; every share includes n/N."""
    since_dt = _dt(since)
    if since_dt is None:
        raise ValueError(f"неверная дата since: {since!r}")
    since_iso = since_dt.isoformat()
    if not _schema_ready(store):
        return {"since": since_iso, "error": "реестр fleet пуст или недоступен"}

    accepted = {
        (str(r["project"]), str(r["tz"]))
        for r in _query(store, """
            SELECT DISTINCT project,tz FROM events
             WHERE kind='accept' AND ts>=? AND project IS NOT NULL AND tz IS NOT NULL
        """, (since_iso,))
    }
    if not accepted:
        accepted = {
            (str(r["project"]), str(r["tz"]))
            for r in _query(store, "SELECT project,tz FROM tasks WHERE state='accepted' AND updated_at>=?", (since_iso,))
        }

    owner_kinds = {"requeue", "reject", "freeze", "approve", "revoke"}
    interventions = 0
    for row in _query(store, """
        SELECT kind,worker_id,project,tz FROM events
         WHERE ts>=? AND project IS NOT NULL AND tz IS NOT NULL
    """, (since_iso,)):
        key = (str(row["project"]), str(row["tz"]))
        if row.get("worker_id") is None and str(row.get("kind")) in owner_kinds and key in accepted:
            interventions += 1

    request_latencies: list[float] = []
    for row in _query(store, """
        SELECT t.project,t.tz,t.created_at,MIN(c.started_at) first_started
          FROM tasks t LEFT JOIN claims c ON c.project=t.project AND c.tz=t.tz
         WHERE t.kind='request' AND t.created_at>=?
         GROUP BY t.project,t.tz,t.created_at
    """, (since_iso,)):
        created, started = _dt(row.get("created_at")), _dt(row.get("first_started"))
        if created and started and started >= created:
            request_latencies.append((started - created).total_seconds() / 60.0)

    now = _utcnow()
    # "submitted" само по себе не означает "ждёт review": после reject исходная
    # задача остаётся submitted, пока generated fix проходит свой цикл. Считать такие семьи
    # review-latency ошибочно — именно так метрика показывала десятки часов ожидания при
    # реально работающем fix. Берём только те сдачи, у которых прямо сейчас есть незавершённый review.
    review_ages = [
        _minutes_since(row.get("submitted_at") or row.get("updated_at"), now)
        for row in _query(store, """
            SELECT t.updated_at,
                   (SELECT MAX(e.ts) FROM events e
                     WHERE e.project=t.project AND e.tz=t.tz AND e.kind='submit') submitted_at
              FROM tasks t
             WHERE t.state='submitted'
               AND EXISTS (
                   SELECT 1 FROM tasks r
                    WHERE r.project=t.project AND r.kind='review'
                      AND (r.tz='review:' || t.tz OR r.tz LIKE 'review:' || t.tz || ':%')
                      AND r.state IN ('ready','taken','draft')
               )
        """)
    ]

    touched = {
        (str(r["project"]), str(r["tz"]))
        for r in _query(store, "SELECT DISTINCT project,tz FROM claims WHERE started_at>=?", (since_iso,))
    }
    requeued = {
        (str(r["project"]), str(r["tz"]))
        for r in _query(store, """
            SELECT DISTINCT project,tz FROM events
             WHERE kind='requeue' AND ts>=? AND project IS NOT NULL AND tz IS NOT NULL
        """, (since_iso,))
    }

    attempts = _query(store, "SELECT kind,payload FROM events WHERE ts>=? AND (kind='write' OR kind='deny')", (since_iso,))
    foreign_n = sum(1 for row in attempts if _foreign(row))
    attempt_N = sum(1 for row in attempts if row.get("kind") == "write") + foreign_n

    # Adoption: did the client name its worker, or did the guard infer it from path
    # ownership? Both attribute the event to the same id, so without the flag the
    # registry cannot tell protocol use from protocol bypass. Events recorded before
    # the flag existed carry no verdict and are left out of N rather than counted
    # against it.
    declared_n = declared_N = 0
    for row in _query(store, "SELECT payload FROM events WHERE ts>=? AND kind='write'", (since_iso,)):
        body = _payload(row.get("payload"))
        if "declared" not in body:
            continue
        declared_N += 1
        if body.get("declared"):
            declared_n += 1

    # Claim duration measures one agent attempt, not task size: requeue/fix recovery can
    # create several short claims for the same TZ. Keep it for operational latency, but
    # aggregate work tasks separately so owner agents do not infer "3-minute TZ" from retries.
    durations: list[float] = []
    work_effort_by_task: dict[tuple[str, str], float] = {}
    for row in _query(store, """
        SELECT c.project,c.tz,c.started_at,c.released_at,t.kind
          FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz
         WHERE c.released_at IS NOT NULL AND c.released_at>=?
    """, (since_iso,)):
        start, end = _dt(row.get("started_at")), _dt(row.get("released_at"))
        if not start or not end or end < start:
            continue
        minutes = (end - start).total_seconds() / 60.0
        durations.append(minutes)
        if str(row.get("kind")) == "work":
            key = (str(row.get("project")), str(row.get("tz")))
            work_effort_by_task[key] = work_effort_by_task.get(key, 0.0) + minutes

    work_efforts = list(work_effort_by_task.values())
    p25, p75 = _percentile(durations, 0.25), _percentile(durations, 0.75)
    work_p25, work_p75 = _percentile(work_efforts, 0.25), _percentile(work_efforts, 0.75)

    # Номер задачи в чате против первого вердикта ревью. Чат web-ChatGPT берёт задачи подряд
    # (queue.chat_budget); если к концу длинного чата качество падает, это видно здесь.
    positions: dict[str, int] = {}
    for row in _query(store, "SELECT id,worker_id FROM claims ORDER BY started_at,id"):
        wid = str(row.get("worker_id"))
        positions[wid] = positions.get(wid, 0) + 1
        positions[f"claim:{row.get('id')}"] = positions[wid]
    by_position: dict[str, dict[str, int]] = {}
    for sub in _query(store, """
        SELECT e.ts,e.project,e.tz,e.worker_id,
               (SELECT c.id FROM claims c WHERE c.worker_id=e.worker_id AND c.project=e.project
                  AND c.tz=e.tz AND c.started_at<=e.ts ORDER BY c.started_at DESC LIMIT 1) claim_id,
               (SELECT e2.kind FROM events e2 WHERE e2.project=e.project AND e2.tz=e.tz
                  AND e2.kind IN ('accept','reject') AND e2.ts>=e.ts ORDER BY e2.ts,e2.id LIMIT 1) verdict
          FROM events e JOIN workers w ON w.id=e.worker_id
         WHERE e.kind='submit' AND e.ts>=? AND w.kind='chatgpt'
    """, (since_iso,)):
        pos = positions.get(f"claim:{sub.get('claim_id')}")
        if not pos:
            continue
        slot = by_position.setdefault(str(pos), {"accept": 0, "reject": 0, "pending": 0})
        slot[str(sub.get("verdict")) if sub.get("verdict") in ("accept", "reject") else "pending"] += 1

    return {
        "since": since_iso,
        "owner_interventions_per_accepted": _ratio(interventions, len(accepted)),
        "request_to_execution_min": {
            "n": len(request_latencies),
            "median": median(request_latencies) if request_latencies else None,
        },
        "review_wait_age_min": {
            "n": len(review_ages),
            "median": median(review_ages) if review_ages else None,
            "max": max(review_ages) if review_ages else None,
        },
        "requeue_rate": _ratio(len(requeued & touched), len(touched)),
        "verdict_by_chat_position": by_position,
        "foreign_path_rate": _ratio(foreign_n, attempt_N),
        "protocol_declared_rate": _ratio(declared_n, declared_N),
        "task_time_min": {  # backward compatibility: historically this meant claim attempt
            "n": len(durations),
            "median": median(durations) if durations else None,
            "p25": p25,
            "p75": p75,
            "spread": (p75 - p25) if p25 is not None and p75 is not None else None,
        },
        "claim_attempt_time_min": {
            "n": len(durations),
            "median": median(durations) if durations else None,
            "p25": p25,
            "p75": p75,
        },
        "work_task_effort_min": {
            "n": len(work_efforts),
            "median": median(work_efforts) if work_efforts else None,
            "p25": work_p25,
            "p75": work_p75,
            "spread": (work_p75 - work_p25) if work_p25 is not None and work_p75 is not None else None,
        },
    }


