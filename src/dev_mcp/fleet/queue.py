from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .model import Task, norm_path, paths_overlap, task_root
from . import probe, world


def _as_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [_as_dict(row) for row in rows]


def _json_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return ()
    else:
        data = value
    if not isinstance(data, (list, tuple)):
        return ()
    return tuple(str(item) for item in data)


def _task(row: dict[str, Any], *, state: str | None = None) -> Task:
    return Task(
        project=str(row["project"]),
        tz=str(row["tz"]),
        state=state or str(row["state"]),
        priority=int(row["priority"]),
        kind=str(row["kind"]),
        spec_path=str(row["spec_path"]),
        spec_sha=str(row["spec_sha"]),
        needs=_json_list(row["needs"]),
        owns=_json_list(row["owns"]),
        resources=_json_list(row["resources"]),
        acceptance=_json_list(row["acceptance"]),
        title=str(row["title"]),
        body=str(row["body"]),
    )


def _touch(store: Any, worker_id: str) -> None:
    store.touch_worker(worker_id)


# Сколько задач подряд берёт один чат web-ChatGPT. 22.09.2026: при «одна задача —
# новый чат» пропускную способность держал не счётчик рабочих мест, а темп новых
# чатов (ChatGPT ограничивает их создание, расширение — ≤5 в час): каждое ТЗ стоит
# до четырёх чатов (work, review, fix, review:fix), и в среднем работало 0.7 агента.
# Чат, который берёт задачи подряд, тратит новый чат раз в CHAT_TASK_BUDGET задач.
# Потолок — ради свежего контекста: ранние задачи длинного чата модель видит хуже.
# CLI-исполнители (Claude Code) держат контекст сами — для них потолка нет.
CHAT_TASK_BUDGET = 20  # по умолчанию; живое значение — chat_budget(store)
BUDGETED_KINDS = frozenset({"chatgpt"})
CHAT_BUDGET_RANGE = (1, 50)

# Настройки флота, которые владелец меняет на ходу (fleet.cmd chat-budget N, пульт
# fleet_owner): файл рядом с реестром, а не таблица — схема реестра версионирована, и
# работающий сервер отверг бы базу новой версии. Файл перечитывается по mtime.
SETTINGS_FILE = "settings.json"
_settings_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def settings_path(db_path: Any) -> Path | None:
    raw = str(db_path or "")
    if not raw or raw == ":memory:":
        return None
    return Path(raw).with_name(SETTINGS_FILE)


def read_settings(store: Any) -> dict[str, Any]:
    path = settings_path(getattr(store, "path", ""))
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _settings_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    _settings_cache[str(path)] = (mtime, data)
    return data


def write_setting(db_path: Any, key: str, value: Any) -> Path:
    path = settings_path(db_path)
    if path is None:
        raise ValueError("у реестра нет файла — настройки некуда записать")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data[key] = value
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def chat_budget(store: Any) -> int:
    """Сколько задач подряд берёт один чат web-ChatGPT сейчас."""
    try:
        value = int(read_settings(store).get("chat_task_budget", CHAT_TASK_BUDGET))
    except (TypeError, ValueError):
        return CHAT_TASK_BUDGET
    low, high = CHAT_BUDGET_RANGE
    return value if low <= value <= high else CHAT_TASK_BUDGET


def task_budget(tx: Any, worker_id: str, budget: int = CHAT_TASK_BUDGET) -> tuple[int, int | None]:
    """(сколько задач этот воркер уже брал, потолок или None — без потолка)."""
    worker = tx.one("SELECT kind FROM workers WHERE id=?", (worker_id,))
    kind = str(worker["kind"] or "") if worker is not None else ""
    taken = int(tx.scalar("SELECT COUNT(*) FROM claims WHERE worker_id=? "
                          "AND state!='prepare_failed'", (worker_id,)) or 0)
    if tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='retired_claim_counts'"):
        taken += int(tx.scalar('SELECT taken FROM retired_claim_counts WHERE worker_id=?',
                               (worker_id,)) or 0)
    return taken, (budget if kind in BUDGETED_KINDS else None)


def budget_left(store: Any, worker_id: str) -> int | None:
    """Сколько задач ещё можно взять в этом чате; None — потолка нет."""
    budget = chat_budget(store)
    with store.read() as tx:
        taken, cap = task_budget(tx, worker_id, budget)
    return None if cap is None else max(0, cap - taken)


def family_root(tz: str) -> str:
    """review:fix:TZ-04:2 → TZ-04: исходная задача, к которой относятся её ревью и фиксы."""
    return task_root(tz)


def _worked_on_family(tx: Any, worker_id: str, project: str, review_tz: str) -> bool:
    """Держал ли воркер work/fix-claim этой семьи задач (исходная и все её fix).

    Пока чат брал одну задачу, хватало сравнить автора самой ревьюируемой сдачи.
    Чат, берущий задачи подряд, может написать TZ-09, а потом получить
    review:fix:TZ-09 — ревью исправления поверх своей же работы. Независимость ревью
    держится на том, что его делает тот, кто в этой семье ничего не писал.
    """
    root = family_root(review_tz)
    row = tx.one(
        "SELECT 1 AS hit FROM claims WHERE worker_id=? AND project=? "
        "AND state!='prepare_failed' AND (tz=? OR tz=? OR tz LIKE ?) LIMIT 1",
        (worker_id, project, root, f"fix:{root}", f"fix:{root}:%"),
    )
    return row is not None


def _held_before(tx: Any, worker_id: str, project: str, tz: str) -> bool:
    row = tx.one(
        "SELECT 1 AS hit FROM claims WHERE worker_id=? AND project=? AND tz=? "
        "AND state!='prepare_failed' LIMIT 1",
        (worker_id, project, tz),
    )
    return row is not None


_BLIND_BATCH = re.compile(r"TZ-MR-batch-([0-9a-z]+)\Z")
_BLIND_FIX = re.compile(r"fix:TZ-MR-batch-([0-9a-z]{6,40})(?::[1-9][0-9]*)?\Z")
_BLIND_REVIEW_ZONE = re.compile(r"batch-([a-z0-9]{6,40})(?:/\*\*)?\Z")


def _blind_batch_for_task(row: dict[str, Any], *, historical: bool = False) -> str | None:
    """Derive a blind batch only from typed MR identity or an exact review zone.

    The rejected fix body and spec are untrusted: inferring a batch from their
    prose would turn candidate hints into the supposed source of independence.
    """
    if str(row.get("project")) != "Mill Works":
        return None
    tz = str(row.get("tz") or "")
    match = _BLIND_BATCH.fullmatch(tz)
    if match is not None:
        return match.group(1)
    # A rejected MR creates fix:TZ-MR-batch-* in review.decide. Its ordinary
    # body carries the rejected candidate, so *both* peek and take must replace
    # it with a case-only envelope before a fresh worker can inspect it. The
    # identity alone is insufficient: bind it to its exact owned review zone.
    review_fix = (_BLIND_FIX.fullmatch(tz)
                  if str(row.get("kind")) == "fix" else None)
    maybe_blind_fix = (str(row.get("kind")) == "fix" and
                       tz.startswith("fix:TZ-MR-batch-"))
    # A legacy fix of a review-artifact request can itself contain old verdict
    # prose. Its canonical, single-batch review ownership is the only safe key.
    is_fix = str(row.get("kind")) == "fix" and tz.startswith("fix:request:")
    is_old_request = historical and str(row.get("kind")) == "request" and tz.startswith("request:")
    if not (is_fix or is_old_request or maybe_blind_fix):
        return None
    # Returning None for an ill-formed MR fix would expose its raw rejected
    # candidate as an ordinary task. Empty string is an explicitly malformed
    # blind identity: the launcher and resume must fail closed, never fall back.
    if maybe_blind_fix and review_fix is None:
        return ""
    root = str(row.get("project_root") or "")
    if not root:
        return ""
    try:
        prefix = norm_path(str(Path(root) / "data" / "reviews")) + "/"
    except ValueError:
        return ""
    batches: set[str] = set()
    for owned in _json_list(row.get("owns")):
        try:
            path = norm_path(owned, project_root=root)
        except ValueError:
            continue
        if path.startswith(prefix):
            match = _BLIND_REVIEW_ZONE.fullmatch(path[len(prefix):])
            if match is not None:
                batches.add(match.group(1))
    if len(batches) != 1:
        return ""
    batch = next(iter(batches))
    if review_fix is not None and batch != review_fix.group(1):
        return ""
    return batch


def _blind_handoff_body(row: dict[str, Any]) -> str | None:
    """Server-owned, case-only envelope for a fresh Mill Works reviewer.

    Source specs can contain a prior reject or candidate path. The active task
    row must itself be sanitized, not only the returned take Task: resume reads
    that same row in a later process. Full source remains in the spec file.
    """
    batch = _blind_batch_for_task(row)
    if batch is None:
        return None
    return "[fleet-blind] " + json.dumps(
        {"batch_id": f"batch-{batch}", "phase": "case_only"},
        ensure_ascii=False, sort_keys=True,
    )


def blind_read_denial(store: Any, worker_id: str, path: str) -> str | None:
    """Deny reading a blind review's candidate or old verdict before seal.

    Called by the read/search policy layer; queue alone cannot mediate those
    tools. The active claim and seal are taken from server state, not blind.json.
    """
    if not worker_id:
        return None
    with store.read() as tx:
        row = tx.one(
            "SELECT t.*, p.root AS project_root FROM claims c "
            "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "JOIN projects p ON p.name=t.project "
            "WHERE c.worker_id=? AND c.state='active' "
            "ORDER BY c.id DESC LIMIT 1",
            (worker_id,),
        )
    if row is None or str(row["project"]) != "Mill Works":
        return None
    from .briefing import _blind_batch_marker, _blind_is_sealed

    task = _task(_as_dict(row))
    batch = _blind_batch_marker(task)
    if batch is None:
        return None
    root = Path(str(row["project_root"]))
    # Malformed markers fail closed over both candidate trees, never recover
    # an identity from the original contaminated task body.
    protected = (
        norm_path(str(root / "data" / "batches" / "out" / batch)) if batch
        else norm_path(str(root / "data" / "batches" / "out")),
        norm_path(str(root / "data" / "reviews" / batch)) if batch
        else norm_path(str(root / "data" / "reviews")),
    )
    target = norm_path(path)
    old_spec = str(row["spec_path"] or "").strip()
    try:
        forbidden_spec = norm_path(old_spec, project_root=str(root)) if old_spec else ""
    except ValueError:
        forbidden_spec = ""
    if not (any(target == prefix or target.startswith(prefix + "/") for prefix in protected)
            or bool(forbidden_spec and target == forbidden_spec)):
        return None
    if batch and _blind_is_sealed(store, worker_id, task, batch):
        return None
    return (
        "FLEET_BLIND_SEAL_REQUIRED: сначала независимо запиши hidden replay "
        "и получи подтверждённый сервером blind_seal; MANIFEST и прежние "
        "review/verdict до этого не читаются"
    )


def _blind_batch_exposed(
    tx: Any, worker_id: str, project: str, tz: str, row: dict[str, Any] | None = None,
) -> bool:
    """Do not give a blind miner review to someone who has seen its candidate.

    A prior MA review exposes the complete manifest in its *task envelope*;
    there need not be a read_file event. Claims are durable across release,
    rejection, escalation, reactivation and freeze/thaw. Also respect explicit
    file-read evidence when the worker saw a manifest or verdict outside a
    review claim. Both sources are server-recorded, never worker assertions.
    """
    batch = _blind_batch_for_task(row or {"project": project, "tz": tz})
    if batch is None or not worker_id:
        return False
    mined = f"TZ-MA-batch-{batch}"
    blind = f"TZ-MR-batch-{batch}"
    seen_claim = tx.one(
        "SELECT 1 AS hit FROM claims WHERE worker_id=? AND project=? AND "
        "(tz IN (?,?,?,?,?,?,?,?) OR tz LIKE ? OR tz LIKE ? OR tz LIKE ? OR tz LIKE ?) LIMIT 1",
        (
            worker_id, project,
            mined, f"fix:{mined}", f"review:{mined}", f"review:fix:{mined}",
            blind, f"fix:{blind}", f"review:{blind}", f"review:fix:{blind}",
            f"fix:{mined}:%", f"review:fix:{mined}:%",
            f"fix:{blind}:%", f"review:fix:{blind}:%",
        ),
    )
    if seen_claim is not None:
        return True
    # A migrated/compacted history can preserve the canonical author on the
    # task even when its old claim is no longer present.
    if tx.one(
        "SELECT 1 AS hit FROM tasks WHERE project=? AND tz=? AND author_worker=? LIMIT 1",
        (project, mined, worker_id),
    ) is not None:
        return True

    # Completed work must remain disqualifying even if historical claims were
    # compacted and the current task's author_worker was reset on reimport.
    # Both taking the miner task (possible candidate exposure) and submitting
    # it (proven authorship) are durable server events, not worker assertions.
    if tx.one(
        "SELECT 1 AS hit FROM events WHERE worker_id=? AND project=? "
        "AND kind IN ('take','submit') "
        "AND (tz IN (?,?,?,?,?,?,?,?) OR tz LIKE ? OR tz LIKE ? OR tz LIKE ? OR tz LIKE ?) LIMIT 1",
        (
            worker_id, project,
            mined, f"fix:{mined}", f"review:{mined}", f"review:fix:{mined}",
            blind, f"fix:{blind}", f"review:{blind}", f"review:fix:{blind}",
            f"fix:{mined}:%", f"review:fix:{mined}:%",
            f"fix:{blind}:%", f"review:fix:{blind}:%",
        ),
    ) is not None:
        return True

    # Previous claims on another request/fix for this batch exposed its review
    # context in the task envelope even if MANIFEST was never opened. Never
    # give those workers a fresh blind attempt under a different task id.
    root = str(row.get("project_root") or "") if row else ""
    if root:
        for previous in tx.query(
            "SELECT t.project,t.tz,t.kind,t.owns,p.root AS project_root "
            "FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "JOIN projects p ON p.name=t.project "
            "WHERE c.worker_id=? AND c.project=? AND t.kind IN ('fix','request')",
            (worker_id, project),
        ):
            if _blind_batch_for_task(_as_dict(previous), historical=True) == batch:
                return True

        # A meta-review of a request/fix has empty owns. Its task envelope can
        # nevertheless contain the previous reviewer's candidate rationale,
        # including in a final-review escalation. Resolve the server-generated
        # review target, never infer exposure from the prose itself.
        from .review import _review_target

        for previous in tx.query(
            "SELECT t.tz,t.body FROM claims c JOIN tasks t "
            "ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.worker_id=? AND c.project=? AND t.kind='review'",
            (worker_id, project),
        ):
            try:
                source_project, source_tz = _review_target(
                    str(previous["tz"]), str(previous["body"] or ""), project)
            except ValueError:
                continue
            source = tx.one(
                "SELECT t.project,t.tz,t.kind,t.owns,p.root AS project_root "
                "FROM tasks t JOIN projects p ON p.name=t.project "
                "WHERE t.project=? AND t.tz=?",
                (source_project, source_tz),
            )
            if source is not None and _blind_batch_for_task(
                _as_dict(source), historical=True
            ) == batch:
                return True

    # Any previously claimed task body may have embedded the candidate before
    # sanitization (including a cross-project request with unrelated owns).
    # The worker saw that envelope at take, even if no candidate file was read.
    if tx.one(
        "SELECT 1 AS hit FROM claims c JOIN tasks t ON "
        "t.project=c.project AND t.tz=c.tz WHERE c.worker_id=? "
        "AND LOWER(COALESCE(t.body,'')) LIKE ? LIMIT 1",
        (worker_id, f"%batch-{batch}%"),
    ) is not None:
        return True

    # A prior checkpoint, request, escalation, run or other recorded payload
    # can expose the candidate before its first MANIFEST read. The server event
    # history is durable even after original task bodies have been rewritten.
    # Fail closed on ambiguous provenance, not only a curated set of kinds.
    if tx.one(
        "SELECT 1 AS hit FROM events WHERE worker_id=? AND "
        "LOWER(COALESCE(payload,'')) LIKE ? LIMIT 1",
        (worker_id, f"%batch-{batch}%"),
    ) is not None:
        return True

    # Any candidate or old review artifact can expose the answer, not just
    # MANIFEST.md and verdict.json. Use full path segments to avoid confusing
    # one batch with another bearing the same prefix.
    candidate = f"%/data/batches/out/batch-{batch}/%"
    review = f"%/data/reviews/batch-{batch}/%"
    return tx.one(
        "SELECT 1 AS hit FROM events WHERE worker_id=? AND kind='read' "
        "AND (LOWER(REPLACE(path_norm, CHAR(92), '/')) LIKE ? "
        "OR LOWER(REPLACE(path_norm, CHAR(92), '/')) LIKE ?) LIMIT 1",
        (worker_id, candidate, review),
    ) is not None


def _reviewed_before(tx: Any, worker_id: str, review_row: dict[str, Any]) -> bool:
    """Держал ли воркер другое ревью той же сдачи (например, ушёл в escalate).

    Финальное ревью после escalate должен делать другой агент: тот, кто не смог проверить,
    второй раз не сможет тоже.
    """
    from .review import _review_target

    try:
        project, target = _review_target(
            str(review_row["tz"]), str(review_row.get("body") or ""), str(review_row["project"])
        )
    except ValueError:
        return False
    row = tx.one(
        "SELECT 1 AS hit FROM claims WHERE worker_id=? AND project=? AND (tz=? OR tz LIKE ?) LIMIT 1",
        (worker_id, project, f"review:{target}", f"review:{target}:%"),
    )
    return row is not None


def _active_claim_for_worker(tx: Any, worker_id: str) -> dict[str, Any] | None:
    row = tx.one(
        "SELECT c.id,c.project,c.tz,c.started_at,t.state FROM claims c "
        "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
        "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
        (worker_id,),
    )
    return _as_dict(row) if row is not None else None


def _require_addressed_claim(claim: dict[str, Any] | None, claim_id: int | None) -> None:
    """Reject a stale action inside the same transaction that changes the claim."""
    if claim_id is not None and (
        type(claim_id) is not int or claim_id <= 0
        or claim is None or int(claim["id"]) != claim_id
    ):
        raise ValueError("FLEET_STALE_CLAIM: claim_id does not match active claim")


def _claimed_paths(row: dict[str, Any]) -> tuple[str, ...]:
    """owns плюс собственный файл ТЗ — для задач, которые вообще что-то пишут.

    Протокол просит писать строку расхождения и точку продолжения в свой файл ТЗ.
    Без этого такая запись шла бы как FLEET_OUT_OF_CLAIM и портила метрику
    «вне зоны». Review (owns пуст) ничего не пишет и файл ТЗ не держит.
    """
    owns = _json_list(row.get("owns"))
    spec = str(row.get("spec_path") or "").strip()
    root = str(row.get("project_root") or "").strip()
    if not owns or not spec or not root:
        return owns
    try:
        spec_norm = norm_path(spec, project_root=root)
    except ValueError:
        return owns
    if any(paths_overlap(spec_norm, owned) for owned in owns):
        return owns
    return (*owns, spec_norm)


