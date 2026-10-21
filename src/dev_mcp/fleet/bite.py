"""Кусающий тест для fix (архитектура v2 §5, эксперимент E5; 27.09.2026).

Зачем. 37% вердиктов по задачам с приёмкой — отказ при зелёной приёмке: ревьюер находит дефект,
который приёмка ТЗ пропустила (`fleet.cmd flow`). Fix чинит дефект, но если он не оставил теста,
ствол от того же класса дефекта не защищён — следующая правка вернёт его, и снова поймает только
человек-ревьюер. Правило: fix сдаётся с командой теста, который ПАДАЕТ на отклонённой версии и
ПРОХОДИТ на исправленной. Сервер проверяет это сам, механически.

Как. Снимок зоны в момент взятия fix (fleet/evidence.capture) — это и есть отклонённая версия.
В отдельной git-worktree от HEAD проекта (не мешает никому в общем дереве):
  after  — наложить текущие файлы зоны fix → тест обязан пройти (rc=0);
  before — вернуть НЕтестовые файлы к снимку, тесты оставить текущими → тест обязан упасть.
Итог — событие `bite` и строка в памятке ревьюера. Не git, нет окружения (venv, node_modules),
тест красный и в after — вердикт «не проверено», сдачу это не блокирует: решает ревьюер.
Выключатель — DEV_MCP_FLEET_BITE=0.
"""
from __future__ import annotations

from dev_mcp import machine

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import evidence
from .model import norm_path

ENABLED = os.environ.get("DEV_MCP_FLEET_BITE", "1") != "0"
RUN_TIMEOUT_S = float(os.environ.get("DEV_MCP_FLEET_BITE_TIMEOUT_S", "600"))
WT_BASE = Path(os.environ.get("DEV_MCP_FLEET_WT_BASE", str(machine.WORKTREE_BASE)))
OPT_OUT = ("bite: n/a", "bite: нет", "кусающий: нет")

Runner = Callable[..., subprocess.CompletedProcess]


def opted_out(note: str) -> str:
    """Причина отказа от кусающего теста из note сдачи ('bite: n/a — визуальная правка'), или ''."""
    for line in str(note or "").splitlines():
        low = line.strip().casefold()
        for mark in OPT_OUT:
            if low.startswith(mark):
                return line.strip()[len(mark):].strip(" —-:") or "(причина не названа)"
    return ""


def from_note(note: str) -> str:
    """Команда кусающего теста строкой в note: «bite: py -3 -m pytest test/x.py -q».

    Транспорт для клиентов со старой схемой fleet_submit (только agent, note): ChatGPT берёт список
    инструментов один раз при подключении коннектора, и поле bite_test до переподключения не видно.
    Отказ («bite: n/a — …») сюда не попадает — его разбирает opted_out.
    """
    if opted_out(note):
        return ""
    for line in str(note or "").splitlines():
        text = line.strip().strip("`")
        if text.casefold().startswith("bite:"):
            cmd = text[len("bite:"):].strip().strip("`").strip()
            # агенты дописывают пояснение: «bite: py -3 -m pytest t.py: падает до фикса» / «… — падает»
            cmd = re.split(r"\s+[—–]\s+|\s+-\s+|;\s+|:\s+", cmd, maxsplit=1)[0]
            return cmd.rstrip(":").strip("`").strip()
    return ""


def parse_command(raw: str, policy: Any) -> list[str]:
    """Команда теста → argv под теми же правилами, что `run`: разрешённый бинарь, без абсолютных путей."""
    argv = [a.strip('"') for a in shlex.split(str(raw or "").strip(), posix=False)]
    if not argv:
        raise ValueError("bite_test пуст")
    return validate_argv(argv, policy)


