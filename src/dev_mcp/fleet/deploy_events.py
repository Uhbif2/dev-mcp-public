"""TZ-39: turn verified platform-guard journal receipts into durable fleet events.

Called by fleet.tools._fleet_maintenance via tick(store). Cursor, accepted events and
resulting task thaws share ONE SQLite transaction; a crash retries the entire
unacknowledged batch. Only a verified known_good(stage=after, candidate=SHA)
immediately followed by deployed can thaw platform_deploy waits. Rollbacks
never satisfy deployment waits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SOURCE = re.compile(r"\b(?:change_source\s*=\s*|until:\s*)platform_deploy\b", re.I)
_TARGET = re.compile(r"\b(?:commit|candidate|sha|revision)\s*[=:]\s*([0-9a-f]{40})\b", re.I)
_BARE_SHA = re.compile(r"\b[0-9a-f]{40}\b", re.I)
_AFTER = re.compile(r"\b(?:after|since|after_ts|after_time)\s*[=:]\s*(\S+)", re.I)
_CURSOR_KIND = "platform_guard_cursor"
_MAX_BYTES = 8 * 1024 * 1024
_MAX_LINE = 65536


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None
        return date.astimezone(timezone.utc)
    except ValueError:
        return None


def _scope(data: bytes, offset: int) -> str:
    """Hash the consumed prefix's head and tail so replacement/truncation resets the cursor."""
    if offset <= 0:
        return sha256(b"").hexdigest()
    sample = data[:min(offset, 4096)]
    if offset > 4096:
        sample += data[max(0, offset - 4096):offset]
    return sha256(sample).hexdigest()


