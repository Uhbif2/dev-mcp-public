"""Durable Deep Research requests exchanged with FleetChrome.

No schema migration: requests and terminal transitions are fleet events. The
extension transports only through the explicit fleet_research_* MCP bridge;
research content is never inserted in the public telemetry/audit channel.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CONCURRENT_LIMIT = 3
DAILY_LIMIT = 10
TIMEOUT = timedelta(minutes=40)
MAX_REPORT = 200_000
_ID = re.compile(r"^r-[0-9a-f]{24}$")
_EVENTS = ("research.requested", "research.completed", "research.unavailable")


def _time(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _snapshot(tx: Any, now: datetime) -> dict[str, dict[str, Any]]:
    # Active requests are at most 40 minutes old. Keep the current day plus the
    # preceding day so a request at 23:59 is still visible past midnight.
    cutoff = _iso(now - timedelta(days=2))
    rows = tx.query(
        "SELECT kind,payload FROM events WHERE kind IN (?,?,?) AND ts>=? ORDER BY id",
        (*_EVENTS, cutoff),
    )
    state: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            request_id = payload["id"]
        except (TypeError, ValueError, KeyError):
            continue
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
            continue
        if row["kind"] == "research.requested":
            if isinstance(payload.get("question"), str):
                state[request_id] = {**payload, "status": "requested"}
        elif request_id in state and state[request_id]["status"] == "requested":
            state[request_id]["status"] = (
                "completed" if row["kind"] == "research.completed" else "unavailable"
            )
            state[request_id]["reason"] = payload.get("reason", "")
    return state


def _notify(tx: Any, item: dict[str, Any], *, status: str, reason: str = "",
            path: str = "") -> None:
    msg = (f"Deep Research {item['id']}: {status}; "
           + (f"отчёт: {path}" if status == "completed"
              else f"причина: {reason[:300]}"))
    tx.change(
        """INSERT INTO messages
           (id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,body,
            created_at,delivered_at,acked_at)
           VALUES(?,?,?,NULL,0,'system','deep-research','note',?,?,NULL,NULL)""",
        ("m-" + uuid.uuid4().hex[:12], "research:" + item["id"],
         item["agent"], msg, _iso(_time())),
        event={"kind": "research.notified", "worker_id": item["agent"],
               "project": item["project"], "payload": {"id": item["id"], "status": status}},
    )


def _terminal(tx: Any, item: dict[str, Any], status: str,
              *, now: datetime, reason: str = "", path: str = "") -> None:
    tx.record_event(
        kind="research.completed" if status == "completed" else "research.unavailable",
        worker_id=item["agent"], project=item["project"], ts=_iso(now),
        payload={"id": item["id"], "reason": reason[:400], "path": path},
    )
    _notify(tx, item, status=status, reason=reason, path=path)


def _expire(tx: Any, state: dict[str, dict[str, Any]], now: datetime) -> None:
    for item in state.values():
        if item["status"] != "requested":
            continue
        try:
            started = datetime.fromisoformat(item["requested_at"])
            due = started + TIMEOUT
        except (TypeError, ValueError, KeyError):
            due = now
        if now >= due:
            _terminal(tx, item, "unavailable", now=now, reason="таймаут 40 минут")
            item["status"] = "unavailable"
            item["reason"] = "таймаут 40 минут"


def request(store: Any, *, agent: str, question: str, project: str,
            now: datetime | None = None) -> str | dict[str, str]:
    """Register an asynchronous request and return its id without waiting."""
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 8000:
        raise ValueError("question must contain 1–8000 characters")
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("agent is required")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project is required")
    timestamp = _time(now)
    with store.tx() as tx:
        project_row = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (project,))
        if not project_row:
            raise ValueError("unknown or inactive fleet project")
        state = _snapshot(tx, timestamp)
        _expire(tx, state, timestamp)
        active = sum(1 for item in state.values() if item["status"] == "requested")
        if active >= CONCURRENT_LIMIT:
            return {"status": "unavailable", "reason": "лимит: 3 одновременных исследования"}
        today = timestamp.date()
        used = sum(
            1 for item in state.values()
            if datetime.fromisoformat(item["requested_at"]).date() == today
        )
        if used >= DAILY_LIMIT:
            return {"status": "unavailable", "reason": "лимит: 10 исследований за UTC-сутки"}
        item = {"id": "r-" + uuid.uuid4().hex[:24], "agent": agent,
                "project": project, "question": question.strip(),
                "requested_at": _iso(timestamp),
                "deadline_at": _iso(timestamp + TIMEOUT)}
        tx.record_event(kind="research.requested", worker_id=agent, project=project,
                        ts=_iso(timestamp), payload=item)
        return item["id"]


def pending(store: Any, *, now: datetime | None = None) -> list[dict[str, str]]:
    """Current work for the extension; no report text in monitoring logs."""
    timestamp = _time(now)
    with store.tx() as tx:
        state = _snapshot(tx, timestamp)
        _expire(tx, state, timestamp)
        return [
            {k: item[k] for k in ("id", "question", "project", "requested_at", "deadline_at")}
            for item in state.values() if item["status"] == "requested"
        ]


def finish(store: Any, *, request_id: str, status: str, report: str = "",
           reason: str = "", now: datetime | None = None) -> dict[str, str]:
    """Complete one request idempotently, committing file and inbox facts."""
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ValueError("invalid research request id")
    if status not in ("complete", "unavailable"):
        raise ValueError("status must be complete or unavailable")
    if not isinstance(report, str) or len(report) > MAX_REPORT:
        raise ValueError("report is too large")
    if status == "complete" and not report.strip():
        raise ValueError("completed research must have a non-empty report")
    if not isinstance(reason, str) or len(reason) > 1000:
        raise ValueError("invalid unavailability reason")
    if status == "unavailable" and not reason.strip():
        raise ValueError("unavailability requires a reason")
    timestamp = _time(now)
    with store.tx() as tx:
        state = _snapshot(tx, timestamp)
        _expire(tx, state, timestamp)
        item = state.get(request_id)
        if not item:
            raise ValueError("unknown research request")
        if item["status"] != "requested":
            return {"id": request_id, "status": item["status"],
                    "reason": item.get("reason", "")}
        if status == "unavailable":
            _terminal(tx, item, "unavailable", now=timestamp, reason=reason.strip())
            return {"id": request_id, "status": "unavailable", "reason": reason.strip()}
        row = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (item["project"],))
        if not row:
            _terminal(tx, item, "unavailable", now=timestamp, reason="проект недоступен")
            return {"id": request_id, "status": "unavailable", "reason": "проект недоступен"}
        root = Path(row["root"]).resolve()
        folder = root / "intent" / "research"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (request_id + ".md")
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=folder, prefix=".research-", suffix=".tmp",
                delete=False,
            ) as output:
                tmp_name = output.name
                output.write(report)
            os.replace(tmp_name, target)
        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)
        _terminal(tx, item, "completed", now=timestamp, path=str(target))
        return {"id": request_id, "status": "completed", "path": str(target)}
