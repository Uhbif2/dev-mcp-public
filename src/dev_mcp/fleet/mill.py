"""Мельница памяти в фоне dev-mcp: сбор опыта и раздача партий флоту без владельца.

Раз в INTERVAL_S: `mill run` по источникам (fleet, claude_code; дальше идёт с watermark, повтор
дешёвый), затем `mill tick`. tick пишет файлы ТЗ пар «автор → слепой ревьюер» и печатает new_tz,
здесь они ставятся в очередь через spec.sync_selected (как `fleet.cmd bootstrap`). Код мельницы —
отдельный проект флота (`projects.root` для MILL_PROJECT): dev-mcp его только запускает, в его
данные не пишет. Нет проекта, нет `tick` (старая мельница) или сбой — пропуск до следующего круга,
причина в событии `mill_error`. Выключатель — DEV_MCP_FLEET_MILL=0.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MILL_PROJECT = "Mill Works"
INTERVAL_S = float(os.environ.get("DEV_MCP_FLEET_MILL_INTERVAL_S", 600))
RUN_TIMEOUT_S = 3600.0
TICK_TIMEOUT_S = 300.0
SOURCES = ("fleet", "claude_code")

Runner = Callable[[list[str], Path, float], subprocess.CompletedProcess]


def _run(argv: list[str], cwd: Path, timeout_s: float) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONUTF8="1")
    return subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout_s)


def mill_root(store: Any) -> Path | None:
    with store.read() as tx:
        row = tx.one("SELECT root FROM projects WHERE name=?", (MILL_PROJECT,))
    root = Path(str(row["root"])) if row is not None else None
    return root if root is not None and (root / "mill" / "__main__.py").is_file() else None


def _last_json(text: str) -> dict[str, Any]:
    for line in reversed((text or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def record_failure(store: Any, error: str) -> str | None:
    """Escalate three identical consecutive errors once, quarantining their signature.

    Quarantine is an event rather than a destructive edit in the Mill Works tree.
    Distinct errors retain their own streak, and healthy ticks reset the streak.
    """
    signature = " ".join(str(error or "").split())[:500]
    if not signature:
        return None
    with store.tx() as tx:
        recent = tx.query(
            "SELECT kind,payload FROM events WHERE kind IN ('mill_error','mill_tick') "
            "ORDER BY id DESC LIMIT 2"
        )
        streak = 1
        for row in recent:
            if str(row["kind"]) != "mill_error":
                break
            prior = json.loads(str(row["payload"] or "{}"))
            if prior.get("error") != signature:
                break
            streak += 1
        tx.record_event(kind="mill_error", payload={"error": signature})
        if streak < 3:
            return None
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:8]
        tz = f"request:{digest}"
        exists = tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?", (MILL_PROJECT, tz))
        if exists is not None:
            return None
        row = tx.one("SELECT root FROM projects WHERE name=?", (MILL_PROJECT,))
        if row is None:
            return None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES(?,?,?,'ready',1,'request','','','[]','[]','[]','[]',?,?,?)",
            (MILL_PROJECT, tz, "Устранить повторяющуюся ошибку Mill Works",
             f"[fleet-request] Повторяющаяся ошибка (>=3): {signature}. "
             "Определи проблемный case/document, исправь обработчик и проверь "
             "продолжение остальных пар. Подпись изолирована, не повышай бюджет вслепую.",
             now, now),
            event={"kind": "mill_repair_open", "project": MILL_PROJECT, "tz": tz,
                   "payload": {"signature": signature, "streak": streak}},
        )
        tx.record_event(kind="mill_quarantine", project=MILL_PROJECT, tz=tz,
                        payload={"signature": signature, "repair": tz})
        return tz


def record_case_quarantine(store: Any, entries: list[dict[str, str]]) -> list[str]:
    """Mirror producer-isolated Cases as individually repairable, idempotent work.

    The producer's leases.sqlite is the authority for exclusion.  A fleet event
    alone cannot exclude a Case; here we preserve its address and queue repair.
    """
    created: list[str] = []
    with store.tx() as tx:
        for item in entries:
            if not isinstance(item, dict):
                continue
            case_id = item.get("case_id")
            reason = item.get("reason")
            if (not isinstance(case_id, str) or not case_id.startswith("case:")
                    or not 5 < len(case_id) <= 256
                    or not isinstance(reason, str) or not reason.strip()):
                continue
            digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
            tz = f"request:case-{digest}"
            if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?",
                      (MILL_PROJECT, tz)) is not None:
                continue
            if tx.one("SELECT 1 FROM projects WHERE name=?", (MILL_PROJECT,)) is None:
                continue
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            detail = " ".join(reason.split())[:280]
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES(?,?,?,'ready',1,'request','','','[]','[]','[]','[]',?,?,?)",
                (MILL_PROJECT, tz, "Исправить карантинный Case Mill Works",
                 f"[fleet-request] Case {case_id} изолирован в producer case_quarantine: "
                 f"{detail}. Разбери причину, проверь исправление, затем освободи Case "
                 "штатным release_quarantine; не удаляй сырые события и не повышай "
                 "бюджет вслепую.", now, now),
                event={"kind": "mill_case_repair_open", "project": MILL_PROJECT, "tz": tz,
                       "payload": {"case_id": case_id}},
            )
            tx.record_event(kind="mill_quarantine", project=MILL_PROJECT, tz=tz,
                            payload={"case_id": case_id, "reason": detail,
                                     "producer": "case_quarantine", "repair": tz})
            created.append(tz)
    return created


def tick(store: Any, *, policy: Any | None = None, run: Runner = _run,
         sources: tuple[str, ...] = SOURCES) -> dict[str, Any]:
    from .spec import sync_selected

    root = mill_root(store)
    if root is None:
        return {"skipped": "no_mill_project"}
    # Без диспетчера (`tick`) сбор только копил бы партии без задач флота: ждём его.
    probe = run(["py", "-3", "-m", "mill", "tick", "--dry"], root, TICK_TIMEOUT_S)
    if probe.returncode != 0:
        return {"skipped": "no_tick"}
    report: dict[str, Any] = {"run": {}}
    for source in sources:
        done = run(["py", "-3", "-m", "mill", "run", "--sources", source], root, RUN_TIMEOUT_S)
        report["run"][source] = done.returncode
    done = run(["py", "-3", "-m", "mill", "tick"], root, TICK_TIMEOUT_S)
    if done.returncode != 0:
        report["tick"] = f"rc={done.returncode}: {(done.stderr or done.stdout)[-300:]}"
        # A bad source Case must not prevent dispatch of already-materialized pairs.
        # The producer's Case selection still needs an upstream quarantine mechanism.
        out = {}
    else:
        out = _last_json(done.stdout)
    producer_quarantine = out.get("quarantine", [])
    quarantine = [
        {"case_id": item["case_id"], "reason": item["reason"]}
        for item in producer_quarantine
        if isinstance(item, dict) and isinstance(item.get("case_id"), str)
        and isinstance(item.get("reason"), str)
    ] if isinstance(producer_quarantine, list) else []
    if quarantine:
        record_case_quarantine(store, quarantine)
    report["quarantine"] = quarantine
    new_tz = [str(x) for x in out.get("new_tz", []) if str(x).strip()]
    # Самолечение: файл пары записан, а в очередь не попал (упал прошлый круг) — ставим сейчас.
    with store.read() as tx:
        queued = {str(r["tz"]) for r in tx.query(
            "SELECT tz FROM tasks WHERE project=?", (MILL_PROJECT,))}
    for path in sorted((root / "ToDo" / "tz").glob("TZ-M[AR]-*.md")):
        if path.stem not in queued and path.stem not in new_tz:
            new_tz.append(path.stem)
    if done.returncode == 0:
        report["tick"] = {k: out.get(k) for k in ("completed", "renewed", "open_pairs", "pool")}
    if new_tz:
        report["bootstrap"] = sync_selected(store, MILL_PROJECT, str(root), new_tz, policy=policy)
    report["new_tz"] = new_tz
    return report


def run_mill(db_path: str | Path, stop_event: threading.Event, *, policy: Any | None = None,
             interval_s: float = INTERVAL_S) -> None:
    from .store import Store

    store = Store.open(str(db_path))
    stop_event.wait(120)  # не мешать холодному старту сервера и флоту
    while not stop_event.is_set():
        try:
            report = tick(store, policy=policy)
            kind = "mill_error" if isinstance(report.get("tick"), str) else "mill_tick"
            if kind == "mill_error":
                record_failure(store, str(report["tick"]))
            else:
                with store.tx() as tx:
                    tx.record_event(kind=kind, payload=report)
        except Exception as exc:  # noqa: BLE001 — фон обязан жить
            try:
                record_failure(store, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
        stop_event.wait(interval_s)
