"""Per-accept retry schedule, persisted in the existing integration event ledger.

Scheduling never grants acceptance or trusts file stats as seals. A changed
fingerprint only permits the normal, complete integration validation to run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable

MAX_DELAY_S = 3600.0
EARLY_WAKE_S = 120.0


def retry_reason(payload: dict) -> str:
    """Stable reason key for backoff. 05.11: an acceptance failure carries the
    command's output tail (timings, a 550-char window that shifts by a byte), so
    the raw text never repeated, attempt stayed 1 and a red gradle acceptance
    re-ran every 10 min all night (18 runs). Output is not the reason."""
    code = payload.get('reason_code')
    if code:
        return str(code)
    raw = str(payload.get('reason') or 'gate_wait')
    head, sep, _ = raw.partition(':')
    return (head if sep and len(head) <= 40 else raw)[:200]


class RetryPolicy:
    def __init__(self, store: Any, head: Callable[[str, str], str], *,
                 interval_s: float = 600, clock: Callable[[], float] = time.time):
        self.store, self.head = store, head
        self.interval_s, self.clock = interval_s, clock
        self._heads: dict[str, str] = {}
        self._checked: dict[tuple[str, str, str], float] = {}

    def begin_tick(self) -> None:
        self._heads.clear()

    def fingerprint(self, project: str, tz: str, payload: dict, *, files: bool = True) -> str:
        refs = {(project, tz)}
        for raw in payload.get('waiting_for') or []:
            if not isinstance(raw, str):
                continue
            p, sep, task = raw.partition('/')
            refs.add((p, task) if sep else (project, raw))
        data: list[Any] = []
        if retry_reason(payload) == 'source_dependencies':
            # 08.11: ТЗ, ждущее зависимостей, будит только смена их состояния (и собственного
            # определения). HEAD и take/submit ожидаемых задач будили его на каждом коммите
            # ведущего: 6 ТЗ Nova × 1340 полных строгих проверок с 26.10.
            with self.store.read() as tx:
                for p, task in sorted(refs):
                    row = tx.one('SELECT state,spec_sha,needs FROM tasks WHERE project=? AND tz=?',
                                 (p, task))
                    data.append((p, task, tuple(row) if row else None))
            return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        with self.store.read() as tx:
            root = str(tx.scalar('SELECT root FROM projects WHERE name=?', (project,)) or '')
            for p, task in sorted(refs):
                row = tx.one('SELECT state,spec_sha,needs,acceptance FROM tasks WHERE project=? AND tz=?',
                             (p, task))
                event = tx.scalar("SELECT MAX(id) FROM events WHERE project=? AND tz=? AND kind IN "
                                  "('accept','submit','release','reject','take','requeue',"
                                  "'submission_file_seal','claim_revision','claim_accept')", (p, task))
                data.append((p, task, tuple(row) if row else None, event))
            if payload.get('gate_wait'):
                data.append(('gate_finished',tx.scalar(
                    "SELECT MAX(id) FROM events WHERE project=? AND kind IN ('gate','gate_wait','integrate_error')",
                    (project,))))
        if project not in self._heads:
            self._heads[project] = self.head(root, 'HEAD') if root else ''
        data.append(self._heads[project])
        if payload.get('reason_code')=='resource_busy' or payload.get('reason')=='resource_busy':
            from .world import run_resource_snapshot
            data.append(run_resource_snapshot())
        if not files:
            return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        # File identity is only a wake-up signal. Refuse traversal/symlinks here;
        # the integrator still hashes and verifies the full candidate separately.
        paths = payload.get('deferred_paths') or payload.get('deferred') or []
        for rel in sorted(p for p in paths if isinstance(p, str))[:400]:
            path = Path(rel)
            if path.is_absolute() or '..' in path.parts or not root:
                continue
            target = Path(root) / path
            try:
                if target.is_symlink() or not target.resolve().is_relative_to(Path(root).resolve()):
                    continue
                stat = target.stat()
                data.append((rel, stat.st_mtime_ns, stat.st_size))
            except OSError:
                data.append((rel, None))
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()

    def due(self, project: str, tz: str, payload: dict) -> bool:
        retry = payload.get('retry') or {}
        if not retry:
            return True  # legacy event receives a durable schedule on its first retry
        now = self.clock()
        if now >= float(retry.get('next_at', 0)):
            return True
        # 05.11.2026: the early-wake fingerprint (SQL per waiting task, git HEAD, stat of
        # every deferred path) ran for every pending accept on every 20 s tick — ~13 % of
        # the server's CPU. A change is still noticed, at most EARLY_WAKE_S later.
        # The registry/HEAD part is cheap (indexed) and catches a new submit at once;
        # only the stat of deferred files waits for EARLY_WAKE_S.
        if retry.get('quick') and self.fingerprint(project, tz, payload, files=False) != retry['quick']:
            return True
        key = (project, tz, str(retry.get('fingerprint')))
        if now - self._checked.get(key, float('-inf')) < EARLY_WAKE_S:
            return False
        if len(self._checked) > 20000:
            self._checked.clear()
        self._checked[key] = now
        return self.fingerprint(project, tz, payload) != retry.get('fingerprint')

    def schedule(self, project: str, tz: str, payload: dict, previous: dict) -> dict:
        fingerprint = self.fingerprint(project, tz, payload)
        old = previous.get('retry') or {}
        reason = retry_reason(payload)
        unchanged = old.get('fingerprint') == fingerprint and old.get('reason') == reason
        attempt = min(16, int(old.get('attempt', 0)) + 1) if unchanged else 1
        delay = min(MAX_DELAY_S, self.interval_s * 2 ** min(4, attempt - 1))
        return {'fingerprint': fingerprint, 'quick': self.fingerprint(project, tz, payload, files=False),
                'attempt': attempt, 'reason': reason,
                'next_at': self.clock() + delay, 'delay_s': delay}
