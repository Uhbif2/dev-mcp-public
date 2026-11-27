"""Неизменяемый git-снимок файлов без worktree (FLEET-SERVICE-WORK 03.11 §4 У3, §7 шаг 2).

Временный индекс → `hash-object -w` → `write-tree` → `commit-tree`. Рабочая копия, общий
индекс и HEAD проекта не трогаются. Использует снимок сдачи
(`submission_shadow`, `seal_snapshot`); ветка ведущего отменена 06.11.

Снимок сдачи — пока ТЕНЬ (§7: «в тени рядом с печатями»): при каждом `fleet_submit` в общем
дереве сервер фиксирует байты записанных задачей файлов git-объектом и пишет событие
`submission_snapshot` {claim_id, commit, tree, base, paths, blobs}. Ни приёмка, ни интеграция
его не читают; ссылка не создаётся (недостижимые объекты git хранит ≥ 2 недель — срок тени).
Сверка «снимок / печать» по истории — `tools/snapshot_shadow.py`. Только после неё снимок
заменит печати.

07.11 (LEAD-SYSTEM-DESIGN 1.3): у сдачи с печатью снимок — `seal_snapshot`: синхронно, из байтов
печати, под ref `refs/fleet/sealed/<submit_id>`. Тень по записям осталась для сдач без печати.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger(__name__)

MAX_PATHS = 400
AUTHOR = {"GIT_AUTHOR_NAME": "Fleet", "GIT_AUTHOR_EMAIL": "fleet@localhost",
          "GIT_COMMITTER_NAME": "Fleet", "GIT_COMMITTER_EMAIL": "fleet@localhost"}


def true_rel(root: Path, rel_lower: str) -> str | None:
    """Путь из журнала (нижний регистр) → настоящий регистр на диске; нет файла — None."""
    cur, parts = root, []
    for part in rel_lower.split("/"):
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        match = next((n for n in names if n.casefold() == part.casefold()), None)
        if match is None:
            return None
        parts.append(match)
        cur = cur / match
    return "/".join(parts)


def rels_under(root: Path, norms: Iterable[str]) -> list[str]:
    """path_norm журнала → относительные пути внутри root (нижний регистр, без повторов)."""
    prefix = str(root).replace("\\", "/").casefold().rstrip("/") + "/"
    out: dict[str, None] = {}
    for norm in norms:
        value = str(norm or "").replace("\\", "/").casefold()
        if value.startswith(prefix):
            out.setdefault(value[len(prefix):], None)
    return list(out)


def commit_files(root: Path, base: str, rels_lower: Iterable[str], message: str, *,
                 max_paths: int = MAX_PATHS, author: dict[str, str] | None = None,
                 refused: dict[str, str] | None = None
                 ) -> tuple[str, str, list[str], dict[str, str]]:
    """Коммит base + текущие байты файлов. → (commit или '', tree, пути, путь → blob).

    commit пустой, если ни один файл не изменил дерево base. Удалённые и переименованные
    файлы, небезопасные имена, ссылки и файлы с приватным ключом в снимок не входят; refused
    (если передан) получает путь → причину (07.11, 1.3: отказ виден, а не молчаливый пропуск).
    """
    skipped: dict[str, str] = refused if refused is not None else {}
    from .manual_commit import _PRIVATE_KEY, CommitRefused, _git, _safe_name

    paths: list[str] = []
    blobs: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="fleet-snapshot-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(root, "read-tree", base, env=env)
        ordered = sorted(rels_lower)
        for low in ordered[max_paths:]:
            skipped[low] = "truncated"
        for low in ordered[:max_paths]:
            rel = true_rel(root, low)
            if rel is None:
                skipped[low] = "deleted_or_renamed"
                continue
            try:
                _safe_name(rel)
            except CommitRefused:
                skipped[rel] = "unsafe_name"
                continue
            full = root / rel
            if not full.is_file() or full.is_symlink():
                skipped[rel] = "not_a_regular_file"
                continue
            if _PRIVATE_KEY.search(full.read_bytes()):
                skipped[rel] = "private_key"
                continue
            blob = _git(root, "hash-object", "-w", "--", str(full)).decode("ascii")
            _git(root, "update-index", "--add", "--cacheinfo", "100644", blob, rel, env=env)
            paths.append(rel)
            blobs[rel] = blob
        tree = _git(root, "write-tree", env=env).decode("ascii")
    commit = ""
    if paths and tree != _git(root, "rev-parse", base + "^{tree}").decode("ascii"):
        commit = _git(root, "commit-tree", tree, "-p", base, "-m", message,
                      env=author or AUTHOR).decode("ascii")
    return commit, tree, paths, blobs


def submission_shadow(store: Any, project: str, tz: str, worker_id: str, claim_id: int) -> dict:
    """Снимок одной сдачи (тень). Ошибка — событие, а не отказ сдачи."""
    from .manual_commit import CommitRefused, _git

    with store.read() as tx:
        row = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (project,))
        if row is None or not row["root"]:
            return {}
        take = tx.one("SELECT id FROM events WHERE kind='take' AND worker_id=? AND project=? "
                      "AND tz=? AND json_extract(payload,'$.claim_id')=? ORDER BY id DESC LIMIT 1",
                      (worker_id, project, tz, claim_id))
        since = int(take["id"]) if take is not None else None
        writes = [] if since is None else [r["path_norm"] for r in tx.query(
            "SELECT path_norm FROM events WHERE kind='write' AND worker_id=? AND project=? "
            "AND tz=? AND id>? ORDER BY id", (worker_id, project, tz, since))]
    root = Path(str(row["root"]))
    payload: dict[str, Any] = {"claim_id": claim_id, "mode": "shadow"}
    if since is None:
        payload["skipped"] = "нет события take этого захвата"
    else:
        # Корень проекта может быть подпапкой репозитория (Main Hub — C:\hub\_todo\hub-fleet):
        # пути индекса считаются от верха репозитория.
        try:
            root = Path(_git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip())
        except (CommitRefused, OSError):
            root = None
    if since is not None and root is None:
        payload["skipped"] = "проект не в git"
    elif since is not None:
        rels = rels_under(root, writes)
        if not rels:
            payload["skipped"] = "записей нет"
        else:
            try:
                base = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii")
                refused: dict[str, str] = {}
                commit, tree, paths, blobs = commit_files(
                    root, base, rels, f"fleet: снимок сдачи {tz} (claim {claim_id})\n\n"
                    f"Fleet-Task: {project}/{tz}\nFleet-Claim: {claim_id}", refused=refused)
                payload.update(base=base, commit=commit, tree=tree, paths=paths[:200],
                               blobs=dict(list(blobs.items())[:200]), written=len(rels),
                               unchanged=not commit)
                if refused:
                    payload["refused"] = dict(list(refused.items())[:200])
            except (CommitRefused, OSError) as exc:
                payload["error"] = str(exc)[:400]
    with store.tx() as tx:
        tx.record_event(kind="submission_snapshot", worker_id=worker_id, project=project, tz=tz,
                        payload=payload)
    return payload


def start_submission_shadow(store: Any, project: str, tz: str, worker_id: str, claim_id: int) -> bool:
    """Ответ fleet_submit не ждёт git: снимок — своим потоком."""
    if os.environ.get("DEV_MCP_FLEET_SUBMIT_SNAPSHOT", "1") == "0":
        return False

    def work() -> None:
        try:
            submission_shadow(store, project, tz, worker_id, claim_id)
        except Exception:
            LOG.exception("submission snapshot failed for %s/%s", project, tz)

    threading.Thread(target=work, name=f"submit-snapshot:{tz}", daemon=True).start()
    return True


def seal_snapshot(store: Any, project: str, tz: str, worker_id: str, claim_id: int,
                  submit_id: int, sealed: dict[str, str | None], captured: dict[str, bytes]) -> dict:
    """Печать = снимок (LEAD-SYSTEM-DESIGN 1.3, 07.11): blob-ы из байтов печати, сразу.

    Байты — те же, по которым посчитан sha печати (не перечитываются). Коммит base + blob-ы
    держит ref `refs/fleet/sealed/<submit_id>`: недостижимые объекты git со временем удаляет,
    а принятая сдача обязана пережить любую задержку интеграции. Удаления (None в печати)
    снимок не несёт — это типизированный отказ `refused`, а не пропуск. Ошибка — событие с
    `error`, сдачу она не отменяет: печать уже записана.
    """
    from .manual_commit import CommitRefused, _git

    payload: dict[str, Any] = {"claim_id": claim_id, "submit_id": submit_id, "mode": "seal"}
    refused: dict[str, str] = {rel: "deleted" for rel, sha in sealed.items() if sha is None}
    refused.update({rel: "not_captured" for rel, sha in sealed.items()
                    if sha is not None and rel not in captured})
    try:
        with store.read() as tx:
            row = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (project,))
        if row is None or not row["root"]:
            payload["skipped"] = "проект не активен"
        else:
            # Пути печати — от верха репозитория (`git status --porcelain`).
            root = Path(_git(Path(str(row["root"])), "rev-parse", "--show-toplevel")
                        .decode("utf-8").strip())
            base = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii")
            blobs: dict[str, str] = {}
            with tempfile.TemporaryDirectory(prefix="fleet-seal-index-") as tmp:
                env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
                _git(root, "read-tree", base, env=env)
                for rel in sorted(captured):
                    blob = _git(root, "hash-object", "-w", "--stdin", f"--path={rel}",
                                data=captured[rel]).decode("ascii")
                    _git(root, "update-index", "--add", "--cacheinfo", "100644", blob, rel, env=env)
                    blobs[rel] = blob
                tree = _git(root, "write-tree", env=env).decode("ascii")
            commit = _git(root, "commit-tree", tree, "-p", base, "-m",
                          f"fleet: печать сдачи {tz} (submit {submit_id})\n\n"
                          f"Fleet-Task: {project}/{tz}\nFleet-Claim: {claim_id}\n"
                          f"Fleet-Submit: {submit_id}", env=AUTHOR).decode("ascii")
            ref = f"refs/fleet/sealed/{submit_id}"
            _git(root, "update-ref", ref, commit)
            payload.update(base=base, commit=commit, tree=tree, ref=ref,
                           paths=sorted(blobs)[:400], blobs=dict(sorted(blobs.items())[:400]),
                           written=len(blobs))
    except (CommitRefused, OSError, UnicodeError) as exc:
        payload["error"] = str(exc)[:400]
    if refused:
        payload["refused"] = dict(sorted(refused.items())[:200])
    with store.tx() as tx:
        tx.record_event(kind="submission_snapshot", worker_id=worker_id, project=project, tz=tz,
                        payload=payload)
    return payload
