"""Route cross-project platform defects into one Fleet Platform task.

The registry is the source of truth: a fingerprint belongs to one platform
request and the sources of that request are recorded in durable events.
The source stays frozen until both an independent fleet accept and a verified
platform-guard deployment of the integrated commit are observed.

Call route_request from the request registrar, and thaw_deployed from the
maintenance loop after deploy_events.tick. Both operations are transactional.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any

from .model import norm_path


PLATFORM = "Fleet Platform"
_PLATFORM_PATH = re.compile(
    r"(?:^|/)(?:dev-mcp(?:-work)?|extension/fleet-tabs|src/dev_mcp/fleet)(?:/|$)", re.I
)
_PLATFORM_TEXT = re.compile(
    r"при[её]мк|acceptance|ревью|review|выдач|fleet[_ -]?take|страж|guard", re.I
)
_PLATFORM_DEFECT = re.compile(
    r"\b(?:дефект\w*|ошибк\w*|сбо\w*|баг\w*|поломк\w*|регресс\w*|"
    r"неправильн\w*|неверн\w*|некорректн\w*|слом\w*|отказ\w*|"
    r"исправ\w*|почин\w*|bug|broken|failure|failed|error|regression|fix)\b"
    r"|\bне\s+(?:работает|проходит|выда[её]т|созда[её]т|принима[её]т|"
    r"срабатывает|совпадает|сопоставл\w*)\b", re.I
)
_PLATFORM_SUBJECT = re.compile(r"\b(?:fleet|dev-mcp|платформ\w*|флот\w*)\b", re.I)
_SHA = re.compile(r"^[a-f0-9]{40}$")
_MARK = "[platform-route]"
_COUNT = re.compile(r"затронуто задач: \d+")


@dataclass(frozen=True)
class RouteResult:
    status: str  # ignored | created | linked | rejected
    task: str | None = None
    affected_tasks: int = 0
    detail: str = ""


class PlatformRoutePending(ValueError):
    """An unresolved source must not create another request or fix."""


def is_platform_request(*, summary: str, detail: str = "", path: str = "") -> bool:
    """Route explicit platform paths or defects of platform workflow topics.

    Acceptance criteria and review steps commonly occur in ordinary requests;
    mentioning them in ``detail`` must not freeze an unrelated source task.
    """
    raw_path = path.replace("\\", "/")
    if _PLATFORM_PATH.search(raw_path):
        return True
    if not _PLATFORM_DEFECT.search(summary):
        return False
    return bool(_PLATFORM_TEXT.search(summary) or
                (_PLATFORM_SUBJECT.search(summary) and _PLATFORM_TEXT.search(detail)))


def _topic(text: str) -> str:
    value = text.casefold().replace("ё", "е")
    if re.search(r"приемк|acceptance", value):
        if re.search(r"команд|command|match|сопостав|сравнен", value):
            return "acceptance-commands"
        return "acceptance"
    if re.search(r"ревью|review", value):
        return "review"
    if re.search(r"выдач|fleet[_ -]?take", value):
        return "take"
    if re.search(r"страж|guard", value):
        return "guard"
    return "code"


def _request_topic(summary: str, detail: str) -> str:
    """Keep the incident subject ahead of incidental acceptance in its detail."""
    subject = _topic(summary)
    return subject if subject != "code" else _topic(detail)


def _path_in_worktree(path: str, topic: str) -> str:
    """Map a live dev-mcp pathname to the matching *work* pathname."""
    normalized = path.replace("\\", "/").strip().casefold()
    match = re.search(r"(?:^|/)(?:dev-mcp-work|dev-mcp)/(.*)", normalized)
    if match:
        relative = match.group(1)
    elif normalized.startswith(("extension/fleet-tabs/", "src/dev_mcp/fleet/")):
        relative = normalized
    else:
        relative = {
            "acceptance-commands": "src/dev_mcp/fleet/acceptance_guard.py",
            "acceptance": "src/dev_mcp/fleet/acceptance_guard.py",
            "review": "src/dev_mcp/fleet/review.py",
            "take": "src/dev_mcp/fleet/queue.py",
            "guard": "guard/core.py",
        }.get(topic, "src/dev_mcp/fleet/tools.py")
    parts = relative.split("/")
    if not relative or any(bit in ("", ".", "..") for bit in parts):
        raise ValueError(f"invalid platform request path: {path!r}")
    return relative


def fingerprint(*, summary: str, detail: str = "", path: str = "") -> str:
    """Stable across originating projects, task numbers, workers and IDs."""
    topic = _request_topic(summary, detail)
    target = _path_in_worktree(path, topic)
    text = summary.casefold().replace("ё", "е")
    text = re.sub(r"\b(?:tz|w|request|fix)[-:_ ]*[0-9a-f]{3,}\b", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"\b(?:ошибка|дефект|баг|исправить|проблема|сбой|сломано)\b", " ", text)
    text = " ".join(re.findall(r"[\w]+", text))
    # Common incident: many projects report the same acceptance command mapping
    # defect with different wording and different task numbers.
    subject = topic if topic == "acceptance-commands" else f"{topic}:{text}"
    return sha256(f"{target}|{subject}".encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _data(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_wait(tx: Any, project: str, tz: str) -> dict[str, Any] | None:
    """Latest platform wait without a subsequent proven thaw."""
    wait = tx.one(
        "SELECT id,payload FROM events WHERE project=? AND tz=? "
        "AND kind='platform_route_wait' ORDER BY id DESC LIMIT 1",
        (project, tz),
    )
    if wait is None:
        return None
    data = _data(wait["payload"])
    for event in tx.query(
        "SELECT payload FROM events WHERE project=? AND tz=? "
        "AND kind='thaw' AND id>? ORDER BY id DESC",
        (project, tz, int(wait["id"])),
    ):
        outcome = _data(event["payload"])
        if (outcome.get("platform_task") == data.get("platform_task")
                and outcome.get("why") == "platform task accepted and deployed"
                and outcome.get("change_source") == "platform_deploy"
                and isinstance(outcome.get("receipt_event_id"), int)):
            return None
        if (outcome.get('platform_task') == data.get('platform_task')
                and outcome.get('why') == 'operator verified platform repair'
                and isinstance(outcome.get('receipt_event_id'),int)):
            proof = tx.one("SELECT payload FROM events WHERE id=? AND kind='manual_platform_resolved'",
                           (outcome['receipt_event_id'],))
            if proof and _data(proof['payload']).get('id') == data.get('platform_task'):
                return None
    return data


def pending_platform(tx: Any, project: str, tz: str) -> str | None:
    """Read-only guard for all new requests/fixes from an awaiting source."""
    wait = _source_wait(tx, project, tz)
    if wait is None:
        return None
    return str(wait.get("platform_task") or "") or None


def require_request_allowed(store: Any, project: str, tz: str) -> None:
    with store.read() as tx:
        target = pending_platform(tx, project, tz)
    if target:
        raise PlatformRoutePending(
            f"{project}/{tz}: ожидает платформенную задачу {target} "
            "и подтверждённую выкатку; новые request/fix запрещены"
        )


def route_request(
    store: Any, *, source_project: str, source_tz: str,
    summary: str, detail: str = "", path: str = "", worker_id: str | None = None,
) -> RouteResult:
    """Create/link a platform task, park its source and reject project retries.

    Unlike open_request, this routes before resolving 'to' inside the source
    project. Atomicity prevents concurrent sources from creating duplicate work.
    """
    if source_project == PLATFORM or not is_platform_request(
        summary=summary, detail=detail, path=path
    ):
        return RouteResult("ignored")
    if not summary.strip():
        raise ValueError("summary is required")
    topic = _request_topic(summary, detail)
    relative = _path_in_worktree(path, topic)
    digest = fingerprint(summary=summary, detail=detail, path=path)
    tz = f"request:{digest[:8]}"
    target_ref = f"{PLATFORM}/{tz}"

    with store.tx() as tx:
        source = tx.one(
            "SELECT state FROM tasks WHERE project=? AND tz=?",
            (source_project, source_tz),
        )
        if source is None:
            raise ValueError(f"source task not found: {source_project}/{source_tz}")
        existing_wait = pending_platform(tx, source_project, source_tz)
        if existing_wait:
            return RouteResult("rejected", existing_wait, detail="источник уже ожидает выкатку")
        root = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (PLATFORM,))
        from . import platform_manual
        if platform_manual.enabled():
            manual = platform_manual.enqueue(tx,digest,title=summary,detail=detail,
                                             source_project=source_project,source_tz=source_tz,
                                             owns=[relative])
            tx.record_event(kind='platform_route_wait',project=source_project,tz=source_tz,
                            payload={'fingerprint':digest,'platform_task':manual,
                                     'source_project':source_project,'source_tz':source_tz})
            # Keep the same source lifecycle, with a manual repair identity.
            # Neither this path nor the report creates a Fleet Platform project.
            now = _now()
            tx.change("UPDATE claims SET state='released',released_at=? WHERE project=? AND tz=? AND state='active'",
                      (now,source_project,source_tz),event='manual_platform_source_released')
            if source['state'] in ('ready','taken'):
                tx.change("UPDATE tasks SET state='frozen',updated_at=? WHERE project=? AND tz=?",
                          (now,source_project,source_tz),event={'kind':'block','project':source_project,
                          'tz':source_tz,'payload':{'reason':'blocked: manual platform repair '+manual,
                                                  'change_source':'platform_deploy','platform_task':manual}})
            return RouteResult('created',manual,1,'платформу исправляет оператор')
        if root is None:
            raise ValueError("Fleet Platform is not registered and active")

        # Refuse repeated reports from the same project, even by another worker
        # or from a different task. Earlier reports remain in the event ledger.
        joined: set[tuple[str, str]] = set()
        duplicate_project = False
        for event in tx.query(
            "SELECT project,tz,payload FROM events WHERE kind='platform_route_wait'"
        ):
            data = _data(event["payload"])
            if data.get("fingerprint") != digest:
                continue
            joined.add((str(event["project"]), str(event["tz"])))
            if event["project"] == source_project:
                duplicate_project = True
        if duplicate_project:
            return RouteResult(
                "rejected", target_ref, len(joined),
                f"повторная заявка проекта {source_project}; см. {target_ref}",
            )

        task = tx.one(
            "SELECT body FROM tasks WHERE project=? AND tz=?", (PLATFORM, tz)
        )
        if task is not None and f"{_MARK} fingerprint={digest}" not in str(task["body"]):
            raise ValueError("platform fingerprint prefix collision")

        now = _now()
        count = len(joined) + 1
        if task is None:
            owned = norm_path(relative, project_root=str(root["root"]))
            body = "\n".join((
                f"{_MARK} fingerprint={digest}",
                f"Что нужно: {summary.strip()}",
                f"Детали: {detail.strip()}",
                f"Путь: {relative}",
                f"Источник: {source_project}/{source_tz}",
                f"затронуто задач: {count}",
                "Устранить дефект платформы и довести задачу до accept и "
                "подтверждённой выкатки platform_guard.",
            ))
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
                "spec_sha,needs,owns,resources,acceptance,author_worker,body,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (PLATFORM, tz, f"Platform defect: {summary.strip()[:100]}",
                 "ready", -10, "request", "", "", "[]",
                 json.dumps([owned]), "[]", "[]", worker_id, body, now, now),
                event={"kind": "platform_route_created", "project": PLATFORM,
                       "tz": tz, "payload": {"fingerprint": digest}},
            )
            status = "created"
        else:
            updated_body = _COUNT.sub(f"затронуто задач: {count}", str(task["body"]))
            tx.change(
                "UPDATE tasks SET body=?,updated_at=? WHERE project=? AND tz=?",
                (updated_body, now, PLATFORM, tz),
                event={"kind": "platform_route_linked", "project": PLATFORM,
                       "tz": tz, "payload": {
                           "fingerprint": digest, "source_project": source_project,
                           "source_tz": source_tz, "affected_tasks": count}},
            )
            status = "linked"

        if source["state"] not in ("ready", "taken"):
            raise ValueError(f"source is not parkable: {source_project}/{source_tz}")
        # store.Txn has no execute(): 13.10 every request_note routed to the platform
        # failed "'Txn' object has no attribute 'execute'" (deep-research fix:TZ-13).
        tx.change(
            "UPDATE claims SET state='released',released_at=? "
            "WHERE project=? AND tz=? AND state='active'",
            (now, source_project, source_tz),
            event={"kind": "platform_route_release", "project": source_project,
                   "tz": source_tz, "worker_id": worker_id,
                   "payload": {"platform_task": target_ref}},
        )
        tx.change(
            "UPDATE tasks SET state='frozen',updated_at=? WHERE project=? AND tz=?",
            (now, source_project, source_tz),
            event={"kind": "block", "project": source_project, "tz": source_tz,
                   "worker_id": worker_id, "payload": {
                       "reason": f"blocked: change_source=platform_deploy; until {target_ref}"
                                 " accepted and its commit deployed",
                       "change_source": "platform_deploy",
                       "platform_task": target_ref}},
        )
        tx.record_event(
            kind="platform_route_wait", project=source_project, tz=source_tz,
            worker_id=worker_id,
            payload={"fingerprint": digest, "platform_task": target_ref,
                     "source_project": source_project, "source_tz": source_tz},
        )
        return RouteResult(status, target_ref, count)


def thaw_deployed(store: Any) -> list[str]:
    """Release waiting sources only after independent accept + integrated deploy.

    Deployment receipts come exclusively from deploy_events.tick (platform
    guard verified known_good); this function does not trust arbitrary text or
    a passed-in SHA. A later rollback prevents release.
    """
    resumed: list[str] = []
    with store.tx() as tx:
        # Manual ownership supersedes the retired self-deployment loop. Only
        # the operator receipt for this exact defect can release its sources.
        manual = tx.query("SELECT t.project,t.tz FROM tasks t JOIN projects p ON p.name=t.project "
                          "WHERE t.state='frozen' AND p.active=1")
        for source in manual:
            project,tz = str(source['project']),str(source['tz'])
            wait = _source_wait(tx,project,tz)
            if not wait: continue
            target = str(wait.get('platform_task') or '')
            proof = tx.one("SELECT id,payload FROM events WHERE kind='manual_platform_resolved' "
                           "AND json_extract(payload,'$.id')=? ORDER BY id DESC LIMIT 1",(target,))
            if not proof: continue
            payload = _data(proof['payload'])
            if (payload.get('producer') != 'operator-verified-deployment'
                    or not _SHA.fullmatch(str(payload.get('candidate','')))
                    or not str(payload.get('evidence','')).strip()): continue
            latest_block = tx.one("SELECT payload FROM events WHERE project=? AND tz=? AND kind='block' "
                                  "ORDER BY id DESC LIMIT 1",(project,tz))
            if not latest_block or _data(latest_block['payload']).get('platform_task') != target:
                continue
            tx.change("UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=? AND state='frozen'",
                      (_now(),project,tz),event={'kind':'thaw','project':project,'tz':tz,'payload':{
                      'why':'operator verified platform repair','platform_task':target,
                      'change_source':'platform_deploy','candidate':payload['candidate'],
                      'receipt_event_id':int(proof['id'])}})
            resumed.append(f'{project}/{tz}')
        newest = tx.one(
            "SELECT id,kind,payload FROM events "
            "WHERE kind IN ('platform_deployed','platform_rolled_back') "
            "ORDER BY id DESC LIMIT 1"
        )
        if newest is None or newest["kind"] != "platform_deployed":
            return resumed
        proof = _data(newest["payload"])
        candidate = str(proof.get("candidate") or "").casefold()
        if not _SHA.fullmatch(candidate) or proof.get("known_good") != candidate:
            return resumed

        frozen = tx.query("SELECT project,tz FROM tasks WHERE state='frozen'")
        for source in frozen:
            project, tz = str(source["project"]), str(source["tz"])
            wait = _source_wait(tx, project, tz)
            if not wait:
                continue
            target = str(wait.get("platform_task") or "")
            if not target.startswith(f"{PLATFORM}/"):
                continue
            target_tz = target[len(PLATFORM) + 1:]
            platform = tx.one(
                "SELECT state FROM tasks WHERE project=? AND tz=?",
                (PLATFORM, target_tz),
            )
            if platform is None or platform["state"] != "accepted":
                continue
            accepted = tx.one(
                "SELECT id FROM events WHERE project=? AND tz=? AND kind='accept' "
                "ORDER BY id DESC LIMIT 1", (PLATFORM, target_tz),
            )
            if accepted is None:
                continue
            commit_ok = False
            for event in tx.query(
                "SELECT id,payload FROM events WHERE project=? AND tz=? "
                "AND kind='integrate' AND id>? AND id<? ORDER BY id DESC",
                (PLATFORM, target_tz, int(accepted["id"]), int(newest["id"])),
            ):
                sha = str(_data(event["payload"]).get("commit") or "").casefold()
                if 7 <= len(sha) <= 40 and re.fullmatch(r"[a-f0-9]+", sha) and candidate.startswith(sha):
                    commit_ok = True
                    break
            if not commit_ok:
                continue
            now = _now()
            tx.change(
                "UPDATE tasks SET state='ready',updated_at=? "
                "WHERE project=? AND tz=? AND state='frozen'",
                (now, project, tz),
                event={"kind": "thaw", "project": project, "tz": tz,
                       "payload": {"why": "platform task accepted and deployed",
                                   "platform_task": target,
                                   "change_source": "platform_deploy",
                                   "candidate": candidate,
                                   "receipt_event_id": int(newest["id"])}},
            )
            resumed.append(f"{project}/{tz}")
    return resumed
