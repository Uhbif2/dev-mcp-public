"""Read-only, fail-closed fleet SQLite canary observations."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CANARY_SECONDS = 45 * 60  # 45 minutes catches repeated maintenance cycles.
SAMPLE_SECONDS = 5 * 60
BLOCK_THAW_LIMIT = 5  # Five repeats are a pathological loop, not a slow task.
ERROR_FACTOR = 3  # Triple the baseline error rate AND at least five new failures.
ERROR_MIN = 5
TAKE_SHARE_LIMIT = .40  # One task dominating >40% of claims means starvation.
MIN_TAKES_FOR_SHARE = 10  # Avoid a false positive on a single quiet-hour claim.


@dataclass(frozen=True)
class Metrics:
    last_id: int
    errors: int
    takes: tuple[str, ...]
    blocks: tuple[str, ...]
    thaws: tuple[str, ...]
    available: bool = True
    transitions: tuple[tuple[str, str], ...] = ()


def _open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.execute("PRAGMA query_only=ON")
    return conn


def snapshot(path: Path, since_id: int = 0, *, now: datetime | None = None,
             window_seconds: int = CANARY_SECONDS) -> Metrics:
    """All reads use SQLite URI mode=ro. Missing/broken databases fail closed."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
    with closing(_open_readonly(path)) as conn:
        conn.execute("SELECT id,ts,kind,project,tz FROM events LIMIT 0")
        last_id = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
        rows = conn.execute(
            "SELECT id,kind,project,tz FROM events "
            "WHERE id>? AND ts>=? ORDER BY id", (since_id, cutoff)
        ).fetchall()
    errors = sum(str(row[1]) == "maintenance_error" for row in rows)
    takes = []
    blocks = []
    thaws = []
    transitions = []
    for _id, kind, project, tz in rows:
        key = str(project or "") + "/" + str(tz or "")
        if kind in ("take", "claim"):
            takes.append(key)
        elif kind == "block":
            blocks.append(key)
            transitions.append((kind, key))
        elif kind == "thaw":
            thaws.append(key)
            transitions.append((kind, key))
    return Metrics(last_id, errors, tuple(takes), tuple(blocks), tuple(thaws),
                   transitions=tuple(transitions))


def regression(baseline: Metrics, observed: Metrics) -> str | None:
    """A stable machine code, never guessed from prose."""
    pending = set()
    pairs = Counter()
    for kind, key in observed.transitions:
        if kind == "block":
            pending.add(key)
        elif kind == "thaw" and key in pending:
            pairs[key] += 1
            pending.remove(key)
    if any(count >= BLOCK_THAW_LIMIT for count in pairs.values()):
        return "canary_cycle"
    if observed.errors >= ERROR_MIN and observed.errors >= ERROR_FACTOR * max(1, baseline.errors):
        return "canary_maintenance"
    if len(observed.takes) >= MIN_TAKES_FOR_SHARE:
        top = max(Counter(observed.takes).values(), default=0)
        if top / len(observed.takes) > TAKE_SHARE_LIMIT:
            return "canary_skew"
    return None


# Ствол = только принятая работа (решение 09.10): путь, который входит в owns задачи проекта,
# ещё не прошедшей ревью, в живую систему не выкатывается.
UNREVIEWED_STATES = ("ready", "taken", "submitted", "frozen")


def unreviewed_owners(path: Path, project: str, root: Path, rel_paths: tuple[str, ...]) -> list[str]:
    """Пары «путь <- задача» для изменённых путей, чьи задачи ещё не приняты. Ошибка чтения — fail closed."""
    from fnmatch import fnmatch

    base = str(root).replace("\\", "/").rstrip("/").casefold()
    # ToDo/ — тексты ТЗ и отчёты, на работу живой системы не влияют.
    rel_paths = tuple(p for p in rel_paths if not p.replace(chr(92), '/').casefold().startswith('todo/'))
    targets = [f"{base}/{p.replace(chr(92), '/').lstrip('/')}".casefold() for p in rel_paths]
    hits: list[str] = []
    with closing(_open_readonly(path)) as conn:
        rows = conn.execute(
            "SELECT tz,owns FROM tasks WHERE project=? AND state IN (?,?,?,?) "
            "AND tz NOT LIKE 'review:%'",
            (project, *UNREVIEWED_STATES),
        ).fetchall()
    for tz, owns_json in rows:
        try:
            owns = [str(o).replace("\\", "/").casefold() for o in json.loads(owns_json or "[]")]
        except ValueError:
            continue
        for target in targets:
            for own in owns:
                own = own.rstrip("/")
                if target == own or target.startswith(own + "/") or fnmatch(target, own.replace("**", "*")):
                    hits.append(f"{target[len(base) + 1:]} <- {tz}")
                    break
    return hits