def _safe_event(value: bytes) -> tuple[dict[str, Any] | None, str]:
    if len(value) > _MAX_LINE:
        return None, "oversize"
    try:
        parsed = json.loads(value.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return None, "invalid_json_or_utf8"
    if not isinstance(parsed, dict):
        return None, "not_object"
    return parsed, ""


def _criterion(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    reason = str(payload.get("reason") or "")
    # A typed change_source is accepted only from the durable server block event.
    if (str(payload.get("change_source") or "").strip().casefold() != "platform_deploy"
            and not _SOURCE.search(reason)):
        return None
    target = str(payload.get("commit") or payload.get("candidate")
                 or payload.get("revision") or payload.get("sha") or "").strip().lower()
    if not target:
        marked = _TARGET.search(reason)
        target = (marked.group(1).lower() if marked else "")
    if not target:
        # Explicit "until: platform_deploy <40-hex>" form from the protocol.
        matched = _BARE_SHA.search(reason)
        target = matched.group().lower() if matched else ""
    if _SHA.fullmatch(target):
        return "commit", target
    after = str(payload.get("after") or payload.get("after_ts")
                or payload.get("since") or "").strip()
    if not after:
        matched = _AFTER.search(reason)
        after = matched.group(1).strip(";,") if matched else ""
    dt = _utc(after)
    if dt:
        return "after", dt.isoformat()
    # Bare change_source without an exact SHA or timestamp is NOT enough.
    return None


def _thaw(tx: Any, candidate: str, deployed_at: str, receipt: str) -> int:
    moment = _utc(deployed_at)
    if moment is None:
        return 0
    resumed = 0
    tasks = tx.query(
        "SELECT project,tz FROM tasks WHERE state='frozen' ORDER BY project,tz"
    )
    for task in tasks:
        project, tz = str(task["project"]), str(task["tz"])
        last = tx.one(
            "SELECT id,kind,ts,payload FROM events WHERE project=? AND tz=? "
            "AND kind IN ('block','freeze','thaw','reactivate','approve','auto_reactivate') "
            "ORDER BY id DESC LIMIT 1", (project, tz),
        )
        if last is None or last["kind"] != "block":
            continue
        # A historical deployed entry must not satisfy a future request for a
        # *new* deployment. This matters on the initial catch-up journal scan.
        blocked_at = _utc(last["ts"])
        if blocked_at is None or moment <= blocked_at:
            continue
        try:
            data = json.loads(last["payload"])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        # TZ-69: a new structured block is decided by the read-only evidence
        # policy in queue.thaw_frozen, after this trusted receipt is recorded.
        # Legacy blocks retain the existing exactly-matched thaw contract.
        registered = data.get("reactivate_policy")
        if (isinstance(registered, dict) and registered.get("kind") in
                {"platform_deploy_commit", "platform_deploy_after"}):
            continue
        wanted = _criterion(data)
        if wanted is None:
            continue
        match = wanted == ("commit", candidate) or (
            wanted[0] == "after" and (point := _utc(wanted[1])) is not None
            and moment > point
        )
        if not match:
            continue
        now = datetime.now(timezone.utc).isoformat()
        tx.change(
            "UPDATE tasks SET state='ready',updated_at=? "
            "WHERE project=? AND tz=? AND state='frozen'",
            (now, project, tz),
            event={
                "kind": "thaw",
                "project": project,
                "tz": tz,
                "payload": {
                    "why": "verified platform deployment",
                    "change_source": "platform_deploy",
                    "candidate": candidate,
                    "known_good": candidate,
                    "receipt": receipt,
                    "blocked_event_id": int(last["id"]),
                    "deployed_at": deployed_at,
                    "previous_state": "frozen",
                },
            },
        )
        resumed += 1
    return resumed


def tick(store: Any, log_path: str | Path | None = None) -> dict[str, int]:
    """Ingest one bounded batch of complete journal lines, exactly once per Store.

    Default path is derived from the trusted Store DB location, not caller note.
    Tests pass explicit temporary log_path. No network or shell invocation.
    """
    if log_path is None:
        db = Path(store.path).resolve()
        if len(db.parents) < 3:
            return {"events": 0, "thawed": 0, "warnings": 0}
        log_path = db.parents[2] / "logs" / "platform-guard.jsonl"
    path = Path(log_path)
    if path.is_symlink() or not path.is_file():
        return {"events": 0, "thawed": 0, "warnings": 0}
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            data = stream.read()
    except OSError:
        return {"events": 0, "thawed": 0, "warnings": 0}
    ident = f"{stat.st_dev}:{stat.st_ino}"
    result = {"events": 0, "thawed": 0, "warnings": 0}
    with store.tx() as tx:
        # A rotated/copied journal may repeat complete old receipts. Cursor
        # alone proves "once per file generation"; event identity also binds
        # SHA/time so replay across generations cannot emit a second receipt.
        seen_deploy: set[tuple[str, str]] = set()
        for recorded in tx.query(
            "SELECT payload FROM events WHERE kind='platform_deployed'"
        ):
            try:
                old = json.loads(recorded["payload"])
            except (TypeError, ValueError):
                continue
            if isinstance(old, dict):
                seen_deploy.add((str(old.get("candidate")), str(old.get("ts"))))
        seen_rollback: set[tuple[str, str]] = set()
        for recorded in tx.query(
            "SELECT payload FROM events WHERE kind='platform_rolled_back'"
        ):
            try:
                old = json.loads(recorded["payload"])
            except (TypeError, ValueError):
                continue
            if isinstance(old, dict):
                seen_rollback.add((str(old.get("code")), str(old.get("ts"))))
        saved = tx.one(
            "SELECT payload FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (_CURSOR_KIND,),
        )
        try:
            cursor = json.loads(saved["payload"]) if saved else {}
        except (ValueError, TypeError):
            cursor = {}
        if not isinstance(cursor, dict):
            cursor = {}
        offset = cursor.get("offset", 0)
        if type(offset) is not int or offset < 0:
            offset = 0
        if (offset > len(data) or cursor.get("identity") != ident
                or cursor.get("scope") != _scope(data, offset)):
            offset, cursor = 0, {}
        # Only acknowledge *complete* lines: append-in-progress gets replayed.
        segment = data[offset:offset + _MAX_BYTES]
        cutoff = segment.rfind(b"\n")
        if cutoff < 0:
            # A single malformed/oversized line must not jam the maintenance
            # loop forever. Skip bounded chunks until a newline is found.
            if len(segment) == _MAX_BYTES:
                tx.record_event(kind="platform_guard_warning", payload={
                    "offset": offset, "reason": "unbounded_line",
                    "scope": ident,
                })
                tx.record_event(kind=_CURSOR_KIND, payload={
                    "identity": ident, "offset": offset + _MAX_BYTES,
                    "scope": _scope(data, offset + _MAX_BYTES),
                    "pending": {}, "previous_rollback": False,
                })
                result["warnings"] += 1
            return result
        new_offset = offset + cutoff + 1
        pending = cursor.get("pending")
        if not isinstance(pending, dict):
            pending = {}
        previous_rollback = bool(cursor.get("previous_rollback", False))
        pos = offset
        for line in segment[:cutoff + 1].splitlines(keepends=True):
            pos += len(line)
            if not line.strip():
                continue
            event, problem = _safe_event(line.rstrip(b"\r\n"))
            if problem:
                tx.record_event(kind="platform_guard_warning", payload={
                    "offset": pos, "reason": problem, "scope": ident,
                })
                result["warnings"] += 1
                pending = {}
                previous_rollback = False
                continue
            code = event.get("code")
            ts = str(event.get("ts") or "")
            parsed = _utc(ts)
            if parsed is None or not isinstance(code, str):
                tx.record_event(kind="platform_guard_warning", payload={
                    "offset": pos, "reason": "missing_code_or_time", "scope": ident,
                })
                result["warnings"] += 1
                pending = {}
                previous_rollback = False
                continue
            if code == "known_good" and event.get("stage") == "after":
                candidate = str(event.get("candidate") or "").lower()
                pending = {"candidate": candidate, "ts": ts} if _SHA.fullmatch(candidate) else {}
                previous_rollback = False
                continue
            if code == "deployed":
                candidate = str(pending.get("candidate") or "")
                if _SHA.fullmatch(candidate) and (
                    not event.get("candidate") or event["candidate"] == candidate
                ):
                    try:
                        age = (parsed - _utc(pending["ts"])).total_seconds()
                    except (TypeError, ValueError, AttributeError):
                        age = -1
                    if 0 <= age <= 30:
                        key = candidate, ts
                        if key not in seen_deploy:
                            tx.record_event(kind="platform_deployed", payload={
                                "candidate": candidate, "known_good": candidate,
                                "ts": ts, "journal_offset": pos, "scope": ident,
                            })
                            seen_deploy.add(key)
                            result["events"] += 1
                            result["thawed"] += _thaw(tx, candidate, ts, f"{ident}:{pos}")
                pending = {}
                previous_rollback = False
                continue
            # No event may be inserted between the verified receipt and deployed.
            pending = {}
            is_rollback = (code in {"rollback_ok", "rollback_failed", "rollback_dirty",
                                    "safe_mode"}
                           or code == "rollback" and event.get("stage") == "after")
            if (is_rollback and not previous_rollback
                    and (code, ts) not in seen_rollback):
                tx.record_event(kind="platform_rolled_back", payload={
                    "ts": ts, "code": code, "success": code in {"rollback_ok", "safe_mode"}
                    or (code == "rollback" and bool(event.get("success"))),
                    "journal_offset": pos, "scope": ident,
                })
                seen_rollback.add((code, ts))
                result["events"] += 1
            previous_rollback = bool(is_rollback)
        tx.record_event(kind=_CURSOR_KIND, payload={
            "identity": ident,
            "offset": new_offset,
            "scope": _scope(data, new_offset),
            "pending": pending,
            "previous_rollback": previous_rollback,
        })
    return result
