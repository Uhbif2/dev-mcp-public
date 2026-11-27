"""Сигналы поля -> поручения ведущему (LEAD-SYSTEM-DESIGN 3.4, 08.11.2026).

У Nova VPN петля поля идёт через устройства людей: бортовой самописец -> приёмник ->
отпечатки ошибок на узле (product/spec/FIELD-LOOP.md, ops/field). У local-library и
deep-research устройств нет, но есть журналы: лог прокси живой библиотеки, результаты
свидетелей, события выкатки. Здесь они читаются так же: повторяющаяся ошибка получает
отпечаток, новый отпечаток становится поручением ведущему проекта (источник `field`).

Проект объявляет источники в fleet-projects.toml (читает оператор, не ведущий):

    [projects."local-library".field]
    max_new = 3           # новых поручений за прогон (по умолчанию 3)
    min_count = 2         # сколько раз сигнал должен повториться (по умолчанию 2)
    every_min = 60        # не чаще (по умолчанию 60)
    quiet_days = 7        # done_when: сигнала нет столько дней
    sources = [
      {path = "C:/.../mcp_proxy_lib.log", kind = "log", since_days = 3},
      {kind = "events", event_kind = "live_deployed", bad_field = "ok", bad_values = [0, false]},
    ]

kind: `jsonl` (строка JSON с level error/critical/fatal или непустым error/exc), `log`
(блок от строки по `match` до следующей записи), `events` (события реестра этого проекта).
Один отпечаток -> одно поручение навсегда: дубль ищется по событиям `instruction`.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

MAX_NEW = 3
MIN_COUNT = 2
QUIET_DAYS = 7
TAIL_BYTES = 2_000_000       # читаем хвост файла, журналы растут годами
BLOCK_LINES = 40
SAMPLE = 3                   # столько примеров в тексте поручения

_LOG_MATCH = re.compile(r"^\[E |\bERROR\b|\bCRITICAL\b|Traceback \(most recent|Fatal error|крэш-луп")
_NEW_RECORD = re.compile(r"^(\[[A-Z] \d{4}-|INFO:|WARNING:|ERROR:|===|\d{4}-\d{2}-\d{2}[ T]\d{2}:)")
_TS = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)")
_NUM = re.compile(r"\d+")
_HEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/][\w\-.\\/ ]{3,}?(?=[\s\"'),:]|$)")
_ERR_LEVELS = {"error", "critical", "fatal", "err", "crit"}
_TS_KEYS = ("ts", "time", "timestamp", "at", "created_at")

_LAST_RUN: dict[str, float] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize(text: str) -> str:
    """Убрать из строки ошибки то, что меняется от раза к разу: время, числа, id, пути."""
    text = _TS.sub("", text)
    text = _HEX.sub("H", text)
    text = _PATH.sub("P", text)
    text = _NUM.sub("N", text)
    return re.sub(r"\s+", " ", text).strip()[:300]


def fingerprint(*parts: str) -> str:
    norm = "|".join(normalize(p) for p in parts if p and p.strip())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 1e9:
        return datetime.fromtimestamp(float(value), timezone.utc)
    if not isinstance(value, str):
        return None
    m = _TS.search(value)
    if not m:
        return None
    try:
        return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - TAIL_BYTES))
        data = fh.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def _resolve(root: Path, raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else root / p


def _jsonl_signals(path: Path, cutoff: datetime, mtime_ok: bool) -> list[dict[str, Any]]:
    out = []
    for line in _tail_lines(path):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        level = str(rec.get("level") or rec.get("severity") or "").lower()
        err = rec.get("error") or rec.get("exc") or rec.get("exception")
        if level not in _ERR_LEVELS and not err:
            continue
        ts = next((t for t in (_parse_ts(rec.get(k)) for k in _TS_KEYS) if t), None)
        if (ts and ts < cutoff) or (ts is None and not mtime_ok):
            continue
        head = str(err or rec.get("message") or rec.get("msg") or rec.get("event") or line)
        out.append({"first": head.splitlines()[0] if head else line, "last": "", "ts": ts, "raw": head})
    return out


def _log_signals(path: Path, cutoff: datetime, mtime_ok: bool, match: re.Pattern[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    last_ts: datetime | None = None
    for line in _tail_lines(path):
        ts = _parse_ts(line) if _NEW_RECORD.match(line) or _TS.match(line) else None
        if ts:
            last_ts = ts
        if cur is not None:
            if _NEW_RECORD.match(line) or len(cur["lines"]) >= BLOCK_LINES:
                out.append(cur)
                cur = None
            elif line.strip():
                cur["lines"].append(line)
        if cur is None and match.search(line):
            cur = {"ts": last_ts, "lines": [line]}
    if cur is not None:
        out.append(cur)
    res = []
    for b in out:
        if (b["ts"] and b["ts"] < cutoff) or (b["ts"] is None and not mtime_ok):
            continue
        res.append({"first": b["lines"][0], "last": b["lines"][-1] if len(b["lines"]) > 1 else "",
                    "ts": b["ts"], "raw": "\n".join(b["lines"][:6])})
    return res


def _events_signals(store: Any, project: str, src: dict[str, Any], cutoff: datetime) -> list[dict[str, Any]]:
    kind = str(src.get("event_kind") or "")
    field = str(src.get("bad_field") or "")
    if not kind or not field:
        return []
    bad = src.get("bad_values") or []
    with store.read() as tx:
        rows = tx.query("SELECT ts,payload FROM events WHERE kind=? AND project=? AND ts>=? ORDER BY id",
                        (kind, project, cutoff.isoformat()))
    out = []
    keys = src.get("text_fields") or ["summary", "detail", "reason", "id"]
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get(field) not in bad:
            continue
        ident = str(data.get("id") or data.get("instance") or "")
        text = " ".join(str(data.get(k))[:200] for k in keys if data.get(k))
        out.append({"first": f"{kind} {field}={data.get(field)} {ident}", "last": text[:200],
                    "ts": _parse_ts(row["ts"]), "raw": text[:300]})
    return out


def collect(store: Any, project: str, root: Path, cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Отпечаток -> {count, first, last, sample, where, ts_first, ts_last}."""
    groups: dict[str, dict[str, Any]] = {}
    for src in cfg.get("sources") or []:
        if not isinstance(src, dict):
            continue
        kind = str(src.get("kind") or "")
        cutoff = _now() - timedelta(days=float(src.get("since_days") or 3))
        where, signals = "", []
        try:
            if kind == "events":
                where = f"события реестра {src.get('event_kind')}"
                signals = _events_signals(store, project, src, cutoff)
            elif kind in ("jsonl", "log") and src.get("path"):
                path = _resolve(root, str(src["path"]))
                if not path.is_file():
                    continue
                where = path.as_posix()
                mtime_ok = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= cutoff
                if kind == "jsonl":
                    signals = _jsonl_signals(path, cutoff, mtime_ok)
                else:
                    pattern = re.compile(str(src["match"])) if src.get("match") else _LOG_MATCH
                    signals = _log_signals(path, cutoff, mtime_ok, pattern)
        except (OSError, re.error):
            continue
        for sig in signals:
            fp = fingerprint(kind, sig["first"], sig["last"])
            g = groups.setdefault(fp, {"count": 0, "first": sig["first"][:300], "last": sig["last"][:300],
                                       "sample": [], "where": where, "ts_first": sig["ts"], "ts_last": sig["ts"]})
            g["count"] += 1
            if len(g["sample"]) < SAMPLE and sig["raw"] not in g["sample"]:
                g["sample"].append(sig["raw"][:300])
            if sig["ts"]:
                g["ts_first"] = min(filter(None, [g["ts_first"], sig["ts"]]))
                g["ts_last"] = max(filter(None, [g["ts_last"], sig["ts"]]))
    return groups


