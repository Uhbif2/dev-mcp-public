"""Conservative, evidence-based retirement of stale fleet tasks.

Only explicitly proven a/b/c classes are actionable. No changes during analysis.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import task_root

ACTIONABLE = frozenset({"a", "b", "c"})
OPEN = ("submitted", "frozen")
_BLOCKER = re.compile(r"Снять проектный блокер финального ревью\s+(?:[^/\s]+/)?([^\s]+)", re.I)


def _date(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, AttributeError):
        return None


def _paths(value: str) -> list[str]:
    try:
        result = json.loads(value)
        return [str(p).replace("\\", "/").casefold() for p in result if isinstance(p, str)] if isinstance(result, list) else []
    except (ValueError, TypeError):
        return []


def _relative(root: Path, owned: str) -> str | None:
    root_s = str(root.resolve()).replace("\\", "/").casefold().rstrip("/")
    path = owned.replace("\\", "/").casefold().removesuffix("/**")
    if not path.startswith(root_s + "/"):
        return None
    rel = path[len(root_s) + 1:]
    if not rel or ".." in rel.split("/") or any(c in rel for c in "*?["):
        return None
    return rel


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=8, check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_evidence(root: Path, owns: list[str], submitted_at: datetime | None,
                  *, require_clean: bool = True) -> str | None:
    repo = _git(root, "rev-parse", "--show-toplevel")
    if not owns or submitted_at is None or repo is None:
        return None
    if Path(repo).resolve() != root.resolve():
        return None
    rels = [_relative(root, owned) for owned in owns]
    if any(rel is None for rel in rels):
        return None
    commits = []
    for rel in rels:
        assert rel is not None
        # Class a needs a clean, tracked worktree. Class b needs a dated
        # history entry on the exact old path (including directory paths).
        if require_clean:
            if not _git(root, "ls-files", "--error-unmatch", "--", rel):
                return None
            status = _git(root, "status", "--porcelain", "--", rel)
            if status is None or status:
                return None
        entry = _git(root, "log", "-1", "--format=%H|%cI", "HEAD", "--", rel)
        if not entry or "|" not in entry:
            return None
        sha, stamp = entry.split("|", 1)
        commit_at = _date(stamp)
        if commit_at is None or commit_at <= submitted_at:
            return None
        commits.append(f"{rel}@{sha[:12]} ({stamp})")
    return "HEAD: " + "; ".join(commits[:3]) + (f" (+{len(commits)-3})" if len(commits) > 3 else "")


def _covers_owns(root: Path, stale: list[str], witness: list[str]) -> bool:
    """Every stale path must have a same-project ancestor/equal witness.

    A witness owning the entire project is not evidence of replacement.
    """
    base = str(root.resolve()).replace("\\", "/").casefold().rstrip("/")
    def normal(path: str) -> str:
        return path.replace("\\", "/").casefold().removesuffix("/**").rstrip("/")

    eligible = [normal(path) for path in witness if normal(path) != base
                and normal(path).startswith(base + "/")]
    return bool(stale and eligible and all(
        (item := normal(path)).startswith(base + "/") and any(
            item == candidate or item.startswith(candidate + "/")
            for candidate in eligible)
        for path in stale
    ))


def _replacement_evidence(root: Path, stale: dict[str, Any],
                          witness: dict[str, Any],
                          submitted: dict[str, datetime],
                          accepted: dict[str, datetime],
                          git_cache: dict[str, str | None]) -> str | None:
    stamp = submitted.get(stale["tz"])
    accepted_at = accepted.get(witness["tz"])
    if stamp is None or accepted_at is None or accepted_at <= stamp:
        return None
    owns = _paths(stale["owns"])
    if not _covers_owns(root, owns, _paths(witness["owns"])):
        return None
    if stale["tz"] not in git_cache:
        git_cache[stale["tz"]] = _git_evidence(root, owns, stamp, require_clean=False)
    proof = git_cache[stale["tz"]]
    if proof is None:
        return None
    return f"accepted {witness['tz']} ({accepted_at.isoformat()}) after submit {stamp.isoformat()}; {proof}"


def classify(store: Any, project: str, *, days: int = 14,
             now: datetime | None = None) -> list[dict[str, Any]]:
    if days < 0:
        raise ValueError("days must be non-negative")
    now = now or datetime.now(timezone.utc)
    with store.read() as tx:
        project_row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        if project_row is None:
            raise ValueError(f"unknown project: {project}")
        tasks = [dict(x) for x in tx.query(
            "SELECT * FROM tasks WHERE project=?", (project,))]
        events = [dict(x) for x in tx.query(
            "SELECT tz,kind,ts,payload FROM events WHERE project=? "
            "AND kind IN ('accept','submit','task_submitted','review_accept')", (project,))]
        active = {x["tz"] for x in tx.query(
            "SELECT tz FROM claims WHERE project=? AND state='active'", (project,))}
    root = Path(project_row["root"])
    by_tz = {x["tz"]: x for x in tasks}
    accepted: dict[str, datetime] = {}
    submitted: dict[str, datetime] = {}
    for ev in events:
        stamp = _date(ev["ts"])
        if stamp is None or not ev["tz"]:
            continue
        if ev["kind"] in ("accept", "review_accept"):
            accepted[ev["tz"]] = max(accepted.get(ev["tz"], stamp), stamp)
        if ev["kind"] in ("submit", "task_submitted"):
            submitted[ev["tz"]] = max(submitted.get(ev["tz"], stamp), stamp)
    rows = []
    git_cache: dict[str, str | None] = {}
    for task in tasks:
        if task["state"] not in OPEN:
            continue
        age_start = _date(task["updated_at"])
        if age_start is None:
            continue
        age = (now - age_start).total_seconds() / 86400
        if age < days:
            continue
        tz = task["tz"]
        owns = _paths(task["owns"])
        kind = "e"
        evidence = "нет достаточной улики"
        if tz in active:
            kind, evidence = "d", "активный claim — ожидает исполнителя/ревью"
        elif task["kind"] == "review" or task["state"] == "frozen" and task_root(tz) in by_tz and by_tz[task_root(tz)]["state"] == "submitted":
            kind, evidence = "d", "ревью/зависимость ещё ожидает решения"
        else:
            spec = task["spec_path"]
            spec_file = (root / spec).resolve() if spec else None
            if spec_file is not None and (
                not spec_file.is_relative_to(root.resolve()) or not spec_file.is_file()
            ):
                kind, evidence = "e", f"orphan: ТЗ отсутствует: {spec}"
            else:
                replacements = []
                for other in tasks:
                    if other["tz"] == tz or other["state"] != "accepted":
                        continue
                    if other["tz"] not in accepted:
                        continue
                    proof = _replacement_evidence(root, task, other, submitted, accepted, git_cache)
                    if proof:
                        replacements.append((accepted[other["tz"]], proof))
                if replacements:
                    _, evidence = max(replacements, key=lambda row: row[0])
                    kind = "b"
                else:
                    match = _BLOCKER.search(task["title"] + " " + task["body"][:300])
                    if match:
                        target = match.group(1).rstrip(".,;")
                        dep = by_tz.get(target)
                        if dep and dep["state"] in ("accepted", "rejected"):
                            kind, evidence = "c", f"блокер {target} уже {dep['state']}"
                        elif dep is None and target in accepted:
                            kind, evidence = "c", f"блокер {target}: есть accept event"
                    if kind == "e" and task["state"] == "submitted":
                        stamp = submitted.get(tz, age_start)
                        proof = _git_evidence(root, owns, stamp)
                        if proof:
                            kind, evidence = "a", proof
                    if kind == "e" and task["state"] == "submitted":
                        kind, evidence = "d", "сдано; подтверждения принятия/замены нет"
        rows.append({"id": tz, "project": project, "state": task["state"],
                     "age_days": round(age, 1), "class": kind, "evidence": evidence,
                     "from": task["state"], "updated_at": task["updated_at"]})
    return sorted(rows, key=lambda x: (-x["age_days"], x["id"]))


def _verify_b_before_apply(store: Any, project: str, rows: list[dict[str, Any]]) -> None:
    """Independently re-read the evidence before any --apply side effect."""
    candidates = [row for row in rows if row["class"] == "b"]
    if not candidates:
        return
    with store.read() as tx:
        project_row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        tasks = {row["tz"]: dict(row) for row in tx.query(
            "SELECT * FROM tasks WHERE project=?", (project,))}
        events = [dict(row) for row in tx.query(
            "SELECT tz,kind,ts FROM events WHERE project=? AND "
            "kind IN ('accept','review_accept','submit','task_submitted')", (project,))]
    if project_row is None:
        raise RuntimeError("class b verification: missing project")
    root = Path(project_row["root"])
    submitted: dict[str, datetime] = {}
    accepted: dict[str, datetime] = {}
    for event in events:
        stamp = _date(event["ts"])
        if stamp is None or not event["tz"]:
            continue
        mapping = accepted if event["kind"] in ("accept", "review_accept") else submitted
        mapping[event["tz"]] = max(mapping.get(event["tz"], stamp), stamp)

    git_cache: dict[str, str | None] = {}
    for row in candidates:
        stale = tasks.get(row["id"])
        if stale is None or stale["state"] != row["from"] or stale["updated_at"] != row["updated_at"]:
            raise RuntimeError(f"class b verification failed for {row['id']}: stale task changed")
        spec = stale["spec_path"]
        if spec and not (root / spec).is_file():
            raise RuntimeError(f"class b verification failed for {row['id']}: orphan spec")
        proven = any(
            _replacement_evidence(root, stale, witness, submitted, accepted, git_cache)
            == row["evidence"]
            for witness in tasks.values()
            if witness["tz"] != row["id"] and witness["state"] == "accepted"
        )
        if not proven:
            raise RuntimeError(f"class b verification failed for {row['id']}: no complete proof")


def retire(store: Any, project: str, *, days: int = 14, apply: bool = False,
           preimage_dir: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    rows = classify(store, project, days=days, now=now)
    counts = dict(sorted(Counter(r["class"] for r in rows).items()))
    result: dict[str, Any] = {"project": project, "days": days, "counts": counts,
                              "total": len(rows), "rows": rows, "applied": 0}
    if not apply:
        return result
    chosen = [r for r in rows if r["class"] in ACTIONABLE]
    if not chosen:
        return result
    _verify_b_before_apply(store, project, chosen)
    # Full snapshots, not merely classification, precede any change.
    with store.read() as tx:
        snapshots = []
        for row in chosen:
            original = tx.one("SELECT * FROM tasks WHERE project=? AND tz=?",
                              (project, row["id"]))
            if original:
                snapshots.append({"task": dict(original), "decision": row,
                                  "claims": [dict(c) for c in tx.query(
                                      "SELECT * FROM claims WHERE project=? AND tz=?",
                                      (project, row["id"]))]})
    directory = preimage_dir or Path(str(store.path)).parent / "retired"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    pre = directory / f"zombies-{stamp.replace(':', '').replace('+', '_')}.json"
    with pre.open("x", encoding="utf-8") as out:
        json.dump(snapshots, out, ensure_ascii=False, indent=2)
    result["preimage"] = str(pre)
    with store.tx() as tx:
        for entry in snapshots:
            item = entry["decision"]
            cursor = tx.change(
                "UPDATE tasks SET state='rejected',updated_at=? WHERE project=? AND tz=? "
                "AND state=? AND updated_at=? AND NOT EXISTS (SELECT 1 FROM claims "
                "WHERE project=? AND tz=? AND state='active')",
                (stamp, project, item["id"], item["from"], item["updated_at"],
                 project, item["id"]),
                event={"kind": "task_retired", "project": project, "tz": item["id"],
                       "payload": {"class": item["class"], "reason": "zombie queue cleanup",
                                   "evidence": item["evidence"], "preimage": str(pre)}})
            result["applied"] += int(cursor.rowcount or 0)
    return result
