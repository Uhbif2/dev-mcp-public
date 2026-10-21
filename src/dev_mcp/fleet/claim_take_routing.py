"""Fail-closed claim-worktree adapter for queue, tool routes and successors.

Public contract for TZ-41/42/43:

    router = ClaimTakeRouting(store)
    ctx = router.resolve_claim_context(agent, claim_id)
    path = project_path_to_claim(requested_path, ctx, read_only=False)
    command = claim_command_environment(ctx)

The trusted queue calls prepare_claim_context immediately after allocating an
id, then persists ctx.as_event() in the same transaction as the claim. No
agent-provided tree mapping or checkout mode is ever accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .claim_context import ClaimContext, ClaimRoutingError
from .claim_tree import ClaimTreeError, ClaimWorktrees, _git, python_environment
from .model import norm_path, paths_overlap


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _dirty_owned_paths(root: Path, owns: Sequence[str] | None,
                       needs: Sequence[str]) -> list[str]:
    """Only dirt intersecting this claim's source zone blocks a HEAD checkout.

    Git -z status includes both sides of renames. Task needs are normally TZ
    identifiers, not paths, and must not accidentally reserve a file zone.
    """
    result = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = result.stdout.split("\0")
    changed: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        changed.append(entry[3:])
        if (entry[0] in "RC" or entry[1] in "RC") and i < len(entries) and entries[i]:
            changed.append(entries[i])
            i += 1
    zones = [norm_path(item, project_root=str(root)) for item in (owns or ())]
    zones.extend(norm_path(item, project_root=str(root)) for item in needs
                 if Path(item).is_absolute())
    bad: list[str] = []
    for rel in changed:
        if rel.replace("\\", "/").casefold().startswith("todo/"):
            continue
        dirty = norm_path(rel, project_root=str(root))
        if owns is None or any(paths_overlap(dirty, zone) for zone in zones):
            bad.append(rel)
    return bad


def prepare_claim_context(*, claim_id: int, worker_id: str, project: str,
                          tz: str, project_root: str | Path,
                          settings: Mapping[str, Any] | None = None,
                          owns: Sequence[str] | None = None,
                          needs: Sequence[str] = (),
                          review_revision: str = "") -> ClaimContext:
    """Prepare checkout outside the writer, while the queue holds a reservation.

    The deterministic `claim-<id>` ID recovers a worktree left by a process
    crash between Git's open and reservation finalization; no second branch is created.
    Worktree mode is enabled *only* when both trusted flags are exactly true.
    """
    if (type(claim_id) is not int or claim_id <= 0 or not worker_id or not project or not tz
            or len(str(claim_id)) > 128 or len(worker_id) > 128):
        raise ClaimRoutingError("invalid trusted claim identity")
    info = settings or {}
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ClaimRoutingError(f"project root unavailable: {exc}") from exc
    if info.get("claim_trees") is not True or info.get("commit") is not True:
        return ClaimContext(claim_id, worker_id, project, tz, root, root, "", "", "shared")
    if tz.startswith("review:"):
        # A review must never inspect the current trunk in place of the
        # candidate it is supposed to judge.
        if not review_revision or len(review_revision) != 40 or any(
            char not in "0123456789abcdef" for char in review_revision
        ):
            raise ClaimRoutingError("isolated review requires a sealed revision")
    elif review_revision:
        raise ClaimRoutingError("only review claims may bind a sealed revision")
    tree_id = f"claim-{claim_id}"
    try:
        manager = ClaimWorktrees(root)
        sha = _git(root, "rev-parse", "--verify", "HEAD").stdout.strip()
        if review_revision and _git(
            root, "rev-parse", "--verify", f"{review_revision}^{{commit}}"
        ).stdout.strip() != review_revision:
            raise ClaimRoutingError("sealed review revision unavailable")
        path = manager.path(tree_id)
        if getattr(path, "is_junction", lambda: False)():
            raise ClaimRoutingError("junction worktree is forbidden")
        existing = path.exists() or path.is_symlink()
        if not existing and not review_revision:
            dirty = _dirty_owned_paths(root, owns, needs)
            if dirty:
                raise ClaimRoutingError(
                    "dirty trunk overlaps claim owns/needs: " + ", ".join(dirty[:3])[:160])
        tree = manager.open(tree_id, sha).resolve(strict=True)
        if tree != path.resolve(strict=True):
            raise ClaimRoutingError("worktree path mismatch")
        if not _within(tree, manager.workspace.resolve(strict=True)):
            raise ClaimRoutingError("worktree escaped trusted workspace")
        tip = _git(tree, "rev-parse", "--verify", "HEAD").stdout.strip()
        if review_revision and not existing:
            # Worktrees.open only creates a branch at the current trunk HEAD.
            # Move this *new private* review branch to the verified sealed
            # commit, without touching the source HEAD or other claims.
            _git(tree, "reset", "--hard", review_revision)
            tip = _git(tree, "rev-parse", "--verify", "HEAD").stdout.strip()
        expected = review_revision or sha
        if tip != expected:
            raise ClaimRoutingError("existing claim worktree has stale/changed base")
        if review_revision and _git(
            tree, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout.strip():
            raise ClaimRoutingError("sealed review worktree is dirty")
        if _git(tree, "rev-parse", "--show-toplevel").stdout.strip():
            top = Path(_git(tree, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
            if top != tree:
                raise ClaimRoutingError("worktree escaped expected checkout")
        return ClaimContext(claim_id, worker_id, project, tz, root, tree, expected, manager.branch(tree_id), "isolated")
    except (ClaimTreeError, OSError, ValueError) as exc:
        raise ClaimRoutingError(f"claim worktree unavailable: {exc}") from exc


class ClaimTakeRouting:
    """Resolver bound to trusted queue Store (and never to client input)."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def resolve_active_claim_context(self, agent: str) -> ClaimContext:
        """Server tools use this, not any claim id provided by the client."""
        if not agent:
            raise ClaimRoutingError("agent identity required")
        with self.store.read() as tx:
            row = tx.one(
                "SELECT id FROM claims WHERE worker_id=? AND state='active' "
                "ORDER BY id DESC LIMIT 1", (agent,),
            )
        if row is None:
            raise ClaimRoutingError("no active claim for worker")
        return self.resolve_claim_context(agent, int(row["id"]))

    def resolve_claim_context(self, agent: str, claim_id: int) -> ClaimContext:
        if not agent or type(claim_id) is not int or claim_id <= 0:
            raise ClaimRoutingError("agent and numeric claim id are required")
        with self.store.read() as tx:
            claim = tx.one(
                "SELECT c.id,c.project,c.tz,c.worker_id,c.state,p.root "
                "FROM claims c JOIN projects p ON p.name=c.project "
                "WHERE c.id=?", (claim_id,),
            )
            if claim is None or claim["worker_id"] != agent or claim["state"] != "active":
                raise ClaimRoutingError("claim does not belong to active worker")
            records = tx.query(
                "SELECT payload FROM events WHERE kind='take' AND worker_id=? "
                "AND project=? AND tz=? ORDER BY id DESC",
                (agent, claim["project"], claim["tz"]),
            )
        data = next(
            (obj for row in records
             if (obj := _parse_event(row["payload"])).get("claim_id") == claim_id),
            None,
        )
        if data is None:
            raise ClaimRoutingError("claim lacks durable take event")
        binding = data.get("claim_context")
        if binding is None and "claim_context" not in data:
            # Pre-transition takes have a trusted claim_id but no tree binding.
            # Their checkout mode is fixed at take time, not by today's flags.
            root = Path(claim["root"]).resolve(strict=True)
            return ClaimContext(claim_id, agent, str(claim["project"]),
                                str(claim["tz"]), root, root, "", "", "shared")
        if not isinstance(binding, dict):
            raise ClaimRoutingError("claim take event has no durable context")
        context = ClaimContext.from_event(binding)
        if (context.claim_id != claim_id or context.worker_id != agent
                or context.project != claim["project"] or context.tz != claim["tz"]
                or context.project_root != Path(claim["root"]).resolve(strict=True)):
            raise ClaimRoutingError("durable binding does not match active claim")
        # A valid shared binding from the previous epoch must remain shared.
        if context.mode == "isolated":
            manager = ClaimWorktrees(context.project_root)
            expected = f"claim-{claim_id}"
            if getattr(manager.path(expected), "is_junction", lambda: False)():
                raise ClaimRoutingError("claim worktree junction is forbidden")
            verified = manager._verified_path(expected).resolve(strict=True)
            if verified != context.tree_path or manager.branch(expected) != context.branch:
                raise ClaimRoutingError("claim worktree mapping was altered")
            if not _within(verified, manager.workspace.resolve(strict=True)):
                raise ClaimRoutingError("claim tree escaped trusted workspace")
            base = _git(context.project_root, "rev-parse", "--verify", f"{context.base_sha}^{{commit}}").stdout.strip()
            if base != context.base_sha:
                raise ClaimRoutingError("claim base is no longer available")
            if context.tz.startswith("review:"):
                tip = _git(verified, "rev-parse", "--verify", "HEAD").stdout.strip()
                if tip != context.base_sha:
                    raise ClaimRoutingError("review checkout differs from sealed revision")
                if _git(
                    verified, "status", "--porcelain=v1", "--untracked-files=all"
                ).stdout.strip():
                    raise ClaimRoutingError("review checkout differs from sealed revision: dirty files")
        return context


