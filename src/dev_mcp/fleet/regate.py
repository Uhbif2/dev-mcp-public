"""Recheck stale red project gates using the existing Integrator._gate."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from .integrate import Integrator
from .model import norm_path, paths_overlap
from . import queue
from .store import Store

_LOCKS_GUARD = threading.Lock()
_PROJECT_LOCKS: dict[str, threading.RLock] = {}
RECHECK_AFTER_S = 1200
MIN_INTERVAL_S = 600
COLLECT_ERROR_THRESHOLD = 5
COLLECT_RE = re.compile(r"(?:^|\n)(?:_+\s*)?ERROR collecting\s+([^\s:]+\.(?:py|js|ts|mjs))", re.I)

@contextmanager
def project_gate_lock(project: str, *, blocking: bool = True) -> Iterator[bool]:
    """Per-project reentrant mutex shared by normal and maintenance gates.

    Normal: with project_gate_lock(project). Maintenance: blocking=False
    and inspect yielded acquired flag. Reentrancy permits calling _gate.
    """
    if not project:
        raise ValueError("project required")
    with _LOCKS_GUARD:
        lock = _PROJECT_LOCKS.setdefault(project, threading.RLock())
    acquired = lock.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()

def _epoch(ts: str) -> float:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0

def _collecting_paths(payload: dict, root: str) -> list[str]:
    text = str(payload.get("log_tail") or "")
    names = list(dict.fromkeys(COLLECT_RE.findall(text)))
    from .integrate import _failed_paths
    return _failed_paths(["ERROR " + x for x in names], root)

def _changed_since(root: str, zone: list[str], last_gate: float) -> bool:
    base = Path(root)
    names: list[str] = []
    for name in zone:
        try:
            path = Path(name)
            rel = path.relative_to(base)
            if rel.parts:
                names.append(rel.as_posix())
            if path.is_file() and path.stat().st_mtime > last_gate:
                return True
        except (OSError, ValueError):
            continue
    if not names:
        return False
    # A deleted tracked path has no mtime; use git's status as a fallback.
    try:
        proc = subprocess.run(["git", "status", "--porcelain", "--", *names],
                              cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
        return proc.returncode == 0 and any(
            line[:2].strip() in {"D", "R"} for line in proc.stdout.splitlines()
        )
    except (OSError, subprocess.SubprocessError):
        return False

class Regater:
    """Durable cooldown and latest-gate checks; call tick from server maintenance."""
    def __init__(self, store: Store, integrator: Integrator, *,
                 clock: Callable[[], float] = time.time,
                 stale_after_s: float = RECHECK_AFTER_S,
                 min_interval_s: float = MIN_INTERVAL_S):
        self.store, self.integrator = store, integrator
        self.clock, self.stale_after_s = clock, stale_after_s
        self.min_interval_s = min_interval_s

    def _last(self, project: str) -> tuple[int, float, int, dict] | None:
        with self.store.read() as tx:
            row = tx.one("SELECT id,ts,rc,payload FROM events WHERE project=? AND "
                         "kind='gate' ORDER BY id DESC LIMIT 1", (project,))
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        return int(row["id"]), _epoch(str(row["ts"])), int(row["rc"] or 0), payload

    def _cooldown(self, project: str, now: float) -> bool:
        with self.store.read() as tx:
            row = tx.one("SELECT ts FROM events WHERE project=? AND "
                         "kind='regate_attempt' ORDER BY id DESC LIMIT 1", (project,))
        return row is not None and now - _epoch(str(row["ts"])) < self.min_interval_s

    def _blocked_zone(self, project: str) -> list[str]:
        with self.store.read() as tx:
            zone, _ = queue.red_zone(tx, project)
            rows = tx.query("SELECT kind,state,owns FROM tasks WHERE project=? AND "
                            "(state='submitted' OR (kind='review' AND state "
                            "IN ('ready','taken','frozen')))", (project,))
        if not zone:
            return []
        for row in rows:
            try:
                owns = json.loads(row["owns"] or "[]")
            except (ValueError, TypeError):
                continue
            if isinstance(owns, list) and any(
                isinstance(o, str) and paths_overlap(o, z)
                for o in owns for z in zone
            ):
                return zone
        return []

    def _narrow_styk(self, project: str, root: str, failed: list[str]) -> None:
        # Old styk owns would otherwise keep all 18 obsolete error files blocked.
        if not failed:
            return
        owns = json.dumps(sorted({norm_path(f, project_root=root) for f in failed}),
                          ensure_ascii=False)
        with self.store.tx() as tx:
            for row in tx.query("SELECT tz,owns FROM tasks WHERE project=? AND tz LIKE 'styk:%' "
                                "AND state IN ('ready','submitted','draft','frozen')", (project,)):
                if row["owns"] != owns:
                    tx.change("UPDATE tasks SET owns=? WHERE project=? AND tz=?",
                              (owns, project, row["tz"]),
                              event={"kind":"regate_zone", "project":project,
                                     "tz":str(row["tz"]), "payload":{"failed":failed}})

    def tick(self) -> list[dict]:
        with self.store.read() as tx:
            projects = [(str(r["name"]), str(r["root"])) for r in
                        tx.query("SELECT name,root FROM projects WHERE active=1 ORDER BY name")]
        results: list[dict] = []
        for project, root in projects:
            before = self._last(project)
            now = self.clock()
            if before is None or before[2] == 0 or self._cooldown(project, now):
                continue
            zone = self._blocked_zone(project)
            if not zone or (now - before[1] < self.stale_after_s
                            and not _changed_since(root, zone, before[1])):
                continue
            with project_gate_lock(project, blocking=False) as acquired:
                if not acquired:
                    continue
                current = self._last(project)
                if current is None or current[0] != before[0] or self._cooldown(project, now):
                    continue
                self.integrator._record(kind="regate_attempt", project=project,
                                        payload={"previous_gate":before[0]})
                from .integrate import Batch
                result = self.integrator._gate(Batch(project, root), force=True)  # перепроверка красного — намеренно тот же ключ
                if result is None:
                    continue
                payload = result.get("payload") or {}
                collected = _collecting_paths(payload, root) if result.get("rc") else []
                failed = list(dict.fromkeys([*(payload.get("failed") or []), *collected]))
                if len(collected) >= COLLECT_ERROR_THRESHOLD:
                    self.integrator._record(kind="gate_collect_error", project=project,
                                             rc=int(result["rc"]),
                                             payload={"failed":collected, "count":len(collected)})
                if result.get("rc"):
                    self._narrow_styk(project, root, failed)
                else:
                    queue.close_resolved_styks(self.store)
                self.integrator._record(kind="regate_result", project=project,
                                         rc=int(result["rc"]),
                                         payload={"previous_gate":before[0], "failed":failed})
                results.append(result)
        return results


def run_regater(db_path: str | Path, stop_event: threading.Event, *,
                interval_s: float = 60.0) -> None:
    """Background maintenance entry-point, for the trusted server launcher.

    The integrator and this loop share project_gate_lock; failures in one tick
    are recorded and do not terminate future ticks or other project gates.
    """
    from .integrate import Integrator
    store = Store.open(str(db_path))
    integrator = Integrator(store, Path(db_path).with_name("integrate-state.json"))
    regater = Regater(store, integrator)
    while not stop_event.is_set():
        try:
            regater.tick()
        except Exception as exc:
            try:
                integrator._record(kind="regate_error",
                                   payload={"error": f"{type(exc).__name__}: {exc}"[:300]})
            except Exception:
                pass
        stop_event.wait(interval_s)
