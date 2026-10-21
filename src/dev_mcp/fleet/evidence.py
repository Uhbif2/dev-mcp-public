"""Что изменилось в зоне задачи и по какому состоянию файлов сделан прогон.

Два пробела, которые закрывает этот модуль (приём взят у supercov):

1. Ревьюер не видел, что именно сделал автор: проекты не обязаны быть
   git-репозиториями, а журнал хранит только факт записи, не содержимое. Поэтому
   при взятии задачи снимаем owns (снимок), а памятка ревью показывает diff
   «снимок → сейчас» и сигналы, которые rc=0 не ловит: удалённые и ослабленные
   тесты, изменённую шапку ТЗ.
2. Доказательство не было привязано к состоянию: прогон приёмки, после которого
   файлы правили, всё ещё засчитывался. Теперь каждый run несёт хеш файлов своей
   зоны, а accept считает только прогоны по текущему состоянию.

Всё здесь — вспомогательные сведения для координации: любой сбой возвращает
пустой результат и никогда не ломает основную операцию (CONTRACT §11).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .model import norm_path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".supercov", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
}
MAX_FILES = 300
MAX_BLOB_BYTES = 1_000_000
MAX_DIFF_FILE_CHARS = 8_000
MAX_DIFF_TOTAL_CHARS = 30_000


# ---------------------------------------------------------------- пути и хеши

def _rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        out.append({k: row[k] for k in row.keys()} if hasattr(row, "keys") else dict(row))
    return out


def expand(owned: Iterable[str]) -> tuple[list[str], bool]:
    """owns → конкретные файлы (для `каталог/**` — обход с отсечкой служебных папок)."""
    files: list[str] = []
    truncated = False
    for raw in owned:
        path = str(raw or "").strip()
        if not path:
            continue
        if path.endswith("/**") or os.path.isdir(path):
            # Legacy/spec claims often own a bare directory rather than "dir/**".
            # Treat both forms identically for evidence; hashing the directory inode
            # itself returns None and made real edits look "untouched".
            base = path[:-3] if path.endswith("/**") else path
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames if d.casefold() not in SKIP_DIRS)
                for name in sorted(filenames):
                    if name.endswith((".pyc", ".pyo")):
                        continue
                    if len(files) >= MAX_FILES:
                        truncated = True
                        break
                    try:
                        files.append(norm_path(os.path.join(dirpath, name)))
                    except ValueError:
                        continue
                if truncated:
                    break
        else:
            files.append(path)
    return sorted(set(files)), truncated


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def tree_hash(owned: Iterable[str]) -> str:
    files, _ = expand(owned)
    state = [[path, _sha(path)] for path in files]
    return hashlib.sha256(json.dumps(state, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------- кто что сейчас держит

def claim_paths(store: Any, claim_id: int) -> list[str]:
    with store.read() as tx:
        rows = _rows(tx.query("SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim_id,)))
    return [str(row["path_norm"]) for row in rows]


def _latest_claim(tx: Any, project: str, tz: str) -> int | None:
    row = tx.one(
        "SELECT id FROM claims WHERE project=? AND tz=? ORDER BY id DESC LIMIT 1",
        (project, tz),
    )
    return int(row["id"]) if row is not None else None


def subject(store: Any, worker_id: str) -> dict[str, Any] | None:
    """Задача, файлы которой сейчас проверяет или меняет воркер.

    Для обычной работы — свой claim. Для review — claim исследуемой задачи: ревьюер
    ничего не держит, но его прогоны доказывают состояние именно её файлов.
    """
    from . import review as review_mod

    with store.read() as tx:
        row = tx.one(
            "SELECT c.id,c.project,c.tz,t.kind,t.body FROM claims c "
            "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
            (worker_id,),
        )
        if row is None:
            return None
        project, tz, kind = str(row["project"]), str(row["tz"]), str(row["kind"])
        claim_id: int | None = int(row["id"])
        source_tz = tz
        if kind == "review":
            project, source_tz = review_mod._review_target(tz, str(row["body"] or ""), project)
            claim_id = _latest_claim(tx, project, source_tz)
    paths = claim_paths(store, claim_id) if claim_id is not None else []
    return {"project": project, "tz": tz, "kind": kind, "source_tz": source_tz,
            "claim_id": claim_id, "paths": paths}


def subject_tree(store: Any, worker_id: str) -> str | None:
    try:
        info = subject(store, worker_id)
        if info is None or not info["paths"]:
            return None
        return tree_hash(info["paths"])
    except Exception:
        return None


# ------------------------------------------------------------------- снимок

def snapshot_root(store: Any) -> Path | None:
    path = str(getattr(store, "path", "") or "")
    if not path or path == ":memory:":
        return None
    return Path(path).parent / "snapshots"


def capture(store: Any, claim_id: int) -> bool:
    """Снять owns claim в момент взятия. Идемпотентно: повторный вызов не перетирает."""
    root = snapshot_root(store)
    if root is None:
        return False
    try:
        manifest_path = root / f"claim-{claim_id}.json"
        if manifest_path.exists():
            return True
        files, truncated = expand(claim_paths(store, claim_id))
        blobs = root / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, str | None] = {}
        for path in files:
            try:
                data = Path(path).read_bytes()
            except OSError:
                manifest[path] = None
                continue
            sha = hashlib.sha256(data).hexdigest()
            manifest[path] = sha
            blob = blobs / sha
            if len(data) <= MAX_BLOB_BYTES and not blob.exists():
                blob.write_bytes(data)
        manifest_path.write_text(
            json.dumps({"files": manifest, "truncated": truncated}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


def extend_capture(store: Any, claim_id: int, added_paths: Iterable[str]) -> bool:
    """Добавить baseline для новых путей auto-expanded claim, не перетирая старый снимок."""
    root = snapshot_root(store)
    if root is None:
        return False
    try:
        manifest_path = root / f"claim-{claim_id}.json"
        if not manifest_path.exists():
            return capture(store, claim_id)
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = dict(data.get("files") or {})
        files, truncated = expand(added_paths)
        blobs = root / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        for path in files:
            if path in manifest:
                continue
            try:
                raw = Path(path).read_bytes()
            except OSError:
                manifest[path] = None
                continue
            sha = hashlib.sha256(raw).hexdigest()
            manifest[path] = sha
            blob = blobs / sha
            if len(raw) <= MAX_BLOB_BYTES and not blob.exists():
                blob.write_bytes(raw)
        manifest_path.write_text(
            json.dumps(
                {"files": manifest, "truncated": bool(data.get("truncated")) or truncated},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


def _load_manifest(root: Path, claim_id: int) -> dict[str, Any] | None:
    try:
        return json.loads((root / f"claim-{claim_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _text(data: bytes | None) -> str | None:
    if data is None:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ------------------------------------------------------ сигналы для ревьюера

_ASSERT_RE = re.compile(r"\bassert\b|\.assert[A-Z]\w*\(|\bexpect\(|\bpytest\.raises\(")
_TEST_RE = re.compile(r"^\s*(?:async\s+)?def\s+test\w*\s*\(|\b(?:test|it)\s*\(\s*['\"`]", re.M)
_SKIP_RE = re.compile(
    r"@unittest\.skip|pytest\.mark\.(?:skip|xfail)|\.skip\(|\b(?:test|it|describe)\.(?:skip|todo)\b|\bxit\("
)


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_") or name.endswith("_test.py")
        or ".test." in name or ".spec." in name
        or "/test/" in path or "/tests/" in path
    )


def _counts(text: str) -> tuple[int, int, int]:
    return len(_ASSERT_RE.findall(text)), len(_TEST_RE.findall(text)), len(_SKIP_RE.findall(text))


def test_signals(path: str, before: str | None, after: str | None, deleted: bool) -> list[str]:
    """Детерминированные признаки «тест стал проверять меньше» — то, чего не видит rc=0."""
    if not is_test_path(path) or before is None:
        return []
    name = path.rsplit("/", 1)[-1]
    if deleted:
        return [f"удалён тестовый файл {name}"]
    if after is None:
        return []
    a0, t0, s0 = _counts(before)
    a1, t1, s1 = _counts(after)
    out = []
    if t1 < t0:
        out.append(f"{name}: тестов стало меньше ({t0} → {t1})")
    if a1 < a0:
        out.append(f"{name}: проверок стало меньше ({a0} → {a1})")
    if s1 > s0:
        out.append(f"{name}: пропусков (skip/xfail/todo) стало больше ({s0} → {s1})")
    return out


# ------------------------------------------------------------- секция ревью

def _spec_drift(store: Any, project: str, source_tz: str) -> str | None:
    from . import spec as spec_mod

    with store.read() as tx:
        row = tx.one(
            "SELECT t.spec_path,t.spec_sha,t.body,p.root FROM tasks t "
            "JOIN projects p ON p.name=t.project "
            "WHERE t.project=? AND t.tz=?",
            (project, source_tz),
        )
    if row is None or not row["spec_path"] or not row["spec_sha"] or not row["root"]:
        return None
    try:
        path = Path(str(row["root"])) / str(row["spec_path"])
        current = spec_mod.spec_sha(str(path))
        parsed = spec_mod.parse_spec(str(path.resolve()))
    except Exception:
        return None
    if current != str(row["spec_sha"]):
        return (
            "шапка файла ТЗ изменена после регистрации задачи — приёмка и owns берутся "
            "из зафиксированной версии, не из файла; выясни, зачем автор правил шапку"
        )
    if parsed.body != str(row["body"] or ""):
        return (
            "тело файла ТЗ изменено после регистрации задачи — исполнитель получил "
            "зафиксированное тело из реестра; синхронизируй ТЗ через bootstrap или выясни, "
            "зачем файл меняли во время работы"
        )
    return None


def review_section(store: Any, project: str, source_tz: str) -> str:
    """Что сделал автор: сводка, сигналы и diff «снимок при взятии → сейчас»."""
    root = snapshot_root(store)
    with store.read() as tx:
        claim_id = _latest_claim(tx, project, source_tz)
    if root is None or claim_id is None:
        return ""
    manifest = _load_manifest(root, claim_id)
    signals: list[str] = []
    drift = _spec_drift(store, project, source_tz)
    if drift:
        signals.append(drift)
    if manifest is None:
        lines = [
            f"Изменения {project}/{source_tz}: снимка на момент взятия нет "
            "(задача взята до включения снимков) — восстанавливай изменения по файлам owns сами.",
        ]
        if signals:
            lines += ["Сигналы для ревьюера:", *[f"  - {s}" for s in signals]]
        return "\n".join(lines)

    before_files: dict[str, str | None] = dict(manifest.get("files") or {})
    current_files, truncated = expand(claim_paths(store, claim_id))
    summary: list[str] = []
    diffs: list[str] = []
    total = 0
    for path in sorted(set(before_files) | set(current_files)):
        old_sha = before_files.get(path)
        new_sha = _sha(path)
        if old_sha == new_sha:
            continue
        blob = root / "blobs" / old_sha if old_sha else None
        try:
            old_bytes = blob.read_bytes() if blob is not None and blob.exists() else (None if old_sha else b"")
        except OSError:
            old_bytes = None
        try:
            new_bytes = Path(path).read_bytes() if new_sha else b""
        except OSError:
            new_bytes = None
        old_text, new_text = _text(old_bytes), _text(new_bytes)
        deleted = new_sha is None and old_sha is not None
        status = "создан" if old_sha is None else ("удалён" if deleted else "изменён")
        signals += test_signals(path, old_text if old_sha else None, new_text, deleted)
        if old_text is None or new_text is None:
            summary.append(f"  {status}: {path} (двоичный или слишком большой — смотри файл)")
            continue
        chunk = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=f"до/{path.rsplit('/', 1)[-1]}", tofile=f"после/{path.rsplit('/', 1)[-1]}", n=3,
        ))
        plus = sum(1 for line in chunk.splitlines() if line.startswith("+") and not line.startswith("+++"))
        minus = sum(1 for line in chunk.splitlines() if line.startswith("-") and not line.startswith("---"))
        summary.append(f"  {status}: {path} (+{plus} −{minus})")
        if len(chunk) > MAX_DIFF_FILE_CHARS:
            chunk = chunk[:MAX_DIFF_FILE_CHARS] + "\n… (diff файла обрезан — остальное читай в файле)\n"
        if total + len(chunk) > MAX_DIFF_TOTAL_CHARS:
            diffs.append(f"… diff {path} не поместился — читай файл целиком\n")
            continue
        total += len(chunk)
        diffs.append(chunk)

    lines = [f"Что изменилось в зоне {project}/{source_tz} (снимок при взятии → сейчас):"]
    lines += summary or ["  (ничего — файлы зоны совпадают со снимком)"]
    if manifest.get("truncated") or truncated:
        lines.append(f"  (зона больше {MAX_FILES} файлов — показана часть)")
    lines.append("Сигналы для ревьюера (то, что rc=0 не ловит):")
    lines += [f"  - {s}" for s in signals] or ["  (нет)"]
    if diffs:
        lines += ["--- diff ---", "".join(diffs).rstrip()]
    return "\n".join(lines)