def _event(kind: str, *, worker_id: str, project: str, tz: str, **payload: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "worker_id": worker_id,
        "project": project,
        "tz": tz,
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def _repairs(row: dict[str, Any]) -> bool:
    """Доводит начатое или чинит ствол: fix, заявка на чужую правку, стык."""
    return str(row["kind"]) in {"fix", "request"} or task_root(str(row["tz"])).startswith("styk:")


_DEADLOCK_REVIEW_RULE = "deadlock_review_exemption:"
_DEADLOCK_REVIEW_FINGERPRINT = hashlib.sha256(
    b"TZ-54:resolver-or-verified-disjoint-diff:v1"
).hexdigest()
_DEADLOCK_REVIEW_SHADOW = timedelta(hours=24)
_DEADLOCK_REVIEW_SAMPLE = timedelta(hours=1)


def _deadlock_review_source_exempt(
    tx: Any, project: str, target: str, cache: dict[str, Any] | None = None,
) -> bool:
    """Only a TZ-49-enforced, 24h-observed TZ-54 proof may bypass a red styk.

    Review assignment and review.accept both call red_overlap; an exemption
    limited to assignment would strand a reviewer at the accept boundary.
    The expensive deadlock snapshot is shared by all reviews in one _available.
    """
    from .shadow import ShadowRules

    state = cache if cache is not None else {}
    if "rules" not in state:
        state["rules"] = ShadowRules._rules(tx)
    rule = state["rules"].get(_DEADLOCK_REVIEW_RULE + project)
    if (rule is None or rule.mode != "enforce"
            or datetime.now(timezone.utc) - rule.changed_at < _DEADLOCK_REVIEW_SHADOW):
        return False
    if "sources" not in state:
        from .deadlock import exempt_reviews
        from .review import _review_target

        sources: set[tuple[str, str]] = set()
        for proj, review_tz in exempt_reviews(tx):
            row = tx.one(
                "SELECT tz,body,project FROM tasks WHERE project=? AND tz=? AND kind='review' "
                "AND state IN ('ready','taken','submitted','frozen')",
                (proj, review_tz),
            )
            if row is not None:
                try:
                    sources.add(_review_target(str(row["tz"]), str(row["body"] or ""), proj))
                except ValueError:
                    continue
        state["sources"] = sources
    return (project, target) in state["sources"]


def _deadlock_review_shadow_tick(
    store: Any, project: str | None = None, worker_id: str | None = None,
) -> None:
    """Hourly TZ-54 what-would-unblock samples, outside the hot _available loop.

    New queue exemptions are registered as TZ-49 zone rules and observe the
    existing red block for at least 24 hours before TZ-49 may promote them.
    Neither a sample nor a shadow rule changes the effective review gate.
    """
    from .deadlock import exempt_reviews
    from .review import _review_target
    from .shadow import ShadowRules

    stamp = datetime.now(timezone.utc)
    with store.read() as tx:
        if worker_id is not None and tx.one(
            "SELECT 1 FROM workers WHERE id=? AND revoked_at IS NULL",
            (worker_id,),
        ) is None:
            return
        sql = "SELECT project,tz,body FROM tasks WHERE kind='review' AND state='ready'"
        params: tuple[str, ...] = ()
        if project is not None:
            sql += " AND project=?"
            params = (project,)
        reviews = _rows(tx.query(sql, params))
        if not reviews:
            return
        rules = ShadowRules._rules(tx)
        last = {
            str(row["project"]): str(row["last_ts"])
            for row in tx.query(
                "SELECT project,MAX(ts) AS last_ts FROM events "
                "WHERE kind='deadlock_review_shadow_sample' GROUP BY project"
            )
        }
        pending: set[str] = set()
        for row in reviews:
            name = str(row["project"])
            rule = rules.get(_DEADLOCK_REVIEW_RULE + name)
            if rule is not None and rule.mode == "enforce":
                continue
            try:
                if name in last and stamp - datetime.fromisoformat(
                    last[name].replace("Z", "+00:00")
                ) < _DEADLOCK_REVIEW_SAMPLE:
                    continue
            except ValueError:
                pass
            pending.add(name)
        if not pending:
            return
        zones = {name: red_zone(tx, name)[0] for name in pending}
        waiting: set[tuple[str, str]] = set()
        reviewed_sources: dict[tuple[str, str], str] = {}
        for row in reviews:
            name = str(row["project"])
            if name not in pending or not zones[name]:
                continue
            try:
                source_project, target = _review_target(str(row["tz"]), str(row["body"] or ""), name)
            except ValueError:
                continue
            if source_project != name or task_root(target).startswith("styk:"):
                continue
            source = tx.one("SELECT owns FROM tasks WHERE project=? AND tz=?", (name, target))
            if source is not None and any(
                paths_overlap(owned, failed)
                for owned in _json_list(source["owns"]) for failed in zones[name]
            ):
                key = (name, str(row["tz"]))
                waiting.add(key)
                reviewed_sources[key] = target
        if not waiting:
            return
        proposed = waiting & exempt_reviews(tx)

    # Only this hourly sampling path pays for the full deadlock snapshot and
    # event writes; normal peek/_available remains entirely read-only.
    with store.tx() as tx:
        for name in sorted({p for p, _ in waiting}):
            tx.record_event(
                kind="deadlock_review_shadow_sample", ts=stamp.isoformat(),
                project=name, payload={
                    "minimum_observation_hours": 24,
                    "blocked_reviews": sorted(t for p, t in waiting if p == name),
                    "would_unblock": sorted(t for p, t in proposed if p == name),
                    "fingerprint": _DEADLOCK_REVIEW_FINGERPRINT,
                },
            )
    shadow = ShadowRules(store, min_shadow=_DEADLOCK_REVIEW_SHADOW)
    for name in sorted({p for p, _ in proposed}):
        rule_id = _DEADLOCK_REVIEW_RULE + name
        shadow.register(rule_id, "zone", _DEADLOCK_REVIEW_FINGERPRINT, now=stamp)
        for _, review_tz in sorted(key for key in proposed if key[0] == name):
            # Match the subject of the later accept event (source task, not
            # review task), so TZ-49 replay can detect accepts without edits.
            shadow.check(
                rule_id, would_block=True,
                subject=f"{name}/{reviewed_sources[(name, review_tz)]}",
                reason=f"old red-styk gate would hold review {review_tz}",
                code="tz54_would_unblock", now=stamp,
            )
        shadow.advance(rule_id, now=stamp)


def _accepted_watch(tx: Any, project: str, tz: str, kind: str) -> bool:
    """An accepted watchdog episode is terminal, even if an old path requeues it.

    Watchdog assigns a different watch:<id> to a genuinely new episode. A
    judgment apply failure or stale block/thaw must not reuse the old one.
    Other triage and unfinished work retain their ordinary retry lifecycle.
    """
    if kind != "triage" or not tz.startswith("watch:"):
        return False
    return tx.one(
        "SELECT 1 FROM events WHERE project=? AND tz=? AND kind='accept' LIMIT 1",
        (project, tz),
    ) is not None


def _available(tx: Any, worker_id: str, project: str | None = None,
               skipped: list | None = None, *,
               preview_by_project: bool = False,
               snapshot: dict[str, Any] | None = None) -> Iterator[tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]]:
    """Готовые задачи, которые можно выдать этому воркеру прямо сейчас, в порядке выдачи.

    Один код и для выдачи (take_next), и для прогноза (peek_next): расширение,
    которое решает, открывать ли новую вкладку, должно видеть ровно то, что
    получит новая вкладка, а не приблизительную оценку.
    """
    sql = (
        "SELECT t.*, p.priority AS project_priority, p.root AS project_root FROM tasks t "
        "JOIN projects p ON p.name=t.project "
        "WHERE t.state='ready' AND p.active=1 "
        "AND NOT (t.kind='triage' AND t.tz LIKE 'watch:%' AND EXISTS ("
        "SELECT 1 FROM events e WHERE e.project=t.project AND e.tz=t.tz "
        "AND e.kind='accept'))"
    )
    params: list[Any] = []
    if project is not None:
        sql += " AND t.project=?"
        params.append(project)
    # Внутри проекта сначала доводится начатое: ревью, затем исправления, и только
    # потом новая работа. 22.09.2026 ревью и фиксы вставали в конец своего приоритета
    # (created_at позже новых ТЗ) и ждали 17-27 минут; TZ-09 шла до accepted 95 минут,
    # из них 67 — в очереди, а зависящие от неё ТЗ всё это время были заблокированы.
    # Заявка на чужую правку (request) идёт вместе с fix: её ждёт чья-то начатая задача.
    candidates = _rows(tx.query(sql, tuple(params)))
    if not candidates:
        return

    # Критический путь важнее возраста среди задач одинакового класса/приоритета.
    # Если acceptance одной ready-задачи сразу снимет последнюю dependency с нескольких
    # следующих, её раннее выполнение уменьшает общую длину очереди. При этом fanout НИКОГДА
    # не обгоняет review/fix/request, project priority или явный task priority.
    #
    # direct_fanout — сколько ready-задач вообще ссылаются на эту dependency;
    # unlock_now — у скольких из них это ПОСЛЕДНЯЯ непринятая dependency прямо сейчас.
    # One read transaction owns this snapshot. Reuse dependency evidence across
    # project forecasts without a time cache or weakening per-project admission.
    shared = snapshot if snapshot is not None else {}
    if "tasks" not in shared:
        shared["tasks"] = _rows(tx.query("SELECT project,tz,state,needs,resources FROM tasks"))
        shared["world_config"] = world.config()
        shared["index"] = world.build_index(
            shared["tasks"], world.provided_levels(tx), world.block_refs(tx))
    all_tasks = shared["tasks"]
    world_cfg = shared["world_config"]
    world_set = world.world_names(world_cfg)
    run_scoped = world.run_scoped(world_cfg)
    index = shared["index"]

    def unmet_of(item: dict[str, Any]) -> list[str]:
        # Зависимость от задачи-доказательства в чужом мире держит только её входы (world.py).
        return world.unmet_needs(
            str(item["project"]), _json_list(item["needs"]), _json_list(item.get("resources")),
            index, world_set,
        )

    direct_fanout: dict[tuple[str, str], int] = {}
    unlock_now: dict[tuple[str, str], int] = {}
    for item in all_tasks:
        if str(item["state"]) != "ready":
            continue
        project_name = str(item["project"])
        if not _json_list(item["needs"]):
            continue
        unmet = unmet_of(item)
        for dep in unmet:
            key = (project_name, world.dep_tz(dep))
            direct_fanout[key] = direct_fanout.get(key, 0) + 1
        if len(unmet) == 1:
            key = (project_name, world.dep_tz(unmet[0]))
            unlock_now[key] = unlock_now.get(key, 0) + 1

    # Оставшаяся цепочка (07.10.2026): прямой fanout не видит глубины — TZ-78 Nova держала
    # одну задачу напрямую и 36 транзитивно, цепочку глубиной 13. Сначала — задача, за которой
    # самая длинная цепочка незавершённой работы (HLF), затем — немедленная разблокировка,
    # затем — сколько задач за ней всего.
    below: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for item in all_tasks:
        if str(item["state"]) in ("accepted", "rejected") or not _json_list(item["needs"]):
            continue
        node = (str(item["project"]), str(item["tz"]))
        for dep in unmet_of(item):
            below.setdefault((node[0], world.dep_tz(dep)), set()).add(node)
    chain: dict[tuple[str, str], int] = {}

    def chain_below(key: tuple[str, str], path: frozenset = frozenset()) -> int:
        if key in chain:
            return chain[key]
        if key in path:  # цикл в needs — не падаем, считаем обрывом
            return 0
        val = max((1 + chain_below(nxt, path | {key}) for nxt in below.get(key, ())), default=0)
        chain[key] = val
        return val

    def descendants(key: tuple[str, str]) -> int:
        seen: set[tuple[str, str]] = set()
        stack = [key]
        while stack:
            for nxt in below.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return len(seen)

    def _candidate_order(row: dict[str, Any]) -> tuple[Any, ...]:
        kind = str(row["kind"])
        kind_rank = 0 if kind in {"review", "cross_review"} else 1 if _repairs(row) else 2
        key = (str(row["project"]), str(row["tz"]))
        # 14.10: kind_rank стоял раньше приоритета проекта — любое ревью Fleet Platform
        # обгоняло работу интенций; за ночь платформа заняла 76 % агент-часов, три интенции
        # не прошли ни стадии. Порядок проектов — первым, «доводи начатое» — внутри проекта.
        return (
            int(row["project_priority"]),
            kind_rank,
            int(row["priority"]),
            -chain_below(key),
            -unlock_now.get(key, 0),
            -descendants(key),
            -direct_fanout.get(key, 0),
            str(row["created_at"]),
            str(row["project"]),
            str(row["tz"]),
        )

    candidates.sort(key=_candidate_order)
    if "active_paths" not in shared:
        shared["active_paths"] = [
            (str(row["path_norm"]), int(row["id"]), str(row["project"]),
             str(row["tz"]), str(row["worker_id"]))
            for row in _rows(
                tx.query(
                    "SELECT cp.path_norm,c.id,c.project,c.tz,c.worker_id FROM claim_paths cp "
                    "JOIN claims c ON c.id=cp.claim_id WHERE c.state IN ('active','preparing')"
                )
            )
        ]
    active_paths = shared["active_paths"]
    # Only two *verified isolated* claims in the same project may overlap a
    # physical source-path zone; shared/legacy claims still serialize writes.
    from .goals import project_info
    isolated_active: dict[int, bool] = {}

    def _event_isolation(payload: Any, claim_id: int) -> bool:
        try:
            data = json.loads(str(payload))
        except (TypeError, ValueError):
            return False
        return (isinstance(data, dict) and data.get("claim_id") == claim_id
                and isinstance(data.get("claim_context"), dict)
                and data["claim_context"].get("mode") == "isolated")

    def independent_checkout(active: tuple[str, int, str, str, str], project: str) -> bool:
        _, claim_id, held_project, tz, worker = active
        if held_project != project:
            return False
        info = project_info(project)
        if info.get("claim_trees") is not True or info.get("commit") is not True:
            return False
        if claim_id not in isolated_active:
            events = tx.query(
                "SELECT payload FROM events WHERE kind='take' AND worker_id=? "
                "AND project=? AND tz=? ORDER BY id DESC LIMIT 30",
                (worker, project, tz),
            )
            isolated_active[claim_id] = any(
                _event_isolation(event["payload"], claim_id) for event in events
            )
        return isolated_active[claim_id]

    reserved_by_scope: dict[str,list[str]] = {}
    def _skip(row: dict[str, Any], why: str) -> None:
        if skipped is not None:
            skipped.append((str(row["project"]), str(row["tz"]), why))

    for row in candidates:
        reserved = reserved_by_scope.setdefault(str(row['project']) if preview_by_project else '',[])
        if _json_list(row["needs"]) and unmet_of(row):
            _skip(row, "ждёт задачи")
            continue

        blind_batch = _blind_batch_for_task(row)
        if blind_batch == "":
            _skip(row, "слепая выдача: нет доверенной идентичности и зоны")
            continue

        # An independent reverse-model audit is a web-ChatGPT assignment,
        # never another Gemini invocation or an ordinary review verdict.
        if row["kind"] == "cross_review":
            candidate_worker = tx.one("SELECT kind,revoked_at FROM workers WHERE id=?", (worker_id,))
            if (candidate_worker is None or str(candidate_worker["kind"]) != "chatgpt"
                    or candidate_worker["revoked_at"] is not None
                    or row.get("author_worker") == worker_id):
                _skip(row, "перекрёстное ревью: не этому агенту")
                continue

        resources = _json_list(row["resources"])
        # Датчик говорит «ресурса нет» (телефон не подключён) — выдавать бесполезно: агент лишь
        # заблокируется. Задача ждёт появления ресурса, а не перебора чатов.
        if resources and world.down_resources(tx, resources, world_cfg):
            _skip(row, "нет ресурса")
            continue
        # Ресурс scope="run" берёт сама команда на время процесса (world.hold_for_run), не захват.
        exclusive = [r for r in resources if r not in run_scoped]
        if exclusive:
            placeholders = ",".join("?" for _ in exclusive)
            busy = tx.one(
                "SELECT 1 AS busy FROM claim_resources cr "
                "JOIN claims c ON c.id=cr.claim_id "
                f"WHERE c.state IN ('active','preparing') AND cr.resource IN ({placeholders}) LIMIT 1",
                exclusive,
            )
            if busy is not None:
                _skip(row, "ресурс занят")
                continue

        owns = _claimed_paths(row)
        if any(paths_overlap(owned, claimed) for owned in reserved for claimed in owns):
            _skip(row, "зона зарезервирована ремонтом")
            continue
        if any(paths_overlap(owned, active[0]) and not independent_checkout(active, str(row["project"]))
               for owned in owns for active in active_paths):
            # Резерв зоны (07.10.2026): fix/request, упёршийся в чужие захваты, не должен ждать
            # вечно, пока новая работа раз за разом занимает его зону. fix:styk:4 Beta App
            # девять дней стоял ready: в проекте всегда кто-то работал. Зона «весь проект» не
            # резервируется — это остановило бы проект целиком.
            root = str(row.get("project_root") or "").replace("\\", "/").rstrip("/").casefold()
            if _repairs(row) and not any(
                root and root == owned.replace("\\", "/").rstrip("/").casefold() for owned in owns
            ):
                reserved.extend(owns)
            _skip(row, "зона у другого захвата")
            continue

        if row["kind"] == "review" and (
            row.get("author_worker") == worker_id
            or (worker_id and _worked_on_family(tx, worker_id, str(row["project"]), str(row["tz"])))
            or (worker_id and _reviewed_before(tx, worker_id, row))
        ):
            _skip(row, "ревью: автор или уже видел")
            continue

        # A red trunk holds integration, not the independent review verdict.
        # A review judges the submitted candidate, even when its owns overlap
        # a failing styk. Blocking it can deadlock the resolver's own review.

        # TZ-MR-batch is a *work* task, not kind=review. Its hidden tests have
        # to precede any exposure to the corresponding TZ-MA candidate. A
        # previously assigned MA reviewer already saw the candidate in their
        # envelope, even when they never opened MANIFEST.md.
        if worker_id and _blind_batch_exposed(
            tx, worker_id, str(row["project"]), str(row["tz"]), row
        ):
            _skip(row, "слепой пакет уже показан")
            continue

        # Чат, который уже держал эту задачу и вернул её без сдачи, её больше не получает.
        # 24.09.2026 W292 брал fix:TZ-165 двадцать раз подряд («Задача №20: blocker
        # неизменён, submit не выполнялся»), W272 — семнадцать: finish возвращал задачу
        # в ready, и следующий fleet_take отдавал её тому же чату первой.
        if worker_id and _held_before(tx, worker_id, str(row["project"]), str(row["tz"])):
            _skip(row, "этот агент уже держал задачу")
            continue
        # Один предикат для прогноза и выдачи (аудит Codex 14.10, §5 п.3).
        if worker_id and _triple_sibling_conflict(tx, worker_id, row):
            _skip(row, "этот агент уже делал вариант этой тройки")
            continue

        yield row, owns, resources


def _record_routing_shadow(store: Any, worker_id: str, actual: dict[str, Any], eligible: list) -> None:
    """Advisory telemetry cannot hold the claim's writer or undo a valid take."""
    try:
        with store.read() as tx:
            event = _routing_shadow_event(tx, worker_id, actual, eligible)
        if event is not None:
            with store.tx() as tx:
                tx.record_event(**event)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('routing shadow observation skipped')


