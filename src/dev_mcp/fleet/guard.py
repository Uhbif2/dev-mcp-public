from __future__ import annotations

import sqlite3
import threading
import weakref
from dataclasses import dataclass
from typing import Any

from .model import Decision, norm_path, paths_overlap


@dataclass(frozen=True)
class _WorkerState:
    id: str
    label: str
    revoked: bool
    revoke_reason: str


@dataclass(frozen=True)
class _ClaimState:
    id: int
    project: str
    tz: str
    worker_id: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class _Snapshot:
    event_id: int
    workers: dict[str, _WorkerState]
    claims: tuple[_ClaimState, ...]


class _RegistryReader:
    """Cheap read-side cache for guard decisions.

    Active claims are rebuilt only when the monotonic events.id changes.  The
    MAX(id) probe is intentionally tiny; the expensive join over claims and
    claim_paths does not run on every Policy.resolve().
    """

    def __init__(self, store: Any) -> None:
        self._store_ref = weakref.ref(store)
        self._lock = threading.Lock()
        self._snapshot: _Snapshot | None = None
        self._conn: sqlite3.Connection | None = None

    def snapshot(self) -> _Snapshot:
        with self._lock:
            store = self._store_ref()
            if store is None:
                raise RuntimeError("fleet store no longer exists")

            # Store.healthy() may open a short-lived SQLite connection, so it is
            # deliberately paid only while establishing the first snapshot.
            # Afterwards the MAX(events.id) probe below is both the cache
            # invalidator and the liveness check; any DB error fails open in
            # check_write/check_exec as degraded mode.
            if self._snapshot is None:
                try:
                    healthy = bool(store.healthy())
                except Exception as exc:
                    raise RuntimeError("fleet health check failed") from exc
                if not healthy:
                    raise RuntimeError("fleet store is unhealthy")

            conn = self._connection(store)
            event_id = self._event_id(conn, store)
            cached = self._snapshot
            if cached is not None and cached.event_id == event_id:
                return cached

            fresh = self._load_snapshot(conn, store)
            self._snapshot = fresh
            return fresh

    def _connection(self, store: Any) -> sqlite3.Connection | None:
        path = getattr(store, "path", None)
        if not path or path == ":memory:":
            return None
        if self._conn is None:
            conn = sqlite3.connect(
                str(path),
                timeout=0.05,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 50")
            conn.execute("PRAGMA query_only = ON")
            self._conn = conn
        return self._conn

    @staticmethod
    def _event_id(conn: sqlite3.Connection | None, store: Any) -> int:
        if conn is not None:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
            return int(row[0] if row is not None else 0)
        with store.read() as tx:
            value = tx.scalar("SELECT COALESCE(MAX(id), 0) FROM events")
        return int(value or 0)

    def _load_snapshot(
        self,
        conn: sqlite3.Connection | None,
        store: Any,
    ) -> _Snapshot:
        # A read transaction gives one coherent view of events/workers/claims.
        if conn is not None:
            conn.execute("BEGIN")
            try:
                event_row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM events"
                ).fetchone()
                event_id = int(event_row[0] if event_row is not None else 0)
                worker_rows = list(
                    conn.execute(
                        """
                        SELECT id, label, revoked_at, revoke_reason
                        FROM workers
                        ORDER BY id
                        """
                    ).fetchall()
                )
                claim_rows = list(
                    conn.execute(
                        """
                        SELECT c.id, c.project, c.tz, c.worker_id, cp.path_norm
                        FROM claims AS c
                        LEFT JOIN claim_paths AS cp ON cp.claim_id = c.id
                        WHERE c.state = 'active'
                        ORDER BY c.id, cp.path_norm
                        """
                    ).fetchall()
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        else:
            with store.read() as tx:
                event_id = int(
                    tx.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0
                )
                worker_rows = list(
                    tx.query(
                        """
                        SELECT id, label, revoked_at, revoke_reason
                        FROM workers
                        ORDER BY id
                        """
                    )
                )
                claim_rows = list(
                    tx.query(
                        """
                        SELECT c.id, c.project, c.tz, c.worker_id, cp.path_norm
                        FROM claims AS c
                        LEFT JOIN claim_paths AS cp ON cp.claim_id = c.id
                        WHERE c.state = 'active'
                        ORDER BY c.id, cp.path_norm
                        """
                    )
                )

        workers = {
            str(row["id"]): _WorkerState(
                id=str(row["id"]),
                label=str(row["label"] or ""),
                revoked=row["revoked_at"] is not None,
                revoke_reason=str(row["revoke_reason"] or ""),
            )
            for row in worker_rows
        }

        grouped: dict[int, dict[str, Any]] = {}
        for row in claim_rows:
            claim_id = int(row["id"])
            item = grouped.setdefault(
                claim_id,
                {
                    "project": str(row["project"]),
                    "tz": str(row["tz"]),
                    "worker_id": str(row["worker_id"]),
                    "paths": [],
                },
            )
            if row["path_norm"]:
                item["paths"].append(str(row["path_norm"]))

        claims = tuple(
            _ClaimState(
                id=claim_id,
                project=item["project"],
                tz=item["tz"],
                worker_id=item["worker_id"],
                paths=tuple(item["paths"]),
            )
            for claim_id, item in sorted(grouped.items())
        )
        return _Snapshot(event_id=event_id, workers=workers, claims=claims)


_READERS_LOCK = threading.Lock()
_READERS: weakref.WeakKeyDictionary[Any, _RegistryReader] = weakref.WeakKeyDictionary()


def _reader_for(store: Any) -> _RegistryReader:
    with _READERS_LOCK:
        reader = _READERS.get(store)
        if reader is None:
            reader = _RegistryReader(store)
            _READERS[store] = reader
        return reader


def _claim_label(snapshot: _Snapshot, claim: _ClaimState) -> str:
    worker = snapshot.workers.get(claim.worker_id)
    if worker is not None and worker.label:
        who = f"{claim.worker_id} ({worker.label})"
    else:
        who = claim.worker_id
    return f"{who}, {claim.project}/{claim.tz}"


def _matches_path(claim: _ClaimState, path_norm: str) -> bool:
    return any(paths_overlap(owned, path_norm) for owned in claim.paths)


def _decision(store: Any, worker_id: str | None, abs_path: str) -> Decision:
    worker_id = worker_id or None
    try:
        snapshot = _reader_for(store).snapshot()
    except (OSError, sqlite3.Error, RuntimeError):
        return Decision(
            allow=True,
            code="",
            reason="fleet registry unreadable; operation allowed in degraded mode",
            worker_id=worker_id,
            degraded=True,
        )

    path_norm = norm_path(abs_path)

    # Order is contractual: revocation wins even over the worker's own path.
    if worker_id is not None:
        worker = snapshot.workers.get(worker_id)
        if worker is not None and worker.revoked:
            detail = worker.revoke_reason or "причина владельцем не указана"
            return Decision(
                allow=False,
                code="FLEET_REVOKED",
                reason=(
                    f"Воркер {worker_id} отозван владельцем: {detail}. "
                    "Чтобы запросить продолжение, оставь владельцу request_note одним вызовом; "
                    "не создавай новый worker для обхода отзыва."
                ),
                worker_id=worker_id,
                degraded=False,
            )

    matching = tuple(
        claim for claim in snapshot.claims if _matches_path(claim, path_norm)
    )

    if worker_id is not None:
        foreign = tuple(claim for claim in matching if claim.worker_id != worker_id)
        if foreign:
            holder = foreign[0]
            return Decision(
                allow=False,
                code="FLEET_FOREIGN_PATH",
                reason=(
                    f"Путь держит {_claim_label(snapshot, holder)}. "
                    "Если это твоя задача после перезапуска — вызови fleet_join одним вызовом; "
                    "иначе не меняй чужую зону и оставь request_note одним вызовом, если доступ нужен."
                ),
                worker_id=worker_id,
                degraded=False,
            )

        if matching:
            return Decision(
                allow=True,
                code="",
                reason="",
                worker_id=worker_id,
                degraded=False,
            )

        return Decision(
            allow=True,
            code="FLEET_OUT_OF_CLAIM",
            reason=(
                f"Путь свободен, но не входит в owns активной заявки воркера {worker_id}; "
                "операция разрешена и должна быть записана как предупреждение. "
                f"Если это часть новой задачи — вызови fleet_take(agent='{worker_id}') одним вызовом."
            ),
            worker_id=worker_id,
            degraded=False,
        )

    if len(matching) == 1:
        inferred = matching[0]
        worker = snapshot.workers.get(inferred.worker_id)
        if worker is not None and worker.revoked:
            detail = worker.revoke_reason or "причина владельцем не указана"
            return Decision(
                allow=False,
                code="FLEET_REVOKED",
                reason=(
                    f"Воркер {inferred.worker_id} однозначно выведен из активного claim и "
                    f"отозван владельцем: {detail}. Чтобы запросить продолжение, оставь "
                    "владельцу request_note одним вызовом; не создавай новый worker для обхода отзыва."
                ),
                worker_id=inferred.worker_id,
                degraded=False,
            )
        return Decision(
            allow=True,
            code="",
            reason=(
                f"worker inferred from the only active claim: "
                f"{_claim_label(snapshot, inferred)}"
            ),
            worker_id=inferred.worker_id,
            degraded=False,
        )

    if len(matching) > 1:
        candidates = "; ".join(_claim_label(snapshot, claim) for claim in matching)
        return Decision(
            allow=False,
            code="FLEET_NO_WORKER",
            reason=(
                f"Путь совпадает с несколькими активными заявками: {candidates}. "
                "Передай agent; если worker_id ещё нет — вызови fleet_join одним вызовом."
            ),
            worker_id=None,
            degraded=False,
        )

    return Decision(
        allow=True,
        code="",
        reason="",
        worker_id=None,
        degraded=False,
    )


def check_write(store: Any, worker_id: str | None, abs_path: str) -> Decision:
    """Decide whether a filesystem write may proceed."""

    decision = _decision(store, worker_id, abs_path)
    if decision.allow and decision.code == "FLEET_OUT_OF_CLAIM" and worker_id:
        foreign = _foreign_project(store, worker_id, abs_path)
        if foreign:
            return Decision(
                allow=False,
                code="FLEET_FOREIGN_PROJECT",
                reason=(
                    f"Путь лежит в проекте флота «{foreign}», а твоя задача — из другого проекта. "
                    "Чужой проект не правят из своей задачи: оставь request_note (kind=other, "
                    f"кому — проект «{foreign}») с тем, что нужно изменить, и продолжай своё."
                ),
                worker_id=worker_id,
                degraded=False,
            )
    return decision


def _foreign_project(store: Any, worker_id: str, abs_path: str) -> str:
    """13.10: задачи Main Hub и local-library правили ствол платформы — грязный ствол
    держал выдачу и предпроверки. Запись внутрь корня другого проекта флота — отказ."""
    try:
        with store.read() as tx:
            mine = {str(r["project"]) for r in tx.query(
                "SELECT project FROM claims WHERE worker_id=? AND released_at IS NULL", (worker_id,))}
            roots = [(str(r["name"]), norm_path(str(r["root"]))) for r in tx.query(
                "SELECT name,root FROM projects")]
    except Exception:  # noqa: BLE001 — недоступный реестр не должен ломать запись
        return ""
    if not mine:
        return ""
    path = norm_path(abs_path)
    owner = max(((name, root) for name, root in roots
                 if path == root or path.startswith(root.rstrip("/") + "/")),
                key=lambda item: len(item[1]), default=None)
    return owner[0] if owner and owner[0] not in mine else ""


def check_exec(store: Any, worker_id: str | None, cwd: str) -> Decision:
    """Guard run/run_task without treating a broad cwd as ownership of every file.

    Execution commonly starts at the project root while claims own narrow files.
    A symmetric path-overlap check on cwd would therefore make every neighbour's
    claim look foreign and serialize the whole fleet.  CONTRACT §10 requires the
    exec path to enforce the kill switch; file ownership remains check_write's job.
    """

    del cwd  # cwd is part of the public contract; ownership is intentionally not inferred from it.
    worker_id = worker_id or None
    try:
        snapshot = _reader_for(store).snapshot()
    except (OSError, sqlite3.Error, RuntimeError):
        return Decision(
            allow=True,
            code="",
            reason="fleet registry unreadable; operation allowed in degraded mode",
            worker_id=worker_id,
            degraded=True,
        )

    if worker_id is not None:
        worker = snapshot.workers.get(worker_id)
        if worker is not None and worker.revoked:
            detail = worker.revoke_reason or "причина владельцем не указана"
            return Decision(
                allow=False,
                code="FLEET_REVOKED",
                reason=(
                    f"Воркер {worker_id} отозван владельцем: {detail}. "
                    "Чтобы запросить продолжение, оставь владельцу request_note одним вызовом; "
                    "не создавай новый worker для обхода отзыва."
                ),
                worker_id=worker_id,
                degraded=False,
            )

    return Decision(
        allow=True,
        code="",
        reason="",
        worker_id=worker_id,
        degraded=False,
    )
