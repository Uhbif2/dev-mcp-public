"""Свидетели обещаний: система сама и регулярно проверяет, работает ли обещанное человеку.

Владелец, 04.11.2026: ведущий не замечал очевидного — истёкший манифест обновлений неделю
останавливал автообновление всех Windows-клиентов; `promote` не мог пройти на настоящей сводке
приёмника; «обновился сам» оказалось запуском через SSH; живые ворота были красными по четырём
причинам, а итог назвал одну. Общая причина — проверка держится на внимании агента и на
приёмке кусочков, а не на регулярном замере обещания целиком. Отсюда:

- свидетель — команда проекта, которая замеряет одно обещание вживую и последней строкой stdout
  печатает JSON `{"verdict": "works_live|broken|unknown", "summary": "..."}`;
- `reason` принимается вместо пустого summary, `next_step` доставляется ведущему;
  явный unknown с rc=2 — открытые ворота доказательства, works_live требует rc=0;
- список свидетелей задаёт оператор/владелец в `fleet-projects.toml` (как `gate`): агент не может
  подсунуть серверу команду;
- сервер гоняет их сам по расписанию, когда их ресурсы мира на связи, и пишет `witness_result`;
- памятка ведущего начинается с красных и отсутствующих свидетелей: «сломалось без тебя» и
  «обещание никто не замеряет» видны в первом же ответе, а не после чьей-то догадки.

```toml
[[projects."Nova VPN".witness]]
id = "android-update"
obligation = "O-android-update"
cmd = ["py", "-3", "ops/pilot/android_update_witness.py"]
every_h = 3
resources = ["телефон"]
```
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

KIND = "witness_result"
VERDICTS = ("works_live", "broken", "unknown")
# 06.11 (LEAD-SYSTEM-DESIGN Р1): прибор — вне зоны записи ведущего. Ведущий за ночь 150 раз
# правил свой свидетель без единого продуктового файла; «позеленело» нельзя было отличить
# от «поправили градусник». Приборы строят рабочие (fleet_lead issue), ведущему — отказ стража.
INSTRUMENT_DIRS = ("ops/witness/", "tools/witness/")
INSTRUMENT_FILES = ("ops/lead/ledger_check.py",)
LEVELS = ("L0", "L1", "L2", "L3", "L4")
TIMEOUT_S = 300
_lock = threading.Lock()
_running = False
_last_started = 0.0
TICK_EVERY_S = 120.0


def _config() -> dict[str, list[dict[str, Any]]]:
    from . import goals
    out: dict[str, list[dict[str, Any]]] = {}
    for project, info in goals._load().items():
        if isinstance(info, dict):
            try:  # ячейки проекта (ops/lead/fleet.toml) добавляются к операторским, не заменяют их
                info = goals.project_info(project)
            except Exception:  # noqa: BLE001 — битый конфиг проекта не гасит свидетелей оператора
                pass
        items = info.get("witness") if isinstance(info, dict) else None
        if not isinstance(items, list):
            continue
        valid = []
        for item in items:
            if (isinstance(item, dict) and isinstance(item.get("id"), str)
                    and isinstance(item.get("cmd"), list) and item["cmd"]
                    and all(isinstance(a, str) for a in item["cmd"])):
                valid.append(item)
        if valid:
            out[project] = valid
    return out


def _latest(tx: Any, project: str) -> dict[str, dict[str, Any]]:
    rows = tx.query("SELECT ts,payload FROM events WHERE kind=? AND project=? ORDER BY id",
                    (KIND, project))
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            payload["ts"] = str(row["ts"])
            previous = latest.get(payload["id"])
            if previous is not None:
                payload["previous_verdict"] = previous.get("verdict")
            latest[payload["id"]] = payload
    return latest


def _parse(stdout: str) -> dict[str, str] | None:
    for line in reversed([x.strip() for x in stdout.splitlines() if x.strip()]):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("verdict") in VERDICTS:
            return {"verdict": str(data["verdict"]),
                    "summary": str(data.get("summary") or data.get("reason") or "")[:400],
                    "next_step": str(data.get("next_step") or "")[:400]}
        break
    return None


def _script(item: dict[str, Any]) -> str | None:
    argv = [str(a) for a in item.get("cmd") or []]
    return next((a for a in argv[1:] if a.endswith(".py")), None)


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").casefold().strip()


def instrument_sha(root: str, item: dict[str, Any]) -> str:
    """sha256 прибора: байты скрипта ячейки (без скрипта — её команды)."""
    import hashlib
    script = _script(item)
    try:
        data = (Path(root) / script).read_bytes() if script else json.dumps(item.get("cmd")).encode()
    except OSError:
        return ""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _head(root: str) -> str:
    try:
        from .live_deploy import _git_head
        return str(_git_head(root)[0] or "")
    except Exception:  # noqa: BLE001 — квитанция без HEAD лучше, чем без замера
        return ""


def instrument_rel(project: str, path: str) -> str:
    """Путь прибора проекта (относительный) или "" — для стража записи ведущего."""
    from . import goals, project_config
    root = project_config.project_root(project)
    if not root:
        return ""
    prefix = _norm(root).rstrip("/") + "/"
    value = _norm(path)
    if not value.startswith(prefix):
        return ""
    rel = value[len(prefix):]
    if rel.startswith(INSTRUMENT_DIRS) or rel in INSTRUMENT_FILES:
        return rel
    try:
        info = goals.project_info(project)
    except Exception:  # noqa: BLE001
        info = {}
    scripts = {_norm(_script(w) or "").lstrip("./") for w in info.get("witness") or [] if isinstance(w, dict)}
    scripts |= {_norm(x).strip("/") for x in info.get("instruments") or [] if isinstance(x, str)}
    scripts.discard("")
    return rel if any(rel == s or rel.startswith(s + "/") for s in scripts) else ""


def run_one(root: str, item: dict[str, Any]) -> dict[str, Any]:
    """Запустить свидетеля в корне проекта. Нет файла скрипта — это пробел, а не ошибка среды.

    Квитанция несёт sha прибора, команду и HEAD проекта (Р1). Ячейка с закреплённым `sha`
    при другом приборе даёт `instrument_changed`: такой замер не переход уровня."""
    argv = [str(a) for a in item["cmd"]]
    script = _script(item)
    base = {"id": item["id"], "obligation": str(item.get("obligation") or ""), "cmd": argv,
            "level": str(item.get("level") or ""), "instrument_sha": instrument_sha(root, item),
            "head": _head(root)}
    if item.get("source"):
        base["source"] = str(item["source"])
    if script and not (Path(root) / script).is_file():
        return {**base, "verdict": "missing", "rc": None,
                "summary": f"свидетеля нет: {script} не существует — обещание никто не замеряет"}
    pinned = str(item.get("sha") or "").strip().lower()
    if pinned and not base["instrument_sha"].startswith(pinned):
        return {**base, "verdict": "instrument_changed", "rc": None, "pinned_sha": pinned,
                "summary": (f"прибор изменён после приёмки (закреплён {pinned[:12]}, сейчас "
                            f"{base['instrument_sha'][:12]}): замер не засчитывается, нужна приёмка "
                            "прибора с отрицательным контролем и новый sha в ячейке")}
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=float(item.get("timeout_s") or TIMEOUT_S))
    except subprocess.TimeoutExpired:
        return {**base, "verdict": "error", "rc": None, "summary": "таймаут свидетеля"}
    except OSError as exc:
        return {**base, "verdict": "error", "rc": None, "summary": f"{type(exc).__name__}: {exc}"[:300]}
    parsed = _parse(proc.stdout)
    verdict = parsed["verdict"] if parsed else "unknown"
    summary = parsed["summary"] if parsed else "свидетель не напечатал JSON-вердикт последней строкой"
    # rc=2 + explicit unknown is an open evidence gate, not a failed launch.
    # A nonzero process can never certify works_live, even if it printed that first.
    if (proc.returncode != 0 and
            (verdict == "works_live" or not parsed or
             (verdict == "unknown" and proc.returncode != 2))):
        verdict = "error"
        summary = (summary + " | " + proc.stderr.strip()[-300:]).strip(" |")
        if not proc.stderr.strip():
            summary = (summary + f" | exit={proc.returncode}").strip(" |")
    return {**base, "verdict": verdict, "rc": proc.returncode, "summary": summary,
            "next_step": parsed["next_step"] if parsed else "",
            "duration_s": round(time.monotonic() - started, 1)}


def due(store: Any, *, now: datetime | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    from . import world
    now = now or datetime.now(timezone.utc)
    cfg = _config()
    out = []
    with store.read() as tx:
        for project, items in cfg.items():
            row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
            if row is None:
                continue
            latest = _latest(tx, project)
            for item in items:
                last = latest.get(item["id"])
                every = timedelta(hours=float(item.get("every_h") or 6))
                if last is not None:
                    try:
                        if datetime.fromisoformat(last["ts"]) + every > now:
                            continue
                    except ValueError:
                        pass
                if world.down_resources(tx, item.get("resources") or []):
                    continue  # Ресурса нет — замер не делаем и не выдаём за провал.
                out.append((project, str(row["root"]), item))
    return out


def tick(store: Any, *, sync: bool = False) -> list[dict[str, Any]]:
    """Шаг контроллера: не чаще TICK_EVERY_S, свидетели по одному, вне потока контроллера."""
    global _running, _last_started
    if os.environ.get("DEV_MCP_FLEET_WITNESS", "1") == "0":
        return []

    def work() -> list[dict[str, Any]]:
        from . import product
        results = []
        try:
            product.sync(store)  # LEAD-SYSTEM-DESIGN 2.1: определения O-* из книг ведущих
        except Exception:  # проекция не должна останавливать замеры
            LOG.exception("product projection failed")
        for project, root, item in due(store):
            result = run_one(root, item)
            with store.tx() as tx:
                tx.record_event(kind=KIND, project=project, tz=f"witness:{item['id']}",
                                payload=result)
                event_id = int(tx.scalar("SELECT MAX(id) FROM events WHERE kind=? AND project=?",
                                         (KIND, project)))
                product.record_witness(tx, project, event_id, result, str(item.get("level") or ""))
            results.append(result)
        return results

    if sync:
        return work()
    with _lock:
        if _running or time.monotonic() - _last_started < TICK_EVERY_S:
            return []
        _running, _last_started = True, time.monotonic()

    def run() -> None:
        global _running
        try:
            work()
        finally:
            with _lock:
                _running = False

    threading.Thread(target=run, name="fleet-witness", daemon=True).start()
    return []


def lead_lines(store: Any, project: str) -> list[str]:
    """Первые строки памятки ведущего: что сломано или не замеряется — без его догадки."""
    items = _config().get(project) or []
    if not items:
        return []
    with store.read() as tx:
        latest = _latest(tx, project)
    bad, gaps, good = [], [], []
    for item in items:
        last = latest.get(item["id"])
        label = f"{item['id']} ({item.get('obligation') or '—'})"
        if last is None:
            gaps.append(f"  ? {label}: ещё не запускался")
        elif last.get("verdict") == "missing":
            gaps.append(f"  ? {label}: {last.get('summary')}")
        elif last.get("verdict") == "instrument_changed":
            gaps.append(f"  ? {label}: прибор изменён — {last.get('summary')}")
        elif last.get("verdict") in {"broken", "error"}:
            fell = " — СЛОМАЛОСЬ БЕЗ ТЕБЯ (было works_live)" if last.get("previous_verdict") == "works_live" else ""
            bad.append(f"  ✗ {label}: {last.get('verdict')}{fell} — {last.get('summary')} [{last.get('ts', '')[:16]}]")
        elif last.get("verdict") == "unknown":
            gaps.append(f"  ? {label}: unknown — {last.get('summary')}")
        else:
            good.append(item["id"])
        if last and last.get("verdict") != "works_live" and last.get("next_step"):
            (bad if last.get("verdict") in {"broken", "error"} else gaps).append(
                f"    Следующий шаг свидетеля: {last['next_step']}")
    lines = ["Свидетели обещаний (замеры системы, не твои; works_live в книге — только отсюда):"]
    lines += bad + gaps
    if good:
        lines.append("  ✓ works_live: " + ", ".join(good))
    if bad or gaps:
        lines.append("  Красное — разрыв в продукте: чини продукт. Прибор (ops/witness, tools/witness, "
                     "скрипт ячейки, ops/lead/ledger_check.py) не твой — страж откажет. Прибора нет или "
                     "он показал ложный вердикт — fleet_lead(action='issue') рабочему с доказательством; "
                     "приёмка прибора — с отрицательным контролем.")
    return lines


__all__ = ["tick", "run_one", "due", "lead_lines", "KIND"]
