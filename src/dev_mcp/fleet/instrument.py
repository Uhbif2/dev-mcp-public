"""Приёмка прибора с отрицательным контролем (LEAD-SYSTEM-DESIGN 2.3, §2 Р1).

Прибор (свидетель обещания) строит рабочий флота, не ведущий. Доверие к прибору — не к его
автору, а к двум квитанциям одним и тем же байтом скрипта:
- **красный на поломке** — прогон в дереве с подложенной поломкой продукта (git worktree
  исполнителя) не даёт `works_live`;
- **зелёный на эталоне** — прогон в корне проекта даёт `works_live`.
Обе квитанции — событие `instrument_accepted` {cell, sha, red, green}. После него оператор
закрепляет `sha` в ячейке `fleet-projects.toml`; запуск с другим sha даёт `instrument_changed`
(witness.run_one), и такой замер переходом уровня не считается (product.state).

Прибор без приёмки замеряет, но строка брифа называет его: уровень по нему не доказан чужим
глазом. Закреплённый sha без квитанции отрицательного контроля — тоже строка брифа.

`python -m dev_mcp.fleet.instrument accept <проект> <ячейка> --broken <дерево с поломкой>`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

KIND = "instrument_accepted"


def _cell(project: str, cell_id: str) -> tuple[str, dict[str, Any]]:
    from . import goals, project_config
    root = project_config.project_root(project)
    if not root:
        raise ValueError(f"проект {project!r} без корня")
    for item in goals.project_info(project).get("witness") or []:
        if isinstance(item, dict) and item.get("id") == cell_id:
            return root, item
    raise ValueError(f"в {project!r} нет ячейки свидетеля {cell_id!r}")


def accept(store: Any, project: str, cell_id: str, broken_root: str) -> dict[str, Any]:
    """Два прогона одного прибора: поломка → не works_live, эталон → works_live."""
    from . import witness
    root, item = _cell(project, cell_id)
    item = {k: v for k, v in item.items() if k != "sha"}  # приёмка меряет текущий байт
    sha = witness.instrument_sha(root, item)
    if not sha or witness.instrument_sha(broken_root, item) != sha:
        raise ValueError("в дереве с поломкой другой байт прибора — сравнивать нечего")
    if Path(broken_root).resolve() == Path(root).resolve():
        raise ValueError("дерево с поломкой не может быть корнем проекта")
    red = witness.run_one(broken_root, item)
    green = witness.run_one(root, item)
    receipt = {
        "cell": cell_id, "obligation": str(item.get("obligation") or ""), "sha": sha,
        "red": {k: red.get(k) for k in ("verdict", "rc", "head", "summary")},
        "green": {k: green.get(k) for k in ("verdict", "rc", "head", "summary")},
        "broken_root": str(broken_root),
    }
    receipt["accepted"] = red.get("verdict") != "works_live" and green.get("verdict") == "works_live"
    with store.tx() as tx:
        tx.record_event(kind=KIND, project=project, tz=f"witness:{cell_id}", payload=receipt)
    return receipt


def audit(store: Any, project: str) -> dict[str, list[str]]:
    """Ячейки без приёмки; закреплённые sha без квитанции «красный на поломке»."""
    from . import goals
    cells = [c for c in goals.project_info(project).get("witness") or [] if isinstance(c, dict)]
    with store.read() as tx:
        rows = tx.query("SELECT payload FROM events WHERE kind=? AND project=?", (KIND, project))
    accepted: set[tuple[str, str]] = set()
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if data.get("accepted"):
            accepted.add((str(data.get("cell")), str(data.get("sha"))))
    unpinned, unproven = [], []
    for cell in cells:
        pinned = str(cell.get("sha") or "").strip().lower()
        if not pinned:
            unpinned.append(str(cell.get("id")))
        elif not any(c == cell.get("id") and s.startswith(pinned) for c, s in accepted):
            unproven.append(str(cell.get("id")))
    return {"unpinned": unpinned, "unproven": unproven}


def brief_lines(store: Any, project: str) -> list[str]:
    try:
        found = audit(store, project)
    except Exception:  # noqa: BLE001 — строка брифа необязательна
        return []
    lines = []
    if found["unpinned"]:
        lines.append(f"Приборы без приёмки (нет sha в ячейке): {len(found['unpinned'])} — "
                     + ", ".join(found["unpinned"][:6])
                     + ". Переход уровня по ним не проверен отрицательным контролем: приёмку прибора "
                       "выдай рабочему (fleet_lead issue), не правь прибор сам")
    if found["unproven"]:
        lines.append("Закреплённый sha без квитанции «красный на поломке»: "
                     + ", ".join(found["unproven"][:6]))
    return lines


def main(argv: list[str] | None = None) -> int:
    from .store import Store
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 5 or args[0] != "accept" or args[3] != "--broken":
        print(__doc__.strip().splitlines()[-1])
        return 2
    db = Path(__file__).resolve().parents[3] / "data" / "fleet" / "fleet.sqlite"
    receipt = accept(Store.open(str(db)), args[1], args[2], args[4])
    print(json.dumps(receipt, ensure_ascii=False, indent=1))
    if receipt["accepted"]:
        print(f'\nзакрепи в fleet-projects.toml, ячейка {args[2]}: sha = "{receipt["sha"]}"')
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
