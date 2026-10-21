"""Server-origin owner notifications for proven intent stages.

Only trusted stage-prover code calls this module. Test adapters replace HTTP,
toast and paths; imports never read credentials or send anything.
"""
from __future__ import annotations

from dev_mcp import machine

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


INBOX_PATH = machine.OWNER_INBOX
OVERVIEW_URL = "http://127.0.0.1:8766/owner/overview"
_KINDS = frozenset({"needs", "done"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _toast():
    from dev_mcp.fleet.windows_notify import WindowsToast
    return WindowsToast("OpenAI.Fleet")


def _event_rows(store: Any, kind: str, project: str | None = None) -> list[dict]:
    with store.read() as tx:
        rows = tx.query(
            "SELECT payload FROM events WHERE kind=? AND project=? ORDER BY id",
            (kind, project),
        ) if project is not None else tx.query(
            "SELECT payload FROM events WHERE kind=? ORDER BY id", (kind,),
        )
    result = []
    for row in rows:
        try:
            value = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _record(store: Any, kind: str, project: str, payload: dict) -> None:
    with store.tx() as tx:
        tx.record_event(kind=kind, project=project, payload=payload)


def _fallback_once(store: Any, project: str, reason: str) -> None:
    stamp = _now()
    day = stamp[:10]
    # This is a diagnostic, not a second delivery.
    if any(row.get("date") == day
           for row in _event_rows(store, "owner_channel.fallback", project)):
        return
    _record(store, "owner_channel.fallback", project,
            {"date": day, "reason": reason})


def notify_owner(store: Any, project: str, kind: str, text: str) -> dict:
    """Idempotent notification keyed by (project, kind, SHA-256(text))."""
    if kind not in _KINDS or not isinstance(project, str) or not project.strip():
        raise ValueError("invalid owner notification")
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("empty or overlong owner message")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    previous = next((row for row in _event_rows(store, "owner_channel.sent", project)
                     if row.get("kind") == kind and row.get("sha256") == digest), None)
    if previous is not None:
        return previous

    stamp = _now()
    key = f"{project}:{kind}:{digest}"
    cleaned = " ".join(text.split())
    title = "Нужно от владельца" if kind == "needs" else "Готово"
    result: dict[str, Any] = {
        "project": project, "kind": kind, "sha256": digest,
        "channel": "windows", "sent_at": stamp,
    }
    with store.read() as tx:
        browser_queue = tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_notices'")
    if browser_queue:
        result.update(channel='fleet-chrome', delivery_state='pending')
        _record(store,"owner_channel.sent",project,result)
        return result
    # TickTick — только официальный MCP https://mcp.ticktick.com через OAuth-коннектор агента
    # (владелец 19.10.2026). Сервер dev-mcp токенов TickTick не хранит и задач сам не создаёт.

    if result["channel"] == "windows":
        # Filebrowser presents this private local file. The toast itself has
        # fixed, non-private text and links only to the validated local UI.
        path = INBOX_PATH
        if not path.parent.is_dir():
            raise OSError("owner inbox directory unavailable")
        document = "intent/understood.md" if kind == "needs" else "intent/handover.md"
        line = (f"- {stamp[:16]} UTC | {project} | {title}: {cleaned} "
                f"| документ: {project}/{document}\n")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
        try:
            accepted = bool(_toast().show(OVERVIEW_URL, notice_kind=kind))
        except (OSError, ValueError, RuntimeError):
            accepted = False
        # WinRT acceptance is not evidence that the owner saw or approved it.
        result.update(inbox_written=True, toast_accepted=accepted, attempts=1,
                      delivery_state='api_accepted' if accepted else 'pending',
                      retry_at=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat())
    _record(store, "owner_channel.sent", project, result)
    return result


def retry_pending_toasts(store: Any) -> int:
    """Retry current explicit failures, never replay unverified historical notices."""
    with store.read() as tx:
        if tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_notices'"):
            return 0  # the intrinsic browser outbox owns current actionable notices
    now = _now()
    latest = {(r.get('project'),r.get('kind'),r.get('sha256')):r
              for r in _event_rows(store,'owner_channel.toast_attempt')}
    done = {(r.get('project'),r.get('kind'),r.get('sha256'))
            for r in _event_rows(store,'owner.done')}
    count = 0
    for sent in _event_rows(store,'owner_channel.sent'):
        key = (sent.get('project'),sent.get('kind'),sent.get('sha256'))
        current = latest.get(key,sent)
        if (sent.get('channel') != 'windows' or 'toast_accepted' not in sent
                or current.get('toast_accepted') is not False or key in done
                or str(current.get('retry_at','9999')) > now):
            continue
        with store.read() as tx:
            if not tx.one('SELECT 1 FROM projects WHERE name=? AND active=1',(sent['project'],)):
                continue
        attempts = int(current.get('attempts',1))+1
        try:
            accepted = bool(_toast().show(OVERVIEW_URL,notice_kind=sent['kind']))
        except (OSError, ValueError, RuntimeError):
            accepted = False
        _record(store,'owner_channel.toast_attempt',sent['project'],{
            'project':sent['project'],'kind':sent['kind'],'sha256':sent['sha256'],
            'toast_accepted':accepted,'attempts':attempts,
            'delivery_state':'api_accepted' if accepted else 'pending',
            'retry_at':(datetime.now(timezone.utc)+timedelta(
                seconds=min(3600,60*2**min(attempts-1,6)))).isoformat()})
        count += 1
        if count >= 2:
            break
    return count


def run_notice_retries(store: Any, stop_event: Any) -> None:
    """A slow Windows transport cannot hold the intent prover or controller."""
    import logging
    while not stop_event.is_set():
        try:
            retry_pending_toasts(store)
        except Exception:
            logging.getLogger(__name__).exception('owner notice retry deferred')
        stop_event.wait(30)


def notify_ready_ops_plans(store: Any) -> int:
    """Only a proved rollback plan can produce an approval request, never approval."""
    from .stages import _owner_stage_notice
    with store.read() as tx:
        rows=tx.query("SELECT o.id,o.project,o.spec,p.root FROM outcomes o "
                      "JOIN projects p ON p.name=o.project JOIN outcomes r ON r.id=o.parent_id "
                      "WHERE o.title='owner_approved' AND o.state='open' AND r.state='open' AND p.active=1")
    count=0
    for row in rows:
        dependencies=json.loads(row['spec']).get('depends_on',[])
        if not dependencies:
            continue
        with store.read() as tx:
            proven=True
            for dep in dependencies:
                supplier=tx.one("SELECT state,evidence_spec FROM outcomes WHERE id=?",(dep,))
                if not supplier or supplier['state']!='done':
                    proven=False
                    break
                expected=json.loads(supplier['evidence_spec'])
                if not tx.one("SELECT 1 FROM evidence WHERE outcome_id=? AND verdict='pass' "
                              "AND kind=? AND subject_version=?",
                              (dep,expected.get('kind'),expected.get('subject_version'))):
                    proven=False
                    break
        if not proven:
            continue
        plan=Path(row['root'])/'intent/rollback-plan.md'
        if (not plan.is_file() or plan.resolve()!=plan.absolute()
                or plan.stat().st_size > 48_000):
            continue
        digest=hashlib.sha256(plan.read_bytes()).hexdigest()
        from dev_mcp.owner.overview import current_ops_approval_requested
        if not current_ops_approval_requested(Path(row['root'])):
            continue
        notice=_owner_stage_notice('understood',plan)
        text=(f"Завершена приёмка стадии плана операции {row['id']}; "
              f"текущий файл SHA-256 {digest}. "
              "Нужно отдельное решение по этому плану в intent/rollback-plan.md. "
              "Уведомление не является разрешением на исполнение. "
              +(notice[1] if notice else 'Границы и откат описаны в проверенном плане.'))
        notify_owner(store,row['project'],'needs',text[:4096])
        count+=1
    return count


__all__ = ["notify_owner"]
