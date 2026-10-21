"""Bounded, idempotent account-overload reports from the FleetChrome extension.

Only aggregate counts and timestamps are accepted. No tab URL, label, chat ID,
page content or browser identity is persisted in these events.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

_KINDS = {"overload": "account_overload", "recovered": "account_recovered"}
_OVERLOAD_KEYS = {"type", "at", "chats_1h", "chats_3h", "chats_5h", "pause_min"}
_RECOVERED_KEYS = {"type", "at", "overload_at"}


def _integer(value: Any, *, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid account_event {name}")
    return value


def _parse(raw: str, now_ms: int) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, str) or not 1 <= len(raw.encode("utf-8")) <= 512:
        raise ValueError("invalid account_event size")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid account_event JSON") from exc
    if not isinstance(payload, dict) or payload.get("type") not in _KINDS:
        raise ValueError("invalid account_event type")
    name = payload["type"]
    if set(payload) != (_OVERLOAD_KEYS if name == "overload" else _RECOVERED_KEYS):
        raise ValueError("invalid account_event fields")
    at = _integer(payload["at"], low=now_ms - 48 * 3_600_000,
                  high=now_ms + 5 * 60_000, name="at")
    if name == "overload":
        for key in ("chats_1h", "chats_3h", "chats_5h"):
            _integer(payload[key], low=0, high=10000, name=key)
        _integer(payload["pause_min"], low=1, high=120, name="pause_min")
        if not (payload["chats_1h"] <= payload["chats_3h"] <= payload["chats_5h"]):
            raise ValueError("invalid account_event window counts")
    else:
        begin = _integer(payload["overload_at"], low=now_ms - 48 * 3_600_000,
                         high=at, name="overload_at")
        payload["pause_min"] = round((at - begin) / 60_000, 2)
    return _KINDS[name], payload


def record(store: Any, account_event: str) -> bool:
    """Persist one browser observation, deduplicated across heartbeat retries.

    Returns True for an inserted event, False for a replay. Raises ValueError
    on malformed input; no browser-provided event kind or free text is trusted.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    kind, payload = _parse(account_event, now_ms)
    with store.tx() as tx:
        recent = tx.query("SELECT payload FROM events WHERE kind=? ORDER BY id DESC LIMIT 512",
                          (kind,))
        for row in recent:
            try:
                old = json.loads(row["payload"])
            except (TypeError, ValueError, KeyError):
                continue
            if isinstance(old, dict) and old.get("at") == payload["at"]:
                return False
        tx.record_event(kind=kind, tool="fleet_extension_report", payload=payload)
    return True


def metrics(conn: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Account limit summary over the preceding rolling 24 hours."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()
    rows = conn.execute(
        "SELECT kind,payload FROM events WHERE kind IN "
        "('account_overload','account_recovered') AND ts>=?", (cutoff,)
    ).fetchall()
    overloads = recoveries = 0
    chats: list[float] = []
    pauses: list[float] = []
    for kind, raw in rows:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if kind == "account_overload" and type(payload.get("chats_3h")) is int:
            overloads += 1
            chats.append(float(payload["chats_3h"]))
        if kind == "account_recovered" and type(payload.get("pause_min")) in (int, float):
            recoveries += 1
            pauses.append(float(payload["pause_min"]))
    return {
        "overloads": overloads,
        "recoveries": recoveries,
        "median_pause_min": statistics.median(pauses) if pauses else None,
        "median_chats_3h": statistics.median(chats) if chats else None,
    }
