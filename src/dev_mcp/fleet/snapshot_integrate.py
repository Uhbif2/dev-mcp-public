"""Коммит принятой сдачи по её байтам, а не по общему дереву (FLEET-SERVICE-WORK 03.11 У3, шаг 3).

Ночь 05–06.11: 1984 интеграции Nova отложены «байты изменены незавершённой записью», 485/259 у
deep-research/local-library — «дерево отличается от принятой сдачи». Ведущий возвращается к
работе через ~20 с после приёмки и правит те же файлы; правило «рабочее дерево == печать» на
таком потоке не выполняется никогда, и веха не попадает в историю.

Печать (sha256 байтов файла при сдаче) — доверенный якорь приёмки. Байты для неё берутся из
git-объектов снимка сдачи (`submission_snapshot`, тень с 03.11) или из текущего файла, если он
ещё совпадает; каждый источник сверяется с печатью. Коммит: временный индекс от HEAD →
`write-tree` → `commit-tree` → CAS `update-ref HEAD <new> <old>`. Рабочее дерево не трогается:
более поздняя работа остаётся в нём незакоммиченной правкой поверх новой HEAD. В общем индексе
обновляются только записи, равные старой HEAD, — чужие подготовленные байты не перезаписываются.

Удаления, ссылки и подмодули не поддерживаются: такой кандидат идёт прежним путём.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Mapping

from .manual_commit import CommitRefused, _git, _safe_name

AUTHOR = {"GIT_AUTHOR_NAME": "Fleet", "GIT_AUTHOR_EMAIL": "fleet@localhost",
          "GIT_COMMITTER_NAME": "Fleet", "GIT_COMMITTER_EMAIL": "fleet@localhost"}


def _git_raw(root: Path, *args: str, data: bytes | None = None) -> bytes | None:
    """stdout без strip (байты файла); ошибка → None."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        out = subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=root, env=env, input=data,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


_PATHS_PER_CALL = 200      # WinError 206: длинный argv на 677 путях (Nova) — частями


def _git_paths(root: Path, prefix: tuple[str, ...], literal: list[str]) -> bytes | None:
    """`git <prefix> -- <paths>` частями; любая ошибка части → None (fail-closed), не b''."""
    out = b""
    for i in range(0, len(literal), _PATHS_PER_CALL):
        chunk = _git_raw(root, *prefix, "--", *literal[i:i + _PATHS_PER_CALL])
        if chunk is None:
            return None
        out += chunk
    return out


_DIGESTS: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}
_DIGESTS_MAX = 20000


def _blob_bytes(root: Path, rel: str, blob: str, sha: str) -> bytes | None:
    """Байты blob-а (вид после checkout или сырой), если их sha256 == печати.

    Отложенная веха повторяется на каждом проходе интегратора: blob неизменяем, его sha256
    считается один раз — повтор с той же неудачей не запускает git.
    """
    key = (str(root).casefold(), rel, blob)
    known = _DIGESTS.get(key)
    if known is not None and sha not in known:
        return None
    digests: list[str | None] = []
    for args in (("cat-file", "--filters", f"--path={rel}", blob), ("cat-file", "blob", blob)):
        data = _git_raw(root, *args)
        digest = None if data is None else hashlib.sha256(data).hexdigest()
        if digest == sha:
            return data
        digests.append(digest)
    if len(_DIGESTS) >= _DIGESTS_MAX:
        _DIGESTS.clear()
    _DIGESTS[key] = (digests[0], digests[1])
    return None