def _routing_shadow_event(
    tx: Any, worker_id: str, actual: dict[str, Any],
    eligible: list[tuple[dict[str, Any], tuple[str, ...], tuple[str, ...]]],
) -> dict[str, Any] | None:
    """Compare lane assignment with the real candidate; never block a take.

    Observation uses a read snapshot AFTER the committed take. No automatic
    promotion to enforce: this remains explicitly advisory telemetry.
    """
    from .routing import LaneState, lanes, shadow_pick

    name = str(actual["project"])
    current = str(actual["tz"])
    if str(actual["kind"]) in ("review", "cross_review"):
        return _event(
            "routing_would_pick", worker_id=worker_id, project=name, tz=current,
            actual=current, proposed=current, would_change=False,
            lane="", leader="", reason="independent_review", mode="shadow",
        )
    # Accepted tasks remain in the header graph so their last author can
    # lead the following task of the same connected component. Filtering them
    # out would lose warm ownership exactly at the acceptance hand-off.
    headers = _rows(tx.query(
        "SELECT project,tz,kind,owns,needs FROM tasks WHERE project=? LIMIT 257", (name,),
    ))
    if len(headers) > 256:
        return _event('routing_shadow_skipped', worker_id=worker_id, project=name,
                      tz=current, reason='history_budget', limit=256, mode='shadow')
    groups = lanes(headers)
    if not groups:
        return None
    candidates = [item for item, _, _ in eligible if str(item["project"]) == name]
    holds = _rows(tx.query(
        "SELECT c.tz,c.worker_id,w.last_seen,w.revoked_at FROM claims c "
        "JOIN workers w ON w.id=c.worker_id "
        "WHERE c.project=? AND c.state IN ('active','released') ORDER BY c.id DESC",
        (name,),
    ))
    # The queue's official stale reclaim uses 30 minutes without heartbeat;
    # do not infer death merely because a browser tab is inactive.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    states = []
    for lane in groups:
        ready = tuple(row for row in candidates if str(row["tz"]) in lane.tzs)
        if not ready:
            continue
        holder, live = None, False
        for claim in holds:
            if str(claim["tz"]) not in lane.tzs:
                continue
            holder = str(claim["worker_id"])
            try:
                live = (claim["revoked_at"] is None and
                        _parse_time(str(claim["last_seen"])) > cutoff)
            except (TypeError, ValueError):
                live = False
            break
        states.append(LaneState(lane, ready, holder, live))
    observation = shadow_pick(worker_id, states, current)
    source = next((state for state in states if observation.proposed in state.lane.tzs), None)
    return _event(
        "routing_would_pick", worker_id=worker_id, project=name, tz=current,
        actual=current, proposed=observation.proposed,
        would_change=observation.would_change,
        lane=source.lane.name if source else "",
        leader=source.leader if source else "",
        reason=("foreign_lane" if source and source.leader not in (None, worker_id)
                else "warm_lane" if source and source.leader == worker_id
                else "free_lane" if source else "no_lane_candidate"),
        mode="shadow",
    )


def _review_revision_at_take(store: Any, tx: Any, row: dict[str, Any]) -> str:
    """Choose a review checkout from the server-sealed author submission only.

    The task body is a routing hint, never evidence of candidate bytes. The
    author claim, immutable submission ref and current source fingerprint must
    all be verified before making the reviewer checkout visible.
    """
    from .claim_context import ClaimRoutingError
    from .claim_review_bite import _submission
    from .review import _review_target

    try:
        project, tz = _review_target(str(row["tz"]), str(row["body"]), str(row["project"]))
        if project != row["project"]:
            raise ClaimRoutingError("review target crosses project boundary")
        author = tx.one(
            "SELECT c.id FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.project=? AND c.tz=? AND c.state='released' AND t.state='submitted' "
            "ORDER BY c.id DESC LIMIT 1",
            (project, tz),
        )
        if author is None:
            raise ClaimRoutingError("review target has no submitted author claim")
        sealed, context, source = _submission(store, int(author["id"]))
        if (context.mode != "isolated" or source["task_state"] != "submitted"
                or sealed.project != project or sealed.tz != tz):
            raise ClaimRoutingError("review target lacks an isolated sealed submission")
        return sealed.revision
    except ClaimRoutingError:
        raise
    except Exception as exc:
        raise ClaimRoutingError(f"sealed review evidence unavailable: {exc}") from exc


_TRIPLE_CANDIDATE = re.compile(r"^(?P<base>.+)-candidate-\d+$")


def _triple_sibling_conflict(tx: Any, worker_id: str, row: Any) -> bool:
    """One worker must not take two variants of a triple stage, nor its consensus.

    14.10.2026: W3203 took all three understood candidates of fast-jev-compaction-fixes
    in a row; the server requires three distinct workers, so the stage stayed frozen
    13 h with nothing visible.
    """
    tz = str(row["tz"])
    match = _TRIPLE_CANDIDATE.match(tz)
    base = match.group("base") if match else tz
    if match is None and tx.one(
            "SELECT 1 FROM tasks WHERE project=? AND tz=? LIMIT 1",
            (row["project"], base + "-candidate-1")) is None:
        return False
    return tx.one(
        "SELECT 1 FROM claims WHERE project=? AND worker_id=? AND tz LIKE ? AND tz<>? "
        "AND state!='prepare_failed' LIMIT 1",
        (row["project"], worker_id, base + "-candidate-%", tz)) is not None


PREPARATION_LEASE_S = 180


def _abandon_preparation(tx: Any, claim: Any, reason: str) -> None:
    claim_id = int(claim["id"])
    changed = tx.change("UPDATE claims SET state='prepare_failed',released_at=? "
                        "WHERE id=? AND state='preparing'", (_now(), claim_id),
                        event="claim_preparation_released")
    if not changed.rowcount:
        return
    tx.change("UPDATE tasks SET state='ready',updated_at=? "
              "WHERE project=? AND tz=? AND state='taken' AND NOT EXISTS ("
              "SELECT 1 FROM claims WHERE project=? AND tz=? "
              "AND state IN ('active','preparing'))",
              (_now(), claim["project"], claim["tz"], claim["project"], claim["tz"]),
              event={"kind": "claim_prepare_failed", "project": claim["project"],
                     "tz": claim["tz"], "worker_id": claim["worker_id"],
                     "payload": {"claim_id": claim_id, "reason": reason[:350]}})


