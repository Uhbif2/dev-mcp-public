"""TZ-42: accepted-only, fail-closed claim integration.

Only a trusted Store and TZ-41 sealed acceptance authorize this adapter. The
Web agent never supplies an accepted flag or a workspace path. No caller may
treat a successful review as an automatically delivered release.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from typing import Any, Callable, Literal

from .claim_review_bite import (
    ClaimReviewError, _submission, verify_accepted_claim,
)
from .claim_tree import ClaimTreeError, ClaimWorktrees, _clean


class ClaimAcceptError(RuntimeError):
    """Rejected authority, unsafe tree, dirty trunk, or unavailable gate."""


@dataclass(frozen=True)
class ClaimAcceptResult:
    claim_id: int
    status: Literal["legacy", "integrated", "already_integrated",
                    "needs_result", "gate_failed"]
    accepted_sha: str = ""
    integrated_sha: str = ""
    conflict_paths: tuple[str, ...] = ()
    repair_status: str = ""
    repair_tz: str = ""
    gate_rc: int | None = None
    finished_at: str = ""

    @property
    def terminal_state(self) -> str:
        return self.status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(cwd: Path, *args: str, check: bool = True,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), "-c", "core.quotepath=false", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
            env=env, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaimAcceptError(f"Git unavailable: {exc}") from exc
    if check and p.returncode:
        raise ClaimAcceptError(
            f"git {args[0]} failed ({p.returncode}): "
            + p.stderr[:250].decode("utf-8", "replace")
        )
    return p


def _value(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.decode("utf-8", "strict").strip()


def _path(raw: bytes) -> str:
    try:
        name = raw.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise ClaimAcceptError("invalid Unicode Git pathname") from exc
    p = PurePosixPath(name)
    if (not name or name.startswith("/") or "\\" in name or "\x00" in name
            or ":" in name or p.is_absolute()
            or any(x in {"", ".", "..", ".git"} for x in name.split("/"))):
        raise ClaimAcceptError("unsafe Git pathname")
    return name


def _within_owner(root: Path, relative: str, owners: tuple[str, ...]) -> bool:
    prefix = root.as_posix().replace("\\", "/").rstrip("/") + "/"
    for owner in owners:
        normalized = str(owner).replace("\\", "/")
        if not normalized.casefold().startswith(prefix.casefold()):
            continue
        pattern = normalized[len(prefix):]
        if not pattern or ".." in pattern.split("/"):
            continue
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatchcase(relative.casefold(), pattern.casefold()):
                return True
        elif relative.casefold() == pattern.casefold() or (
                relative.casefold().startswith(pattern.casefold().rstrip("/") + "/")):
            return True
    return False


def _dirty_overlap(root: Path, paths: tuple[str, ...]) -> list[str]:
    """Урок 55: общий ствол не бывает чистым (ToDo-движение контроллера, работа других
    задач). Перемотка ствола и гейт по HEAD безопасны, пока грязь не касается путей этой
    правки; git сам откажет в ff, если перезаписал бы незакоммиченный файл."""
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    entries = status.split(b"\0")
    dirty: set[str] = set()
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        dirty.add(_path(entry[3:]))
        if entry[:1] in (b"R", b"C") and i < len(entries):
            dirty.add(_path(entries[i]))
            i += 1
    wanted = {x.casefold() for x in paths}
    return sorted(x for x in dirty if x.casefold() in wanted)


def _paths_and_tree(root: Path, base: str, revision: str,
                    owns: tuple[str, ...]) -> tuple[str, ...]:
    """Validate exact reviewed delta and its entire tree using machine Git paths."""
    delta = _git(root, "diff", "--no-renames", "--name-only", "-z",
                 base, revision).stdout
    paths = tuple(_path(p) for p in delta.split(b"\0") if p)
    if len(paths) > 400:
        raise ClaimAcceptError("oversized reviewed path set")
    if not paths:
        raise ClaimAcceptError("empty accepted delta")
    for path in paths:
        if not _within_owner(root, path, owns):
            raise ClaimAcceptError(f"unowned accepted path: {path}")
        # A lexical path can also escape via an existing symlink/junction.
        target = root / path
        if not (target.resolve(strict=False) == root or
                root in target.resolve(strict=False).parents):
            raise ClaimAcceptError("Git path resolves outside project")
    numbered = _git(root, "diff", "--no-renames", "--numstat", "-z",
                    base, revision).stdout
    changed_lines = 0
    for entry in numbered.split(b"\0"):
        if not entry:
            continue
        parts = entry.split(b"\t", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ClaimAcceptError("binary or malformed accepted diff")
        changed_lines += int(parts[0]) + int(parts[1])
        if changed_lines > 200_000:
            raise ClaimAcceptError("oversized accepted diff")
    entries = _git(root, "ls-tree", "-r", "-z", revision).stdout
    total_bytes = 0
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, sha = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ClaimAcceptError("malformed committed tree") from exc
        name = _path(raw_path)
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ClaimAcceptError(f"symlink/submodule/unsafe Git object: {name}")
        if name in paths:
            size = int(_value(root, "cat-file", "-s", sha.decode("ascii")))
            total_bytes += size
            if total_bytes > 16 * 1024 * 1024:
                raise ClaimAcceptError("oversized reviewed blobs")
    return paths


def _event_data(store: Any, kind: str, claim_id: int, revision: str) -> list[dict]:
    with store.read() as tx:
        rows = tx.query("SELECT payload FROM events WHERE kind=? ORDER BY id DESC", (kind,))
    events = []
    for row in rows:
        try:
            data = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        if (isinstance(data, dict) and data.get("claim_id") == claim_id
                and data.get("submission_revision") == revision):
            events.append(data)
    return events


def _ensure_conflict_repair(store: Any, proof: Any,
                            conflict_paths: tuple[str, ...],
                            trunk_sha: str) -> str:
    """Create one *runnable* repair, atomically linked to this accepted revision.

    A textual depends_on without a tasks row leaves accepted work stranded.
    The link event and ready task share a transaction, so retry after a crash
    finds the same resolver instead of allocating another review chain.
    """
    from .model import task_root
    from .review import _next_tz

    with store.tx() as tx:
        for row in tx.query(
                "SELECT payload FROM events WHERE kind='claim_conflict_repair_open' "
                "AND project=? AND tz=? ORDER BY id DESC", (proof.project, proof.tz)):
            try:
                data = json.loads(row["payload"] or "{}")
            except (ValueError, TypeError):
                continue
            if (data.get("claim_id") == proof.claim_id and
                    data.get("submission_revision") == proof.submission_revision):
                repair_tz = str(data.get("repair_tz") or "")
                if not repair_tz or tx.one(
                        "SELECT 1 FROM tasks WHERE project=? AND tz=?",
                        (proof.project, repair_tz)) is None:
                    raise ClaimAcceptError("durable repair dependency is missing")
                return repair_tz

        original = tx.one("SELECT * FROM tasks WHERE project=? AND tz=?",
                          (proof.project, proof.tz))
        if original is None or original["state"] != "accepted":
            raise ClaimAcceptError("accepted source for repair missing")
        # This is not a rejected submission: normal `fix` requires a before/after
        # bite against a *rejected* revision. A conflict against a newer trunk
        # instead needs fresh work and independent review on that trunk.
        repair_tz = _next_tz(tx, proof.project, f"repair:{task_root(proof.tz)}")
        body = "\n".join((
            f"[fleet-claim-conflict] source={proof.project}/{proof.tz}",
            f"claim_id={proof.claim_id}; accepted_revision={proof.submission_revision}",
            f"trunk_sha={trunk_sha}",
            "Conflict paths: " + json.dumps(conflict_paths, ensure_ascii=False),
            "Resolve these conflicting changes on a NEW claim branch based on the current trunk.",
            "Do not replay the previous accepted revision or merge automatically.",
            "Submit the repaired revision and obtain a NEW independent reviewer acceptance",
            "and a fresh green trunk gate before integration or delivery.",
        ))
        now = _now()
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proof.project, repair_tz, f"Repair claim Git conflict: {proof.tz}",
             "ready", int(original["priority"]), "work", original["spec_path"],
             original["spec_sha"], "[]", original["owns"], original["resources"],
             original["acceptance"], None, body, now, now),
            event={"kind": "claim_conflict_repair_open", "project": proof.project,
                   "tz": proof.tz,
                   "payload": {"claim_id": proof.claim_id,
                               "submission_revision": proof.submission_revision,
                               "repair_tz": repair_tz,
                               "conflict_paths": list(conflict_paths),
                               "trunk_sha": trunk_sha}},
        )
        return repair_tz


class ClaimAcceptFlow:
    """Serialized accepted revision -> reviewed-only Git commit -> trunk gate.

    The project flag is trusted configuration, not a Web-controlled argument.
    The gate callback MUST run the full project gate from the supplied trunk
    Path and return an integer rc. A separate queue integration adapter owns
    dispatch, durable repair-task creation and delivery eligibility.
    """

    def __init__(self, store: Any, *, settings: Callable[[str], dict] | None = None,
                 gate: Callable[[Path], int] | None = None,
                 repair: Callable[[int, str, tuple[str, ...]], str] | None = None):
        self.store = store
        if settings is None:
            from .goals import project_info
            settings = project_info
        self.settings = settings
        self.gate = gate
        self.repair = repair

    def _record(self, kind: str, proof: Any, **values: Any) -> None:
        with self.store.tx() as tx:
            tx.record_event(
                kind=kind, project=proof.project, tz=proof.tz,
                worker_id=proof.reviewer,
                payload=dict(claim_id=proof.claim_id,
                             submission_revision=proof.submission_revision,
                             submission_id=proof.submission_id,
                             accepted_sha=proof.submission_revision,
                             accepted_tree_sha=proof.tree_sha, **values),
            )

    def _gate(self, proof: Any, sha: str, paths: tuple[str, ...],
              root: Path, *, repeated: bool) -> ClaimAcceptResult:
        previous = _event_data(self.store, "claim_trunk_gate", proof.claim_id,
                               proof.submission_revision)
        if previous:
            state = previous[0]
            if state.get("integrated_sha") != sha:
                raise ClaimAcceptError("previous gate belongs to another trunk revision")
            rc = state.get("rc")
            return ClaimAcceptResult(
                proof.claim_id, "already_integrated" if rc == 0 else "gate_failed",
                proof.submission_revision, sha, gate_rc=rc if type(rc) is int else None,
                repair_status=str(state.get("repair_status", "")),
                finished_at=str(state.get("finished_at", "")),
            )
        if self.gate is None:
            raise ClaimAcceptError("full trunk gate unavailable; cannot report integration")
        if _value(root, "rev-parse", "HEAD") != sha or _dirty_overlap(root, paths):
            raise ClaimAcceptError("trunk changed before gate")
        try:
            rc = self.gate(root)
            if type(rc) is not int:
                raise ValueError("gate must return integer rc")
        except Exception:
            rc = -1
        if _value(root, "rev-parse", "HEAD") != sha or _dirty_overlap(root, paths):
            rc = -1
        status = "already_integrated" if repeated and rc == 0 else (
            "integrated" if rc == 0 else "gate_failed"
        )
        repair_status = "" if rc == 0 else "styk_required"
        finished = _now()
        self._record("claim_trunk_gate", proof, integrated_sha=sha, rc=rc,
                     status=status, repair_status=repair_status,
                     paths=list(paths), finished_at=finished)
        return ClaimAcceptResult(proof.claim_id, status, proof.submission_revision, sha,
                                 gate_rc=rc, repair_status=repair_status,
                                 finished_at=finished)

    def on_accept(self, claim_id: int, submission_revision: str) -> ClaimAcceptResult:
        """No trusted accepted=True parameter. Fail closed on stale/foreign evidence."""
        if type(claim_id) is not int or claim_id <= 0 or not re.fullmatch(
                r"[0-9a-f]{40}", submission_revision):
            raise ClaimAcceptError("invalid trusted claim identity or revision")
        # The shared/flag-off route does not produce TZ-41 sealed snapshots.
        # Defer to the existing legacy integrator without touching Git or
        # manufacturing an "accepted=True" proof. Require a trusted terminal
        # accepted task first; a forged author note alone never qualifies.
        with self.store.read() as tx:
            bound = tx.one(
                "SELECT c.worker_id,t.state,t.project,t.tz FROM claims c "
                "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                "WHERE c.id=?", (claim_id,))
        if bound is None or bound["state"] != "accepted":
            raise ClaimAcceptError("claim has no trusted terminal accept")
        flags = self.settings(str(bound["project"]))
        if flags.get("commit") is not True or flags.get("claim_trees") is not True:
            return ClaimAcceptResult(claim_id, "legacy", finished_at=_now())
        # Accepted event + reviewer claim + runs are checked before any Git.
        try:
            proof = verify_accepted_claim(self.store, claim_id, submission_revision)
            submission, ctx, task = _submission(self.store, claim_id)
        except (ClaimReviewError, ClaimTreeError) as exc:
            raise ClaimAcceptError(f"acceptance unverified: {exc}") from exc
        if self.gate is None:
            raise ClaimAcceptError("enabled claim integration requires a full trunk gate")
        if (ctx.mode != "isolated" or proof.project != submission.project
                or proof.tz != submission.tz or proof.submission_id != submission.submission_id
                or proof.submission_revision != submission.revision or
                proof.tree_sha != submission.tree_sha or
                ctx.branch != submission.branch or ctx.base_sha != submission.base_sha or
                ctx.claim_id != claim_id or task["task_state"] != "accepted"):
            raise ClaimAcceptError("accepted evidence and take metadata mismatch")
        manager = ClaimWorktrees(ctx.project_root)
        if manager.path(f"claim-{claim_id}").resolve(strict=True) != ctx.tree_path:
            raise ClaimAcceptError("accepted claim checkout was substituted")
        root = ctx.project_root
        # Reading snapshot's immutable Git objects is safe. Validate modes and
        # every path before creating accepted-only commits or touching trunk.
        paths = _paths_and_tree(root, ctx.base_sha, submission.revision,
                                submission.owns)
        if _value(root, "rev-parse", f"{submission.revision}^{{tree}}") != proof.tree_sha:
            raise ClaimAcceptError("accepted tree mismatch")
        with manager._lock():
            dirty = _dirty_overlap(root, paths)
            if dirty:
                raise ClaimAcceptError(
                    "dirty trunk overlaps accepted paths; do not repair automatically: "
                    + ", ".join(dirty[:3]))
            if _value(root, "rev-parse", "--verify", f"{ctx.base_sha}^{{commit}}") != ctx.base_sha:
                raise ClaimAcceptError("missing trusted claim base")
            if _value(ctx.tree_path, "symbolic-ref", "--short", "HEAD") != ctx.branch:
                raise ClaimAcceptError("claim branch changed")
            # Uncommitted files in source are *expected*: TZ-41 sealed their
            # exact content. Freshness is checked again under the lock.
            try:
                from .claim_review_bite import _fresh
                _fresh(ctx, submission)
            except ClaimReviewError as exc:
                raise ClaimAcceptError(f"source changed after acceptance: {exc}") from exc
            trunk_sha = _value(root, "rev-parse", "HEAD")
            if _git(root, "merge-base", "--is-ancestor", ctx.base_sha, trunk_sha,
                    check=False).returncode:
                raise ClaimAcceptError("trunk no longer descends from claim base")
            prior = _event_data(self.store, "claim_integration_prepared",
                                claim_id, submission_revision)
            already = next((p["integrated_sha"] for p in prior
                            if re.fullmatch(r"[0-9a-f]{40}", str(p.get("integrated_sha", "")))
                            and _git(root, "merge-base", "--is-ancestor",
                                     p["integrated_sha"], trunk_sha,
                                     check=False).returncode == 0), None)
            if already:
                self._record("claim_integrated", proof, integrated_sha=already,
                             recovered=True, paths=list(paths))
                return self._gate(proof, already, paths, root, repeated=True)
            previous_conflicts = _event_data(
                self.store, "claim_integration_obstacle", claim_id, submission_revision)
            if previous_conflicts:
                # Once a reviewed revision collided with a newer trunk,
                # replaying it after an unrelated tree change is NOT a fix.
                # Resolution must be a newly submitted, independently reviewed
                # revision, not the same author approval recycled.
                conflict = tuple(previous_conflicts[0].get("conflict_paths", ()))
                repair_tz = _ensure_conflict_repair(
                    self.store, proof, conflict, trunk_sha)
                return ClaimAcceptResult(
                    claim_id, "needs_result", proof.submission_revision, trunk_sha,
                    conflict_paths=conflict,
                    repair_status="requires_new_independent_acceptance",
                    repair_tz=repair_tz,
                    finished_at=_now(),
                )
            env = dict(os.environ,
                       GIT_AUTHOR_NAME="Fleet Accepted",
                       GIT_AUTHOR_EMAIL="fleet@localhost",
                       GIT_COMMITTER_NAME="Fleet Accepted",
                       GIT_COMMITTER_EMAIL="fleet@localhost",
                       GIT_AUTHOR_DATE="2001-01-01T00:00:00+0000",
                       GIT_COMMITTER_DATE="2001-01-01T00:00:00+0000")
            reviewed_commit = _git(
                root, "commit-tree", proof.tree_sha, "-p", ctx.base_sha,
                "-m", (f"Fleet accepted claim {claim_id}\n\nFleet-Submission: {proof.submission_id}\n"
                      f"Fleet-Task: {proof.project}/{proof.tz}\nFleet-Reviewer: {proof.reviewer}"),  # урок 56: без трейлера страж не выкатывает
                env=env,
            ).stdout.decode("ascii").strip()
            with tempfile.TemporaryDirectory(prefix="fleet-accepted-") as td:
                detached = Path(td) / "checkout"
                _git(root, "worktree", "add", "--detach", str(detached), reviewed_commit)
                try:
                    if _git(root, "merge-base", "--is-ancestor", trunk_sha,
                            reviewed_commit, check=False).returncode != 0:
                        rebased = _git(detached, "rebase", trunk_sha, check=False)
                        if rebased.returncode:
                            conflict = _git(detached, "diff", "--name-only",
                                            "--diff-filter=U", "-z", check=False).stdout
                            names = tuple(sorted(_path(p) for p in conflict.split(b"\0") if p))
                            _git(detached, "rebase", "--abort")
                            if not names:
                                raise ClaimAcceptError("rebase failed without typed paths")
                            repair_tz = _ensure_conflict_repair(
                                self.store, proof, names, trunk_sha)
                            repair_status = "needs_result"
                            if self.repair:
                                self.repair(claim_id, submission_revision, names)
                            self._record("claim_integration_obstacle", proof,
                                         conflict_paths=list(names),
                                         depends_on={"kind": "result",
                                                     "tz": repair_tz},
                                         resolver_work=f"{proof.project}/{repair_tz}",
                                         repair_status=repair_status,
                                         requires_new_acceptance=True,
                                         base_sha=ctx.base_sha, trunk_sha=trunk_sha)
                            return ClaimAcceptResult(
                                claim_id, "needs_result", proof.submission_revision, trunk_sha,
                                conflict_paths=names, repair_status=repair_status,
                                repair_tz=repair_tz,
                                finished_at=_now())
                    merged = _value(detached, "rev-parse", "HEAD")
                    # The prepared receipt closes the crash window after FF.
                    self._record("claim_integration_prepared", proof,
                                 integrated_sha=merged, base_sha=ctx.base_sha,
                                 trunk_before=trunk_sha, paths=list(paths))
                    if (_value(root, "rev-parse", "HEAD") != trunk_sha
                            or _dirty_overlap(root, paths)):
                        raise ClaimAcceptError("trunk changed during integration")
                    _git(root, "merge", "--ff-only", merged)
                finally:
                    _git(root, "worktree", "remove", "--force", str(detached))
            if _value(root, "rev-parse", "HEAD") != merged:
                raise ClaimAcceptError("trunk fast-forward lacks confirmed HEAD")
            self._record("claim_integrated", proof, integrated_sha=merged,
                         paths=list(paths), recovered=False)
            return self._gate(proof, merged, paths, root, repeated=False)


def on_accept(store: Any, claim_id: int, submission_revision: str, *,
              settings: Callable[[str], dict] | None = None,
              gate: Callable[[Path], int] | None = None,
              repair: Callable[[int, str, tuple[str, ...]], str] | None = None,
              ) -> ClaimAcceptResult:
    return ClaimAcceptFlow(store, settings=settings, gate=gate,
                           repair=repair).on_accept(claim_id, submission_revision)


def release_ready(store: Any, claim_id: int, submission_revision: str) -> bool:
    """Fail-closed delivery/canary boundary for an *exact current trunk HEAD*.

    Returning False is not an authorization to call legacy delivery. A trusted
    server caller must use this predicate before scheduling any release
    operation for a claim-mode submission. An author's event or a green gate
    from another HEAD is never accepted as delivery proof.
    """
    try:
        proof = verify_accepted_claim(store, claim_id, submission_revision)
        with store.read() as tx:
            row = tx.one("SELECT root FROM projects WHERE name=?", (proof.project,))
        if row is None:
            return False
        root = Path(str(row["root"])).resolve(strict=True)
        head = _value(root, "rev-parse", "HEAD")
        if not _clean(root):
            return False
        integrated = _event_data(store, "claim_integrated", claim_id, submission_revision)
        gates = _event_data(store, "claim_trunk_gate", claim_id, submission_revision)
        return bool(
            any(e.get("integrated_sha") == head for e in integrated)
            and any(e.get("integrated_sha") == head and type(e.get("rc")) is int
                    and e["rc"] == 0 for e in gates)
        )
    except (ClaimReviewError, ClaimAcceptError, ClaimTreeError, ValueError, OSError):
        return False
