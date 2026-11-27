"""Точка продолжения ведущего, собранная из реестра (08.11, LEAD-SYSTEM-DESIGN 2.1).

STATE.md — заметки самого ведущего и теряется; то, что реестр знает точно (вехи и «забытое»,
контрольные точки, поручения владельца, препятствия, красные O-*), сервер показывает в брифе сам.
Только чтение; каждый блок изолирован — сбой одного не роняет остальные.
"""
from __future__ import annotations

import json
from typing import Any

from . import lead

BUDGET = 2500
_OPEN_STATUSES = ("open", "noted", "accepted")


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _payload(row: Any) -> dict[str, Any]:
    try:
        data = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _lead_rows(store: Any, project: str, kind: str, limit: int) -> list[Any]:
    with store.read() as tx:
        rows = tx.query("SELECT ts,tz,payload FROM events WHERE project=? AND kind=? "
                        "AND (UPPER(tz) LIKE 'TZ-LEAD%' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%') "
                        "ORDER BY id DESC LIMIT ?", (project, kind, limit))
    return [r for r in rows if lead.is_lead(str(r["tz"]))]


def _milestones(store: Any, project: str) -> list[str]:
    out: list[str] = []
    for row in _lead_rows(store, project, "submit", 40):
        claim = lead.milestone_claim(str(_payload(row).get("note") or ""))
        if claim is None:
            continue
        move = f"{claim['from']}→{claim['to']}" if claim["from"] else f"→{claim['to']}"
        out.append(f"  - {str(row['ts'])[:16]} {claim['obligation']} {move}; забытое: "
                   f"{_short(claim['forgotten'], 110) or 'не указано'}")
        if len(out) == 3:
            break
    return ["Последние вехи (из сдач ведущего):", *out] if out else []


def _checkpoints(store: Any, project: str) -> list[str]:
    out: list[str] = []
    for row in _lead_rows(store, project, "checkpoint", 3):
        data = _payload(row)
        out.append(f"  - {str(row['ts'])[:16]} сделано: {_short(data.get('done'), 90)}; "
                   f"дальше: {_short(data.get('next'), 90)}")
    return ["Последние контрольные точки ведущего:", *out] if out else []


def _instructions(store: Any, project: str) -> list[str]:
    from . import instructions
    open_ = [i for i in instructions.items(store, project) if i.get("status") in _OPEN_STATUSES]
    if not open_:
        return []
    return [f"Открытых поручений владельца: {len(open_)}",  # 08.11: свежие сверху — старый хвост виден счётом
            *(f"  - [{i['id']}] {i['status']}: {_short(i.get('text'), 90)}" for i in open_[-5:][::-1])]


def _obstacles(store: Any, project: str) -> list[str]:
    from . import obstacles
    open_ = obstacles.open_items(store, project)
    if not open_:
        return []
    return [f"Открытых препятствий: {len(open_)} ({', '.join(str(i['id']) for i in open_[:6])})"]


def _outcomes(store: Any, project: str) -> list[str]:
    from . import product
    items = product.state(store, project)
    bad = [i for i in items if i.get("broken") or i.get("stale") or not i.get("met")]
    if not bad:
        return []
    counts = {"сломано": [], "устарело": [], "не замерено": [], "ниже требуемого": []}
    for i in bad:
        key = ("сломано" if i.get("broken") else "устарело" if i.get("stale") else
               "не замерено" if not i.get("measured") else "ниже требуемого")
        counts[key].append(str(i["id"]))
    parts = [f"{k} {len(v)} ({', '.join(v[:3])}{'…' if len(v) > 3 else ''})" for k, v in counts.items() if v]
    return [f"O-* не в зелёном: {len(bad)} из {len(items)}: " + "; ".join(parts)]


STATE_LIMIT = 3 * 1024


def _state_size(store: Any, project: str) -> list[str]:
    """08.11 (3.1): STATE.md — только текущее; лимит не запрет, а строка, пока его превышают."""
    from pathlib import Path
    with store.read() as tx:
        root = tx.scalar("SELECT root FROM projects WHERE name=?", (project,))
    path = Path(str(root or "")) / "ToDo" / "lead" / "STATE.md"
    size = path.stat().st_size if root and path.is_file() else 0
    if size <= STATE_LIMIT:
        return []
    return [f"STATE.md {size // 1024} КБ > 3 КБ: оставь только текущее — история уже в реестре "
            "(строки выше) и в ToDo/lead/log/."]


def continuity_lines(store: Any, project: str) -> list[str]:
    """Короткие строки брифа (≤ ~2500 знаков); пустой реестр — пустой список."""
    out: list[str] = []
    for block in (_state_size, _milestones, _checkpoints, _instructions, _obstacles, _outcomes):
        try:
            lines = block(store, project)
        except Exception:  # noqa: BLE001 — один блок не роняет остальные
            continue
        if lines and sum(len(x) + 1 for x in [*out, *lines]) <= BUDGET:
            out.extend(lines)
    return out