def recover_preparing(store: Any) -> int:
    """Recover expired checkout reservations even when no agent calls take."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=PREPARATION_LEASE_S)).isoformat()
    with store.read() as tx:
        rows = tx.query("SELECT * FROM claims WHERE state='preparing' AND started_at<?", (cutoff,))
    if not rows:
        return 0
    with store.tx() as tx:
        for row in rows:
            _abandon_preparation(tx, row, "checkout preparation lease expired")
    return len(rows)


def take_next(store: Any, worker_id: str, project: str | None = None, *,
              launch_label: str | None = None) -> Task | None:
    from .goals import project_info
    from .claim_take_routing import prepare_claim_context
    from .claim_context import ClaimRoutingError

    _touch(store, worker_id)
    _deadlock_review_shadow_tick(store, project, worker_id)
    recover_preparing(store)
    with store.read() as tx:
        if tx.one("SELECT 1 FROM claims WHERE worker_id=? AND state IN ('active','preparing')",
                  (worker_id,)) is not None:
            return None
        if next(iter(_available(tx, worker_id, project)), None) is None:
            return None
    tried: set[tuple[str, str]] = set()
    while True:
        with store.tx() as tx:
            worker = tx.one("SELECT revoked_at FROM workers WHERE id=?", (worker_id,))
            if worker is None or worker["revoked_at"] is not None or tx.one(
                    "SELECT 1 FROM claims WHERE worker_id=? AND state IN ('active','preparing')",
                    (worker_id,)) is not None:
                return None
            taken, cap = task_budget(tx, worker_id, chat_budget(store))
            if cap is not None and taken >= cap:
                return None
            eligible = list(_available(tx, worker_id, project))
            choice = next((r for r in eligible if (r[0]["project"], r[0]["tz"]) not in tried), None)
            if choice is None:
                return None
            row, owns, resources = choice
            tried.add((row["project"], row["tz"]))
            settings = project_info(str(row["project"]))
            isolated = settings.get("claim_trees") is True and settings.get("commit") is True
            try:
                revision = (_review_revision_at_take(store, tx, row)
                            if isolated and row["kind"] == "review" else "")
            except ClaimRoutingError as exc:
                tx.record_event(kind="claim_take_refused", project=row["project"], tz=row["tz"],
                                worker_id=worker_id, payload={"reason": str(exc)[:350]})
                continue
            now = _now()
            claim_id = int(tx.change(
                "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES(?,?,?,?,?)",
                (row["project"], row["tz"], worker_id, "preparing" if isolated else "active", now),
                event="claim_preparation_reserved" if isolated else "claim_reserved").lastrowid)
            statements = [
                ("INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)", (claim_id, path))
                for path in owns
            ] + [
                ("INSERT INTO claim_resources(claim_id,resource) VALUES(?,?)", (claim_id, resource))
                for resource in resources
            ]
            statements.append(("UPDATE tasks SET state='taken',updated_at=? WHERE project=? AND tz=?",
                               (now, row["project"], row["tz"])))
            blind_body = _blind_handoff_body(row)
            if blind_body is not None:
                statements.append(("UPDATE tasks SET body=? WHERE project=? AND tz=?",
                                   (blind_body, row["project"], row["tz"])))
            event = _event("take", worker_id=worker_id, project=row["project"],
                           tz=row["tz"], claim_id=claim_id)
            tx.changes(statements, event="claim_preparation_started" if isolated else event)
            if not isolated:
                if launch_label is not None:
                    _launch_receipt(tx, worker_id, claim_id, row, launch_label)
        if not isolated:
            _record_routing_shadow(store, worker_id, row, eligible)
            return _task({**row, "body": blind_body} if blind_body is not None else row, state="taken")
        # All filesystem/Git work happens after releasing the registry writer.
        try:
            checkout = prepare_claim_context(
                claim_id=claim_id, worker_id=worker_id, project=str(row["project"]),
                tz=str(row["tz"]), project_root=str(row["project_root"]), settings=settings,
                owns=owns, needs=_json_list(row["needs"]), review_revision=revision)
            binding = checkout.as_event()
        except Exception as exc:
            with store.tx() as tx:
                claim = tx.one("SELECT * FROM claims WHERE id=?", (claim_id,))
                _abandon_preparation(tx, claim, str(exc))
                tx.record_event(kind="claim_take_refused", project=row["project"], tz=row["tz"],
                                worker_id=worker_id, payload={"claim_id": claim_id,
                                                             "reason": str(exc)[:350]})
            if not isinstance(exc, ClaimRoutingError):
                raise
            continue
        with store.tx() as tx:
            claim = tx.one("SELECT * FROM claims WHERE id=?", (claim_id,))
            current = tx.one("SELECT state,spec_sha,updated_at FROM tasks WHERE project=? AND tz=?",
                             (row["project"], row["tz"]))
            worker = tx.one("SELECT revoked_at FROM workers WHERE id=?", (worker_id,))
            if (claim is None or claim["state"] != "preparing" or current is None
                    or current["state"] != "taken" or current["updated_at"] != now
                    or current["spec_sha"] != row["spec_sha"] or worker is None
                    or worker["revoked_at"] is not None):
                if claim is not None:
                    _abandon_preparation(tx, claim, "reservation changed during checkout preparation")
                return None
            if row["kind"] == "review":
                try:
                    fresh_revision = _review_revision_at_take(store, tx, row)
                except ClaimRoutingError:
                    fresh_revision = None
                if fresh_revision != revision:
                    _abandon_preparation(tx, claim, "review submission changed during preparation")
                    return None
            event["payload"] = {"claim_id": claim_id, "claim_context": binding}
            tx.change("UPDATE claims SET state='active' WHERE id=? AND state='preparing'",
                      (claim_id,), event=event)
            if launch_label is not None:
                _launch_receipt(tx, worker_id, claim_id, row, launch_label)
        _record_routing_shadow(store, worker_id, row, eligible)
        return _task({**row, "body": blind_body} if blind_body is not None else row, state="taken")


def _launch_receipt(tx: Any, worker_id: str, claim_id: int, row: Any, label: str) -> None:
    from .launch import LEASE_S
    tx.record_event(kind='launch_reserved', worker_id=worker_id, project=row['project'], tz=row['tz'],
                    payload={'status':'reserved','worker_id':worker_id,'label':label,
                             'project':row['project'],'tz':row['tz'],'claim_id':claim_id,'lease_s':LEASE_S})


def peek_next(store: Any, project: str | None = None) -> tuple[Task | None, int]:
    """Что получит новый воркер и сколько задач сейчас можно выдать. Ничего не меняет."""
    with store.read() as tx:
        rows = [row for row, _, _ in _available(tx, "", project)]
    return (_blind_preview_task(rows[0]) if rows else None), len(rows)


def _blind_preview_task(row: dict[str, Any]) -> Task:
    """No contaminated fix prose in launcher/peek even before claim creation."""
    body = _blind_handoff_body(row)
    return _task({**row, "body": body} if body is not None else row, state="ready")


def peek_projects(store: Any) -> list[tuple[str, Task | None, int]]:
    """peek_next по каждому активному проекту: автозапуск открывает вкладку под проект,
    и ей важно, что есть в её проекте, а не что первое во всей очереди."""
    with store.read() as tx:
        names = [
            str(row["name"])
            for row in _rows(tx.query("SELECT name FROM projects WHERE active=1 ORDER BY priority ASC,name ASC"))
        ]
        first: dict[str,Task] = {}
        counts = dict.fromkeys(names,0)
        for row,_,_ in _available(tx,'',preview_by_project=True):
            name = str(row['project'])
            if name not in counts: continue
            counts[name] += 1
            if name not in first: first[name] = _blind_preview_task(row)
    return [(name,first.get(name),counts[name]) for name in names]


def _just_submitted(tx: Any, worker_id: str) -> dict[str, Any] | None:
    """Последний claim воркера, освобождённый самим submit (см. submit)."""
    row = tx.one(
        "SELECT c.project,c.tz,t.state FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
        "WHERE c.worker_id=? AND c.state='released' ORDER BY c.id DESC LIMIT 1",
        (worker_id,),
    )
    if row is None or str(row["state"]) not in ("submitted", "accepted", "rejected"):
        return None
    return _as_dict(row)


# Сколько раз подряд задачу возвращают в ready без сдачи, прежде чем флот сам её заморозит.
# Второй возврат подряд — значит, дело не в исполнителе, а в препятствии вне задачи.
FRUITLESS_FINISH_FREEZE = 2
_RESET_KINDS = ("submit", "thaw", "approve", "reactivate", "accept", "reject")


def _previous_fruitless(tx: Any, project: str, tz: str) -> int:
    """Сколько возвратов без сдачи подряд уже было у задачи (с последнего сброса)."""
    count = 0
    for row in tx.query(
        "SELECT kind,payload FROM events WHERE project=? AND tz=? AND kind IN "
        "('finish','requeue','submit','thaw','approve','reactivate','accept','reject','block') "
        "ORDER BY id DESC LIMIT 20",
        (project, tz),
    ):
        kind = str(row["kind"])
        if kind in _RESET_KINDS or kind == "block":
            break
        if kind == "requeue":
            continue  # вкладка умерла с задачей — это не препятствие в самой задаче
        if kind == "finish":
            try:
                payload = json.loads(str(row["payload"] or "{}"))
                state = payload.get("task_state")
                summary = str(payload.get("summary") or "")
            except ValueError:
                state, summary = None, ""
            if state != "ready":
                break
            # handoff: означает отказ/истощение конкретного ChatGPT-чата, а не blocker
            # самой задачи. Такие освобождения не должны накапливать task-level freeze score.
            if summary.casefold().lstrip().startswith("handoff:"):
                continue
        count += 1
    return count


def finish(store: Any, worker_id: str, summary: str, *,
           claim_id: int | None = None, action_receipt: dict[str, str] | None = None) -> str:
    """Освободить claim. Возвращает состояние задачи после: submitted | ready | frozen | ''.

    '' — активного claim нет, потому что submit уже освободил его сам: повторный finish после
    сдачи — штатный шаг протокола, а не ошибка.
    """
    _touch(store, worker_id)
    with store.tx() as tx:
        claim = _active_claim_for_worker(tx, worker_id)
        _require_addressed_claim(claim, claim_id)
        if claim is None:
            if _just_submitted(tx, worker_id) is not None:
                return ""
            raise ValueError(f"{worker_id}: нет активной задачи")

        next_state = "submitted" if claim["state"] == "submitted" else "ready"
        current = tx.one("SELECT kind FROM tasks WHERE project=? AND tz=?",
                         (claim["project"], claim["tz"]))
        if (next_state == "ready" and current is not None and _accepted_watch(
                tx, str(claim["project"]), str(claim["tz"]), str(current["kind"]))):
            # A worker holding a legacy reissued watchdog claim must not put
            # the already accepted episode back in the queue on finish.
            next_state = "accepted"
        summary_text = str(summary or "")
        explicit_block = bool(re.match(r"(?is)^\s*(?:blocked\b|заблокирован\w*\b|блокер\b)", summary_text))
        automatic_handoff = summary_text.casefold().lstrip().startswith("handoff:")
        if next_state == "ready":
            kind = tx.one("SELECT kind FROM tasks WHERE project=? AND tz=?", (claim["project"], claim["tz"]))
            if (
                kind is not None
                and str(kind["kind"]) in ("work", "fix", "request")
                and not automatic_handoff
                and (
                    explicit_block
                    or _previous_fruitless(tx, str(claim["project"]), str(claim["tz"])) + 1 >= FRUITLESS_FINISH_FREEZE
                )
            ):
                next_state = "frozen"
        now = _now()
        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                "UPDATE claims SET state='released',released_at=? WHERE id=?",
                (now, claim["id"]),
            )
        ]
        if claim["state"] != "submitted":
            statements.append(
                (
                    "UPDATE tasks SET state=?,updated_at=? WHERE project=? AND tz=?",
                    (next_state, now, claim["project"], claim["tz"]),
                )
            )
        if next_state == "frozen":
            # Как fleet_block: задача не раздаётся циклом, а оттаивает сама (thaw_frozen) —
            # когда приняты задачи, названные в причине, или по времени.
            tx.changes(
                statements,
                event=_event(
                    "block",
                    worker_id=worker_id,
                    project=str(claim["project"]),
                    tz=str(claim["tz"]),
                    reason=summary,
                    automatic=True,
                    previous_state="taken",
                ),
            )
            from .actions import record_receipt
            record_receipt(tx, worker_id, int(claim["id"]), action_receipt)
            return next_state
        tx.changes(
            statements,
            event=_event(
                "finish",
                worker_id=worker_id,
                project=str(claim["project"]),
                tz=str(claim["tz"]),
                summary=summary,
                task_state=next_state,
                automatic_handoff=automatic_handoff,
            ),
        )
        from .actions import record_receipt
        record_receipt(tx, worker_id, int(claim["id"]), action_receipt)
    return next_state


def _final_review_infrastructure_block(
    tx: Any, claim: Any, review_task: Any, reason: str, worker_id: str,
) -> tuple[str, list[int]]:
    """Validate a dependency-only pause; never decide the reviewed work's verdict.

    A final review cannot escalate and must not reject a source change merely
    because the execution environment cannot run its sealed acceptance. This
    exception requires repeated *server-recorded* failed attempts at an exact
    mandatory command and a single, existing, still-pending request. It does
    not prove the cause of failure and deliberately grants no acceptance.
    """
    from . import review

    if review.FINAL_MARK not in str(review_task["body"] or ""):
        raise ValueError("review нельзя block: используй fleet_review(..., 'escalate', note)")
    project, tz = str(claim["project"]), str(claim["tz"])
    source_project, source_tz = review._review_target(
        tz, str(review_task["body"] or ""), project)
    source = tx.one(
        "SELECT state,acceptance FROM tasks WHERE project=? AND tz=?",
        (source_project, source_tz))
    acceptance = _json_list(review_task["acceptance"])
    if (source is None or str(source["state"]) != "submitted" or not acceptance
            or acceptance != _json_list(source["acceptance"])):
        raise ValueError("final review: sealed acceptance/source state не подтверждены")

    requests = set(re.findall(r"(?<![\w:])request:[0-9a-f]{8}\b", reason, flags=re.I))
    if len(requests) != 1:
        raise ValueError("final review block требует одну явную dependency request:<8 hex> в reason")
    dependency = next(iter(requests))
    current = tx.one(
        "SELECT tz,kind,state FROM tasks WHERE project=? AND LOWER(tz)=LOWER(?)",
        (project, dependency))
    if (current is None or str(current["kind"]) != "request"
            or str(current["state"]) in {"accepted", "rejected"}):
        raise ValueError("final review block требует существующий незавершённый infrastructure request")
    dependency = str(current["tz"])
    if _json_list(review_task["needs"]) not in ((), (dependency,)):
        raise ValueError("final review block: непредвиденные dependencies; не заменяем их")

    failed: dict[str, list[int]] = {command: [] for command in acceptance}
    succeeded: set[str] = set()
    for attempt in tx.query(
        "SELECT id,rc,payload,path_norm FROM events "
        "WHERE project=? AND tz=? AND worker_id=? AND kind='run' AND tool='run' "
        "AND ts>=? AND rc IS NOT NULL ORDER BY id",
        (project, tz, worker_id, claim["started_at"]),
    ):
        for observed in review._payload_commands(attempt["payload"]):
            for command in acceptance:
                if review._matches_acceptance_run(
                    command, observed, str(attempt["path_norm"] or "")):
                    if attempt["rc"] == 0:
                        succeeded.add(command)
                    else:
                        failed[command].append(int(attempt["id"]))
    # A red gate is not permission to reject, but neither is a single arbitrary
    # failure permission to freeze the entire review lane indefinitely.
    evidence = next(
        (ids for command, ids in failed.items()
         if command not in succeeded and len(ids) >= 3), [])
    if not evidence:
        raise ValueError(
            "final review block требует >=3 неуспешных run одной sealed acceptance "
            "после взятия review, без успешного прогона этой команды")
    return dependency, evidence[-3:]


def block(store: Any, worker_id: str, reason: str, *,
          claim_id: int | None = None, action_receipt: dict[str, str] | None = None) -> None:
    """Release an externally blocked task without immediately reissuing it.

    finish() returns unfinished work to ready. For a blocker outside owns or an
    external blocker that creates a take/finish loop. block() preserves the shared
    worktree, releases the claim, and freezes the exact task; maintenance later thaws it
    automatically when named dependencies are accepted or on a bounded retry schedule.
    """
    _touch(store, worker_id)
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("block: нужна причина")
    with store.tx() as tx:
        claim = _active_claim_for_worker(tx, worker_id)
        _require_addressed_claim(claim, claim_id)
        if claim is None:
            raise ValueError(f"{worker_id}: нет активной задачи")
        task = tx.one(
            "SELECT kind,state,body,acceptance,needs FROM tasks WHERE project=? AND tz=?",
            (claim["project"], claim["tz"]),
        )
        if task is None:
            raise ValueError(f"{claim['project']}/{claim['tz']}: задача не найдена")
        if str(task["kind"]) == "review":
            dependency, run_ids = _final_review_infrastructure_block(
                tx, claim, task, reason, worker_id)
            now = _now()
            tx.changes(
                [
                    ("UPDATE claims SET state='released',released_at=? WHERE id=?",
                     (now, claim["id"])),
                    ("UPDATE tasks SET state='frozen',needs=?,updated_at=? "
                     "WHERE project=? AND tz=?",
                     (json.dumps([dependency]), now, claim["project"], claim["tz"])),
                ],
                event=_event(
                    "block", worker_id=worker_id,
                    project=str(claim["project"]), tz=str(claim["tz"]),
                    reason=reason, previous_state=str(task["state"]),
                    failed_acceptance_run_ids=run_ids,
                    origin="fleet_auto", gate="dependency",
                    reactivate_policy={
                        "kind": "accepted_work",
                        "target": f"{claim['project']}/{dependency}",
                        "authority": "fleet_task_events",
                    },
                ),
            )
            from .actions import record_receipt
            record_receipt(tx,worker_id,int(claim['id']),action_receipt)
            return
        if str(task["state"]) == "submitted":
            raise ValueError("submitted-задачу нельзя block: сначала заверши review lifecycle")
        if _accepted_watch(tx, str(claim["project"]), str(claim["tz"]), str(task["kind"])):
            # A legacy accepted watch may already be taken when this guard is
            # deployed. Release that claim without fabricating a fresh block.
            now = _now()
            tx.changes([
                ("UPDATE claims SET state='released',released_at=? WHERE id=?",
                 (now, claim["id"])),
                ("UPDATE tasks SET state='accepted',updated_at=? WHERE project=? AND tz=?",
                 (now, claim["project"], claim["tz"])),
            ], event=_event("watch_terminal_restore", worker_id=worker_id,
                            project=str(claim["project"]), tz=str(claim["tz"]),
                            reason="historical accept; duplicate block suppressed"))
            from .actions import record_receipt
            record_receipt(tx, worker_id, int(claim["id"]), action_receipt)
            return
        own_probe = self_acceptance_until(reason, task["acceptance"])
        # Persist the *source* of automatic authority at block creation.
        # Free-form reason text is not itself a verified resolution predicate:
        # until: additionally requires a registered server probe, and a task
        # dependency must be a unique, existing task from registered needs.
        policy_binding: dict[str, Any] = {}
        if not own_probe:
            from .deploy_events import _criterion
            deployment = _criterion({"reason": reason})
            dependencies = _json_list(task["needs"])
            if deployment is not None:
                phase, target = deployment
                policy_binding = {
                    "origin": "fleet_auto", "gate": "operational",
                    "reactivate_policy": {
                        "kind": ("platform_deploy_commit" if phase == "commit"
                                 else "platform_deploy_after"),
                        "target": target, "authority": "platform_guard",
                    },
                }
            elif len(dependencies) == 1:
                dependency = dependencies[0].strip()
                if (dependency and dependency != str(claim["tz"])
                        and tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?",
                                   (claim["project"], dependency)) is not None):
                    policy_binding = {
                        "origin": "fleet_auto", "gate": "dependency",
                        "reactivate_policy": {
                            "kind": "accepted_work",
                            "target": f"{claim['project']}/{dependency}",
                            "authority": "fleet_task_events",
                        },
                    }
            elif probe.from_reason(reason):
                # No registered proof yet. The only possible predicate is
                # supplied by a subsequent *server-owned* block_probe event.
                policy_binding = {
                    "origin": "fleet_auto", "gate": "operational",
                    "reactivate_policy": {"kind": "registered_probe"},
                }
        now = _now()
        tx.changes(
            [
                (
                    "UPDATE claims SET state='released',released_at=? WHERE id=?",
                    (now, claim["id"]),
                ),
                (
                    "UPDATE tasks SET state='frozen',updated_at=? WHERE project=? AND tz=?",
                    (now, claim["project"], claim["tz"]),
                ),
            ],
            event=_event(
                "freeze" if own_probe else "block",
                worker_id=worker_id,
                project=str(claim["project"]),
                tz=str(claim["tz"]),
                reason=reason,
                previous_state=str(task["state"]),
                **policy_binding,
            ),
        )
        if own_probe:
            _self_acceptance_triage(tx, str(claim["project"]), str(claim["tz"]), reason)
        from .actions import record_receipt
        record_receipt(tx, worker_id, int(claim["id"]), action_receipt)


def _zone_untouched(store: Any, claim_id: int, own: list[str]) -> bool:
    """Файлы зоны не изменились со взятия задачи — по снимку evidence.capture.

    Снимка нет (evidence недоступен, задачу взяли не через fleet_take) или он усечён —
    считаем, что изменения были: проверка без базы не должна отказывать.
    """
    from . import evidence

    root = evidence.snapshot_root(store)
    if root is None:
        return False
    manifest = evidence._load_manifest(root, claim_id)
    if not manifest or manifest.get("truncated"):
        return False
    before = manifest.get("files") or {}
    current, truncated = evidence.expand(own)
    if truncated:
        return False
    if set(current) != set(before):
        return False
    return all(evidence._sha(path) == before.get(path) for path in current)


def _verified_recovery_without_changes(
    tx: Any,
    worker_id: str,
    claim: dict[str, Any],
    own: list[str],
) -> bool:
    """Allow a recovery worker to submit preserved work after fresh verification.

    The empty-submit guard exists to catch a chat that accidentally submits the
    previous task. It should not force a meaningless file edit when this exact
    task already contains work from an earlier claim and a fresh worker only had
    to re-run verification after recovery/requeue.
    """
    prior_paths = [
        str(row["path_norm"])
        for row in tx.query(
            """SELECT DISTINCT path_norm FROM events
               WHERE project=? AND tz=? AND kind='write' AND ts<?
                 AND path_norm<>''""",
            (claim["project"], claim["tz"], claim["started_at"]),
        )
    ]
    if not any(paths_overlap(previous, current) for previous in prior_paths for current in own):
        return False
    verified = tx.one(
        """SELECT 1 AS hit FROM events
           WHERE project=? AND tz=? AND worker_id=? AND kind='run'
             AND ts>=? AND rc=0 LIMIT 1""",
        (claim["project"], claim["tz"], worker_id, claim["started_at"]),
    )
    return verified is not None


def _accepted_external_fix_without_changes(tx: Any, worker_id: str,
                                           claim: dict[str, Any]) -> bool:
    """Allow a no-diff fix only after an *independently accepted* cited blocker.

    The cited dependency comes from a server-recorded independent rejection,
    NOT the submitting agent's note. A newer independent accept and a fresh
    successful run after that accept are both mandatory. Restrict this bridge
    to Fleet Platform requests: ordinary no-op code fixes remain fail-closed.
    """
    fix_tz = str(claim["tz"])
    match = re.fullmatch(r"fix:(request:[0-9a-f]{8})(?::[0-9]+)?", fix_tz)
    if match is None:
        return False
    source_tz = match.group(1)
    rejection = tx.one(
        "SELECT ts,payload FROM events WHERE kind='reject' AND project=? "
        "AND tz=? ORDER BY id DESC LIMIT 1",
        (claim["project"], source_tz),
    )
    if rejection is None:
        return False
    try:
        proof = json.loads(str(rejection["payload"]))
    except (TypeError, ValueError):
        return False
    if not isinstance(proof, dict) or proof.get("fix_task") != fix_tz:
        return False
    # A reviewer's independently recorded diagnosis binds one exact external
    # release request. Arbitrary text in the fix worker's note is ignored.
    references = set(re.findall(
        r"(?<![\w])Fleet Platform/(request:[0-9a-f]{8})(?![\w])",
        str(proof.get("note") or ""),
    ))
    if len(references) != 1:
        return False
    dependency = next(iter(references))
    accepted_task = tx.one(
        "SELECT kind,state FROM tasks WHERE project='Fleet Platform' AND tz=?",
        (dependency,),
    )
    if (accepted_task is None or accepted_task["kind"] != "request"
            or accepted_task["state"] != "accepted"):
        return False
    acceptance = tx.one(
        "SELECT ts,worker_id FROM events WHERE project='Fleet Platform' "
        "AND tz=? AND kind='accept' AND ts>? ORDER BY id DESC LIMIT 1",
        (dependency, rejection["ts"]),
    )
    if acceptance is None or acceptance["worker_id"] == worker_id:
        return False
    since = max(str(claim["started_at"]), str(acceptance["ts"]))
    return tx.one(
        "SELECT 1 FROM events WHERE project=? AND tz=? AND worker_id=? "
        "AND kind='run' AND rc=0 AND ts>=? LIMIT 1",
        (claim["project"], fix_tz, worker_id, since),
    ) is not None


def _vanished_generated_draft_fix(tx: Any, claim: dict[str, Any], own: list[str]) -> bool:
    """A planner-generated draft repair is obsolete if its exact draft vanished.

    This is *not* a green-run waiver for arbitrary fixes.  The trusted
    ``draft_fix_open/refresh`` event must bind the fix id, fingerprint, spec
    and single claimed path to one canonical draft.  An existing, inaccessible
    or symlinked draft (or parent) fails closed.  A missing leaf in an existing
    real drafts directory means there is no longer a draft to edit; independent
    review still has to accept the no-diff submission.
    """
    fix_tz = str(claim["tz"])
    if not re.fullmatch(r"fix:draft:[0-9a-f]{16}", fix_tz):
        return False
    task = tx.one(
        "SELECT kind,spec_path,spec_sha FROM tasks WHERE project=? AND tz=?",
        (claim["project"], fix_tz),
    )
    if task is None or str(task["kind"]) != "fix":
        return False
    generated = tx.one(
        "SELECT payload FROM events WHERE project=? AND tz=? "
        "AND kind IN ('draft_fix_open','draft_fix_refresh') "
        "ORDER BY id DESC LIMIT 1",
        (claim["project"], fix_tz),
    )
    if generated is None:
        return False
    try:
        proof = json.loads(str(generated["payload"]))
    except (TypeError, ValueError):
        return False
    if not isinstance(proof, dict):
        return False
    rel, fingerprint = proof.get("draft"), proof.get("fingerprint")
    if (not isinstance(rel, str)
            or not re.fullmatch(r"ToDo/tz-drafts/TZ-[^/\\]+\.md", rel, re.I)
            or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            or fix_tz != "fix:draft:" + hashlib.sha256(rel.casefold().encode()).hexdigest()[:16]
            or str(task["spec_path"]) != rel
            or str(task["spec_sha"]) != fingerprint):
        return False
    project = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (claim["project"],))
    if project is None:
        return False
    root = Path(str(project["root"]))
    directory = root / "ToDo" / "tz-drafts"
    leaf = directory / rel.rsplit("/", 1)[-1]
    try:
        if (any(path.is_symlink() for path in (root, root / "ToDo", directory, leaf))
                or not directory.is_dir()
                # A successful enumeration is stronger evidence of absence than
                # Path.exists(), which can mask inaccessible leaves on Windows.
                or any(path.name.casefold() == leaf.name.casefold()
                       for path in directory.iterdir())
                or own != [norm_path(str(leaf))]):
            return False
    except (OSError, ValueError):
        return False
    return True


def _corrected_generated_draft_header_fix(
    store: Any, tx: Any, worker_id: str, claim: dict[str, Any], own: list[str],
) -> bool:
    """Allow no-diff review when a *specific* generated YAML-header fault is gone.

    This is not permission to submit arbitrary no-op fixes. Bind the generated
    fix to its original draft_invalid fingerprint, ensure bytes changed since
    that failing observation, and independently parse the repaired header.
    An isolated claim reads its own validated sealed tree, never live trunk.
    """
    fix_tz = str(claim["tz"])
    if not re.fullmatch(r"fix:draft:[0-9a-f]{16}", fix_tz):
        return False
    sealed = tx.one(
        "SELECT 1 FROM events WHERE kind='claim_submission' AND project=? AND tz=? "
        "AND worker_id=? AND ts>=? LIMIT 1",
        (claim["project"], fix_tz, worker_id, claim["started_at"]),
    ) is not None
    task = tx.one(
        "SELECT kind,spec_path,spec_sha FROM tasks WHERE project=? AND tz=?",
        (claim["project"], fix_tz),
    )
    if task is None or task["kind"] != "fix":
        return False
    generated = tx.one(
        "SELECT payload FROM events WHERE project=? AND tz=? AND "
        "kind IN ('draft_fix_open','draft_fix_refresh') ORDER BY id DESC LIMIT 1",
        (claim["project"], fix_tz),
    )
    if generated is None:
        return False
    try:
        proof = json.loads(str(generated["payload"]))
    except (ValueError, TypeError):
        return False
    if not isinstance(proof, dict):
        return False
    rel, fingerprint = proof.get("draft"), proof.get("fingerprint")
    if (not isinstance(rel, str)
            or not re.fullmatch(r"ToDo/tz-drafts/TZ-[^/\\]+\.md", rel, re.I)
            or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            or fix_tz != "fix:draft:" + hashlib.sha256(rel.casefold().encode()).hexdigest()[:16]
            or task["spec_path"] != rel or task["spec_sha"] != fingerprint):
        return False
    project = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (claim["project"],))
    if project is None:
        return False
    project_root = Path(str(project["root"]))
    if own != [norm_path(str(project_root / rel.replace("/", os.sep)))]:
        return False
    root = project_root
    sealed_tree = ""
    if sealed:
        try:
            from .claim_review_bite import _submission

            submission, context, _ = _submission(store, int(claim["id"]))
            if context.mode != "isolated" or submission.author != worker_id:
                return False
            root = context.tree_path
            sealed_tree = submission.tree_sha
        except (OSError, RuntimeError, ValueError):
            return False
    path = root / rel.replace("/", os.sep)
    try:
        if (any(part.is_symlink() for part in (root, root / "ToDo", path.parent, path))
                or not path.is_file()):
            return False
        # Trust the source event, not a worker-supplied note or a green command
        # that might check another file. Its fingerprint includes bytes+reason.
        stem = re.match(r"TZ-[A-Za-z0-9]+", path.stem, re.I)
        if stem is None:
            return False
        previous = tx.query(
            "SELECT payload FROM events WHERE project=? AND tz=? AND kind='draft_invalid' "
            "ORDER BY id DESC",
            (claim["project"], stem.group(0)),
        )
        reasons = []
        for item in previous:
            try:
                value = json.loads(str(item["payload"]))
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get("draft") == rel and value.get("fingerprint") == fingerprint:
                reasons.append(str(value.get("reason") or ""))
        reason = next((r for r in reasons if re.search(
            r"(?i)yaml|шапк|обязательн.{0,30}пол|поле (tz|owns|project|needs|acceptance)",
            r,
        )), "")
        if not reason:
            return False
        data = path.read_bytes()
        if sealed_tree:
            snapshot = subprocess.run(
                ["git", "-C", str(project_root), "show", f"{sealed_tree}:{rel}"],
                capture_output=True, timeout=30,
            )
            if snapshot.returncode or snapshot.stdout != data:
                return False
        if hashlib.sha256(data + b"\0" + reason.encode("utf-8")).hexdigest() == fingerprint:
            return False  # still exactly the offending bytes
        from .spec import _header, _load_header, parse_spec

        _body, header = _header(path)
        fields = _load_header(path, header)
        if not isinstance(fields, dict) or not {
            "tz", "project", "owns", "needs", "acceptance",
        }.issubset(fields):
            return False
        parsed = parse_spec(str(path.absolute()))
        if (parsed.project != claim["project"] or parsed.tz.casefold() != stem.group(0).casefold()
                or not parsed.owns or not parsed.acceptance):
            return False
        # A draft still needs its canonical filename; parsing arbitrary text
        # with the correct keys is not proof the planner's exact defect is gone.
        return True
    except (OSError, ValueError, TypeError):
        return False


def _review_retested_out_of_scope_fix_without_changes(
    tx: Any, worker_id: str, claim: dict[str, Any], own: list[str],
) -> bool:
    """Bridge a superseded request fix only with an independent failed/retested case.

    A reviewer must have identified the defect in a file outside this fix's
    owns, a *different* worker must actually have changed that file after the
    rejection, and the same reviewer-failed test files must now pass.  The
    resulting submission still needs another independent review.  This is
    deliberately not a general green-test waiver for no-op tasks.
    """
    fix_tz = str(claim["tz"])
    match = re.fullmatch(r"fix:(request:[0-9a-f]{8})(?::[0-9]+)?", fix_tz)
    if match is None:
        return False
    source_tz = match.group(1)
    rejection = tx.one(
        "SELECT ts,worker_id,payload FROM events WHERE project=? AND tz=? "
        "AND kind='reject' ORDER BY id DESC LIMIT 1",
        (claim["project"], source_tz),
    )
    if rejection is None or rejection["worker_id"] == worker_id:
        return False
    try:
        proof = json.loads(str(rejection["payload"]))
    except (TypeError, ValueError):
        return False
    if not isinstance(proof, dict) or proof.get("fix_task") != fix_tz:
        return False
    note = str(proof.get("note") or "").replace("\\", "/")
    if not any(flag in note.casefold() for flag in ("вне owns", "outside owns", "вне зоны")):
        return False
    tests = set(re.findall(r"(?<![\w/])tests/[A-Za-z0-9_./-]+\.py", note))
    refs = set(re.findall(r"(?<![\w/])src/dev_mcp/[A-Za-z0-9_./-]+\.py", note))
    external = {
        ref for ref in refs if not any(path.casefold().endswith("/" + ref.casefold())
                                       for path in own)
    }
    if not tests or not external:
        return False

    original = tx.query(
        "SELECT path_norm FROM events WHERE project=? AND tz=? AND kind='write' "
        "AND ts<? AND path_norm<>''",
        (claim["project"], source_tz, rejection["ts"]),
    )
    if not any(paths_overlap(str(row["path_norm"]), path) for row in original for path in own):
        return False

    # The run must belong to the worker who actually rejected the source.
    # Another worker's red test is not independent reviewer evidence.
    failures = tx.query(
        "SELECT payload FROM events WHERE project=? AND tz IN (?,?) AND kind='run' "
        "AND rc<>0 AND ts<=? AND worker_id=?",
        (claim["project"], source_tz, "review:" + source_tz,
         rejection["ts"], rejection["worker_id"]),
    )
    if not any(_run_covers_tests(row["payload"], tests) for row in failures):
        return False

    changes = tx.query(
        "SELECT path_norm,worker_id FROM events WHERE kind='write' AND ts>? "
        "AND ts<=? AND path_norm<>''",
        (rejection["ts"], claim["started_at"]),
    )
    if not any(row["worker_id"] != worker_id
               and any(str(row["path_norm"]).casefold().endswith("/" + path.casefold())
                       for path in external)
               for row in changes):
        return False

    fresh = tx.query(
        "SELECT payload FROM events WHERE project=? AND tz=? AND worker_id=? "
        "AND kind='run' AND rc=0 AND ts>=?",
        (claim["project"], fix_tz, worker_id, claim["started_at"]),
    )
    return any(_run_covers_tests(row["payload"], tests) for row in fresh)


def _accepted_blocked_regression_without_changes(
    tx: Any, worker_id: str, claim: dict[str, Any], own: list[str],
) -> bool:
    """No-diff retry of a fix whose *reviewed* missing test was accepted elsewhere.

    Only a numbered request-fix retry qualifies.  Three independent server
    receipts must agree: the previous fix's rejecting review names a test
    outside owns, its block names the external request for that test, and a
    separate reviewer accepts that request after its author wrote the named
    test.  The retry worker must also re-run that exact test successfully.
    """
    fix_tz = str(claim["tz"])
    match = re.fullmatch(r"(fix:request:[0-9a-f]{8}):([0-9]+)", fix_tz)
    if match is None or int(match.group(2)) < 2:
        return False
    previous = (match.group(1) if int(match.group(2)) == 2
                else f"{match.group(1)}:{int(match.group(2)) - 1}")
    project = str(claim["project"])
    started = str(claim["started_at"])
    rejects = tx.query(
        "SELECT ts,worker_id,payload FROM events WHERE project=? AND tz=? "
        "AND kind='reject' AND ts<? ORDER BY id DESC",
        (project, previous, started),
    )
    for rejection in rejects:
        if rejection["worker_id"] == worker_id:
            continue
        try:
            proof = json.loads(str(rejection["payload"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(proof, dict) or proof.get("fix_task") != fix_tz:
            continue
        note = str(proof.get("note") or "").replace("\\", "/")
        named_tests = set(re.findall(
            r"(?<![\w/])tests/[A-Za-z0-9_./-]+\.py", note,
        ))
        named_tests = {path for path in named_tests if ".." not in path.split("/")}
        named_tests = {
            path for path in named_tests
            if not any(str(item).casefold().endswith("/" + path.casefold()) for item in own)
        }
        if not named_tests:
            continue
        # The fix's original implementation must have actually been written
        # inside this same claimed zone before the independent rejection.
        earlier = tx.query(
            "SELECT path_norm FROM events WHERE project=? AND tz=? AND kind='write' "
            "AND ts<=? AND path_norm<>''",
            (project, previous, rejection["ts"]),
        )
        if not any(paths_overlap(str(row["path_norm"]), path)
                   for row in earlier for path in own):
            continue
        blocks = tx.query(
            "SELECT ts,payload FROM events WHERE project=? AND tz=? "
            "AND kind IN ('block','freeze') AND ts>=? AND ts<? ORDER BY id",
            (project, fix_tz, rejection["ts"], started),
        )
        for block in blocks:
            try:
                blocked = json.loads(str(block["payload"]))
            except (TypeError, ValueError):
                continue
            if not isinstance(blocked, dict):
                continue
            reason = str(blocked.get("reason") or "").replace("\\", "/")
            cited_tests = set(re.findall(
                r"(?<![\w/])tests/[A-Za-z0-9_./-]+\.py", reason,
            ))
            tests = named_tests & cited_tests
            if not tests:
                continue
            dependencies = set(re.findall(
                r"(?<![\w:])(request:[0-9a-f]{8})(?![\w:])", reason,
            ))
            if len(dependencies) != 1:
                continue
            dependency = next(iter(dependencies))
            external = tx.one(
                "SELECT kind,state,owns FROM tasks WHERE project=? AND tz=?",
                (project, dependency),
            )
            if (external is None or external["kind"] != "request"
                    or external["state"] != "accepted"):
                continue
            external_owns = _json_list(external["owns"])
            if not all(any(str(path).replace("\\", "/").casefold().endswith(
                    "/" + test.casefold()) for path in external_owns) for test in tests):
                continue
            accepts = tx.query(
                "SELECT ts,worker_id FROM events WHERE project=? AND tz=? "
                "AND kind='accept' AND ts>? ORDER BY id DESC",
                (project, dependency, block["ts"]),
            )
            for accepted in accepts:
                if accepted["worker_id"] in {worker_id, rejection["worker_id"]}:
                    continue
                writes = tx.query(
                    "SELECT ts,worker_id,path_norm FROM events WHERE project=? AND tz=? "
                    "AND kind='write' AND ts>? AND ts<=? AND path_norm<>''",
                    (project, dependency, block["ts"], accepted["ts"]),
                )
                if not all(any(
                        str(row["path_norm"]).replace("\\", "/").casefold().endswith(
                            "/" + test.casefold())
                        and row["worker_id"] not in {worker_id, accepted["worker_id"]}
                        for row in writes) for test in tests):
                    continue
                fresh = tx.query(
                    "SELECT payload FROM events WHERE project=? AND tz=? AND worker_id=? "
                    "AND kind='run' AND rc=0 AND ts>=?",
                    (project, fix_tz, worker_id, max(started, str(accepted["ts"]))),
                )
                if any(_run_covers_tests(row["payload"], tests) for row in fresh):
                    return True
    return False


def _run_covers_tests(payload: Any, tests: set[str]) -> bool:
    """Accept only an actual pytest test-run command naming the exact files.

    A green ``run`` receipt only contains argv and rc, not pytest's collected/
    executed counts.  Therefore this waiver must fail closed for *all* modes
    that may exit successfully without executing the named regression: help,
    collection, selection/deselection, plugins' unknown flags, and wrappers.
    An ordinary unfiltered pytest run of the named files is the only eligible
    command; the caller's run event must separately have rc=0.
    """
    if not tests:
        return False
    try:
        info = json.loads(str(payload))
    except (TypeError, ValueError):
        return False
    argv = info.get("argv") if isinstance(info, dict) else None
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(arg, str) and arg for arg in argv)):
        return False
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable in {"pytest", "pytest.exe", "pytest-script.py"}:
        args = argv[1:]
    elif executable in {"py", "py.exe", "python", "python.exe", "python3", "python3.exe"}:
        position = 1
        # The Windows py launcher may select an interpreter before -m, but
        # Python options / script arguments before -m must not be accepted.
        if executable in {"py", "py.exe"} and position < len(argv) and re.fullmatch(
                r"-3(?:\.\d{1,2})?", argv[position]):
            position += 1
        if argv[position:position + 2] != ["-m", "pytest"]:
            return False
        args = argv[position + 2:]
    else:
        return False

    # All other switches can alter collection or test execution (including
    # --collect-only, --help, -k, -m, --deselect, --ignore, --lf), or load
    # arbitrary plugin/config options.  These cannot authorize a no-diff fix.
    presentation_only = {
        "-q", "-qq", "--quiet", "-v", "-vv", "--verbose", "-s",
        "--capture=no", "-x", "--exitfirst", "--disable-warnings",
        "--tb=short", "--tb=line", "--tb=auto", "--tb=long",
        "--color=no", "--color=yes", "--color=auto",
    }
    seen: set[str] = set()
    for arg in args:
        path = arg.replace("\\", "/")
        if arg in presentation_only:
            continue
        if path not in tests:
            return False
        seen.add(path)
    return seen == tests


ACCEPTANCE_WAIVER = ("приёмка: невозможна", "приемка: невозможна", "acceptance: impossible")
REQUIRE_GREEN_SUBMIT = os.environ.get("DEV_MCP_FLEET_REQUIRE_GREEN_SUBMIT", "1") != "0"


def _unproven_submit_reason(store: Any, tx: Any, worker_id: str, claim: dict[str, Any], note: str) -> str:
    """Руль §4 v2: сдача без зелёного прогона приёмки по ТЕКУЩЕМУ состоянию файлов зоны.

    Ревьюер всё равно обязан прогнать приёмку сам (review.decide); автор, сдавший без прогона
    или поправивший файлы после него, тратит круг ревью на то, что ловится одной командой.
    Выход для приёмки, которую агенту не запустить (железо, владелец), — строка в note:
    «приёмка: невозможна — почему»; ревьюер увидит её и решит сам.
    """
    if not REQUIRE_GREEN_SUBMIT:
        return ""
    worker = tx.one("SELECT kind FROM workers WHERE id=?", (worker_id,))
    if worker is None or str(worker["kind"] or "") not in BUDGETED_KINDS:
        return ""
    task = tx.one("SELECT kind,acceptance FROM tasks WHERE project=? AND tz=?", (claim["project"], claim["tz"]))
    if task is None or str(task["kind"]) not in ("work", "fix"):
        return ""
    acceptance = _json_list(task["acceptance"])
    if not acceptance:
        return ""
    low = str(note or "").casefold()
    if any(mark in low for mark in ACCEPTANCE_WAIVER):
        return ""
    from .review import _missing_runs

    since = str(claim["started_at"])
    missing = _missing_runs(tx, worker_id, since, acceptance)
    if missing:
        return (
            "сдача без зелёной приёмки: после взятия задачи нет успешного прогона "
            + "; ".join(f"`{cmd}`" for cmd in missing)
            + ". Запусти каждую через run и сдай, когда она зелёная. Запустить невозможно (железо, "
            "владелец) — строка в note: «приёмка: невозможна — почему»."
        )
    own = [str(r["path_norm"]) for r in tx.query("SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim["id"],))]
    if own:
        # An isolated run is recorded against the claim worktree (tools.py),
        # not the source trunk. Use that same verified checkout for freshness.
        # The take-event binding is server-owned and immutable; current project
        # flags must not silently change an existing claim's checkout mode.
        take_events = tx.query(
            "SELECT payload FROM events WHERE kind='take' AND worker_id=? "
            "AND project=? AND tz=? ORDER BY id DESC",
            (worker_id, claim["project"], claim["tz"]),
        )
        isolated = False
        for item in take_events:
            try:
                payload = json.loads(str(item["payload"] or "{}"))
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("claim_id") == claim["id"]:
                context = payload.get("claim_context")
                isolated = isinstance(context, dict) and context.get("mode") == "isolated"
                break
        if isolated:
            try:
                from .claim_take_routing import ClaimTakeRouting, project_path_to_claim

                bound = ClaimTakeRouting(store).resolve_claim_context(
                    worker_id, int(claim["id"])
                )
                if bound.mode != "isolated":
                    raise ValueError("claim binding no longer isolated")
                own = [str(project_path_to_claim(p, bound, read_only=True)) for p in own]
            except Exception:
                return (
                    "сдача без подтверждённой приёмки: нельзя проверить состояние "
                    "файлов изолированного claim; повтори проверку после восстановления "
                    "серверного claim binding."
                )
        try:
            from . import evidence

            tree = evidence.tree_hash(own)
        except Exception:
            tree = None
        stale = _missing_runs(tx, worker_id, since, acceptance, tree) if tree else []
        if stale:
            return (
                "файлы задачи менялись после последнего зелёного прогона приёмки — он доказывает уже "
                "не то, что сдаётся. Прогони заново: " + "; ".join(f"`{cmd}`" for cmd in stale)
            )
    return ""


def _empty_submit_reason(store: Any, tx: Any, worker_id: str, claim: dict[str, Any]) -> str:
    """Почему сдачу нельзя принять: вкладка сдаёт задачу, не изменив ни одного её файла.

    Guard ловит случай, когда длинный чат случайно сдаёт предыдущую задачу под новым TZ.
    Но no-op/reconciliation не должен требовать фиктивного редактирования ТЗ: если у задачи
    есть acceptance и этот worker после take успешно прогнал КАЖДУЮ acceptance-команду,
    свежая зелёная проверка сама является достаточным доказательством для независимого review.
    """
    worker = tx.one("SELECT kind FROM workers WHERE id=?", (worker_id,))
    if worker is None or str(worker["kind"] or "") not in BUDGETED_KINDS:
        return ""
    task = tx.one(
        "SELECT kind,acceptance FROM tasks WHERE project=? AND tz=?",
        (claim["project"], claim["tz"]),
    )
    if task is None or str(task["kind"]) not in ("work", "fix"):
        return ""
    # TZ-41 isolated claims edit a private Git worktree, not the source
    # project files in tasks.owns. A server-sealed source tree delta is the
    # authoritative proof of nonempty work in this mode. Never treat an
    # author note, a plain file path or a naked claim_submission as proof.
    seal = tx.one(
        "SELECT payload FROM events WHERE kind='claim_submission' AND worker_id=? "
        "AND project=? AND tz=? AND ts>=? ORDER BY id DESC LIMIT 1",
        (worker_id, claim["project"], claim["tz"], claim["started_at"]),
    )
    delta = tx.one(
        "SELECT payload FROM events WHERE kind='claim_submission_delta' AND worker_id=? "
        "AND project=? AND tz=? AND ts>=? ORDER BY id DESC LIMIT 1",
        (worker_id, claim["project"], claim["tz"], claim["started_at"]),
    )
    if seal is not None and delta is not None:
        try:
            sealed = json.loads(str(seal["payload"]))
            change = json.loads(str(delta["payload"]))
        except (ValueError, TypeError):
            sealed, change = {}, {}
        if (isinstance(sealed, dict) and isinstance(change, dict)
                and sealed.get("claim_id") == claim["id"]
                and change.get("claim_id") == claim["id"]
                and sealed.get("submission_id") == change.get("submission_id")
                and sealed.get("tree_sha") == change.get("tree_sha")
                and change.get("changed") is True):
            return ""
    own = [str(r["path_norm"]) for r in tx.query("SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim["id"],))]
    if not own or not _zone_untouched(store, int(claim["id"]), own):
        return ""
    if _verified_recovery_without_changes(tx, worker_id, claim, own):
        return ""
    if _accepted_external_fix_without_changes(tx, worker_id, claim):
        return ""
    if _vanished_generated_draft_fix(tx, claim, own):
        return ""
    if _corrected_generated_draft_header_fix(store, tx, worker_id, claim, own):
        return ""
    if _review_retested_out_of_scope_fix_without_changes(tx, worker_id, claim, own):
        return ""
    if _accepted_blocked_regression_without_changes(tx, worker_id, claim, own):
        return ""

    acceptance = _json_list(task["acceptance"])
    if acceptance:
        # Reuse the exact same command normalization/evidence rule as independent review,
        # including legacy cmd.exe wrapper == direct project-local batch.
        from .review import _missing_runs
        if not _missing_runs(tx, worker_id, str(claim["started_at"]), acceptance):
            return ""
    written = [
        str(r["path_norm"])
        for r in tx.query(
            "SELECT DISTINCT path_norm FROM events WHERE kind='write' AND worker_id=? AND ts>=? AND path_norm<>''",
            (worker_id, claim["started_at"]),
        )
    ]
    elsewhere = ", ".join(written[:4]) + (" …" if len(written) > 4 else "")
    return (
        f"FLEET_EMPTY_SUBMIT: сдача {claim['tz']} не принята — ни один файл её зоны "
        f"({', '.join(own[:4])}{' …' if len(own) > 4 else ''}) не изменился с момента взятия. "
        + (f"Писал ты в: {elsewhere} — это не файлы {claim['tz']}. " if written else "")
        + f"В этом чате задачи идут подряд: сверься с памяткой, что работаешь именно над {claim['tz']} "
        f"(источник — её ТЗ-файл). Если задача уже выполнена и менять нечего — прогони все её "
        f"acceptance-команды свежим успешным run/run_task и сдай снова; фиктивная правка ТЗ не нужна."
    )


def _triage_submit_reason(tx: Any, worker_id: str, claim: dict[str, Any]) -> str:
    """Итог задачи-суждения — правка файлов зоны или заявка, не прозой в note (v2 §6, 07.10.2026):
    прозу никто не исполнит, а правку ТЗ код перерегистрирует сам (judgment.apply_accepted)."""
    task = tx.one("SELECT kind FROM tasks WHERE project=? AND tz=?", (claim["project"], claim["tz"]))
    if task is None or str(task["kind"]) != "triage":
        return ""
    since = str(claim["started_at"])
    wrote = tx.one(
        "SELECT 1 FROM events WHERE kind='write' AND worker_id=? AND project=? AND tz=? AND ts>=? LIMIT 1",
        (worker_id, claim["project"], claim["tz"], since),
    )
    asked = tx.one(
        "SELECT 1 FROM tasks WHERE kind='request' AND author_worker=? AND created_at>=? LIMIT 1",
        (worker_id, since),
    )
    if wrote is None and asked is None:
        return (
            "Итог задачи-суждения — правка файлов её зоны (ТЗ, файл решения) или заявка request_note; "
            "вывод прозой в note никто не исполнит. Внеси правку и сдай снова."
        )
    return ""


_ACCEPTANCE_SHADOW_RULE = "acceptance_guard:submission"
_ACCEPTANCE_SHADOW_FINGERPRINT = hashlib.sha256(b"TZ-57:sealed-submission-acceptance:v1").hexdigest()


def _sealed_acceptance_observation(store: Any, worker_id: str) -> dict[str, Any] | None:
    """Inspect only a trusted, sealed *isolated* claim, outside the write lock.

    This is deliberately observational. Even a failed verification must not
    veto a submission while TZ-57 is in its mandatory 24-hour shadow period.
    Neither an agent note nor an unsealed checkout can choose the Git base.
    """
    with store.read() as tx:
        claim = _active_claim_for_worker(tx, worker_id)
        if claim is None:
            return None
        kind = tx.scalar("SELECT kind FROM tasks WHERE project=? AND tz=?",
                         (claim["project"], claim["tz"]))
        if kind not in {"work", "fix", "request"}:
            return None
        sealed = tx.one(
            "SELECT 1 FROM events WHERE kind='claim_submission' AND worker_id=? "
            "AND project=? AND tz=? AND ts>=? LIMIT 1",
            (worker_id, claim["project"], claim["tz"], claim["started_at"]),
        )
        if sealed is None:
            return None

    observation: dict[str, Any] = {
        "claim_id": int(claim["id"]), "project": str(claim["project"]),
        "tz": str(claim["tz"]), "submission_id": "", "revision": "",
        "findings": [],
    }
    try:
        from .acceptance_guard import RegisteredProject, check
        from .claim_review_bite import _submission

        submission, context, _ = _submission(store, int(claim["id"]))
        if context.mode != "isolated":
            raise ValueError("sealed submission does not belong to an isolated checkout")
        registered = RegisteredProject.from_store(
            store, str(claim["project"]), str(claim["tz"]), checkout=context.tree_path,
        )
        # Registration stores a source-root path; the isolated checkout must
        # inspect the corresponding *relative* spec, never the live file.
        source_spec = Path(registered.spec_path)
        if source_spec.is_absolute():
            relative_spec = source_spec.resolve(strict=False).relative_to(context.project_root)
            registered = RegisteredProject(
                registered.root, registered.checkout, registered.acceptance,
                registered.owns, relative_spec.as_posix(),
            )
        observation["submission_id"] = submission.submission_id
        observation["revision"] = submission.revision
        observation["findings"] = check(
            registered, str(claim["tz"]), context.base_sha, (),
        )
    except Exception as exc:
        # A broken checker or vanished sealed checkout is itself actionable
        # shadow evidence, not permission to silently certify the candidate.
        observation["findings"] = [
            f"acceptance guard unavailable: {type(exc).__name__}: {str(exc)[:180]}"
        ]
    return observation


def _record_acceptance_shadow(tx: Any, worker_id: str, claim: dict[str, Any],
                              observation: dict[str, Any] | None) -> None:
    """Journal TZ-49 evidence atomically with submission; never enforce here."""
    if observation is None or observation["claim_id"] != claim["id"]:
        return
    from .shadow import ShadowRules

    current = ShadowRules._rules(tx).get(_ACCEPTANCE_SHADOW_RULE)
    if current is None or current.fingerprint != _ACCEPTANCE_SHADOW_FINGERPRINT:
        tx.record_event(kind="shadow_rule_registered", payload={
            "rule_id": _ACCEPTANCE_SHADOW_RULE, "family": "zone",
            "fingerprint": _ACCEPTANCE_SHADOW_FINGERPRINT, "mode": "shadow",
            "minimum_observation_hours": 24,
            "requires_separate_enforcement_decision": True,
        })
    elif current.mode != "shadow":
        # TZ-49 can replay/promote a rule after four hours; TZ-57 has an
        # independent 24-hour minimum AND requires a separate enable decision.
        # Until that integration is implemented, do not leave an accidentally
        # promoted rule in enforce even in the registry.
        tx.record_event(kind="shadow_rule_demoted", payload={
            "rule_id": _ACCEPTANCE_SHADOW_RULE,
            "fingerprint": _ACCEPTANCE_SHADOW_FINGERPRINT,
            "mode": "shadow", "reason": "TZ-57 requires separate enforcement decision",
            "minimum_observation_hours": 24,
        })
    findings = [str(f)[:300] for f in observation["findings"]]
    payload = {
        "rule_id": _ACCEPTANCE_SHADOW_RULE,
        "fingerprint": _ACCEPTANCE_SHADOW_FINGERPRINT,
        "claim_id": int(claim["id"]),
        "submission_id": observation["submission_id"],
        "subject": f"{claim['project']}/{claim['tz']}",
        "revision": observation["revision"],
        "findings": findings[:30], "finding_count": len(findings),
        "mode": "shadow", "minimum_observation_hours": 24,
        "requires_separate_enforcement_decision": True,
    }
    tx.record_event(kind="acceptance_guard_shadow", worker_id=worker_id,
                    project=str(claim["project"]), tz=str(claim["tz"]), payload=payload)
    for finding in findings[:30]:
        tx.record_event(kind="would_block", worker_id=worker_id,
                        project=str(claim["project"]), tz=str(claim["tz"]),
                        payload={**payload, "reason": finding, "code": "acceptance_guard"})


def _legacy_submission_files(tx: Any, claim: dict[str, Any], worker_id: str) -> dict[str, str | None] | None:
    """Capture the candidate's current bytes from the trusted shared checkout.

    Only Git changes inside the server-registered task/fix owns are eligible;
    a write event outside owns does not expand authority to other files;
    the caller records the resulting receipt inside the *same* transaction as
    submit. None means we cannot attest to the candidate, so integration will
    defer rather than interpret current working-tree bytes as accepted bytes.
    """
    from .goals import project_info
    from .integrate import MAX_COMMIT_PATHS, _porcelain_files, _rel_pathspec

    project, tz = str(claim["project"]), str(claim["tz"])
    if project_info(project).get("commit") is not True:
        return None
    taken = tx.one(
        "SELECT payload FROM events WHERE kind='take' AND worker_id=? "
        "AND project=? AND tz=? ORDER BY id DESC LIMIT 1",
        (worker_id, project, tz),
    )
    if taken is None:
        return None
    try:
        provenance = json.loads(str(taken["payload"]))
    except (TypeError, ValueError):
        return None
    if (not isinstance(provenance, dict)
            or provenance.get("claim_id") != claim["id"]):
        return None
    context = provenance.get("claim_context")
    if (context is not None and (not isinstance(context, dict)
                                 or context.get("mode") != "shared")):
        # Isolated claims use claim_accept; neither a naked take event nor a
        # mutable project feature flag may downgrade an isolated submission.
        return None
    row = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (project,))
    if row is None:
        return None
    root = str(row["root"])
    source_root = Path(root)
    try:
        # On Windows a directory junction has is_symlink() == False. Never
        # traverse any reparse-point alias when attesting submitted bytes.
        if source_root.is_symlink() or source_root.is_junction():
            return None
        canonical_root = source_root.resolve(strict=True)
    except (OSError, ValueError):
        return None
    base = task_root(tz)
    tasks = tx.query(
        "SELECT tz,owns FROM tasks WHERE project=? AND (tz=? OR tz LIKE ?)",
        (project, base, f"fix:%{base}%"),
    )
    chain = [str(task["tz"]) for task in tasks if task_root(str(task["tz"])) == base]
    owned: set[str] = set()
    for task in tasks:
        if str(task["tz"]) in chain:
            owned.update(_json_list(task["owns"]))
    # guard.expand_free_path can authorize the task's report/spec or a new
    # artifact after take. Those durable server grants belong to the submission
    # too; a naked write audit entry still grants nothing.
    owned.update(str(path["path_norm"]) for path in tx.query(
        "SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim["id"],)))
    specs = sorted({spec for path in owned
                    if (spec := _rel_pathspec(path, root)) is not None})
    if not specs:
        return {}
    try:
        status = subprocess.run(
            ["git", "-c", "core.quotepath=false", "status", "--porcelain", "-z", "-uall", "--", *specs],
            cwd=root, capture_output=True, timeout=15,
        )
        if status.returncode:
            return None
        files = _porcelain_files(status.stdout.decode("utf-8", errors="surrogateescape"))
        if len(files) > MAX_COMMIT_PATHS:
            return None
        sealed: dict[str, str | None] = {}
        for rel in files:
            relative = Path(rel)
            if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                    or relative.parts[0].casefold() == ".git"):
                return None
            file = source_root / relative
            # Symlinks alone are insufficient: an NTFS junction is not a
            # symlink, yet can map an in-owns path to an external file.
            walk = source_root
            for part in relative.parts:
                walk /= part
                if walk.is_symlink() or walk.is_junction():
                    return None
            try:
                file.resolve(strict=False).relative_to(canonical_root)
            except ValueError:
                return None
            if file.is_file():
                digest = hashlib.sha256()
                with file.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                sealed[rel] = digest.hexdigest()
            elif not file.exists():
                sealed[rel] = None
            else:
                return None
        return sealed
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return None


def submit(store: Any, worker_id: str, note: str, *,
           claim_id: int | None = None, action_receipt: dict[str, str] | None = None) -> None:
    _touch(store, worker_id)
    from .queue_intake import submission_refusal
    source_refusal = submission_refusal(store, worker_id)
    if source_refusal:
        raise ValueError(source_refusal)
    acceptance_shadow = _sealed_acceptance_observation(store, worker_id)
    with store.tx() as tx:
        claim = _active_claim_for_worker(tx, worker_id)
        _require_addressed_claim(claim, claim_id)
        if claim is None:
            raise ValueError(f"{worker_id}: нет активной задачи")
        task_kind = tx.one("SELECT kind FROM tasks WHERE project=? AND tz=?",
                           (claim["project"], claim["tz"]))
        if task_kind is not None and task_kind["kind"] == "cross_review":
            raise ValueError("cross_review: передай типизированный verdict через "
                             "fleet_send(kind='cross_review_verdict'), не fleet_submit")
        refusal = (
            _empty_submit_reason(store, tx, worker_id, claim)
            or _unproven_submit_reason(store, tx, worker_id, claim, note)
            or _triage_submit_reason(tx, worker_id, claim)
        )
        if refusal:
            raise ValueError(refusal)
        # Sealed observations run outside the database write lock. Recheck
        # the claim and sealed identity before committing their audit events.
        if acceptance_shadow is not None:
            latest_seal = tx.one(
                "SELECT payload FROM events WHERE kind='claim_submission' "
                "AND worker_id=? AND project=? AND tz=? ORDER BY id DESC LIMIT 1",
                (worker_id, claim["project"], claim["tz"]),
            )
            try:
                latest = json.loads(str(latest_seal["payload"])) if latest_seal else {}
            except (ValueError, TypeError):
                latest = {}
            if (acceptance_shadow["claim_id"] == claim["id"]
                    and (not acceptance_shadow["submission_id"] or
                         latest.get("submission_id") == acceptance_shadow["submission_id"])):
                _record_acceptance_shadow(tx, worker_id, claim, acceptance_shadow)
        now = _now()
        # Сдача сама освобождает claim и ресурсы. Раньше это делал только отдельный
        # fleet_finish: забывший его агент держал owns и эксклюзивный ресурс до ревью, а
        # его следующий fleet_take отвечал «очередь пуста» (активная задача уже есть).
        tx.changes(
            [
                (
                    "UPDATE tasks SET state='submitted',author_worker=?,updated_at=? "
                    "WHERE project=? AND tz=?",
                    (worker_id, now, claim["project"], claim["tz"]),
                ),
                (
                    "UPDATE claims SET state='released',released_at=? WHERE id=?",
                    (now, claim["id"]),
                ),
            ],
            event=_event(
                "submit",
                worker_id=worker_id,
                project=str(claim["project"]),
                tz=str(claim["tz"]),
                note=note,
            ),
        )
        from .actions import record_receipt
        record_receipt(tx, worker_id, int(claim["id"]), action_receipt)
        # The bytes seal is server-owned, not synthesized by the reviewer or
        # author. Keep its link to the *exact* submit event in this transaction
        # so a failed submission cannot leave a usable orphan receipt.
        sealed_files = _legacy_submission_files(tx, claim, worker_id)
        if sealed_files is not None:
            submitted = tx.one(
                "SELECT id FROM events WHERE project=? AND tz=? AND kind='submit' "
                "AND worker_id=? ORDER BY id DESC LIMIT 1",
                (claim["project"], claim["tz"], worker_id),
            )
            if submitted is not None:
                tx.record_event(
                    kind="submission_file_seal", tool="fleet",
                    project=str(claim["project"]), tz=str(claim["tz"]),
                    payload={"submit_id": int(submitted["id"]), "files": sealed_files},
                )
        # TZ-20: durable outbox, inside the submit transaction.  Gemini network IO
        # must run outside this lock; a controller can replay this request safely.
        # A model verdict never replaces the independent fleet review above.
        task = tx.one("SELECT kind FROM tasks WHERE project=? AND tz=?",
                      (claim["project"], claim["tz"]))
        worker = tx.one("SELECT kind FROM workers WHERE id=?", (worker_id,))
        author_model = str(worker["kind"]) if worker is not None else ""
        if (task is not None and str(task["kind"]) in {"work", "fix", "request"}
                and author_model in {"chatgpt", "gemini"}):
            from . import evidence

            paths = [str(r["path_norm"]) for r in tx.query(
                "SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim["id"],))]
            tx.record_event(kind="cross_review_requested", worker_id=worker_id,
                            project=str(claim["project"]), tz=str(claim["tz"]),
                            payload={"claim_id": int(claim["id"]),
                                     "tree_sha": evidence.tree_hash(paths),
                                     "author_model": author_model,
                                     "outcome_sha": hashlib.sha256(
                                         note.encode("utf-8")).hexdigest()})


def _is_imported_claim(tx: Any, row: dict[str, Any]) -> bool:
    event = tx.one(
        "SELECT payload FROM events "
        "WHERE kind='take' AND worker_id=? AND project=? AND tz=? "
        "ORDER BY id DESC LIMIT 1",
        (row["worker_id"], row["project"], row["tz"]),
    )
    if event is None:
        return False
    try:
        payload = json.loads(str(event["payload"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("imported") is True


def _parse_time(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def requeue_stale(store: Any, *, silence_min: int = 30) -> list[Task]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=silence_min)
    returned: list[Task] = []
    with store.tx() as tx:
        rows = _rows(
            tx.query(
                "SELECT c.id AS claim_id,c.started_at,c.worker_id,t.*,w.last_seen AS worker_last_seen "
                "FROM claims c "
                "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                "JOIN workers w ON w.id=c.worker_id "
                "WHERE c.state='active' AND t.state='taken' "
                "ORDER BY c.id"
            )
        )
        for row in rows:
            if _is_imported_claim(tx, row):
                continue
            try:
                last_seen = _parse_time(str(row["worker_last_seen"]))
            except (TypeError, ValueError):
                continue
            if last_seen > cutoff:
                continue

            wrote = tx.one(
                "SELECT 1 AS hit FROM events "
                "WHERE project=? AND tz=? AND kind='write' AND ts>=? LIMIT 1",
                (row["project"], row["tz"], row["started_at"]),
            )
            if wrote is not None:
                continue

            accepted = tx.one(
                "SELECT 1 AS hit FROM events "
                "WHERE project=? AND tz=? AND kind='accept' AND ts>=? LIMIT 1",
                (row["project"], row["tz"], row["started_at"]),
            )
            if accepted is not None:
                continue

            now = _now()
            tx.changes(
                [
                    (
                        "UPDATE claims SET state='requeued',released_at=? WHERE id=?",
                        (now, row["claim_id"]),
                    ),
                    (
                        "UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=?",
                        (now, row["project"], row["tz"]),
                    ),
                ],
                event=_event(
                    "requeue",
                    worker_id=str(row["worker_id"]),
                    project=str(row["project"]),
                    tz=str(row["tz"]),
                    silence_min=silence_min,
                ),
            )
            returned.append(_task(row, state="ready"))
    return returned


def recover_stale_with_work(store: Any, *, silence_min: int = 180) -> list[Task]:
    """Recover abandoned claims that already changed files.

    Unlike the short no-write reclaim, this path waits for a much longer silence,
    revokes the vanished worker so it cannot resume writing, preserves the shared
    worktree as-is, and requeues the exact task for continuation.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=silence_min)
    returned: list[Task] = []
    with store.tx() as tx:
        rows = _rows(
            tx.query(
                "SELECT c.id AS claim_id,c.started_at,c.worker_id,t.*,w.last_seen AS worker_last_seen "
                "FROM claims c "
                "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                "JOIN workers w ON w.id=c.worker_id "
                "WHERE c.state='active' AND t.state='taken' "
                "ORDER BY c.id"
            )
        )
        for row in rows:
            if _is_imported_claim(tx, row):
                continue
            try:
                last_seen = _parse_time(str(row["worker_last_seen"]))
            except (TypeError, ValueError):
                continue
            if last_seen > cutoff:
                continue

            wrote = tx.one(
                "SELECT 1 AS hit FROM events "
                "WHERE project=? AND tz=? AND kind='write' AND ts>=? LIMIT 1",
                (row["project"], row["tz"], row["started_at"]),
            )
            if wrote is None:
                continue

            accepted = tx.one(
                "SELECT 1 AS hit FROM events "
                "WHERE project=? AND tz=? AND kind='accept' AND ts>=? LIMIT 1",
                (row["project"], row["tz"], row["started_at"]),
            )
            if accepted is not None:
                continue

            now = _now()
            reason = (
                f"automatic recovery {row['project']}/{row['tz']}: "
                f"worker silent >= {silence_min} min; preserved worktree"
            )
            tx.changes(
                [
                    (
                        "UPDATE claims SET state='requeued',released_at=? WHERE id=?",
                        (now, row["claim_id"]),
                    ),
                    (
                        "UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=?",
                        (now, row["project"], row["tz"]),
                    ),
                    (
                        "UPDATE workers SET revoked_at=COALESCE(revoked_at,?),revoke_reason=? WHERE id=?",
                        (now, reason, row["worker_id"]),
                    ),
                ],
                event=_event(
                    "requeue",
                    worker_id=str(row["worker_id"]),
                    project=str(row["project"]),
                    tz=str(row["tz"]),
                    silence_min=silence_min,
                    automatic_recovery=True,
                    preserved_work=True,
                ),
            )
            returned.append(_task(row, state="ready"))
    return returned


