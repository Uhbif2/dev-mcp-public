"""Итог состояния флота первой строкой `fleet_owner status` и `fleet.cmd` (20.11.2026).

Зачем. Агент с ролью владельца видел в fleet_list «autostart=off», «extension=stale»,
«runnable=0», выключенные генераторы — и делал вывод «флот не работает, включите FleetChrome
Supervisor/Autostart и генераторы задач». Флаги не говорили, что выключено намеренно и что из
этого реально мешает. Здесь — однозначный итог (работает ли флот, сколько агентов действительно
работает по свежим предметным действиям — контракт п. 9, сколько задач и почему ждут) и по
каждому флагу: что значит, кто поставил, мешает ли сейчас, одно действие, если оно нужно.

Только чтение: реестр (соединение пульта), журнал audit.jsonl, jobs.json быстрых агентов,
снимки supervisor/browser-control. Строки fleet_list не меняются — их разбирает расширение.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

FRESH_MIN = 15
AUDIT_TAIL_BYTES = 2_000_000
# Предметные действия по ТЗ (контракт п. 9): чтение исходника, запись, run, checkpoint, сдача.
# Не считаются: fleet_list/fleet_extension_report (опрос расширения), fleet_quick report/park,
# fleet_take/fleet_join/fleet_presence/fleet_inbox/fleet_ack (координация), fleet_owner (пульт).
SUBSTANTIVE_TOOLS = frozenset({
    "read_file", "read_image", "read_images", "search_files", "list_dir",
    "edit_file", "write_file", "apply_patch", "run", "run_status", "run_stop", "server_start",
    "git_log", "git_status", "git_diff", "fetch_url",
    "fleet_checkpoint", "fleet_submit", "fleet_review", "fleet_lead", "fleet_finish",
    "fleet_block", "fleet_backup", "request_note",
})
GENERATORS_DECISION = "решение владельца 16.11.2026"


def _parse(raw: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def fresh_actors(audit: Path, *, now: datetime | None = None, minutes: int = FRESH_MIN,
                 tail_bytes: int = AUDIT_TAIL_BYTES) -> dict[str, str]:
    """agent → время последнего предметного вызова за `minutes` мин (по журналу audit)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)
    out: dict[str, str] = {}
    try:
        with Path(audit).open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - tail_bytes))
            lines = stream.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        agent = str(entry.get("agent") or "").strip()
        if (not agent or entry.get("decision") != "allow"
                or str(entry.get("tool") or "") not in SUBSTANTIVE_TOOLS):
            continue
        stamp = _parse(entry.get("ts"))
        if stamp is None or stamp < cutoff:
            continue
        iso = stamp.isoformat()
        if iso > out.get(agent, ""):
            out[agent] = iso
    return out


def _is_quick(agent: str) -> bool:
    head = agent[:1].upper()
    return head == "Q" and agent[1:2].isdigit()


def queue_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """ready (из них ждут needs/ресурсов), на ревью, заморожено, захваты; примеры ожидания."""
    from . import world

    rows = conn.execute("SELECT project,tz,state,needs,resources FROM tasks").fetchall()
    index = world.build_index(rows, world.provided_levels(conn, None), world.block_refs(conn, None))
    world_set = world.world_names()
    counts = {"ready": 0, "waiting": 0, "submitted": 0, "frozen": 0, "taken": 0}
    examples: list[str] = []
    for r in rows:
        state = str(r["state"])
        if state in counts:
            counts[state] += 1
        if state != "ready":
            continue
        info = index[(str(r["project"]), str(r["tz"]))]
        unmet = world.unmet_needs(str(r["project"]), info["needs"], info["resources"], index, world_set)
        if unmet:
            counts["waiting"] += 1
            if len(examples) < 3:
                examples.append(f"{r['project']}/{r['tz']} ждёт {', '.join(sorted(map(str, unmet))[:2])}")
    counts["claims"] = int(conn.execute("SELECT COUNT(*) FROM claims WHERE state='active'").fetchone()[0])
    counts["takeable"] = counts["ready"] - counts["waiting"]
    counts["examples"] = examples
    return counts


