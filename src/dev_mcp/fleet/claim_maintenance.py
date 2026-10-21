"""TZ-43: fail-closed inspection and explicit cleanup of isolated claim Git worktrees.

Nothing here selects a claim based on age alone, unlocks an integration lock,
prunes Git metadata, repairs Git history, or changes the trunk.  The scheduler
connection belongs to a separately reviewed controller request.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping

from .claim_tree import ClaimTreeError, ClaimWorktrees, _ancestor, _clean, _git
from .claim_review_bite import ClaimReviewError, verify_accepted_claim


MAX_REPAIRS_PER_AUDIT = 20


@dataclass(frozen=True)
class Finding:
    kind: str
    claim_id: int | None
    project: str
    path: str
    branch: str = ""
    base_sha: str = ""
    head_sha: str = ""
    owner: str = ""
    lease_state: str = ""
    task_state: str = ""
    dirty: bool | None = None
    finished_at: str = ""
    accepted_sha: str = ""
    integrated_sha: str = ""
    repair_status: str = ""
    lock_owner: str = ""
    reason: str = ""
    eligible: bool = False


@dataclass(frozen=True)
class Audit:
    project: str
    enabled: bool
    trunk_sha: str = ""
    lock_present: bool = False
    findings: tuple[Finding, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class CleanupCandidate:
    claim_id: int
    path: str
    branch: str
    head_sha: str
    trunk_sha: str
    finished_at: str


@dataclass(frozen=True)
class CleanupPlan:
    project: str
    trunk_sha: str
    cutoff: str
    candidates: tuple[CleanupCandidate, ...]
    audit: Audit


@dataclass(frozen=True)
class CleanupOutcome:
    removed: tuple[int, ...]
    held: tuple[int, ...]
    errors: tuple[str, ...]


def _json(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) is not None


def _take_context(events: list[Mapping[str, Any]], claim_id: int) -> dict[str, Any]:
    for row in reversed(events):
        if row["kind"] != "take":
            continue
        payload = _json(row["payload"])
        binding = payload.get("claim_context")
        if isinstance(binding, dict) and binding.get("claim_id") == claim_id:
            return binding
    return {}


def _git_worktrees(root: Path) -> dict[Path, dict[str, str]]:
    """Parse the machine-readable NUL form; never guess at quoted filenames."""
    try:
        p = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaimTreeError(f"worktree inventory unavailable: {exc}") from exc
    if p.returncode:
        raise ClaimTreeError("worktree inventory refused")
    if len(p.stdout) > 2_000_000:
        raise ClaimTreeError("oversized worktree inventory")
    records: dict[Path, dict[str, str]] = {}
    block: dict[str, str] = {}
    for field in p.stdout.split(b"\0") + [b""]:
        if not field:
            if "worktree" in block:
                loc = Path(block["worktree"])
                if loc in records:
                    raise ClaimTreeError("duplicate worktree inventory entry")
                records[loc] = block
            block = {}
            continue
        try:
            text = field.decode("utf-8", "strict")
        except UnicodeError as exc:
            raise ClaimTreeError("invalid Unicode worktree inventory") from exc
        key, _, value = text.partition(" ")
        if key in block:
            raise ClaimTreeError("repeated worktree inventory attribute")
        block[key] = value
    return records


def _lock_evidence(path: Path, claims: list[Mapping[str, Any]]) -> str:
    """A lock's age never proves its owner dead; PID alone can be recycled.

    Core currently writes empty lockfiles. Those have no reliable owner
    provenance and are explicitly unverified, not stale-by-TTL.
    """
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return "unverified:unsafe-lock-link"
    if not path.exists():
        return ""
    try:
        raw = path.read_bytes()
        if len(raw) > 4096:
            return "unverified:oversized-owner-record"
        data = _json(raw.decode("utf-8", "strict"))
        pid = data.get("pid")
        worker = data.get("worker_id")
        born = data.get("process_started_at")
        if type(pid) is not int or pid <= 0 or not isinstance(worker, str):
            return "unverified:missing-pid-or-worker"
        active_lease = any(c["worker_id"] == worker and c["lease_state"] == "active"
                           for c in claims)
        try:
            import psutil  # optional, verification ONLY; never required to boot
            process = psutil.Process(pid)
            alive = process.is_running()
            start_verified = (type(born) in (int, float)
                              and abs(process.create_time() - float(born)) < 1)
        except (ImportError, OSError, ValueError, TypeError):
            return "unverified:process-generation-unavailable"
        if alive and start_verified and active_lease:
            return f"verified-live:pid={pid},worker={worker}"
        return ("not-proven-live:pid-or-generation-or-lease-mismatch;"
                "requires-diagnosis-not-takeover")
    except (OSError, UnicodeError, ValueError):
        return "unverified:owner-record-unreadable"


def _events(store: Any, project: str) -> list[Mapping[str, Any]]:
    with store.read() as tx:
        rows = list(tx.query(
            "SELECT id,kind,project,tz,worker_id,ts,payload FROM events "
            "WHERE project=? AND kind IN "
            "('take','accept','claim_accept','claim_integrated','claim_trunk_gate',"
            "'claim_integration_obstacle','claim_abandon_authorized') "
            "ORDER BY id LIMIT 25001",
            (project,),
        ))
    if len(rows) > 25000:
        raise ClaimTreeError("oversized durable claim event history")
    return rows


class ClaimMaintenance:
    """Read-only audit; explicit reviewed dry-run is required before cleanup."""

    def __init__(self, store: Any, *,
                 settings: Callable[[str], Mapping[str, Any]] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        if settings is None:
            from .goals import project_info
            settings = project_info
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _enabled(self, project: str) -> bool:
        flags = self.settings(project)
        return flags.get("claim_trees") is True and flags.get("commit") is True

    def audit(self, project: str, *, cutoff: datetime | None = None) -> Audit:
        if cutoff is not None and (cutoff.tzinfo is None
                                   or cutoff > self.clock().astimezone(timezone.utc)):
            return Audit(project, False, error="invalid or future cutoff")
        if not self._enabled(project):
            return Audit(project, False, error="claim_trees or commit is disabled")
        try:
            with self.store.read() as tx:
                row = tx.one("SELECT root FROM projects WHERE name=? AND active=1",
                             (project,))
                if row is None:
                    raise ClaimTreeError("unknown or inactive project")
                claims = [dict(x) for x in tx.query(
                    "SELECT c.id,c.tz,c.worker_id,c.state AS lease_state,"
                    "c.released_at,t.state AS task_state,w.last_seen,w.revoked_at "
                    "FROM claims c JOIN tasks t ON c.project=t.project AND c.tz=t.tz "
                    "JOIN workers w ON c.worker_id=w.id "
                    "WHERE c.project=? ORDER BY c.id", (project,))]
            manager = ClaimWorktrees(row["root"])
            root = manager.root
            trunk = _git(root, "rev-parse", "HEAD").stdout.strip()
            if not _sha(trunk):
                raise ClaimTreeError("unverified trunk HEAD")
            inventory = _git_worktrees(root)
            events = _events(self.store, project)
            lock_path = manager.workspace / ".integration.lock"
            locked = lock_path.exists() or lock_path.is_symlink()
            if getattr(lock_path, "is_junction", lambda: False)():
                locked = True
            # Core's lock contains no durable PID+start-time owner receipt.
            # A missing heartbeat therefore NEVER authorizes unlinking it.
            lock_owner = _lock_evidence(lock_path, claims) if locked else ""
            seen: set[Path] = set()
            findings: list[Finding] = []
            if len(claims) > 25_000 or len(inventory) > 25_000:
                raise ClaimTreeError("oversized claim/worktree inventory")
            for claim in claims:
                cid = int(claim["id"])
                path = manager.path(f"claim-{cid}")
                meta = inventory.get(path)
                seen.add(path)
                state = str(claim["task_state"])
                lease = str(claim["lease_state"])
                ctx = _take_context(events, cid)
                # Claims of the shared epoch never had a worktree. Treating
                # each one as missing_tree caused thousands of repair tasks.
                if ctx.get("mode") != "isolated":
                    continue
                branch = manager.branch(f"claim-{cid}")
                base = str(ctx.get("base_sha") or "")
                binding_valid = (
                    ctx.get("mode") == "isolated"
                    and ctx.get("worker_id") == claim["worker_id"]
                    and ctx.get("project") == project and ctx.get("tz") == claim["tz"]
                    and ctx.get("branch") == branch
                    and ctx.get("tree_path") == str(path)
                    and _sha(base)
                )
                missing = meta is None or not path.is_dir()
                forbidden = path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                dirty: bool | None = None
                head = ""
                reason = ""
                if not missing and not forbidden:
                    try:
                        if path.resolve(strict=True) != path or (
                                manager.workspace.resolve(strict=True)
                                not in path.resolve(strict=True).parents):
                            forbidden = True
                        else:
                            head = _git(path, "rev-parse", "HEAD").stdout.strip()
                            dirty = not _clean(path)
                    except (ClaimTreeError, OSError, ValueError):
                        reason = "unverifiable worktree"
                if forbidden:
                    reason = "symlink/junction or path escaped trusted workspace"
                elif missing:
                    reason = "registered claim lacks a safe checkout"
                elif not meta or meta.get("branch") != f"refs/heads/{branch}":
                    reason = "branch/ref disagrees with durable claim identity"
                elif not _sha(head):
                    reason = "invalid or unreadable worktree HEAD"
                elif not binding_valid:
                    reason = "missing or inconsistent trusted take binding"
                elif dirty:
                    reason = "dirty worktree contains uncommitted data"
                proof = self._proof(project, claim, events, trunk, root)
                kind = "held"
                eligible = False
                if missing:
                    kind = "missing_tree"
                elif locked:
                    kind = "stale_lock"
                    reason = "integration lock owner not independently verified"
                elif lease == "active" or state in {"taken", "submitted", "review", "rejected", "ready"}:
                    kind = "active"
                    reason = reason or "nonterminal claim/task"
                elif not reason and proof and cutoff is not None:
                    finished = _date(proof["finished_at"])
                    if (finished is not None and finished < cutoff
                            and finished <= self.clock().astimezone(timezone.utc)
                            and not dirty and _ancestor(root, head, trunk)):
                        kind = "terminal_ready_to_cleanup"
                        eligible = True
                    else:
                        reason = "fresh, invalid finish time, or unmerged branch tip"
                elif not reason:
                    reason = "no verified terminal integration or abandonment"
                findings.append(Finding(
                    kind, cid, project, str(path), branch, base, head,
                    str(claim["worker_id"]), lease, state, dirty,
                    str(proof.get("finished_at", "") if proof else ""),
                    str(proof.get("accepted_sha", "") if proof else ""),
                    str(proof.get("integrated_sha", "") if proof else ""),
                    "needs_result" if any(x["kind"] == "claim_integration_obstacle"
                                          and x["tz"] == claim["tz"] for x in events) else "",
                    lock_owner, reason, eligible))
            for path, meta in inventory.items():
                if path == root or path in seen:
                    continue
                if manager.workspace not in path.parents:
                    continue
                findings.append(Finding(
                    "orphan", None, project, str(path),
                    meta.get("branch", ""), head_sha=meta.get("HEAD", ""),
                    reason="Git worktree without a durable claim binding"))
            return Audit(project, True, trunk, locked, tuple(findings))
        except (ClaimTreeError, OSError, ValueError, TypeError, KeyError) as exc:
            return Audit(project, False, error=f"fail-closed: {exc}")

    def _proof(self, project: str, claim: Mapping[str, Any],
               events: list[Mapping[str, Any]], trunk: str, root: Path
               ) -> dict[str, str] | None:
        cid = int(claim["id"])
        tz = claim["tz"]
        if claim["lease_state"] != "released" or not claim["released_at"]:
            return None
        same = [(x["kind"], _json(x["payload"]), x) for x in events
                if x["tz"] == tz]
        if claim["task_state"] == "accepted":
            accepted = [p for kind, p, _ in same if kind == "claim_accept"
                        and p.get("claim_id") == cid
                        and _sha(p.get("submission_revision"))]
            for payload in reversed(accepted):
                revision = payload["submission_revision"]
                try:
                    verify_accepted_claim(self.store, cid, revision)
                except (ClaimReviewError, ClaimTreeError, ValueError, OSError):
                    continue
                integrated = [p for kind, p, _ in same if kind == "claim_integrated"
                              and p.get("claim_id") == cid
                              and p.get("submission_revision") == revision
                              and _sha(p.get("integrated_sha"))]
                for record in integrated:
                    sha = record["integrated_sha"]
                    gates = [p for kind, p, _ in same if kind == "claim_trunk_gate"
                             and p.get("claim_id") == cid
                             and p.get("submission_revision") == revision
                             and p.get("integrated_sha") == sha
                             and type(p.get("rc")) is int and p["rc"] == 0
                             and _date(p.get("finished_at")) is not None]
                    if gates and _ancestor(root, sha, trunk):
                        return {"accepted_sha": revision, "integrated_sha": sha,
                                "finished_at": str(gates[-1]["finished_at"])}
        if claim["task_state"] == "abandoned":
            for kind, p, e in reversed(same):
                if (kind == "claim_abandon_authorized"
                        and e["worker_id"] is None
                        and p.get("claim_id") == cid
                        and p.get("authority") == "fleet-controller"
                        and p.get("authorized") is True
                        and _date(p.get("finished_at")) is not None):
                    return {"finished_at": str(p["finished_at"])}
        return None

    def plan(self, project: str, *, older_than: datetime) -> CleanupPlan:
        audit = self.audit(project, cutoff=older_than)
        entries = tuple(CleanupCandidate(
            x.claim_id, x.path, x.branch, x.head_sha, audit.trunk_sha, x.finished_at)
            for x in audit.findings if x.eligible and x.claim_id is not None)
        return CleanupPlan(project, audit.trunk_sha, older_than.isoformat(), entries, audit)

    def execute(self, plan: CleanupPlan) -> CleanupOutcome:
        """Apply only a previously enumerated dry-run, re-checking under Store tx.

        The Git manager's core close rechecks cleanliness, branch and ancestor
        under its exclusive lock. No force/prune, even for abandoned claims.
        """
        removed: list[int] = []
        held: list[int] = []
        errors: list[str] = []
        if not plan.audit.enabled or plan.audit.project != plan.project:
            return CleanupOutcome((), tuple(x.claim_id for x in plan.candidates),
                                  ("invalid dry-run audit",))
        cutoff = _date(plan.cutoff)
        if cutoff is None or cutoff > self.clock().astimezone(timezone.utc):
            return CleanupOutcome((), tuple(x.claim_id for x in plan.candidates),
                                  ("invalid cutoff",))
        for candidate in plan.candidates:
            fresh = self.audit(plan.project, cutoff=cutoff)
            current = next((f for f in fresh.findings
                            if f.claim_id == candidate.claim_id), None)
            if (not fresh.enabled or fresh.lock_present or fresh.trunk_sha != plan.trunk_sha
                    or current is None or not current.eligible
                    or current.path != candidate.path or current.branch != candidate.branch
                    or current.head_sha != candidate.head_sha
                    or current.finished_at != candidate.finished_at):
                held.append(candidate.claim_id)
                continue
            try:
                with self.store.tx() as tx:
                    state = tx.one(
                        "SELECT t.state AS task_state,c.state AS lease_state,"
                        "c.released_at FROM claims c JOIN tasks t "
                        "ON c.project=t.project AND c.tz=t.tz "
                        "WHERE c.id=? AND c.project=?",
                        (candidate.claim_id, plan.project))
                    if (state is None or state["lease_state"] != "released"
                            or not state["released_at"]
                            or state["task_state"] not in {"accepted", "abandoned"}):
                        held.append(candidate.claim_id)
                        continue
                    # This core operation does NOT force-delete an unmerged tip.
                    manager = ClaimWorktrees(self._root(plan.project))
                    manager.close(f"claim-{candidate.claim_id}", abandon=False)
                    tx.record_event(
                        kind="claim_maintenance_closed", project=plan.project,
                        payload={"claim_id": candidate.claim_id,
                                 "branch": candidate.branch,
                                 "tree_head": candidate.head_sha,
                                 "trunk_sha": plan.trunk_sha,
                                 "dry_run_cutoff": plan.cutoff})
                removed.append(candidate.claim_id)
            except (ClaimTreeError, OSError, ValueError) as exc:
                held.append(candidate.claim_id)
                errors.append(f"{candidate.claim_id}: {exc}")
        return CleanupOutcome(tuple(removed), tuple(held), tuple(errors))

    def _root(self, project: str) -> Path:
        with self.store.read() as tx:
            item = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (project,))
        if item is None:
            raise ClaimTreeError("unknown maintenance project")
        return Path(item["root"])

    def repair_findings(self, audit: Audit) -> tuple[str, ...]:
        """Bounded repair admission with backpressure across repeated ticks."""
        if not audit.enabled:
            return ()
        opened: list[str] = []
        for f in audit.findings:
            if f.kind not in {"missing_tree", "orphan", "stale_lock"}:
                continue
            # A global integration lock is ONE incident, never N worktree
            # incidents. Do not fan it out across historical claims.
            problem_path = (str(Path(f.path).parent / ".integration.lock")
                            if f.kind == "stale_lock" else f.path)
            digest = hashlib.sha256((audit.project + "\0" + problem_path).encode()).hexdigest()[:12]
            base = f"repair:claim-maintenance:{digest}"
            with self.store.tx() as tx:
                existing = tx.one("SELECT tz FROM tasks WHERE project=? AND tz=?",
                                  (audit.project, base))
                if existing:
                    continue
                outstanding = int(tx.scalar(
                    "SELECT COUNT(*) FROM tasks WHERE project=? AND "
                    "tz LIKE 'repair:claim-maintenance:%' "
                    "AND state NOT IN ('accepted','abandoned')",
                    (audit.project,),
                ) or 0)
                if outstanding >= MAX_REPAIRS_PER_AUDIT:
                    break
                now = self.clock().astimezone(timezone.utc).isoformat()
                # Repair workers receive a unique diagnosis document, never
                # ownership of the affected agent's source or worktree.
                diagnosis = (self._root(audit.project) / "ToDo" / "decisions" /
                             f"claim-maintenance-{digest}.md")
                owns = json.dumps([str(diagnosis)])
                tx.change(
                    "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                    "needs,owns,resources,acceptance,body,created_at,updated_at) "
                    "VALUES(?,?,?,'ready',1,'work','','','[]',?,'[]','[]',?,?,?)",
                    (audit.project, base, "Diagnose claim worktree inconsistency",
                     owns, f"[fleet-maintenance-repair] finding={f.kind}\n"
                     f"path={f.path}\nclaim_id={f.claim_id}\n"
                     f"Write read-only diagnosis to {diagnosis}. "
                     "Never force/prune/unlock or edit another worker's code.",
                     now, now),
                    event={"kind": "claim_maintenance_repair_open",
                           "project": audit.project, "tz": base,
                           "payload": {"claim_id": f.claim_id, "reason": f.kind}},
                )
                opened.append(base)
                if len(opened) >= MAX_REPAIRS_PER_AUDIT:
                    break
        return tuple(opened)

    def _tz(self, project: str, cid: int | None) -> str:
        if cid is None:
            return ""
        with self.store.read() as tx:
            row = tx.one("SELECT tz FROM claims WHERE id=? AND project=?",
                         (cid, project))
        return str(row["tz"]) if row else ""