# ---- оттаивание заблокированных задач ------------------------------------------------
#
# fleet_block и автоматическая заморозка (два возврата без сдачи подряд) снимают задачу с
# раздачи, но до 25.09.2026 вернуть её мог только владелец командой reactivate — а он этого
# не делает. Задача оттаивает сама:
# - сразу, как только приняты все задачи, названные в причине блокировки (TZ-120,
#   fix:TZ-06, request:7c9fbcdf …) — это и есть «дождаться чужой правки»;
# - если названная dependency ещё не accepted — ждём её, а не будим задачу по таймеру;
# - явно внешний blocker (provider secret/spend, participant consent, физический доступ)
#   не переиздаём вслепую: новый агент не может изменить внешний факт и только жжёт чат;
# - только неясный/временный blocker без dependency перепроверяется по таймеру с backoff.
# Заморозка владельцем из локального fleet.cmd freeze не оттаивает никогда: это аварийный
# human-control; web owner-agent permanent freeze не получает.

THAW_AFTER_MIN = 20
MAX_AUTO_THAWS = 3  # число быстрых циклов до exponential backoff, НЕ terminal ceiling
LEGACY_FROZEN_FIX_RECHECK_MIN = 60
_TASK_REF = re.compile(r"\b(?:(?:review|fix|request):)*(?:TZ-\d+[a-z]?|[0-9a-f]{8})(?::\d+)?\b", re.I)
_DURABLE_EXTERNAL_BLOCK = re.compile(
    r"(?i)(provider[- ]?auth|openrouter|api[- ]?key|secret|credential|"
    r"participant\s+consent|human\s+attestation|physical\s+(?:access|action)|"
    r"usb\b|ram\s+window|memory\s+window|hyper-v|guest\s+access|"
    r"секрет|ключ(?:а|ом)?\b|расход(?:а|ом)?\b|оплат|согласи|участник|"
    r"физическ|ручн(?:ое|ого|ая)\s+(?:действие|подтверждение|согласие))"
)