def _quick_open(db_path: Path) -> tuple[int, int]:
    """Открытые задания быстрых агентов и сколько из них ждут отправки сообщения."""
    try:
        from . import quick
        jobs = quick.Book(Path(db_path).parent).jobs().values()
    except Exception:
        return -1, -1
    open_jobs = [j for j in jobs if j.get("state") not in ("closed", "waiting")]
    return len(open_jobs), sum(1 for j in open_jobs if j.get("outbox"))


def _browser(db_path: Path, now: datetime) -> dict[str, Any]:
    from . import supervisor

    control = supervisor.browser_control(Path(db_path))
    status = supervisor._read_json(supervisor._status_path(Path(db_path)))
    sup = status.get("supervisor") if isinstance(status.get("supervisor"), dict) else {}
    ext = status.get("extension") if isinstance(status.get("extension"), dict) else {}
    seen = _parse(ext.get("seen_at"))
    age = None if seen is None else max(0, int((now - seen).total_seconds()))
    return {
        "autostart": bool(control.get("autostart")), "halt": control.get("halt"),
        "by": str(control.get("by") or ""), "at": str(control.get("updated_at") or ""),
        "browser_ready": bool(sup.get("browser_ready")), "action": str(sup.get("action") or ""),
        "runnable": int((sup.get("demand") or {}).get("runnable") or 0)
        if isinstance(sup.get("demand"), dict) else 0,
        "ext_age": age, "ext_version": str(ext.get("version") or ""),
        "stale_s": int(getattr(supervisor, "EXTENSION_STALE_S", 90)),
    }


def _generators_on() -> bool | None:
    try:
        from . import generators
        return generators.enabled()
    except Exception:
        return None


def _short(stamp: str) -> str:
    parsed = _parse(stamp)
    return parsed.strftime("%d.%m %H:%M UTC") if parsed else "время не записано"