def _parse_event(value: object) -> dict[str, Any]:
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def resolve_claim_context(store: Any, agent: str, claim_id: int) -> ClaimContext:
    """Functional entry point; the store is a server-side capability."""
    return ClaimTakeRouting(store).resolve_claim_context(agent, claim_id)


def resolve_active_claim_context(store: Any, agent: str) -> ClaimContext:
    """Resolve current claim from trusted Store; never infer from argv/path."""
    return ClaimTakeRouting(store).resolve_active_claim_context(agent)


@dataclass(frozen=True)
class SealedReviewProjection:
    """Read-only coordinates of one server-verified author's Git submission.

    `root` is the *source* repository, not a foreign claim worktree. Consumers
    must read Git objects at `revision` (or a server-created immutable snapshot),
    never the mutable root/author checkout. `own_paths` limits which changed
    files may be exposed by a source inspector.
    """

    root: Path
    relative: Path
    revision: str
    source_claim_id: int
    own_paths: tuple[Path, ...]

    def is_owned(self, relative: str | Path) -> bool:
        """True only for files under source task's trusted owned paths."""
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            return False
        source = self.root / path
        return any(_within(source, own) for own in self.own_paths)


def sealed_review_projection(store: Any, reviewer: str,
                             requested_path: str | Path) -> SealedReviewProjection | None:
    """Select pinned review bytes from trusted Store, not from a client claim ID.

    None means this is not an active sealed review of the requested project.
    A broken submission, wrong worker, path escape or altered Git ref fails
    closed; no fallback to another claim's checkout is authorized.
    """
    from .claim_review_bite import _claim, _review_claim, _submission, ClaimReviewError
    from .review import _review_target

    if not reviewer:
        return None
    with store.read() as tx:
        active = tx.one(
            "SELECT c.id,t.project,t.tz,t.kind,t.body FROM claims c "
            "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
            (reviewer,),
        )
        if active is None or active["kind"] != "review":
            return None
        project, tz = _review_target(str(active["tz"]), str(active["body"] or ""),
                                     str(active["project"]))
        author = tx.one(
            "SELECT c.id,p.root FROM claims c JOIN projects p ON p.name=c.project "
            "WHERE c.project=? AND c.tz=? ORDER BY c.id DESC LIMIT 1", (project, tz),
        )
    if author is None:
        raise ClaimReviewError("sealed review has no source claim")
    # Shared submissions use the ordinary guarded review path. Their generic
    # claim_submission record is not an isolated Git seal. Decide from the
    # server-owned take binding before asking for an isolated submission.
    _, source_context = _claim(store, int(author['id']))
    if source_context.mode == 'shared':
        return None
    root = Path(author["root"]).resolve(strict=True)
    raw = os.fspath(requested_path)
    if not raw or ".." in Path(raw).parts:
        raise ClaimRoutingError("invalid sealed review path")
    requested = Path(raw)
    lexical = Path(os.path.abspath(requested if requested.is_absolute() else root / requested))
    if not _within(lexical, root):
        return None  # unrelated approved reads still use normal policy
    if not _within(lexical.resolve(strict=False), root):
        raise ClaimRoutingError("sealed review path symlink escapes source root")

    submission, context, original = _submission(store, int(author["id"]))
    if original["task_state"] != "submitted" or context.mode != "isolated":
        raise ClaimReviewError("sealed review source is not submitted and isolated")
    if context.project_root != root:
        raise ClaimReviewError("sealed review project root changed")
    if int(active["id"]) != _review_claim(store, submission, reviewer):
        raise ClaimReviewError("sealed review worker binding changed")
    paths: list[Path] = []
    for item in submission.owns:
        raw_own = item[:-3] if item.endswith("/**") else item
        own = Path(raw_own)
        own = Path(os.path.abspath(own if own.is_absolute() else root / own))
        if not _within(own, root) or not _within(own.resolve(strict=False), root):
            raise ClaimReviewError("sealed submission owns escaped source root")
        paths.append(own)
    return SealedReviewProjection(
        root, lexical.relative_to(root), submission.revision,
        submission.claim_id, tuple(paths),
    )