def _task_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in _TASK_REF.finditer(text or ""):
        ref = match.group(0)
        if re.fullmatch(r"[0-9a-f]{8}", ref, re.I):
            ref = f"request:{ref}"  # голый id заявки — это задача request:<id>
        if ref not in refs:
            refs.append(ref)
    return refs


def self_acceptance_until(reason: str, acceptance: Any) -> bool:
    """A probe of one's own acceptance cannot independently make progress."""
    raw = probe.from_reason(reason)
    if not raw:
        # Legacy block notes may be just "blocked: until: <command>". The
        # generic probe grammar requires punctuation before an inline until,
        # but this common exact-prefix spelling must still fail closed.
        direct = re.match(r"(?is)^\s*(?:blocked\s*:\s*)?until\s*:\s*(.+?)\s*$",
                          str(reason or ""))
        if direct is not None:
            raw = re.split(r"\s+[—–]\s+", direct.group(1), maxsplit=1)[0].strip()
    if not raw:
        return False
    def normalized(command: str) -> tuple[str, ...]:
        try:
            parts = shlex.split(command, posix=True)
        except ValueError:
            return ()
        return tuple(p.strip().replace("\\", "/").casefold() for p in parts)
    target = normalized(raw)
    return bool(target) and any(target == normalized(cmd) for cmd in _json_list(acceptance))