def brief_lines(conn: sqlite3.Connection, db_path: Path, logs_dir: Path, *,
                now: datetime | None = None, actors: dict[str, str] | None = None,
                browser: dict[str, Any] | None = None, generators_on: bool | None | str = "auto",
                quick_open: tuple[int, int] | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    actors = fresh_actors(Path(logs_dir) / "audit.jsonl", now=now) if actors is None else actors
    q = queue_state(conn)
    try:
        b = _browser(Path(db_path), now) if browser is None else browser
    except Exception:
        b = {}
    gens = _generators_on() if generators_on == "auto" else generators_on
    quick_jobs, quick_outbox = _quick_open(Path(db_path)) if quick_open is None else quick_open

    quick_n = sum(1 for a in actors if _is_quick(a))
    regular_n = len(actors) - quick_n
    queue = (f"можно брать {q['takeable']}, ждут needs/ресурсов {q['waiting']}, на ревью {q['submitted']}, "
             f"заморожено {q['frozen']}; захватов {q['claims']}"
             + (f"; быстрых заданий открыто {quick_jobs}" if quick_jobs >= 0 else ""))
    halt = b.get("halt")
    if halt:
        reason = " ".join(str((halt or {}).get("reason", "")).split())[:120]
        head = (f"ИТОГ: флот ОСТАНОВЛЕН аварийно с {_short(str((halt or {}).get('at') or ''))}"
                f"{': ' + reason if reason else ''}. Снимает только владелец: окно расширения или "
                "`fleet.cmd browser resume`.")
    elif actors:
        head = (f"ИТОГ: флот работает — {len(actors)} агентов с предметными действиями за {FRESH_MIN} мин "
                f"(обычных {regular_n}, быстрых {quick_n}). Очередь: {queue}.")
    elif q["claims"] or quick_jobs > 0:
        head = (f"ИТОГ: работа взята, но предметных действий за {FRESH_MIN} мин нет — агенты думают "
                f"или замолчали; будильник (wake) и сторож живости расширения поднимают их сами. "
                f"Очередь: {queue}.")
    elif q["takeable"] > 0:
        head = (f"ИТОГ: работа есть ({q['takeable']} можно брать), но никто не действует — причина в строках "
                f"браузера/расширения ниже. Очередь: {queue}.")
    else:
        head = (f"ИТОГ: флот исправен и простаивает — брать нечего. Это не поломка: поставь написанные ТЗ "
                f"(`fleet_owner bootstrap ПРОЕКТ TZ-…`) или поручи ведущему. Очередь: {queue}.")
    lines = [head]
    if q["examples"]:
        lines.append("  ждут: " + "; ".join(q["examples"]))

    lines.append("Флаги (что значат, кто поставил, мешают ли сейчас):")
    if gens is False:
        lines.append("  - авто-задачи платформы (fix/review/request из заявок): выкл "
                     "(platform.task_generators.enabled). Регистрацию ТЗ это НЕ запрещает: "
                     "`fleet_owner bootstrap ПРОЕКТ TZ-…` — владелец и любой агент, запущенный "
                     "владельцем, сразу ready без проверок; ведущие — `fleet_lead issue`; отклонённое "
                     "ревью возвращает ту же ТЗ в работу. Не пиши владельцу «создание задач отключено».")
    elif gens is True:
        lines.append("  - генераторы задач: вкл — платформа сама ставит fix/review/request.")
    if b:
        who = f"поставил {b['by'] or 'не записано'}, {_short(b['at'])}"
        browser_ok = b.get("browser_ready")
        if not b.get("autostart"):
            lines.append(f"  - autostart=off ({who}): сервер сам не запускает и не перезапускает FleetChrome — "
                         "браузером управляет владелец. "
                         + ("Сейчас не мешает: браузер готов." if browser_ok else
                            "Сейчас мешает быстрым агентам: браузер не готов — владелец открывает FleetChrome."))
        else:
            lines.append(f"  - autostart=on: сервер сам поднимает FleetChrome при спросе ({who}).")
        age = b.get("ext_age")
        stale_s = int(b.get("stale_s") or 90)
        if age is None:
            lines.append("  - расширение: ни разу не отчитывалось — FleetChrome с fleet-tabs не запускался.")
        elif age <= stale_s:
            lines.append(f"  - расширение: живо (v{b.get('ext_version') or '?'}, отчёт {age} с назад).")
        else:
            mins = age // 60
            impact = ("мешает: открытые быстрые задания не отправляются" if quick_jobs > 0 or q["takeable"] > 0
                      else "сейчас не мешает: отправлять нечего")
            lines.append(f"  - extension=stale: последний отчёт {mins} мин назад (порог {stale_s} с); {impact}. "
                         "Если FleetChrome открыт — владелец перезагружает расширение "
                         "(`node extension\\fleet-tabs\\reload-extension.mjs`); закрыт намеренно — это норма.")
        lines.append(f"  - runnable={b.get('runnable', 0)}: столько задач надзиратель браузера (supervisor"
                     f"={b.get('action') or '?'}) может запустить сейчас; 0 при пустой очереди — норма.")
    if quick_outbox > 0:
        lines.append(f"  - быстрые задания ждут отправки: {quick_outbox} — причины в строках «! …» ниже.")
    lines.append("Не проси включать генераторы, autostart или Supervisor, если строка флага выше не говорит, "
                 "что он мешает сейчас.")
    return lines


def safe_brief_lines(conn: sqlite3.Connection, db_path: Path, logs_dir: Path) -> Iterable[str]:
    """Итог — дополнение: ошибка в нём не роняет статус пульта."""
    try:
        return brief_lines(conn, db_path, logs_dir)
    except Exception as exc:  # noqa: BLE001
        return [f"ИТОГ недоступен ({type(exc).__name__}: {str(exc)[:160]}) — смотри строки ниже."]