def project_path_to_claim(path: str | Path, context: ClaimContext, *, read_only: bool = False,
                          owns: tuple[str, ...] | None = None) -> Path:
    """Translate root-relative/absolute paths before the existing fleet guard.

    The caller must STILL run the normal guard on the resulting real path.
    Do not translate explicit external read-only inputs; NEVER permit writes to
    them, other claim trees, symlink escapes or traversal aliases. Optional
    owns are checked against the *source* project coordinate system.
    """
    raw = os.fspath(path)
    if not raw or ".." in Path(raw).parts:
        raise ClaimRoutingError("empty or traversal claim path")
    root, tree = context.project_root, context.tree_path
    requested = Path(raw)
    absolute = requested if requested.is_absolute() else root / requested
    lexical = Path(os.path.abspath(absolute))
    resolved = lexical.resolve(strict=False)
    if context.mode == "shared":
        return resolved
    if _within(lexical, tree):
        if not _within(resolved, tree):
            raise ClaimRoutingError("claim tree symlink escapes checkout")
        candidate = resolved
        source = root / lexical.relative_to(tree)
    elif _within(lexical, root):
        if not _within(resolved, root):
            raise ClaimRoutingError("project symlink escapes root")
        source = resolved
        candidate = tree / resolved.relative_to(root)
    else:
        foreign_workspace = root.parent / ".fleet-trees" / root.name
        if _within(lexical, foreign_workspace) or _within(resolved, foreign_workspace):
            raise ClaimRoutingError("foreign claim tree is forbidden")
        if read_only:
            return resolved
        raise ClaimRoutingError("path outside active claim")
    translated = candidate.resolve(strict=False)
    if not _within(translated, tree):
        raise ClaimRoutingError("translated path escaped claim worktree")
    if owns is not None:
        source_norm = norm_path(str(source))
        if not any(paths_overlap(source_norm, own) for own in owns):
            raise ClaimRoutingError("path outside declared owns")
    return translated


