"""Сводка флота для SessionStart-хука Claude Code: молчит, если здесь нечего решать.

Правила внимания (27.09.2026, после жалобы «сводка во всех чатах по всем проектам»):
- говорит только на новом контексте (source startup/clear), не на resume/compact;
- в папке проекта флота — только о ЭТОМ проекте; все проекты — только в пульте dev-mcp;
- платформу упоминает только когда она не в порядке;
- нет решений, красных гейтов и проблем платформы — полная тишина;
- тот же набор решений повторно не чаще раза в 12 ч (data/fleet/brief-seen.json).
Без --hook (ручной запуск) — сводка по всем проектам, без дедупликации.

Запуск: .venv\\Scripts\\python.exe tools\\fleet_session_brief.py
"""
from __future__ import annotations

import importlib.util
import sys
import re
import threading
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BUDGET_S = 4.5  # весь скрипт, включая импорт; хук не должен тормозить старт сессии
MAX_LINES = 15

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass


def _load_console() -> Any:
    spec = importlib.util.spec_from_file_location("fleet_console", HERE / "fleet.py")
    if spec is None or spec.loader is None:
        raise ImportError("tools/fleet.py не найден")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _platform(fleet: Any, out: dict[str, str]) -> None:
    try:
        out["line"] = fleet.platform_line(health_timeouts=(2.0,))
    except Exception as exc:  # noqa: BLE001
        out["line"] = f"платформа: проверка упала — {exc}"


SEEN_FILE = HERE.parent / "data" / "fleet" / "brief-seen.json"
REPEAT_AFTER_S = 12 * 3600  # тот же набор решений повторно — не чаще раза в 12 ч
SPEAK_ON = {"startup", "clear"}  # resume/compact — продолжение той же работы, не новый контекст


def _healthy(line: str) -> bool:
    low = line.casefold()
    return "dev:8766 жив" in low and "core:8765 жив" in low and "шлюз ок" in low


def brief(scope: str | None, db_path: Path | None = None) -> tuple[list[str], str]:
    """scope: имя проекта (сводка только о нём) или "*" (пульт dev-mcp — все проекты).
    Возвращает (строки, подпись набора); пустые строки — говорить нечего."""
    fleet = _load_console()
    platform: dict[str, str] = {}
    probe = threading.Thread(target=_platform, args=(fleet, platform), daemon=True)
    probe.start()

    project = None if scope == "*" else scope
    path = db_path or fleet.fleet_db_path(fleet.REPO)
    conn = fleet._open_existing(Path(path))
    try:
        conn.execute("PRAGMA busy_timeout=1000")
        # Только корневые ворота, которые правда держат работу: не звенья цепочки («агент ждёт агента»)
        # и не мёртвые заморозки. Главная строка — какие ворота нужны ВСЕ ВМЕСТЕ (27.09: телефон дали,
        # а 37 задач так и стояли за покупкой второго входа — пульт показывал это отдельными пунктами).
        all_items, clusters = fleet._decisions(conn, project)
        asks, _done, handling = fleet._judgment_state(conn, project)
        asked = {(a["project"], a["tz"]) for a in asks}  # и просьбы, и «ждёт условия» — не пункты владельцу
        # Владельцу — только то, что ждёт ЕГО: ворота, которые разбирает система (задача-суждение),
        # и ворота с готовой просьбой (она показана своей строкой) — не дублируем (07.10.2026).
        items = [i for i in all_items if i.get("gate") and int(i.get("needed_for") or 0) > 0
                 and (i["project"], i["tz"]) not in handling and (i["project"], i["tz"]) not in asked]
        ask_lines = fleet.proposal_lines(conn, project)
        red = fleet._red_gates(conn, project)
        missing = fleet.world_lines(conn, project)
        stuck = fleet.stuck_lines(conn, project, limit=0)
        dev_asks = fleet.dev_mcp_asks(conn) if scope == "*" else []
        lesson_asks: list[str] = []
        if scope == "*":  # уроки, которые не лечатся строкой памятки, — работа архитектора (Claude Code)
            try:
                sys.path.insert(0, str(HERE.parent / "src"))
                from dev_mcp.fleet import lessons

                lesson_asks = lessons.attention(lessons.ConnTx(conn))
            except Exception:  # noqa: BLE001 — сводка не падает из-за уроков
                lesson_asks = []
    finally:
        conn.close()

    probe.join(timeout=max(0.1, BUDGET_S - 1.5))
    lines: list[str] = []
    pline = platform.get("line", "")
    if pline and not _healthy(pline):
        lines.extend(pline.splitlines()[:2])  # платформа — только когда она не в порядке
    if red:
        lines.append(f"красный гейт: {', '.join(str(g.get('project', '')) for g in red[:3])}")
    lines.extend(missing)
    if dev_asks:
        lines.append(f"ждут правки dev-mcp (только Claude Code): {len(dev_asks)}")
        lines.extend("  " + a for a in dev_asks[:3])
    lines.extend(lesson_asks[:3])
    # Готовая работа есть, а выдать нечего — назвать, кто держит (общий датчик заторов, 07.10.2026).
    top = re.search(r"держит готовую работу: [^;]+ — (\d+)", stuck[-1]) if stuck else None
    if stuck and not stuck[0].startswith("готовых 0") and (
            " можно выдать 0;" in stuck[0] or (top and int(top.group(1)) >= 3)):
        lines.append("очередь стоит: " + stuck[0])
        lines.extend(stuck[1:])
    lines.extend(line for line in ask_lines if line.startswith("НУЖНО ОТ ТЕБЯ"))
    if items:
        lines.extend(fleet.format_stalls(clusters, 2))
        lines.extend(fleet.format_decisions(items, 3, compact=True))
        lines.append("подробно: fleet.cmd decisions")
    sig = "|".join(sorted(f"{i.get('project')}/{i.get('tz')}:{i.get('state')}" for i in items))
    sig += "#" + "|".join(sorted(f"{a['project']}/{a['tz']}" for a in asks))
    sig += "#" + "|".join(m.split(":")[0] for m in missing)
    sig += "#" + "|".join(lesson_asks[:3])
    sig += f"#red{len(red)}#ok{int(not pline or _healthy(pline))}"
    return lines[:MAX_LINES], sig


