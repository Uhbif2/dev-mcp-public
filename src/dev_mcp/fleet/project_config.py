"""Конфигурация проекта у самого проекта: `ops/lead/fleet.toml` поверх fleet-projects.toml.

Владелец 06.11.2026: администрирование проекта — у ведущего. Свидетели, документы канона,
зависимости кандидата и частота выкатки — дело проекта; раньше их правил только оператор в
общем файле платформы. Ведущий правит файл в своей зоне; платформа берёт из него только
ключи белого списка и проверяет форму. Порт и экземпляр живой поверхности, коммит, деревья
захватов, ресурсы мира остаются у оператора: они задевают другие проекты и транспорт.
Битый файл не ломает проект — действует общий конфиг, ошибка видна в `errors`.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

FILE = Path("ops") / "lead" / "fleet.toml"
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any], list[str]]] = {}
_roots: dict[str, tuple[float, str]] = {}
ROOT_TTL_S = 60.0


def _db() -> Path:
    import os
    return Path(os.environ.get("DEV_MCP_FLEET_DB")
                or Path(__file__).resolve().parents[3] / "data" / "fleet" / "fleet.sqlite")


def project_root(project: str) -> str | None:
    now = time.monotonic()
    hit = _roots.get(project)
    if hit and now - hit[0] < ROOT_TTL_S:
        return hit[1] or None
    root = ""
    try:
        with sqlite3.connect(_db().resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            row = db.execute("SELECT root FROM projects WHERE name=?", (project,)).fetchone()
            root = str(row[0]) if row and row[0] else ""
    except (sqlite3.Error, OSError):
        root = ""
    _roots[project] = (now, root)
    return root or None


def _witnesses(raw: Any, errors: list[str]) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        errors.append("witness: ожидается массив [[witness]]")
        return None
    out = []
    for item in raw:
        if not (isinstance(item, dict) and isinstance(item.get("id"), str)
                and isinstance(item.get("cmd"), list) and item["cmd"]
                and all(isinstance(x, str) for x in item["cmd"])):
            errors.append(f"witness: нужны id (строка) и cmd (непустой список строк): {item!r}"[:200])
            return None
        every = item.get("every_h", 6)
        if not isinstance(every, (int, float)) or not 0.25 <= float(every) <= 168:
            errors.append(f"witness {item['id']}: every_h от 0.25 до 168")
            return None
        out.append({k: item[k] for k in ("id", "cmd", "every_h", "obligation", "resources") if k in item})
    return out


def load(root: str) -> tuple[dict[str, Any], list[str]]:
    path = Path(root) / FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}, []
    with _lock:
        hit = _cache.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1], hit[2]
    errors: list[str] = []
    data: dict[str, Any] = {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raw, errors = {}, [f"{FILE.as_posix()}: {exc}"[:300]]
    if "witness" in raw:
        w = _witnesses(raw["witness"], errors)
        if w is not None:
            data["witness"] = w
    for key in ("canon", "deps"):
        if key in raw:
            if isinstance(raw[key], list) and all(isinstance(x, str) and ".." not in x for x in raw[key]):
                data[key] = list(raw[key])
            else:
                errors.append(f"{key}: список строк без '..'")
    if "gate" in raw:
        if isinstance(raw["gate"], str) and raw["gate"].strip():
            data["gate"] = raw["gate"].strip()
        else:
            errors.append("gate: непустая команда (снять гейт может только оператор)")
    live = raw.get("live")
    if isinstance(live, dict) and "every_min" in live:
        value = live["every_min"]
        if isinstance(value, (int, float)) and 5 <= float(value) <= 240:
            data["live_every_min"] = float(value)
        else:
            errors.append("live.every_min: от 5 до 240")
    unknown = sorted(set(raw) - {"witness", "canon", "deps", "gate", "live"})
    if unknown:
        errors.append(f"неизвестные ключи (оператору): {', '.join(unknown)}")
    with _lock:
        _cache[str(path)] = (mtime, data, errors)
    return data, errors


def overlay(project: str, info: dict[str, Any]) -> dict[str, Any]:
    root = project_root(project)
    if not root:
        return info
    data, errors = load(root)
    if not data and not errors:
        return info
    merged = dict(info)
    errors = list(errors)
    # 06.11 (LEAD-SYSTEM-DESIGN Р1, фаза 0.1): слияние только ДОБАВЛЯЕТ. Ведущий не может снять
    # или подменить ячейку свидетеля, гейт или документ канона оператора — иначе он ослабляет
    # проверку собственной работы (дефект 92a7855: overlay заменял witness/gate целиком).
    if "witness" in data:
        base = [w for w in (info.get("witness") or []) if isinstance(w, dict)]
        taken = {str(w.get("id")) for w in base}
        added = []
        for item in data["witness"]:
            if item["id"] in taken:
                errors.append(f"witness {item['id']}: ячейка оператора, подменить нельзя — "
                              "дай своей ячейке другой id")
                continue
            taken.add(item["id"])
            added.append({**item, "source": FILE.as_posix()})
        merged["witness"] = base + added
    if "canon" in data:
        from .lead_admin import CANON_DEFAULT
        merged["canon"] = list(dict.fromkeys([*(info.get("canon") or CANON_DEFAULT), *data["canon"]]))
    if "gate" in data:
        if str(info.get("gate") or "").strip():
            if data["gate"] != str(info["gate"]).strip():
                errors.append("gate: действует гейт оператора, проект его не заменяет "
                              "(добавь проверку свидетелем или приёмкой)")
        else:
            merged["gate"] = data["gate"]
    if "deps" in data:
        merged["deps"] = list(dict.fromkeys([*(info.get("deps") or []), *data["deps"]]))
    if "live_every_min" in data and isinstance(info.get("live"), dict):
        merged["live"] = {**info["live"], "every_min": data["live_every_min"]}
    merged["project_config"] = {"file": FILE.as_posix(), "errors": errors}
    return merged
