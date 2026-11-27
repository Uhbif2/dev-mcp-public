"""Atomic rolling activity history sampled by extension reports."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .flow import agent_states

_LOCK = threading.Lock()
_DAY = 86400
FIELDS = ("working_regular", "working_quick", "waiting", "open_quick")


def read(path: Path, now: float | None = None) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            ts = float(row["ts"])
            if now - _DAY <= ts <= now and all(
                isinstance(row[k], int) and row[k] >= 0 for k in FIELDS
            ):
                result.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(result, key=lambda row: row["ts"])


def append(path: Path, point: dict, now: float | None = None) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    with _LOCK:
        rows = read(path, now=now)
        if rows and int(rows[-1]["ts"] // 60) >= int(now // 60):
            return rows
        rows.append({"ts": int(now), **{k: int(point[k]) for k in FIELDS}})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as file:
                tmp = file.name
                file.write("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows))
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)
            tmp = None
        finally:
            if tmp is not None:
                os.unlink(tmp)
        return rows


def history_path(store) -> Path:
    return Path(store.path).parent / "activity-24h.jsonl"


def sample(store) -> list[dict]:
    from . import quick

    now = datetime.now(timezone.utc)
    counts = agent_states(store, now=now)
    orders = quick.orders(quick.Book(Path(store.path).parent))
    opened = [j for j in orders if not j.get("close") and
              re.fullmatch(r"pult-q\d+(?:-[0-9a-z]+)?", str(j.get("label", "")), re.I)]
    with store.read() as tx:
        workers = tx.query("SELECT id,first_seen,last_seen FROM workers WHERE kind='quick' AND revoked_at IS NULL")
    recent = set()
    for w in workers:
        try:
            seen = datetime.fromisoformat(w["last_seen"])
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age = (now - seen.astimezone(timezone.utc)).total_seconds()
            if w["last_seen"] != w["first_seen"] and 0 <= age <= 120:
                recent.add(str(w["id"]).upper())
        except (ValueError, TypeError):
            pass
    point = {"working_regular": counts["working"] + counts["thinking"],
             "working_quick": sum(str(j.get("id", "")).upper() in recent for j in opened),
             "waiting": counts["waiting"], "open_quick": len(opened)}
    return append(history_path(store), point, now.timestamp())