def accepted_bytes(root: Path, sealed: Mapping[str, str | None],
                   candidates: Mapping[str, Iterable[str]],
                   missing: list[str] | None = None) -> dict[str, tuple[str, bytes]] | None:
    """Путь → (blob, байты рабочего вида), каждый сверен с печатью; не доказано — None.

    missing (если передан) получает пути, байты которых не нашлись.

    candidates — blob-ы снимков сдач этой задачи по пути (новые первыми). core.autocrlf
    хранит blob в чистом виде, печать — байты диска: сверка идёт по `cat-file --filters`
    (вид после checkout) и по сырому blob.
    """
    out: dict[str, tuple[str, bytes]] = {}
    for rel, sha in sealed.items():
        if sha is None:
            return None
        try:
            _safe_name(rel)
        except CommitRefused:
            return None
        found: tuple[str, bytes] | None = None
        for blob in candidates.get(rel.casefold(), ()):
            data = _blob_bytes(root, rel, blob, sha)
            if data is not None:
                found = (blob, data)
                break
        if found is None:
            full = root / rel
            if full.is_file() and not full.is_symlink():
                data = full.read_bytes()
                if hashlib.sha256(data).hexdigest() == sha:
                    blob = _git(root, "hash-object", "-w", "--stdin", f"--path={rel}",
                                data=data).decode("ascii")
                    found = (blob, data)
        if found is None:
            if missing is None:
                return None
            missing.append(rel)
            continue
        out[rel] = found
    return None if missing else out


def _entries(raw: bytes, *, index: bool) -> dict[str, tuple[str, str]]:
    """ls-tree -z / ls-files -s -z → путь → (mode, blob); конфликтные стадии — '!'."""
    result: dict[str, tuple[str, str]] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        parts = meta.decode("ascii").split()
        rel = path.decode("utf-8")
        if index:
            mode, blob, stage = parts[0], parts[1], parts[2]
            result[rel] = (mode, blob) if stage == "0" else ("!", "!")
        else:
            result[rel] = (parts[0], parts[2])
    return result


def commit_blobs(root: Path, blobs: Mapping[str, str], message: str, *,
                 date: str = "") -> tuple[str, list[str]]:
    """HEAD + эти blob-ы → коммит (CAS). → (sha или '' если дерево не изменилось, пути индекса,
    оставленные как есть, потому что в общем индексе там подготовлены чужие байты)."""
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii")
    literal = [f":(literal){rel}" for rel in blobs]
    # 25.11 (ночное ревью, HIGH): ошибка ls-tree подменялась b'' → все пути считались новыми и
    # коммитились как 100644, исполняемый бит терялся. Теперь отказ, коммит не создаётся.
    listed = _git_paths(root, ("ls-tree", "-z", head), literal)
    if listed is None:
        raise CommitRefused("git ls-tree failed; file modes unknown")
    old = _entries(listed, index=False)
    with tempfile.TemporaryDirectory(prefix="fleet-snapshot-commit-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(root, "read-tree", head, env=env)
        for rel, blob in blobs.items():
            mode = old.get(rel, ("100644", ""))[0]
            if mode not in ("100644", "100755"):
                raise CommitRefused(f"unsupported mode {mode} for {rel}")
            _git(root, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}", env=env)
        tree = _git(root, "write-tree", env=env).decode("ascii")
    if tree == _git(root, "rev-parse", f"{head}^{{tree}}").decode("ascii"):
        return "", []
    stamp = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}
    commit = _git(root, "commit-tree", tree, "-p", head, "-m", message,
                  env={**AUTHOR, **stamp}).decode("ascii")
    _git(root, "update-ref", "-m", "fleet: принятая сдача по снимку", "HEAD", commit, head)
    # Ошибка ls-files: индекс не трогаем вовсе (все пути — «оставлены как есть»), явно.
    staged = _git_paths(root, ("ls-files", "-s", "-z"), literal)
    shared = _entries(staged, index=True) if staged is not None else {rel: ("?", "?") for rel in blobs}
    left, lines = [], []
    for rel, blob in blobs.items():
        if shared.get(rel) != old.get(rel):
            left.append(rel)
            continue
        lines.append(f"{old.get(rel, ('100644', ''))[0]} {blob}\t{rel}")
    if lines:
        _git(root, "update-index", "-z", "--index-info",
             data=("\0".join(lines) + "\0").encode("utf-8"))
    return commit, left
