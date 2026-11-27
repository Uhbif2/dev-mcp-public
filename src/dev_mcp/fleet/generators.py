"""Global opt-in for automatic fleet task generation.

Only an explicit owner bootstrap, lead issue, and the initial review of an
already submitted task bypass this switch.  All other task inserts are gated
at the Store transaction boundary, including producers called outside ticks.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


def enabled() -> bool:
    from .goals import _load_doc

    value = _load_doc().get("platform", {}).get("task_generators", {})
    return isinstance(value, dict) and value.get("enabled") is True


_TASK_INSERT = re.compile(r"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"\x60]?tasks\b", re.I)
_TASK_UPDATE = re.compile(r"^\s*UPDATE\s+[\"\x60]?tasks\s+SET\b", re.I)
_DIRECT = {"lead_issue", "review_open"}
_AUTO_REQUEUE = {"auto_reactivate", "queue_source_repair_reopened",
                 "draft_fix_refresh", "triage_apply_repair_reopened"}


def permitted(sql: str, event: Mapping[str, Any] | str) -> bool:
    """False only for an automatic INSERT into fleet.tasks while switched off."""
    is_insert = bool(_TASK_INSERT.match(sql))
    is_update = bool(_TASK_UPDATE.match(sql))
    if not is_insert and not is_update or enabled():
        return True
    kind = str(event.get("kind") or "") if isinstance(event, Mapping) else event
    if is_update:
        return kind not in _AUTO_REQUEUE
    if isinstance(event, str):
        return event in _DIRECT
    if kind in _DIRECT:
        return True
    if kind == "run" and event.get("tool") == "sync_backlog":
        payload = event.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = {}
        # selected_insert is issued by the explicit bootstrap/selected-TZ path.
        return isinstance(payload, dict) and payload.get("action") == "selected_insert"
    return False
