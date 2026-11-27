"""Серверная проверка рамки после принятой стадии `understood` — тень (владелец, 03.11.2026).

Поручение владельца 03.11: «разрешаю подключать во флот». Дизайн —
`C:/hub/frame-expansion-mcp/docs/FRAME-CHECK-MCP-2026-11-03.md`, ТЗ —
`docs/TZ-frame-check-stage-2026-11-03.md`.

Сервер, а не агент, сверяет `intent/understood.md` с дословной интенцией владельца и его линзами
(`frame_check.py` компонента frame-expansion-mcp, модель другого семейства). Две недели — тень:
стадия не ждёт и не блокируется; развилки — событием `intent.frame_check` и разделом
«## Развилки рамки (тень)» в `intent/owner-brief.md`. Недоступность (код 2, тайм-аут, нет файлов,
исчерпан потолок) — событие `intent.frame_check_unavailable`, тоже без блокировки.

Траты: проверка ≈ $0.001 (OpenRouter, дешёвая модель). Потолок на всю тень — `TOTAL_USD`
(меньше $1, названного владельцем для прогонов frame-expansion); переопределяется
`data/fleet/frame-check.json` → `{"enabled": true, "total_usd": 0.5}`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)

COMPONENT = Path("C:/hub/frame-expansion-mcp")
SCRIPT = COMPONENT / "server" / "frame_check.py"
RUNS = COMPONENT / "evals" / "runs" / "frame"
TIMEOUT_S = 120
TOTAL_USD = 0.5
SECTION = "## Развилки рамки (тень)"
_DATA = Path(__file__).resolve().parents[3] / "data" / "fleet"
_running: set[tuple[str, str]] = set()
_guard = threading.Lock()


def _config(data: Path | None = None) -> dict[str, Any]:
    try:
        cfg = json.loads(((data or _DATA) / "frame-check.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    return {"enabled": cfg.get("enabled", True) is True,
            "total_usd": float(cfg.get("total_usd", TOTAL_USD) or 0.0)}


def _spent(store: Any) -> float:
    with store.read() as tx:
        row = tx.one("SELECT COALESCE(SUM(CAST(json_extract(payload,'$.cost') AS REAL)),0) AS s "
                     "FROM events WHERE kind='intent.frame_check'")
    return float(row["s"] or 0.0) if row is not None else 0.0


def _unavailable(store: Any, project: str, tz: str, reason: str, digest: str = "") -> str:
    with store.tx() as tx:
        tx.record_event(kind="intent.frame_check_unavailable", project=project, tz=tz,
                        payload={"reason": reason[:400], "digest": digest})
    return "unavailable"


def _child_env() -> dict[str, str]:
    # Системный прокси молча вешает urllib на отдельных доменах (dev-mcp/CLAUDE.md);
    # jev ходит напрямую — так же и проверка рамки.
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _argv(owner: Path, frame: Path, out: Path) -> list[str]:
    py = shutil.which("py")
    head = [py, "-3"] if py else [sys.executable]
    return [*head, "-I", str(SCRIPT), str(owner), str(frame), str(out)]


def _record(frame_id: str) -> dict[str, Any]:
    try:
        return json.loads((RUNS / f"{frame_id}.json").read_text(encoding="utf-8")).get("result") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def brief_section(forks: list[dict]) -> str:
    lines = [SECTION, "Проверка рамки нашла места, где агент мог понять задачу не так, как вы. "
             "Пока это только наблюдение: работа не ждёт ответа."]
    for fork in forks[:3]:
        question = " ".join(str(fork.get("question") or "").split())
        if not question:
            continue
        options = [" ".join(str(o).split()) for o in (fork.get("options") or []) if str(o).strip()]
        lines.append(f"- {question}" + (f" Варианты: {'; '.join(options[:3])}." if options else ""))
    return "\n".join(lines) + "\n"


def _write_brief(root: Path, forks: list[dict]) -> bool:
    """Раздел владельцу — замена прежнего раздела тени, остальной текст брифа не трогается."""
    path = root / "intent" / "owner-brief.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    head, sep, tail = text.partition(SECTION)
    if sep:
        rest = tail.split("\n## ", 1)
        tail = ("\n## " + rest[1]) if len(rest) > 1 else ""
        text = head.rstrip("\n") + "\n\n" + brief_section(forks) + tail
    else:
        text = text.rstrip("\n") + "\n\n" + brief_section(forks)
    path.write_text(text, encoding="utf-8")
    return True


def run(store: Any, project: str, tz: str, root: Path, *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        data: Path | None = None) -> str:
    """Одна проверка рамки на версию понимания. → ok / forks / unavailable / skipped."""
    owner = root / ".intent" / "intent.md"
    frame = root / "intent" / "understood.md"
    out = root / "intent" / "consensus" / "understood-frame.md"
    try:
        digest = hashlib.sha256(owner.read_bytes() + b"\0" + frame.read_bytes()).hexdigest()
    except OSError:
        return _unavailable(store, project, tz, "нет intent/understood.md или .intent/intent.md")
    with store.read() as tx:
        seen = tx.one("SELECT 1 FROM events WHERE kind IN ('intent.frame_check','intent.frame_check_unavailable') "
                      "AND project=? AND tz=? AND json_extract(payload,'$.digest')=? LIMIT 1",
                      (project, tz, digest))
    if seen:
        return "skipped"
    cfg = _config(data)
    if not cfg["enabled"]:
        return _unavailable(store, project, tz, "выключено в data/fleet/frame-check.json", digest)
    spent = _spent(store)
    if spent >= cfg["total_usd"]:
        return _unavailable(store, project, tz, f"потолок тени исчерпан: ${spent:.4f} из "
                            f"${cfg['total_usd']:.2f}", digest)
    if not SCRIPT.is_file():
        return _unavailable(store, project, tz, f"нет {SCRIPT}", digest)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = runner(_argv(owner, frame, out), capture_output=True, timeout=TIMEOUT_S,
                      env=_child_env(), cwd=str(COMPONENT))
    except subprocess.TimeoutExpired:
        return _unavailable(store, project, tz, f"тайм-аут {TIMEOUT_S} с", digest)
    except OSError as exc:
        return _unavailable(store, project, tz, f"запуск: {exc}", digest)
    stdout = (done.stdout or b"").decode("utf-8", "replace") if isinstance(done.stdout, bytes) else str(done.stdout or "")
    if done.returncode != 0:
        return _unavailable(store, project, tz, f"код {done.returncode}: {stdout.strip()[-300:]}", digest)
    try:
        summary = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return _unavailable(store, project, tz, "ответ проверки не JSON: " + stdout.strip()[-200:], digest)
    frame_id = str(summary.get("frame_id") or "")
    result = _record(frame_id)
    forks = [f for f in (result.get("forks") or []) if isinstance(f, dict)]
    verdict = str(summary.get("verdict") or result.get("verdict") or "ok")
    payload = {"frame_id": frame_id, "verdict": verdict, "forks": forks[:3],
               "model": result.get("served_model"), "lenses_version": result.get("lenses_version"),
               "cost": float(result.get("cost") or 0.0), "digest": digest, "mode": "shadow"}
    brief = _write_brief(root, forks) if forks else False
    payload["owner_brief"] = brief
    with store.tx() as tx:
        tx.record_event(kind="intent.frame_check", project=project, tz=tz, payload=payload)
    return verdict


def start(store: Any, project: str, tz: str, root: Path) -> bool:
    """Не держать прувер стадий: проверка идёт своим потоком, одна на задачу."""
    if os.environ.get("DEV_MCP_FRAME_CHECK", "1") == "0":
        return False
    key = (project, tz)
    with _guard:
        if key in _running:
            return False
        _running.add(key)

    def work() -> None:
        try:
            run(store, project, tz, root)
        except Exception:
            LOG.exception("frame check failed for %s/%s", project, tz)
            try:
                _unavailable(store, project, tz, "сбой платформы, см. журнал dev-mcp")
            except Exception:
                pass
        finally:
            with _guard:
                _running.discard(key)

    threading.Thread(target=work, name=f"frame-check:{tz}", daemon=True).start()
    return True