def _known(store: Any, project: str) -> set[str]:
    with store.read() as tx:
        rows = tx.query("SELECT json_extract(payload,'$.fingerprint') AS fp FROM events "
                        "WHERE kind='instruction' AND project=? AND json_extract(payload,'$.fingerprint') IS NOT NULL",
                        (project,))
    return {str(r["fp"]) for r in rows}


def _day(ts: datetime | None) -> str:
    return ts.strftime("%d.%m %H:%M") if ts else "?"


def _register(store: Any, project: str, fp: str, g: dict[str, Any], quiet_days: int) -> dict[str, Any]:
    head = g["first"] + (f" ... {g['last']}" if g["last"] else "")
    text = (f"Сигнал поля [{fp}]: {head[:400]}\n"
            f"Повторов: {g['count']} (с {_day(g['ts_first'])} по {_day(g['ts_last'])} UTC). Где: {g['where']}.\n"
            "Примеры:\n" + "\n".join("  " + s.replace("\n", "\n  ") for s in g["sample"]))
    done_when = (f"сигнал {fp} не повторяется {quiet_days} дн. после исправления "
                 "либо объяснён в книге (declined с причиной)")
    mid = "m-" + uuid.uuid4().hex[:8]
    iid = "i-" + mid[2:]
    body = text + f"\nСделано, когда: {done_when}\nПоручение {iid}: закрой fleet_lead(action='instruction')."
    now = _now().isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO messages(id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,body,"
                  "created_at,delivered_at,acked_at) VALUES(?,?,NULL,?,0,'owner','','note',?,?,NULL,NULL)",
                  (mid, f"field:{fp}", f"{project}/TZ-LEAD", body, now),
                  event={"kind": "instruction", "project": project, "tz": "TZ-LEAD", "payload": {
                      "id": iid, "author": "field", "text": text[:4000], "want": "",
                      "done_when": done_when, "priority": 5, "source": "field", "fingerprint": fp,
                      "count": g["count"], "msg_id": mid}})
    return {"id": iid, "fingerprint": fp, "count": g["count"], "first": g["first"], "where": g["where"]}


