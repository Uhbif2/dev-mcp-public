"""Приёмка задачи флота «доставить уведомление владельцу в TickTick» (19.10.2026).

Агент ChatGPT создаёт задачу в TickTick своим официальным коннектором (OAuth, mcp.ticktick.com)
и пишет квитанцию — этот JSON. Скрипт проверяет только форму квитанции: существование задачи в
TickTick сверяет слепой ревьюер тем же коннектором (get_task_by_id). Токенов здесь нет и не будет.

    python -m dev_mcp.owner.ticktick_receipt <квитанция.json>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_NOTICE = re.compile(r"on-[a-f0-9]{24}$")
_ID = re.compile(r"[A-Za-z0-9_-]{6,64}$")
REQUIRED = ("notice", "ticktick_task_id", "ticktick_project_id", "title")


def check(path: Path) -> list[str]:
    if not path.is_file():
        return [f"нет квитанции {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"квитанция не JSON: {exc}"]
    if not isinstance(data, dict):
        return ["квитанция — не объект"]
    errors = [f"нет поля {key}" for key in REQUIRED
              if not isinstance(data.get(key), str) or not data[key].strip()]
    if errors:
        return errors
    if not _NOTICE.fullmatch(data["notice"]) or data["notice"] != path.stem:
        errors.append("notice не совпадает с именем файла")
    for key in ("ticktick_task_id", "ticktick_project_id"):
        if not _ID.fullmatch(data[key]):
            errors.append(f"{key} не похож на id TickTick")
    if data["ticktick_task_id"].startswith(("fake", "test", "example")):
        errors.append("ticktick_task_id выглядит выдуманным")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    errors = check(Path(argv[0]))
    for line in errors:
        print("FAIL:", line)
    if not errors:
        print("ok: квитанция TickTick в порядке")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