def _external_data_admission(tx: Any, project: str, tz: str, subject: str,
                             after_id: int, consumed: set[str]) -> str:
    """A new *admitted* data batch, not a command or task id found in prose.

    The upstream observer must record external_data_ready against the blocked
    task with an immutable SHA-256, UTC observation window, and an independently
    accepted admission event bound to the same digest. No producer/receipt
    means no automatic second triage.
    """
    for row in tx.query(
        "SELECT id,payload FROM events WHERE project=? AND tz=? "
        "AND kind='external_data_ready' AND id>? ORDER BY id DESC",
        (project, tz, after_id),
    ):
        try:
            data = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("subject") != subject:
            continue
        if data.get("change_source") != "external_data_ready":
            continue
        digest = data.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        if digest in consumed:
            continue
        try:
            begin = datetime.fromisoformat(str(data["window_start_utc"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(data["window_end_utc"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        utc = timezone.utc
        if (begin.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0)
                or begin >= end or end > datetime.now(utc)):
            continue
        admission = data.get("admission")
        if not isinstance(admission, dict) or admission.get("sha256") != digest:
            continue
        source_tz = admission.get("tz")
        source_event_id = admission.get("accept_event_id")
        if (not isinstance(source_tz, str) or not source_tz or source_tz == tz
                or type(source_event_id) is not int or source_event_id <= after_id
                or source_event_id >= int(row["id"])):
            continue
        proof = tx.one(
            "SELECT e.payload FROM events e JOIN tasks t "
            "ON t.project=e.project AND t.tz=e.tz "
            "WHERE e.id=? AND e.project=? AND e.tz=? AND e.kind='accept' "
            "AND t.state='accepted'",
            (source_event_id, project, source_tz),
        )
        if proof is None:
            continue
        try:
            accepted = json.loads(proof["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(accepted, dict) and accepted.get("evidence_sha256") == digest:
            return digest
    return ""


def _consumed_external_data(tx: Any, project: str, tz: str, subject: str) -> set[str]:
    """A batch already used for either thaw or triage cannot trigger another."""
    seen: set[str] = set()
    for row in tx.query(
        "SELECT kind,payload FROM events WHERE project=? AND "
        "((tz=? AND kind='thaw') OR kind='triage_open')",
        (project, tz),
    ):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if (row["kind"] == "triage_open" and payload.get("subject") != subject):
            continue
        digest = payload.get("external_digest")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            seen.add(digest)
    return seen


def _self_acceptance_triage(tx: Any, project: str, tz: str, reason: str) -> None:
    """Route engineering unknowns to a bounded decision, not a forever-failing probe."""
    from . import judgment

    subject = f"self-acceptance:{tz}"
    row = tx.one(
        "SELECT 1 FROM tasks WHERE project=? AND kind='triage' AND state IN "
        "('draft','ready','taken','submitted','frozen') AND body LIKE ? LIMIT 1",
        (project, f"{judgment.MARK} subject={subject}%"),
    )
    if row is not None:
        return
    prior = []
    for historic in tx.query(
        "SELECT e.id,e.tz,e.payload,t.state FROM events e JOIN tasks t "
        "ON t.project=e.project AND t.tz=e.tz "
        "WHERE e.project=? AND e.kind='triage_open' ORDER BY e.id DESC",
        (project,),
    ):
        try:
            payload = json.loads(historic["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("subject") == subject:
            prior.append((historic, payload))
    # A decision on the dedicated self-acceptance document is durable even if
    # a later failed/rejected triage changed the most recent task state.
    # Bind it by the structured proposal.file, never by task ids in prose.
    park_id = 0
    expected = f"self-acceptance-{judgment._safe_name(tz)}.md".casefold()
    proposal = judgment._open_proposal(tx, project, tz)
    if proposal is not None:
        decision = proposal["payload"]
        if (decision.get("action") == "park"
                and Path(str(decision.get("file") or "")).name.casefold() == expected):
            park_id = int(proposal["id"])
    digest = ""
    if prior or park_id:
        last, _ = prior[0] if prior else (None, {})
        # Rejected triage is not permission to repeat the same question. Any
        # historical triage (including rejected) consumes the original reason;
        # a fresh independently admitted batch is required for another one.
        decided = tx.one(
            "SELECT id FROM events WHERE project=? AND tz=? AND kind='accept' "
            "ORDER BY id DESC LIMIT 1", (project, str(last["tz"])),
        ) if last is not None and str(last["state"]) == "accepted" else None
        since = max(park_id, int(last["id"]) if last else 0,
                    int(decided["id"]) if decided else 0)
        consumed = _consumed_external_data(tx, project, tz, subject)
        digest = _external_data_admission(tx, project, tz, subject, since, consumed)
        if not digest:
            return
    project_row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
    if project_row is None:
        return
    rel = f"ToDo/decisions/self-acceptance-{judgment._safe_name(tz)}.md"
    body = (
        f"Задача {tz} заморожена: until повторяет собственную acceptance-команду, "
        "которая станет зелёной только после выполнения работы. "
        f"Причина: {reason[:350]}\n"
        f"Опиши в `{rel}` выполнимую процедуру, недостающую зависимость или "
        "конкретное решение; нельзя оставлять собственную приёмку внешним ожиданием.\n"
        f"tz: {tz}\naction: park\nwhy: как выполнить проверку и кто отвечает."
    )
    judgment._insert(tx, project, subject, f"Как выполнить приёмку {tz}", body,
                     [judgment._abs(str(project_row["root"]), rel)], 1,
                     extra={"external_digest": digest} if digest else None)


def close_obsolete_requests(store: Any) -> list[str]:
    """Закрыть generated request, если её source уже принят другим путём.

    Старый request-контур мог оставить ready/frozen dependency даже после того, как
    исходная задача или fix уже завершились. Такая заявка больше не несёт работы, но
    может занимать очередь и конфликтовать owns со всем проектом.
    """
    closed: list[str] = []
    now = _now()
    with store.tx() as tx:
        rows = _rows(tx.query(
            "SELECT project,tz,state FROM tasks "
            "WHERE kind='request' AND state IN ('draft','ready','frozen') ORDER BY project,tz"
        ))
        for row in rows:
            project, tz = str(row["project"]), str(row["tz"])
            created = tx.one(
                "SELECT payload FROM events WHERE project=? AND tz=? AND kind='request_draft' "
                "ORDER BY id ASC LIMIT 1",
                (project, tz),
            )
            if created is None:
                # До request_draft-протокола импорт создавал frozen REQ-XXXXXXXXXXXX
                # без source marker. Если на такой request больше НИКТО не ссылается,
                # это миграционный мусор, а не зависимость. Новые request:<id> сюда
                # намеренно не попадают.
                if str(row["state"]) != "frozen" or not tz.upper().startswith("REQ-"):
                    continue
                structured_ref = tx.one(
                    "SELECT 1 AS hit FROM tasks t, json_each(t.needs) n "
                    "WHERE t.project=? AND CAST(n.value AS TEXT)=? LIMIT 1",
                    (project, tz),
                )
                implicit_ref = False
                if structured_ref is None:
                    for event_row in tx.query(
                        "SELECT payload FROM events WHERE project=? AND kind='block' ORDER BY id DESC",
                        (project,),
                    ):
                        try:
                            reason = str(json.loads(str(event_row["payload"] or "{}")).get("reason") or "")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if re.search(
                            rf"(?i)(?<![A-Za-z0-9]){re.escape(tz)}(?![A-Za-z0-9])",
                            reason,
                        ):
                            implicit_ref = True
                            break
                if structured_ref is not None or implicit_ref:
                    continue
                tx.change(
                    "UPDATE tasks SET state='rejected',updated_at=? "
                    "WHERE project=? AND tz=? AND kind='request' AND state='frozen'",
                    (now, project, tz),
                    event=_event(
                        "auto_obsolete",
                        worker_id=None,
                        project=project,
                        tz=tz,
                        reason="legacy frozen REQ без source marker и без потребителей; ручной gate удалён",
                        previous_state="frozen",
                        legacy_orphan=True,
                    ),
                )
                closed.append(f"{project}/{tz}")
                continue
            try:
                payload = json.loads(str(created["payload"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            source_tz = str(payload.get("source_tz") or "").strip()
            if not source_tz:
                continue
            source = tx.one(
                "SELECT state FROM tasks WHERE project=? AND tz=?",
                (project, source_tz),
            )
            if source is None or str(source["state"]) != "accepted":
                continue
            tx.change(
                "UPDATE tasks SET state='accepted',updated_at=? "
                "WHERE project=? AND tz=? AND kind='request' AND state IN ('draft','ready','frozen')",
                (now, project, tz),
                event=_event(
                    "auto_obsolete",
                    worker_id=None,
                    project=project,
                    tz=tz,
                    reason=f"source {source_tz} уже accepted; отдельная request больше не нужна",
                    source_tz=source_tz,
                    previous_state=str(row["state"]),
                ),
            )
            closed.append(f"{project}/{tz}")
    return closed


def promote_legacy_drafts(store: Any) -> list[str]:
    """Убрать старый ручной gate: legacy request/review/fix draft -> runnable автоматически.

    Новые request создаются сразу ready, escalate открывает ready final-review, а fix-круг
    больше не ждёт owner approve. В реестре остались draft старого протокола; request/fix
    можно выпускать сразу. Review выпускаем только пока исходная задача всё ещё submitted.
    """
    promoted: list[str] = []
    now = _now()
    with store.tx() as tx:
        for row in _rows(tx.query(
            "SELECT project,tz,kind,body,owns FROM tasks WHERE state='draft' AND kind IN ('request','review','fix') "
            "ORDER BY project,tz"
        )):
            project, tz, kind = str(row["project"]), str(row["tz"]), str(row["kind"])
            if kind == "request" and not _json_list(row.get("owns")):
                tx.change(
                    "UPDATE tasks SET state='rejected',updated_at=? WHERE project=? AND tz=? AND state='draft'",
                    (now, project, tz),
                    event=_event(
                        "auto_reject",
                        worker_id=None,
                        project=project,
                        tz=tz,
                        reason="legacy request без owns: выполнить безопасно нечего; ручной owner gate удалён",
                        previous_state="draft",
                    ),
                )
                continue
            if kind == "review":
                try:
                    first = str(row.get("body") or "").splitlines()[0]
                    marker = json.loads(first[len("[fleet-review] "):]) if first.startswith("[fleet-review] ") else {}
                    source_project = str(marker.get("project") or project)
                    source_tz = str(marker.get("tz") or "")
                except (ValueError, TypeError, json.JSONDecodeError):
                    source_project, source_tz = project, ""
                source = tx.one(
                    "SELECT state FROM tasks WHERE project=? AND tz=?",
                    (source_project, source_tz),
                ) if source_tz else None
                if source is None or str(source["state"]) != "submitted":
                    continue
            tx.change(
                "UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=? AND state='draft'",
                (now, project, tz),
                event=_event(
                    "auto_promote",
                    worker_id=None,
                    project=project,
                    tz=tz,
                    reason="legacy manual draft gate removed",
                    previous_state="draft",
                ),
            )
            promoted.append(f"{project}/{tz}")
    return promoted


def reactivate_stale_generated_fixes(
    store: Any, *, after_min: int = LEGACY_FROZEN_FIX_RECHECK_MIN,
) -> list[str]:
    """Вернуть старые generated fix из owner/legacy freeze, если root всё ещё submitted.

    Ручной freeze когда-то использовался как постоянный gate. Для generated fix это
    превращает исходную submitted-задачу в вечный тупик. Свежий fleet_block сюда не
    попадает: его последний event=block и им занимается thaw_frozen.
    """
    now_dt = datetime.now(timezone.utc)
    reactivated: list[str] = []
    with store.tx() as tx:
        rows = _rows(tx.query(
            "SELECT project,tz FROM tasks WHERE state='frozen' AND kind='fix' ORDER BY project,tz"
        ))
        for row in rows:
            project, tz = str(row["project"]), str(row["tz"])
            root_tz = family_root(tz)
            root = tx.one(
                "SELECT state FROM tasks WHERE project=? AND tz=?",
                (project, root_tz),
            )
            if root is None or str(root["state"]) != "submitted":
                continue
            last = tx.one(
                "SELECT id,ts,kind,payload FROM events WHERE project=? AND tz=? "
                "AND kind IN ('block','freeze','thaw','reactivate','approve','auto_reactivate') "
                "ORDER BY id DESC LIMIT 1",
                (project, tz),
            )
            if last is None or str(last["kind"]) != "freeze":
                continue
            # A typed owner/guard/security freeze has no automatic authority.
            # The legacy generated-fix repair path may only consume old
            # unclassified freezes, never a durable protected decision.
            try:
                freeze_payload = json.loads(last["payload"] or "{}")
            except (ValueError, TypeError):
                freeze_payload = {}
            if (isinstance(freeze_payload, dict)
                    and (freeze_payload.get("origin") in {"owner", "guard", "manual"}
                         or freeze_payload.get("gate") in {"security", "manual"})):
                continue
            try:
                age_min = (now_dt - _parse_time(str(last["ts"]))).total_seconds() / 60
            except ValueError:
                continue
            if age_min < after_min:
                continue
            now = _now()
            tx.change(
                "UPDATE tasks SET state='ready',updated_at=? "
                "WHERE project=? AND tz=? AND state='frozen' AND kind='fix'",
                (now, project, tz),
                event=_event(
                    "auto_reactivate",
                    worker_id=None,
                    project=project,
                    tz=tz,
                    reason=f"generated fix root {root_tz} всё ещё submitted; legacy freeze старше {after_min} мин",
                    previous_state="frozen",
                    root_tz=root_tz,
                ),
            )
            reactivated.append(f"{project}/{tz}")
    return reactivated


def _verified_reactivation(tx: Any, project: str, tz: str, blocked: Any,
                           binding: dict[str, Any], now_dt: datetime) -> tuple[Any, Any] | None:
    """Read-only trusted adapter for TZ-69; no state mutation or external I/O.

    The predicate exists in a server-authored block or a server-validated
    block_probe receipt. An accept/probe_run is evidence only when its own
    durable event is newer than this exact blocking transition.
    """
    from .reactivate_evidence import (
        BlockingEvent, DecisionStatus, ResolutionPredicate, TaskSnapshot,
        VerifiedEvidence, evaluate_reactivation,
    )
    if (binding.get("origin") != "fleet_auto"
            or binding.get("gate") not in {"operational", "dependency", "repair"}):
        return None
    policy = binding.get("reactivate_policy")
    if not isinstance(policy, dict):
        return None
    blocked_id = int(blocked["id"])
    latest = tx.one(
        "SELECT id FROM events WHERE project=? AND tz=? AND "
        "kind IN ('block','freeze','thaw','reactivate','approve','auto_reactivate') "
        "ORDER BY id DESC LIMIT 1", (project, tz),
    )
    task = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
    if (latest is None or int(latest["id"]) != blocked_id
            or task is None or task["state"] != "frozen"):
        return None
    predicate = None
    evidence = None
    kind = policy.get("kind")
    if kind == "registered_probe" and binding["gate"] == "operational":
        registered = tx.one(
            "SELECT id,payload FROM events WHERE project=? AND tz=? "
            "AND kind='block_probe' AND id>? ORDER BY id DESC LIMIT 1",
            (project, tz, blocked_id),
        )
        if registered is not None:
            try:
                argv = json.loads(registered["payload"] or "{}").get("argv")
            except (ValueError, TypeError, AttributeError):
                argv = None
            if (isinstance(argv, list) and argv
                    and all(isinstance(x, str) and x for x in argv)):
                token = "probe:" + hashlib.sha256(
                    json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
                    .encode("utf-8"),
                ).hexdigest()
                predicate = ResolutionPredicate(
                    blocked_id, "until_probe", token, "probe_registry")
                passed = tx.one(
                    "SELECT id,ts,payload FROM events WHERE project=? AND tz=? "
                    "AND kind='probe_run' AND id>? ORDER BY id DESC LIMIT 1",
                    (project, tz, int(registered["id"])),
                )
                if passed is not None:
                    try:
                        result = json.loads(passed["payload"] or "{}")
                    except (ValueError, TypeError):
                        result = {}
                    if isinstance(result, dict) and type(result.get("rc")) is int:
                        evidence = VerifiedEvidence(
                            blocked_id, "probe_pass", token,
                            f"fleet-event:{passed['id']}", "fleet_probe",
                            result["rc"] == 0, int(passed["id"]),
                            str(passed["ts"]), str(passed["ts"]))
    elif kind == "accepted_work" and binding["gate"] in {"dependency", "repair"}:
        target = policy.get("target")
        if (policy.get("authority") == "fleet_task_events"
                and isinstance(target, str) and target.startswith(project + "/")):
            depends_on = target[len(project) + 1:]
            if depends_on and depends_on != tz:
                # The task's current needs must still contain the exact
                # predicate named by the original trusted block event.
                # A repair gate (review.py: project system-blocker) names the
                # request opened for it; that request stands in for needs.
                registered = tx.one(
                    "SELECT needs FROM tasks WHERE project=? AND tz=?", (project, tz))
                if registered and (
                        depends_on in _json_list(registered["needs"])
                        if binding["gate"] == "dependency"
                        else depends_on.startswith("request:")
                        and binding.get("request") == depends_on):
                    predicate = ResolutionPredicate(
                        blocked_id, "accepted_work", target, "fleet_task_events")
                    accepted = tx.one(
                        "SELECT e.id,e.ts,e.worker_id,t.author_worker,t.state "
                        "FROM events e JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
                        "WHERE e.project=? AND e.tz=? AND e.kind='accept' AND e.id>? "
                        "ORDER BY e.id DESC LIMIT 1",
                        (project, depends_on, blocked_id),
                    )
                    if (accepted is not None and accepted["state"] == "accepted"
                            and accepted["worker_id"] and accepted["author_worker"]
                            and accepted["worker_id"] != accepted["author_worker"]):
                        evidence = VerifiedEvidence(
                            blocked_id, "work_accepted", target,
                            f"fleet-event:{accepted['id']}", "fleet_acceptance",
                            True, int(accepted["id"]), str(accepted["ts"]),
                            now_dt.isoformat())
    elif (kind == "world_resource_up" and binding["gate"] == "operational"
          and policy.get("authority") == "world_sensor"):
        target = policy.get("target")
        if isinstance(target, str) and world.config().get(target, {}).get("sensor"):
            predicate = ResolutionPredicate(
                blocked_id, kind, target, "world_sensor")
            # The newest sensor transition after the block decides: up then down
            # again is still down.
            sensed = tx.one(
                "SELECT id,ts,payload FROM events WHERE kind=? AND path_norm=? AND id>? "
                "ORDER BY id DESC LIMIT 1",
                (world.EVENT_KIND, world.path_key(target), blocked_id))
            if sensed is not None:
                try:
                    state = json.loads(sensed["payload"] or "{}").get("state")
                except (ValueError, TypeError, AttributeError):
                    state = None
                evidence = VerifiedEvidence(
                    blocked_id, "resource_up", target, f"fleet-event:{sensed['id']}",
                    "fleet_world", state == world.UP, int(sensed["id"]),
                    str(sensed["ts"]), now_dt.isoformat())
    elif (kind in {"platform_deploy_commit", "platform_deploy_after"}
          and binding["gate"] == "operational"
          and policy.get("authority") == "platform_guard"):
        target = policy.get("target")
        if isinstance(target, str):
            predicate = ResolutionPredicate(
                blocked_id, kind, target, "platform_guard")
            # The newest guard outcome is authoritative; a newer rollback or
            # a deployment of a different commit revokes older known-good proof.
            recent = tx.one(
                "SELECT id,kind,payload FROM events WHERE "
                "kind IN ('platform_deployed','platform_rolled_back') "
                "ORDER BY id DESC LIMIT 1")
            if recent is not None and recent["kind"] == "platform_deployed":
                try:
                    data = json.loads(recent["payload"] or "{}")
                except (ValueError, TypeError):
                    data = {}
                candidate = data.get("candidate") if isinstance(data, dict) else None
                if (isinstance(candidate, str)
                        and re.fullmatch("[a-f0-9]{40}", candidate)
                        and data.get("known_good") == candidate):
                    evidence = VerifiedEvidence(
                        blocked_id, "platform_deployed", candidate,
                        f"fleet-event:{recent['id']}", "platform_guard",
                        True, int(recent["id"]), str(data.get("ts") or ""),
                        now_dt.isoformat())
    if predicate is None:
        return None
    verdict = evaluate_reactivation(
        TaskSnapshot(project, tz, str(task["state"]), blocked_id),
        BlockingEvent(blocked_id, project, tz, str(blocked["kind"]),
                      binding["origin"], binding["gate"], str(blocked["ts"])),
        predicate, evidence, now=now_dt,
    )
    if verdict.status is DecisionStatus.ELIGIBLE:
        return predicate, verdict
    return None


def thaw_frozen(store: Any, *, after_min: int = THAW_AFTER_MIN, max_thaws: int = MAX_AUTO_THAWS) -> list[str]:
    """Вернуть в ready заблокированные агентами задачи, препятствие которых могло уйти."""
    # A direct caller of thaw_frozen (without tools._fleet_maintenance) must
    # still consume any durable deployment receipt. tick() is cursor-based and
    # idempotent when the maintenance loop has already run it. Import lazily
    # to keep other thaw categories usable while the platform lane is upgrading.
    try:
        from . import deploy_events
    except ImportError:
        pass
    else:
        deploy_events.tick(store)
    now_dt = datetime.now(timezone.utc)
    thawed: list[str] = []
    world_cfg = world.config()
    with store.tx() as tx:
        # judgment.apply_accepted in older fleets can requeue a watchdog
        # decision after an apply error. Its review accept remains immutable:
        # restore the terminal state before any periodic thaw/retry selection.
        terminal = _rows(tx.query(
            "SELECT t.project,t.tz FROM tasks t WHERE t.kind='triage' "
            "AND t.tz LIKE 'watch:%' AND t.state IN ('ready','frozen') "
            "AND EXISTS (SELECT 1 FROM events e WHERE e.project=t.project "
            "AND e.tz=t.tz AND e.kind='accept')"
        ))
        for row in terminal:
            tx.change(
                "UPDATE tasks SET state='accepted',updated_at=? "
                "WHERE project=? AND tz=? AND state IN ('ready','frozen')",
                (_now(), row["project"], row["tz"]),
                event=_event("watch_terminal_restore", worker_id=None,
                             project=str(row["project"]), tz=str(row["tz"]),
                             reason="historical accept; timer reissue suppressed"),
            )
        frozen = _rows(tx.query(
            "SELECT project,tz,kind,resources,needs,acceptance FROM tasks "
            "WHERE state='frozen' ORDER BY project,tz"
        ))
        for task in frozen:
            project, tz = str(task["project"]), str(task["tz"])
            last = tx.one(
                "SELECT id,ts,kind,payload FROM events WHERE project=? AND tz=? "
                "AND kind IN ('block','freeze','thaw','reactivate','approve','auto_reactivate') "
                "ORDER BY id DESC LIMIT 1",
                (project, tz),
            )
            # freeze is an explicit manual/guard decision (and also the newer
            # self-acceptance blocker kind). An external receipt is NEVER
            # authority to override that freeze. Only legacy block events
            # may use the independent-data thaw path below.
            if last is None or str(last["kind"]) != "block":
                continue
            try:
                payload = json.loads(str(last["payload"] or "{}"))
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            reason = str(payload.get("reason") or "")
            if (payload.get("origin") in {"owner", "guard", "manual"}
                    or payload.get("gate") in {"security", "manual"}):
                continue
            # New structured blocks are never thawed by a textual reason,
            # age-based retry, or an unverified external signal.
            if "reactivate_policy" in payload:
                verdict = _verified_reactivation(tx, project, tz, last, payload, now_dt)
                if verdict is not None:
                    predicate, evaluated = verdict
                    # Re-evaluate the exact predicate and receipt at commit
                    # time as well; an old proof cannot be replayed after a
                    # concurrent transition or after its freshness window.
                    rechecked = _verified_reactivation(
                        tx, project, tz, last, payload, datetime.now(timezone.utc))
                    if rechecked != verdict:
                        continue
                    # Second read in the same SQLite write transaction ensures
                    # the blocking transition and frozen state still match.
                    current = tx.one(
                        "SELECT id,kind,payload FROM events WHERE project=? AND tz=? "
                        "AND kind IN ('block','freeze','thaw','reactivate','approve',"
                        "'auto_reactivate') ORDER BY id DESC LIMIT 1", (project, tz),
                    )
                    still_frozen = tx.one(
                        "SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
                    if (current is not None and int(current["id"]) == int(last["id"])
                            and str(current["kind"]) == "block"
                            and str(current["payload"]) == str(last["payload"])
                            and still_frozen is not None and still_frozen["state"] == "frozen"):
                        tx.change(
                            "UPDATE tasks SET state='ready',updated_at=? "
                            "WHERE project=? AND tz=? AND state='frozen'",
                            (_now(), project, tz),
                            event=_event(
                                "auto_reactivate", worker_id=None,
                                project=project, tz=tz,
                                blocked_event_id=int(last["id"]),
                                predicate={"kind": predicate.kind,
                                           "target": predicate.target,
                                           "authority": predicate.authority},
                                evidence_refs=list(evaluated.evidence_refs),
                                reason="verified independent evidence",
                                origin="fleet_auto",
                            ),
                        )
                        thawed.append(f"{project}/{tz}: verified independent evidence")
                continue
            # Legacy block records used kind='block' even when until was the
            # task's own acceptance. Never let probe/timer make that a thaw
            # predicate: only a genuinely independent source may unblock it.
            if self_acceptance_until(reason, task["acceptance"]):
                subject = f"self-acceptance:{tz}"
                from . import judgment
                pending = tx.one(
                    "SELECT 1 FROM tasks WHERE project=? AND kind='triage' AND state IN "
                    "('draft','ready','taken','submitted','frozen') AND body LIKE ? LIMIT 1",
                    (project, f"{judgment.MARK} subject={subject}%"),
                )
                if pending is not None:
                    continue
                digest = _external_data_admission(
                    tx, project, tz, subject, int(last["id"]),
                    _consumed_external_data(tx, project, tz, subject),
                )
                if digest:
                    why = f"new independently admitted external data: {digest}"
                    tx.change(
                        "UPDATE tasks SET state='ready',updated_at=? "
                        "WHERE project=? AND tz=? AND state='frozen'",
                        (_now(), project, tz),
                        event=_event("thaw", worker_id=None, project=project, tz=tz,
                                     why=why, external_digest=digest),
                    )
                    thawed.append(f"{project}/{tz}: {why}")
                continue
            # Deployment-specific waits are handled ONLY by deploy_events.tick:
            # the durable registry requires an exact commit or explicit time.
            # Do not bypass it with an arbitrary newer deployment, timer,
            # resource sensor, or normal obstacle probe.
            if (str(payload.get("change_source") or "").strip().casefold() == "platform_deploy"
                    or re.search(r"\bchange_source\s*=\s*platform_deploy\b", reason, re.I)
                    or re.search(r"\buntil:\s*platform_deploy\b", reason, re.I)):
                continue
            structured = [*_json_list(task["needs"])]
            for field in ("needs", "dependencies", "depends_on"):
                structured.extend(_json_list(payload.get(field)))
            until = probe.from_reason(reason)
            if until and _TASK_REF.fullmatch(until):
                structured.append(until)
            refs = list(dict.fromkeys(ref for ref in structured if ref.upper() != tz.upper()))
            project_tasks = _rows(tx.query("SELECT tz,state FROM tasks WHERE project=?", (project,)))
            states = {str(r["tz"]).upper(): str(r["state"]) for r in project_tasks}
            canonical_refs = {str(r["tz"]).upper(): str(r["tz"]) for r in project_tasks}
            known = [ref for ref in refs if ref.upper() in states]
            newly_accepted = [
                ref for ref in known if states[ref.upper()] == "accepted" and tx.one(
                    # Resolve case through the small task index first. UPPER(tz)
                    # made every frozen dependency scan project event history
                    # while holding the registry writer (observed: 72.7s).
                    "SELECT 1 FROM events WHERE project=? AND tz=? AND id>? "
                    "AND kind IN ('accept','auto_obsolete') LIMIT 1",
                    (project, canonical_refs[ref.upper()], int(last["id"])),
                ) is not None
            ]
            why = ""
            sensed = [r for r in _json_list(task["resources"]) if world_cfg.get(r, {}).get("sensor")]
            sensed_state = world.states(tx, sensed)
            came_up = [r for r, (st, ts) in sensed_state.items() if st == world.UP and ts > str(last["ts"])]
            if known and len(newly_accepted) != len(known):
                # Dependency названа явно и ещё не выполнена. Таймер тут не меняет
                # фактов и лишь создаёт повторный take/block цикл.
                continue
            if any(st == world.DOWN for st, _ in sensed_state.values()):
                # Ресурса нет (телефон не подключён): таймер фактов не меняет, ждём датчик.
                continue
            probe_ok, probe_argv = probe.passed(tx, project, tz)
            if probe_ok:
                why = "проба препятствия прошла: " + " ".join(probe_argv or [])
            elif probe_argv is not None:
                # Агент дал пробу, и она падает: препятствие на месте, таймер ничего не добавит.
                continue
            elif came_up:
                # Событие, а не таймер: владелец подключил телефон — задача идёт сразу,
                # даже если причина блокировки звучала «навсегда» (usb, физический доступ).
                why = "появился ресурс: " + ", ".join(
                    f"{r} ({sensed_state[r][1][11:16]} UTC)" for r in came_up)
            elif known:
                why = "новые принятые зависимости: " + ", ".join(newly_accepted)
            elif (str(task["kind"]) == "triage" and tz.startswith("watch:")):
                # A watchdog decision blocked on a platform/guard fix has no
                # reason to change merely because the retry interval elapsed.
                # A genuine new finding gets a new watch ID; independent probes
                # and accepted dependencies above can still unblock this one.
                continue
            elif _DURABLE_EXTERNAL_BLOCK.search(reason):
                # Внешнее условие не может изменить новый fleet-agent. Не тратим чат на
                # одинаковую перепроверку; reactivate остаётся доступен автоматике/пульту,
                # когда внешний факт действительно изменился.
                continue
            else:
                try:
                    age_min = (now_dt - _parse_time(str(last["ts"]))).total_seconds() / 60
                except ValueError:
                    continue
                streak = 0
                for row in tx.query(
                    "SELECT kind,payload FROM events WHERE project=? AND tz=? "
                    "AND kind IN ('thaw','submit','approve','reactivate','accept') ORDER BY id DESC LIMIT 10",
                    (project, tz),
                ):
                    if str(row["kind"]) != "thaw":
                        break
                    streak += 1
                # После max_thaws быстрых циклов не ставим permanent owner-gate. Вместо
                # этого растим паузу экспоненциально (с потолком 3 часа), чтобы внешний
                # blocker не создавал churn, но исчезновение причины всё равно когда-нибудь
                # было перепроверено автоматически.
                backoff_power = max(0, streak - max_thaws + 1)
                required_after = min(180, after_min * (2 ** backoff_power))
                if age_min < required_after:
                    continue
                why = (
                    f"заблокирована {int(age_min)} мин назад — автоматическая перепроверка "
                    f"препятствия #{streak + 1} (интервал {required_after} мин)"
                )
            tx.change(
                "UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=? AND state='frozen'",
                (_now(), project, tz),
                event=_event(
                    "thaw",
                    worker_id=None,
                    project=project,
                    tz=tz,
                    why=why,
                    block_reason=reason[:300],
                ),
            )
            thawed.append(f"{project}/{tz}: {why}")
    return thawed


_OPEN_STATES = ("draft", "ready", "taken", "submitted", "frozen")


def close_resolved_styks(store: Any) -> list[str]:
    """Стык, после которого гейт проекта уже был зелёным, закрыт — его цель достигнута.

    07.10.2026: styk:4 Beta App висел открытым с 28.09 (его fix не получал выдачу), а пока стык
    открыт, интегратор новый не открывает — 41 красный гейт за девять дней не разбирал никто.
    Взятое агентом не трогаем: зелёным гейт мог стать от его ещё не сданной правки.
    """
    closed: list[str] = []
    with store.tx() as tx:
        opened = _rows(tx.query(
            "SELECT e.id,e.project,e.tz FROM events e JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
            "WHERE e.kind='styk_open' AND t.state IN ('draft','ready','submitted','frozen') ORDER BY e.id"
        ))
        for row in opened:
            project, root = str(row["project"]), str(row["tz"])
            green = tx.one(
                "SELECT id,ts FROM events WHERE kind='gate' AND project=? AND rc=0 AND id>? "
                "ORDER BY id LIMIT 1",
                (project, int(row["id"])),
            )
            if green is None:
                continue
            family = [
                r for r in _rows(tx.query(
                    "SELECT tz,kind,state FROM tasks WHERE project=? AND state IN ('draft','ready','taken','submitted','frozen')",
                    (project,),
                ))
                if family_root(str(r["tz"])) == root
            ]
            if any(str(r["state"]) == "taken" for r in family):
                continue
            now = _now()
            statements = [
                (
                    "UPDATE tasks SET state=?,updated_at=? WHERE project=? AND tz=?",
                    ("accepted" if str(r["tz"]) == root or str(r["kind"]) == "review" else "rejected",
                     now, project, str(r["tz"])),
                )
                for r in family
            ]
            tx.changes(
                statements,
                event=_event(
                    "auto_obsolete",
                    worker_id=None,
                    project=project,
                    tz=root,
                    reason=f"гейт проекта зелёный после открытия стыка ({green['ts']}); стык больше не нужен",
                    closed=[str(r["tz"]) for r in family],
                ),
            )
            closed.append(f"{project}/{root}")
    return closed


def reopen_orphan_reviews(store: Any) -> list[str]:
    """Инвариант: сданная задача ждёт либо ревью, либо свой fix. Ни того ни другого — ревью заново.

    Иначе задача навсегда `submitted`: её никто не возьмёт, и всё, что от неё зависит, стоит.
    """
    from .review import _open_review_tx, _review_target

    reopened: list[str] = []
    with store.tx() as tx:
        submitted = _rows(tx.query(
            "SELECT project,tz FROM tasks WHERE state='submitted' AND kind<>'review' ORDER BY project,tz"
        ))
        if not submitted:
            return reopened
        open_rows = _rows(tx.query(
            f"SELECT project,tz,kind,body FROM tasks WHERE state IN ({','.join('?' * len(_OPEN_STATES))}) "
            "AND kind IN ('review','fix')",
            _OPEN_STATES,
        ))
        reviewed: set[tuple[str, str]] = set()
        fixing: set[tuple[str, str]] = set()
        for row in open_rows:
            project = str(row["project"])
            if str(row["kind"]) == "review":
                try:
                    reviewed.add(_review_target(str(row["tz"]), str(row["body"] or ""), project))
                except ValueError:
                    continue
            else:
                fixing.add((project, family_root(str(row["tz"]))))
        for row in submitted:
            project, tz = str(row["project"]), str(row["tz"])
            if (project, tz) in reviewed or (project, family_root(tz)) in fixing:
                continue
            review_tz = _open_review_tx(tx, project, tz)
            tx.record_event(
                kind="review_reopen", worker_id=None, project=project, tz=tz,
                payload={"review": review_tz, "why": "submitted без открытого ревью и без открытого fix"},
            )
            reopened.append(f"{project}/{tz}")
    return reopened


def red_zone(tx: Any, project: str) -> tuple[list[str], str]:
    """Зона красного ствола проекта: (пути, чем она задана) или ([], '') — ствол зелёный.

    Ствол красный, пока последний гейт проекта красный. Зона — файлы упавших тестов этого гейта
    и зона открытого стыка (то, что пачка записала). Accept задач, задевающих её, ждёт стыка
    (разбор P0.2): принять ещё одну правку в сломанное место — значит прятать, что сломало.
    Остальной проект идёт: EchoRedactor бывал красным 200+ ч.
    """
    gate = tx.one(
        "SELECT ts,rc,payload FROM events WHERE kind='gate' AND project=? ORDER BY id DESC LIMIT 1",
        (project,),
    )
    if gate is None or not gate["rc"]:
        return [], ""
    # A new or modified project gate may observe red results, but it must not
    # freeze reviews before completing the shadow replay. This is a read-only
    # decision: the gate runner already recorded its "would_block" evidence.
    from .shadow import ShadowRules
    rule = ShadowRules._rules(tx).get(f"project_gate:{project}")
    legacy_styks: set[str] | None = None
    if rule is not None and rule.mode == "shadow":
        # A pre-existing styk represents a *legacy enforced* defect, not a
        # decision invented by the just-registered rule. Do not erase that
        # established red zone when upgrading the integration to shadow.
        # A new styk created after registration is observation only.
        registered = tx.scalar(
            "SELECT MAX(id) FROM events WHERE kind='shadow_rule_registered' "
            "AND json_extract(payload,'$.rule_id')=?",
            (f"project_gate:{project}",),
        )
        # A successful regate (or a green project gate) proves that the old
        # enforced failure episode has ended. A later red gate while the new
        # rule is shadow is only an observation; it must NOT resurrect the
        # old styk's blocked paths.
        old_cleared = tx.one(
            "SELECT 1 FROM events WHERE project=? AND id>? AND "
            "((kind='regate_result' AND rc=0) OR (kind='gate' AND rc=0)) "
            "LIMIT 1",
            (project, registered or 0),
        )
        if old_cleared is not None:
            return [], ""
        legacy_styks = {
            str(row["tz"]) for row in tx.query(
                "SELECT DISTINCT tz FROM events WHERE kind='styk_open' AND project=? "
                "AND id<? AND tz IS NOT NULL", (project, registered or 0),
            )
        }
        if not legacy_styks:
            return [], ""
        # A shadow run must never enlarge the old enforced failure zone.
        # The latest gate is observational; use the last gate from *before*
        # the current rule version was registered, if it was still red.
        gate = tx.one(
            "SELECT ts,rc,payload FROM events WHERE kind='gate' AND project=? "
            "AND id<? ORDER BY id DESC LIMIT 1", (project, registered),
        )
    root_row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
    root = str(root_row["root"] or "") if root_row is not None else ""
    failed: list[str] = []
    if gate is not None and gate["rc"]:
        try:
            failed = [str(p) for p in json.loads(str(gate["payload"] or "{}")).get("failed") or []]
        except (TypeError, ValueError):
            pass
    zone: list[str] = []
    for rel in failed:
        try:
            zone.append(norm_path(rel, project_root=root) if root else rel)
        except ValueError:
            continue
    legacy_failed_zone = tuple(zone)
    labels = [f"гейт {str(gate['ts'])[:16]}"] if gate is not None and gate["rc"] else []
    for row in _rows(tx.query(
        "SELECT tz,owns FROM tasks WHERE project=? AND tz LIKE 'styk:%' "
        "AND state IN ('ready','taken','submitted','frozen') ORDER BY tz",
        (project,),
    )):
        if legacy_styks is not None and str(row["tz"]) not in legacy_styks:
            continue
        zone.extend(_json_list(row["owns"]))
        labels.append(str(row["tz"]))
    if legacy_styks is not None:
        # A regate can *narrow* an already-enforced legacy styk even while
        # the new project-gate rule observes in shadow. It cannot add new
        # failing files: those have no pre-registration enforcement proof.
        regated = tx.one(
            "SELECT rc,payload FROM events WHERE kind='regate_result' AND project=? "
            "AND id>? ORDER BY id DESC LIMIT 1", (project, registered),
        )
        if regated is not None and regated["rc"]:
            try:
                narrowed_rel = json.loads(str(regated["payload"] or "{}")).get("failed") or []
            except (TypeError, ValueError):
                narrowed_rel = []
            narrowed: list[str] = []
            for rel in narrowed_rel:
                try:
                    narrowed.append(norm_path(str(rel), project_root=root) if root else str(rel))
                except ValueError:
                    continue
            zone = [z for z in zone if
                    any(paths_overlap(z, p) for p in narrowed) and
                    any(paths_overlap(z, p) for p in legacy_failed_zone)]
    # Зона «весь проект» (старые стыки) остановила бы проект целиком — её не держим.
    flat = root.replace("\\", "/").rstrip("/").casefold()
    zone = [z for z in zone if z.replace("\\", "/").rstrip("/").casefold() != flat]
    return zone, ", ".join(labels)


def red_overlap(
    tx: Any, project: str, tz: str, owns: Iterator[str] | tuple[str, ...],
    *, _deadlock_cache: dict[str, Any] | None = None,
) -> str:
    """Red trunk normally blocks; TZ-54 proofs can unblock after 24h TZ-49 shadow."""
    if task_root(tz).startswith("styk:"):
        return ""
    zone, label = red_zone(tx, project)
    if not zone:
        return ""
    hits = sorted({z for z in zone for o in owns if paths_overlap(o, z)})
    if not hits:
        return ""
    if _deadlock_review_source_exempt(tx, project, tz, _deadlock_cache):
        return ""
    return f"ствол {project} красный ({label}) в зоне задачи: " + ", ".join(hits[:5])