def scan(store: Any, project: str, root: Path, cfg: dict[str, Any], *, dry: bool = False) -> list[dict[str, Any]]:
    """Новые отпечатки -> поручения ведущему (не больше max_new за прогон).

    dry=True ничего не пишет и возвращает то, что было бы создано."""
    groups = collect(store, project, root, cfg)
    known = _known(store, project)
    min_count = int(cfg.get("min_count") or MIN_COUNT)
    limit = int(cfg.get("max_new") or MAX_NEW)
    quiet = int(cfg.get("quiet_days") or QUIET_DAYS)
    fresh = sorted(((fp, g) for fp, g in groups.items() if fp not in known and g["count"] >= min_count),
                   key=lambda kv: (-kv[1]["count"], kv[0]))
    created = []
    for fp, g in fresh[:limit]:
        if dry:
            created.append({"id": "(dry)", "fingerprint": fp, "count": g["count"],
                            "first": g["first"], "where": g["where"]})
        else:
            created.append(_register(store, project, fp, g, quiet))
    return created


def tick(store: Any, project_info: Callable[[str], dict | None]) -> list[dict[str, Any]]:
    """Периодический вызов (контроллер/интегратор): все активные проекты с секцией field."""
    with store.read() as tx:
        projects = tx.query("SELECT name,root FROM projects WHERE active=1 AND root<>''")
    made: list[dict[str, Any]] = []
    for row in projects:
        name = str(row["name"])
        try:
            cfg = (project_info(name) or {}).get("field")
        except Exception:  # noqa: BLE001 - чужой конфиг не должен ронять цикл
            continue
        if not isinstance(cfg, dict) or not cfg.get("sources"):
            continue
        if time.monotonic() - _LAST_RUN.get(name, -1e9) < 60 * float(cfg.get("every_min") or 60):
            continue
        _LAST_RUN[name] = time.monotonic()
        try:
            made += [{"project": name, **m} for m in scan(store, name, Path(str(row["root"])), cfg)]
        except Exception:  # noqa: BLE001
            continue
    return made