def validate_argv(argv: list[str], policy: Any) -> list[str]:
    """argv под правилами `run`: разрешённый бинарь голым именем, аргументы без абсолютных путей."""
    if not argv:
        raise ValueError("команда пуста")
    name = argv[0]
    allowed = {(b[:-4] if b.lower().endswith(".exe") else b) for b in policy.exec_allowed_bin}
    stem = os.path.basename(name)
    stem = stem[:-4] if stem.lower().endswith(".exe") else stem
    if stem not in allowed or name != os.path.basename(name):
        raise ValueError(f"bite_test: бинарь «{name}» не в списке разрешённых ({', '.join(policy.exec_allowed_bin)})")
    policy.guard_exec_args(argv[1:], bin_name=name)
    return argv


def _git(root: str, *args: str, runner: Runner = subprocess.run, timeout: float = 120) -> subprocess.CompletedProcess:
    return runner(["git", "-C", root, *args], capture_output=True, text=True, encoding="utf-8",
                  errors="replace", timeout=timeout,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _rel(path_norm: str, root_norm: str) -> str | None:
    prefix = root_norm.rstrip("/") + "/"
    return path_norm[len(prefix):] if path_norm.startswith(prefix) else None


def _case_path(root: str, rel: str) -> Path:
    return Path(root) / Path(*rel.split("/"))


UNTRACKED_MAX_FILES = 3000
UNTRACKED_MAX_BYTES = 200 * 1024 * 1024


def _copy_untracked(project_root: str, wt: str, runner: Runner) -> None:
    """Неотслеживаемые, не игнорируемые файлы — в копию: новые файлы задачи git ещё не знает."""
    listed = _git(project_root, "ls-files", "--others", "--exclude-standard", "-z", runner=runner)
    total = 0
    for rel in [r for r in listed.stdout.split("\0") if r][:UNTRACKED_MAX_FILES]:
        src = Path(project_root) / rel
        try:
            size = src.stat().st_size
            if total + size > UNTRACKED_MAX_BYTES:
                break
            dest = _case_path(wt, rel.replace("\\", "/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            total += size
        except OSError:
            continue


def check(store: Any, project_root: str, claim_id: int, argv: list[str], *,
          runner: Runner = subprocess.run, wt_base: Path | None = None) -> dict[str, Any]:
    """Прогнать кусающую проверку. Возвращает payload события `bite`."""
    started = time.monotonic()
    out: dict[str, Any] = {"command": " ".join(argv), "claim_id": claim_id}
    snap_root = evidence.snapshot_root(store)
    manifest = evidence._load_manifest(snap_root, claim_id) if snap_root else None
    if not manifest:
        return {**out, "verdict": "unchecked", "reason": "нет снимка зоны на момент взятия fix"}
    root_norm = norm_path(project_root)
    top = _git(project_root, "rev-parse", "--show-toplevel", runner=runner)
    if top.returncode != 0:
        return {**out, "verdict": "unchecked", "reason": "проект не под git — worktree не создать"}
    files, _ = evidence.expand(evidence.claim_paths(store, claim_id))
    snap: dict[str, str | None] = dict(manifest.get("files") or {})
    current = {p: evidence._sha(p) for p in set(files) | set(snap)}
    code_changed = [p for p, sha in current.items() if not evidence.is_test_path(p) and snap.get(p, None) != sha]
    if not code_changed:
        return {**out, "verdict": "unchecked", "reason": "fix не менял код вне тестов — кусать нечего"}

    base = (wt_base or WT_BASE) / f"bite-{claim_id}-{uuid.uuid4().hex[:6]}"
    base.parent.mkdir(parents=True, exist_ok=True)
    # База — текущее рабочее дерево, а не HEAD: коммит делается только после приёмки, и файлы
    # отклонённой базовой задачи (и соседей) в HEAD ещё нет. 07.10: 8 из 13 fix «не проверено» —
    # тест fix падал в копии от HEAD и на исправленной версии. `stash create` пишет объект
    # коммита с отслеживаемыми правками, не трогая ни дерево, ни список stash.
    snap_ref = _git(project_root, "stash", "create", runner=runner).stdout.strip() or "HEAD"
    add = _git(project_root, "worktree", "add", "--detach", str(base), snap_ref, runner=runner, timeout=300)
    if add.returncode != 0:
        return {**out, "verdict": "unchecked", "reason": f"git worktree add: {add.stderr.strip()[:200]}"}
    try:
        wt = str(base)
        _copy_untracked(project_root, wt, runner)

        def put(path_norm: str, data: bytes | None) -> None:
            rel = _rel(path_norm, root_norm)
            if rel is None:
                return
            dest = _case_path(wt, rel)
            if data is None:
                if dest.exists():
                    dest.unlink()
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        def run() -> int:
            done = runner(argv, cwd=wt, capture_output=True, timeout=RUN_TIMEOUT_S,
                          env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return int(done.returncode)

        for p in current:
            try:
                put(p, Path(p).read_bytes() if current[p] else None)
            except OSError:
                pass
        rc_after = run()
        missing = []
        for p in code_changed:
            sha = snap.get(p)
            if sha is None:
                put(p, None)
                continue
            blob = snap_root / "blobs" / sha
            if not blob.exists():
                missing.append(p)
                continue
            put(p, blob.read_bytes())
        if missing:
            return {**out, "verdict": "unchecked", "rc_after": rc_after,
                    "reason": f"нет копии отклонённой версии для {len(missing)} файлов (больше лимита снимка)"}
        rc_before = run()
    except subprocess.TimeoutExpired:
        return {**out, "verdict": "unchecked", "reason": f"тест не уложился в {int(RUN_TIMEOUT_S)} с"}
    except OSError as exc:
        return {**out, "verdict": "unchecked", "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        _git(project_root, "worktree", "remove", "--force", str(base), runner=runner)
        shutil.rmtree(base, ignore_errors=True)
    out.update(rc_after=rc_after, rc_before=rc_before, dur_s=round(time.monotonic() - started, 1),
               code_files=len(code_changed))
    if rc_after != 0:
        out.update(verdict="unchecked",
                   reason="тест красный и на исправленной версии в чистой копии HEAD (нужны незакоммиченные "
                          "файлы или окружение) — проверь вручную")
    elif rc_before == 0:
        out.update(verdict="toothless", reason="тест проходит и на отклонённой версии — дефект он не ловит")
    else:
        out.update(verdict="bites", reason="падает на отклонённой версии, проходит на исправленной")
    return out


def start(store: Any, project: str, tz: str, project_root: str, claim_id: int, argv: list[str],
          record: Callable[[dict[str, Any]], None]) -> threading.Thread | None:
    """Проверка в фоне: гейт проекта может идти минуты, а сдача не ждёт."""
    if not ENABLED:
        return None

    def work() -> None:
        try:
            payload = check(store, project_root, claim_id, argv)
        except Exception as exc:  # noqa: BLE001 — фоновая проверка не роняет сервер
            payload = {"command": " ".join(argv), "verdict": "unchecked", "reason": f"{type(exc).__name__}: {exc}"}
        record(payload)

    thread = threading.Thread(target=work, name=f"bite-{project}-{tz}", daemon=True)
    thread.start()
    return thread


def review_line(store: Any, project: str, tz: str) -> str:
    """Строка для памятки ревьюера fix-задачи."""
    with store.read() as tx:
        row = tx.one("SELECT payload FROM events WHERE kind='bite' AND project=? AND tz=? ORDER BY id DESC LIMIT 1",
                     (project, tz))
    if row is None:
        return ""
    try:
        data = json.loads(row["payload"] or "{}")
    except ValueError:
        return ""
    mark = {"bites": "КУСАЕТ", "toothless": "НЕ КУСАЕТ", "skipped": "отказ автора",
            "pending": "проверяется"}.get(str(data.get("verdict")), "не проверено")
    cmd = f" `{data['command']}`" if data.get("command") else ""
    return f"Кусающий тест{cmd}: {mark} — {data.get('reason', '')}".rstrip(" —")