@dataclass(frozen=True)
class ClaimCommandEnvironment:
    cwd: Path
    python_executable: Path
    environment: dict[str, str]
    claim_id: int
    tree_revision: str


def validate_claim_command_argv(argv: Sequence[str], context: ClaimContext) -> tuple[str, ...]:
    """Validate command path arguments before an isolated exec is launched.

    The host must still enforce its binary allowlist, guard and sandbox;
    this check rejects obvious trunk/foreign absolute paths and opaque
    inline Python snippets. It cannot sandbox arbitrary executable code.
    """
    if not argv:
        raise ClaimRoutingError("missing command")
    if context.mode == "shared":
        return tuple(argv)
    cleaned: list[str] = []
    for index, token in enumerate(argv):
        if not isinstance(token, str) or not token or "\x00" in token:
            raise ClaimRoutingError("invalid command argument")
        if token in {"-c", "-Command", "-EncodedCommand"}:
            raise ClaimRoutingError("inline command bypasses path isolation")
        sample = token.split("=", 1)[-1] if "=" in token else token
        path_candidate = Path(sample)
        if ".." in path_candidate.parts:
            raise ClaimRoutingError("command argument traverses parent directory")
        if path_candidate.is_absolute():
            canonical = path_candidate.resolve(strict=False)
            venv_root = context.project_root / ".venv"
            if not _within(canonical, context.tree_path):
                if index == 0 and _within(canonical, venv_root):
                    pass  # trusted interpreter, *never* an arbitrary script
                else:
                    raise ClaimRoutingError("absolute argv path outside active claim")
        cleaned.append(token)
    return tuple(cleaned)


def claim_command_environment(context: ClaimContext,
                              environment: Mapping[str, str] | None = None) -> ClaimCommandEnvironment:
    """Use shared root venv, isolated claim cwd and claim's PYTHONPATH.

    The exec tool must reject all user-supplied absolute source paths in argv
    and verify the claim binding immediately before launch.
    """
    environ = dict(os.environ if environment is None else environment)
    if context.mode == "isolated":
        environ = python_environment(context.project_root, context.tree_path, environ)
    revision = ""
    if context.mode == "isolated":
        try:
            revision = _git(context.tree_path, "rev-parse", "--verify", "HEAD").stdout.strip()
        except ClaimTreeError as exc:
            raise ClaimRoutingError("claim revision unavailable") from exc
    return ClaimCommandEnvironment(context.tree_path, context.project_root / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"),
        environ, context.claim_id, revision)
