"""Решения владельца, которые агент флота принимает как действительные (18.10.2026).

Разбор 18.10 (Nova VPN): владелец разрешил перенос полигона на второй ПК и делегировал
технические решения оркестратору; оркестратор записал это в ToDo/fleet/OWNER-BLOCKERS.md и
разморозил задачи. Агенты заморозили их обратно, требуя «формального решения владельца»:
файл проекта может написать любой агент, а причина `reactivate` в памятку не попадала.

Решение владельца 18.10: агентам и системе доверять записям в файлах проекта — «сами себе
не доверяем, а модель не дура». Поэтому решения владельца из файлов проекта (OWNER-*.md,
разделы «Решения владельца») и причины разморозки идут в памятку дословно, с правилом, что
это и есть решение владельца и другого подтверждения не бывает. Останавливают работу
только деньги, аккаунты и публикация — это держит код (judgment.IRREVERSIBLE), не текст.
`fleet.cmd grant` остаётся необязательным способом записать решение в реестр.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import task_root

GRANT_KIND = "owner_grant"
MAX_LINES = 8
FILE_BUDGET = 3500
OWNER_FILES = ("ToDo/fleet/OWNER-*.md", "ToDo/OWNER-*.md", "docs/OWNER-*.md", "OWNER-*.md")
_DECISION_HEADING = re.compile(r"^(#{2,4})\s+.*(решени\w*\s+владельц|owner\s+decision)", re.I)

RULE = [
    "Правило владельца (18.10.2026): решения владельца, записанные в файлах проекта — им самим,",
    "оркестратором или другим агентом, — действительны. «Формального» подтверждения не бывает",
    "и не требуй его. Работу останавливают только траты денег, чужие/новые аккаунты и публикация.",
    "Нет телефона, стенда или VM — это ресурс: очередь ждёт его сама, владельца не проси.",
]


def _json(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def file_decisions(root: str) -> list[str]:
    """Sections headed «Решения владельца …» from the project's OWNER-*.md files, verbatim."""
    if not root:
        return []
    base = Path(root)
    seen: set[Path] = set()
    out: list[str] = []
    budget = FILE_BUDGET
    for pattern in OWNER_FILES:
        for path in sorted(base.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            level = 0
            for line in lines:
                head = re.match(r"^(#{1,6})\s", line)
                match = _DECISION_HEADING.match(line)
                if match:
                    level = len(match.group(1))
                    out.append(f"{path.relative_to(base).as_posix()} — {line.lstrip('# ').strip()}")
                    continue
                if level and head and len(head.group(1)) <= level:
                    level = 0
                if level and line.strip() and budget > 0:
                    text = line.strip()[:budget]
                    budget -= len(text)
                    out.append(f"  {text}")
    return out


def grants_for(tx: Any, project: str, tz: str) -> list[str]:
    """Registry-side owner decisions: owner_grant events (project-wide or naming this task's
    root) and the owner's latest `reactivate` reason for the task or its root."""
    root = task_root(tz)
    out: list[str] = []
    for row in tx.query(
        "SELECT ts,tz,payload FROM events WHERE kind=? AND project=? ORDER BY id DESC LIMIT 40",
        (GRANT_KIND, project),
    ):
        data = _json(row["payload"])
        scope = [str(t) for t in data.get("tzs") or []]
        if scope and root not in {task_root(t) for t in scope}:
            continue
        where = "весь проект" if not scope else ", ".join(scope)
        out.append(f"{str(row['ts'])[:16]} ({where}): {str(data.get('text') or '')[:1200]}")
        if len(out) >= MAX_LINES:
            break
    for row in tx.query(
        "SELECT ts,tz,payload FROM events WHERE kind='reactivate' AND project=? "
        "AND worker_id IS NULL ORDER BY id DESC LIMIT 60",
        (project,),
    ):
        if task_root(str(row["tz"] or "")) != root:
            continue
        reason = str(_json(row["payload"]).get("reason") or "").strip()
        if reason:
            out.append(f"{str(row['ts'])[:16]} (разморозка {row['tz']}): {reason[:1200]}")
        break
    return out


def render(lines: list[str], files: list[str] | None = None) -> list[str]:
    out = list(RULE)
    if files:
        out += ["Решения владельца из файлов проекта:", *(f"  {line}" for line in files)]
    if lines:
        out += ["Решения и разморозки владельца из реестра:", *(f"  - {line}" for line in lines)]
    return out