def _already_shown(scope: str, sig: str) -> bool:
    import json
    import time

    try:
        seen = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        seen = {}
    prev = seen.get(scope) or {}
    if prev.get("sig") == sig and time.time() - float(prev.get("ts", 0)) < REPEAT_AFTER_S:
        return True
    seen[scope] = {"sig": sig, "ts": time.time()}
    try:
        SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return False


def main(scope: str, *, dedup: bool) -> int:
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["lines"], result["sig"] = brief(scope)
        except Exception as exc:  # noqa: BLE001 — хук не имеет права падать
            result["error"] = f"флот: сводка недоступна — {type(exc).__name__}: {exc}"

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=BUDGET_S)
    if "lines" in result:
        if result["lines"] and not (dedup and _already_shown(scope, result["sig"])):
            print("\n".join(result["lines"]))
    elif "error" in result and not dedup:
        print(result["error"][:300])
    sys.stdout.flush()
    return 0


# Пульт dev-mcp — папка самого dev-mcp, где бы она ни лежала (08.10.2026 переезд в C:\hub).
FLEET_ROOTS_HINT = (HERE.parent.as_posix().casefold(), "projects/dev-mcp")


def _hook_scope() -> str | None:
    """Вход хука — JSON с cwd и source. None — молчать: чужой проект или не новый контекст.
    "*" — пульт dev-mcp; иначе имя проекта флота, в папке которого открыта сессия."""
    import json
    import sqlite3

    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    except Exception:  # noqa: BLE001
        return None
    if str(payload.get("source") or "startup") not in SPEAK_ON:
        return None
    norm = str(payload.get("cwd") or "").replace("\\", "/").casefold().rstrip("/")
    if any(h in norm for h in FLEET_ROOTS_HINT):
        return "*"
    try:
        db = HERE.parent / "data" / "fleet" / "fleet.sqlite"
        with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=1) as conn:
            rows = [(str(r[0]), str(r[1])) for r in conn.execute("SELECT name, root FROM projects WHERE active=1")]
    except Exception:  # noqa: BLE001
        return None
    for name, root in rows:
        r = root.replace("\\", "/").casefold().rstrip("/")
        if norm == r or norm.startswith(r + "/"):
            return name
    return None


if __name__ == "__main__":
    import os

    hook = "--hook" in sys.argv[1:]
    scope = _hook_scope() if hook else "*"
    if scope is None:
        os._exit(0)
    try:
        code = main(scope, dedup=hook)
    except BaseException as exc:  # noqa: BLE001
        if not hook:
            print(f"флот: сводка недоступна — {exc}")
        code = 0
    # os._exit: не ждать висящий daemon-поток проверки шлюза.
    os._exit(code)
