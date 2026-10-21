"""TZ-41: trusted, revision-bound review evidence for isolated Git claims.

Integration points: capture_submission before queue.submit releases its claim;
reviewer_run for fleet run; check_fix_bite for fix submissions; accept_claim
before review.decide("accept"); verify_accepted_claim for TZ-42.

Only server-owned Store and take/review events are authoritative. This module
never trusts a note, agent-supplied directory, or a flag named "accepted".
Shared-mode projects remain on the legacy review path.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Iterator, Mapping, Sequence

from .claim_context import ClaimContext
from .claim_tree import ClaimTreeError, ClaimWorktrees, _git as tree_git, python_environment
from .review import _review_target


class ClaimReviewError(RuntimeError):
    """No verified evidence: callers must fail closed, not use trunk or notes."""


@dataclass(frozen=True)
class ClaimSubmission:
    claim_id: int
    submission_id: str
    project: str
    tz: str
    author: str
    revision: str
    tree_sha: str
    tip_sha: str
    base_sha: str
    branch: str
    owns: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]
    environment_version: str
    workspace_fingerprint: str


@dataclass(frozen=True)
class RunEvidence:
    claim_id: int
    submission_revision: str
    reviewer: str
    argv: tuple[str, ...]
    rc: int
    tree_sha: str
    timestamp: str
    output: str


@dataclass(frozen=True)
class BiteEvidence:
    claim_id: int
    previous_revision: str
    revision: str
    reviewer: str
    verdict: str
    old_rc: int | None
    new_rc: int | None
    reason: str = ""


@dataclass(frozen=True)
class AcceptedClaimEvidence:
    claim_id: int
    submission_revision: str
    submission_id: str
    author: str
    reviewer: str
    project: str
    tz: str
    tree_sha: str
    commands: tuple[tuple[str, ...], ...]
    run_ids: tuple[int, ...]
    accepted_at: str
    bite_id: int | None = None
    reviewer_claim_id: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _decode(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_list(value: Any) -> tuple[str, ...]:
    try:
        arr = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise ClaimReviewError("invalid trusted task list") from None
    if not isinstance(arr, list) or not all(isinstance(v, str) for v in arr):
        raise ClaimReviewError("invalid trusted task list")
    return tuple(arr)


def _argv(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            parts = shlex.split(value, posix=os.name != "nt")
            parts = [p.strip('"') for p in parts]
        except ValueError as exc:
            raise ClaimReviewError("invalid acceptance command encoding") from exc
    elif isinstance(value, (tuple, list)) and all(isinstance(x, str) for x in value):
        parts = list(value)
    else:
        raise ClaimReviewError("invalid acceptance command")
    if not parts or any(not p or "\0" in p for p in parts):
        raise ClaimReviewError("empty or malformed acceptance argv")
    for p in parts:
        if "\n" in p or "\r" in p:
            raise ClaimReviewError("newline in acceptance argv")
        try:
            p.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ClaimReviewError("invalid acceptance command encoding") from exc
    return tuple(parts)


def _safe_source_args(argv: tuple[str, ...]) -> None:
    if any(Path(x).is_absolute() or ".." in Path(x).parts
           for x in argv[1:] if not x.startswith("-")):
        raise ClaimReviewError("absolute or traversal source argument is forbidden")


def _project_python(argv: Sequence[str]) -> bool:
    """Allow only project Python commands, including Windows relative prefixes."""
    if not argv:
        return False
    name = str(argv[0]).replace("\\", "/").casefold()
    if name.startswith("./"):
        name = name[2:]
    return name in {
        "python", "python.exe", "py", "py.exe",
        ".venv/scripts/python.exe", ".venv/bin/python",
    }


def _project_python_argv(binary: Path, argv: Sequence[str]) -> list[str]:
    if not _project_python(argv):
        raise ClaimReviewError('expected project Python acceptance')
    name = str(argv[0]).replace('\\','/').rsplit('/',1)[-1].casefold()
    args = list(argv[1:])
    if name in {'py','py.exe'} and args and re.fullmatch(r'-3(?:\.\d+)?',args[0]):
        args.pop(0)
    return [str(binary),*args]


def _snapshot_unchanged(path: Path) -> None:
    if _g(path, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ClaimReviewError("test mutated its immutable snapshot")


def _g(root: Path, *argv: str, env: Mapping[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", timeout=90, env=env,
    )
    if proc.returncode:
        raise ClaimReviewError(f"git {argv[0]} failed: {proc.stderr[:250]}")
    return proc.stdout.strip()


def _claim(store: Any, claim_id: int) -> tuple[dict[str, Any], ClaimContext]:
    if type(claim_id) is not int or claim_id <= 0:
        raise ClaimReviewError("claim_id must be a positive integer")
    with store.read() as tx:
        row = tx.one(
            "SELECT c.id,c.worker_id,c.state AS claim_state,c.started_at,"
            "t.project,t.tz,t.kind,t.state AS task_state,t.owns,t.acceptance,t.body,"
            "t.author_worker,p.root FROM claims c "
            "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "JOIN projects p ON p.name=c.project WHERE c.id=?", (claim_id,),
        )
        if row is None:
            raise ClaimReviewError("unknown claim")
        events = tx.query(
            "SELECT payload FROM events WHERE kind='take' AND worker_id=? AND "
            "project=? AND tz=? ORDER BY id DESC",
            (row["worker_id"], row["project"], row["tz"]),
        )
    take = next((d for e in events if
                 (d := _decode(e["payload"])).get("claim_id") == claim_id), None)
    if not take:
        raise ClaimReviewError("missing trusted take event")
    binding = take.get("claim_context")
    if binding is None:
        # Old shared fleet records have a take event, but no claim_context
        # field. The legacy fallback is allowed only while BOTH trusted
        # project feature flags are not enabled.
        from .goals import project_info
        flags = project_info(str(row["project"]))
        if flags.get("claim_trees") is True and flags.get("commit") is True:
            raise ClaimReviewError("missing trusted isolated take context")
        root = Path(row["root"]).resolve(strict=True)
        return dict(row), ClaimContext(
            claim_id, str(row["worker_id"]), str(row["project"]), str(row["tz"]),
            root, root, "", "", "shared")
    if not isinstance(binding, dict):
        raise ClaimReviewError("invalid trusted take binding")
    try:
        ctx = ClaimContext.from_event(binding)
        if (ctx.claim_id != claim_id or ctx.worker_id != row["worker_id"]
                or ctx.project != row["project"] or ctx.tz != row["tz"]
                or ctx.project_root != Path(row["root"]).resolve(strict=True)):
            raise ClaimReviewError("claim context mismatch")
        from .goals import project_info
        flags = project_info(str(row["project"]))
        if (flags.get("claim_trees") is True and flags.get("commit") is True
                and ctx.mode != "isolated"):
            raise ClaimReviewError("isolated project cannot downgrade to shared")
        if ctx.mode == "isolated":
            mgr = ClaimWorktrees(ctx.project_root)
            if getattr(mgr.path(f"claim-{claim_id}"), "is_junction", lambda: False)():
                raise ClaimReviewError("claim worktree junction forbidden")
            if (mgr._verified_path(f"claim-{claim_id}").resolve(strict=True)
                    != ctx.tree_path or mgr.branch(f"claim-{claim_id}") != ctx.branch):
                raise ClaimReviewError("claim worktree was replaced")
            if _g(ctx.project_root, "rev-parse", "--verify", ctx.base_sha + "^{commit}") != ctx.base_sha:
                raise ClaimReviewError("claim base SHA missing")
    except (ValueError, OSError, ClaimTreeError) as exc:
        raise ClaimReviewError(f"untrusted claim context: {exc}") from exc
    return dict(row), ctx


def _snapshot_tree(ctx: ClaimContext) -> tuple[str, str]:
    """Use a private index: include tracked AND nonignored untracked files, never stage in claim."""
    if ctx.mode != "isolated":
        raise ClaimReviewError("legacy shared project must use old review path")
    with tempfile.TemporaryDirectory(prefix="fleet-index-") as td:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(td) / "index"))
        tip = _g(ctx.tree_path, "rev-parse", "--verify", "HEAD")
        _g(ctx.tree_path, "read-tree", tip, env=env)
        _g(ctx.tree_path, "add", "-A", env=env)
        tree = _g(ctx.tree_path, "write-tree", env=env)
    # Refuse Git symlinks in acceptance snapshot, including a link outside the tree.
    proc = subprocess.run(["git", "-C", str(ctx.tree_path), "ls-tree", "-r", "-z", tree],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if proc.returncode:
        raise ClaimReviewError("cannot inspect immutable snapshot")
    if any(entry.startswith(b"120000 ") for entry in proc.stdout.split(b"\0") if entry):
        raise ClaimReviewError("snapshot contains symlink")
    return tip, tree


def _workspace_fingerprint(ctx: ClaimContext) -> str:
    """Detect even a same-content edit after a green run (mtime/size drift).

    Git tree hashes bind bytes; this second digest binds live file metadata.
    It is not a security boundary against a privileged local actor falsifying
    timestamps, but prevents ordinary touched/reverted sources from reusing
    old acceptance evidence.
    """
    proc = subprocess.run(
        ["git", "-C", str(ctx.tree_path), "ls-files", "-co",
         "--exclude-standard", "-z"],
        capture_output=True, timeout=90,
    )
    if proc.returncode:
        raise ClaimReviewError("cannot fingerprint claim source files")
    digest = hashlib.sha256()
    for raw in sorted(set(proc.stdout.split(b"\0")) - {b""}):
        rel = os.fsdecode(raw)
        source = ctx.tree_path / rel
        try:
            stat = source.lstat()
        except OSError as exc:
            raise ClaimReviewError("claim source disappeared during fingerprint") from exc
        digest.update(raw + b"\0")
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_mode}\n".encode("ascii"))
    return digest.hexdigest()


def _fresh(ctx: ClaimContext, submission: ClaimSubmission) -> None:
    tip, fingerprint = _snapshot_tree(ctx)
    if (tip != submission.tip_sha or fingerprint != submission.tree_sha
            or _workspace_fingerprint(ctx) != submission.workspace_fingerprint):
        raise ClaimReviewError("claim changed since submission / reviewer acceptance")


def _parse_submission(payload: Any) -> ClaimSubmission:
    d = _decode(payload)
    try:
        commands = tuple(_argv(c) for c in d["commands"])
        result = ClaimSubmission(
            int(d["claim_id"]), str(d["submission_id"]), str(d["project"]),
            str(d["tz"]), str(d["author"]), str(d["revision"]),
            str(d["tree_sha"]), str(d["tip_sha"]), str(d["base_sha"]),
            str(d["branch"]), tuple(str(x) for x in d["owns"]), commands,
            str(d["environment_version"]), str(d["workspace_fingerprint"]),
        )
        if not re.fullmatch(r"[0-9a-f]{40}", result.revision) or not re.fullmatch(
            r"[0-9a-f]{40}", result.tree_sha
        ) or not re.fullmatch(r"[0-9a-f]{40}", result.tip_sha):
            raise ValueError("invalid revision digest")
        if not re.fullmatch(r"[0-9a-f]{32}", result.submission_id):
            raise ValueError("invalid submission identity")
        if not re.fullmatch(r"[0-9a-f]{64}", result.workspace_fingerprint):
            raise ValueError("invalid source metadata digest")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ClaimReviewError(f"invalid submission evidence: {exc}") from exc


def _latest(store: Any, claim_id: int) -> tuple[int, ClaimSubmission]:
    with store.read() as tx:
        rows = tx.query(
            "SELECT id,payload FROM events WHERE kind='claim_submission' ORDER BY id DESC"
        )
    for row in rows:
        d = _decode(row["payload"])
        if d.get("claim_id") == claim_id:
            return int(row["id"]), _parse_submission(d)
    raise ClaimReviewError("claim has no sealed submission")


def capture_submission(store: Any, claim_id: int, author: str) -> ClaimSubmission | None:
    """Call BEFORE releasing submitted/fix claim. None = legacy shared project."""
    row, ctx = _claim(store, claim_id)
    if ctx.mode == "shared":
        return None
    if (row["worker_id"] != author or row["claim_state"] != "active"
            or row["task_state"] != "taken"
            or row["kind"] not in {"work", "fix", "request", "triage"}):
        raise ClaimReviewError("submission requires active author claim")
    tip, tree = _snapshot_tree(ctx)
    owns = _json_list(row["owns"])
    commands = tuple(_argv(c) for c in _json_list(row["acceptance"]))
    # Commit the exact index tree into immutable Git object storage; no ref, index,
    # source file or shared HEAD is touched. The adapter must retain this SHA.
    changed = _g(ctx.tree_path, "diff", "--name-only", "-z", ctx.base_sha, tree)
    if changed:
        root = ctx.project_root
        allowed = [Path(x).resolve(strict=False) for x in owns]
        for relative in changed.split("\0"):
            if not relative:
                continue
            path = (root / relative).resolve(strict=False)
            if not any(path == own or own in path.parents for own in allowed):
                raise ClaimReviewError("submission includes modification outside accepted owns")
    rev_env = dict(os.environ, GIT_AUTHOR_NAME="Fleet Evidence",
                   GIT_AUTHOR_EMAIL="fleet@localhost",
                   GIT_COMMITTER_NAME="Fleet Evidence",
                   GIT_COMMITTER_EMAIL="fleet@localhost")
    revision = _g(ctx.tree_path, "commit-tree", tree, "-p", tip, env=rev_env)
    submission_id = uuid.uuid4().hex
    # Unreferenced commit-tree objects are collectible by Git GC. Pin a private
    # immutable evidence ref without moving HEAD or either claim branch.
    _g(ctx.project_root, "update-ref",
       f"refs/fleet/submissions/{submission_id}", revision)
    obj = ClaimSubmission(
        claim_id, submission_id, str(row["project"]), str(row["tz"]),
        author, revision, tree, tip, ctx.base_sha, ctx.branch, owns,
        commands, f"python:{sys.version_info.major}.{sys.version_info.minor}",
        _workspace_fingerprint(ctx),
    )
    with store.tx() as tx:
        current = tx.one("SELECT state,worker_id FROM claims WHERE id=?", (claim_id,))
        if not current or current["state"] != "active" or current["worker_id"] != author:
            raise ClaimReviewError("claim changed during capture")
        tx.record_event(kind="claim_submission", worker_id=author,
                        project=obj.project, tz=obj.tz, payload={
                            **vars(obj), "commands": [list(c) for c in obj.commands],
                        })
    return obj


def _submission(store: Any, claim_id: int) -> tuple[ClaimSubmission, ClaimContext, dict[str, Any]]:
    row, ctx = _claim(store, claim_id)
    _, sub = _latest(store, claim_id)
    if (sub.claim_id != claim_id or sub.author != row["worker_id"]
            or sub.project != row["project"] or sub.tz != row["tz"]
            or sub.branch != ctx.branch or sub.base_sha != ctx.base_sha
            or sub.owns != _json_list(row["owns"])
            or sub.commands != tuple(_argv(c) for c in _json_list(row["acceptance"]))):
        raise ClaimReviewError("submission does not match trusted claim metadata")
    if _g(ctx.project_root, "rev-parse", "--verify", sub.revision + "^{commit}") != sub.revision:
        raise ClaimReviewError("sealed commit missing")
    if _g(ctx.project_root, "rev-parse", "--verify",
          f"refs/fleet/submissions/{sub.submission_id}") != sub.revision:
        raise ClaimReviewError("sealed snapshot reference changed")
    if _g(ctx.project_root, "rev-parse", sub.revision + "^{tree}") != sub.tree_sha:
        raise ClaimReviewError("sealed tree digest does not match revision")
    _fresh(ctx, sub)
    return sub, ctx, row


def _review_claim(store: Any, sub: ClaimSubmission, reviewer: str) -> int:
    if reviewer == sub.author:
        raise ClaimReviewError("reviewer cannot review own claim")
    with store.read() as tx:
        rows = tx.query(
            "SELECT c.id,c.worker_id,t.project,t.tz,t.body FROM claims c "
            "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.worker_id=? AND c.state='active' AND t.kind='review'",
            (reviewer,),
        )
    for row in rows:
        if _review_target(str(row["tz"]), str(row["body"]), str(row["project"])) == (
                sub.project, sub.tz):
            return int(row["id"])
    raise ClaimReviewError("independent active reviewer claim is required")


def reviewer_inspect(store: Any, claim_id: int, reviewer: str, operation: str,
                     path: str, *, query: str = "", pattern: str = "*",
                     max_results: int = 50) -> str:
    """Read a reviewer's pinned author submission, never trunk or author checkout.

    The trusted take, submission ref, immutable Git tree, and active independent
    reviewer are validated on every call. All three operations use the same
    revision as reviewer_run/accept_claim, including unchanged baseline files.
    """
    if operation not in {"read_file", "search_files", "list_dir"}:
        raise ClaimReviewError("unsupported sealed inspect operation")
    if (type(max_results) is not int or not 1 <= max_results <= 100
            or not isinstance(query, str) or len(query) > 512
            or not isinstance(pattern, str) or len(pattern) > 128):
        raise ClaimReviewError("invalid inspect limits")
    sub, ctx, row = _submission(store, claim_id)
    if row["task_state"] != "submitted":
        raise ClaimReviewError("review only submitted claims")
    _review_claim(store, sub, reviewer)
    root = ctx.project_root.resolve(strict=True)
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ClaimReviewError("invalid inspect path")
    requested = Path(path)
    try:
        resolved = (requested if requested.is_absolute() else root / requested).resolve(
            strict=False)
        rel = resolved.relative_to(root).as_posix()
    except (OSError, ValueError) as exc:
        raise ClaimReviewError("inspect path outside sealed project") from exc
    if any(part.casefold() in {".git", ".env", "secrets", "credentials"}
           or part.casefold().startswith(".env.") for part in Path(rel).parts):
        raise ClaimReviewError("protected sealed inspect path")

    listing = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "-z", sub.revision],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
    )
    if listing.returncode:
        raise ClaimReviewError("sealed tree not available")
    files: list[str] = []
    for entry in listing.stdout.split(b"\x00"):
        if not entry:
            continue
        metadata, separator, name = entry.partition(b"\t")
        if not separator or metadata.split(b" ", 1)[0] not in {b"100644", b"100755"}:
            raise ClaimReviewError("unsafe entry in sealed tree")
        name_str = os.fsdecode(name).replace("\\", "/")
        if any(part.casefold() in {".git", ".env", "secrets", "credentials"}
               or part.casefold().startswith(".env.") for part in name_str.split("/")):
            continue
        files.append(name_str)
    scope = [name for name in files if rel == "." or name == rel or name.startswith(rel + "/")]

    def blob(name: str) -> str:
        item = subprocess.run(
            ["git", "-C", str(root), "show", f"{sub.revision}:{name}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
        )
        if item.returncode:
            raise ClaimReviewError("sealed blob unavailable")
        if len(item.stdout) > 256 * 1024:
            raise ClaimReviewError("sealed blob exceeds inspect limit")
        return item.stdout.decode("utf-8", errors="replace")

    if operation == "read_file":
        if rel not in scope:
            raise ClaimReviewError("file missing from sealed submission")
        result = blob(rel)[:65536]
    elif operation == "list_dir":
        result = "\n".join(scope[:max_results]) if scope else "(empty sealed directory)"
    else:
        if not query:
            raise ClaimReviewError("search query is required")
        findings: list[str] = []
        for name in scope[:2000]:
            if not fnmatch(Path(name).name, pattern):
                continue
            for number, line in enumerate(blob(name).splitlines(), start=1):
                if query in line:
                    findings.append(f"{name}:{number}: {line[:300]}")
                    if len(findings) >= max_results:
                        break
            if len(findings) >= max_results:
                break
        result = "\n".join(findings) if findings else "Совпадений нет."
    _fresh(ctx, sub)
    return result


@contextmanager
def _worktree(root: Path, revision: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="fleet-review-") as folder:
        path = Path(folder) / "tree"
        _g(root, "worktree", "add", "--detach", str(path), revision)
        try:
            yield path
        finally:
            _g(root, "worktree", "remove", "--force", str(path))


def _execute(root: Path, revision: str, argv: tuple[str, ...], *,
             python_executable: Path | None, timeout: float) -> tuple[int, str]:
    with _worktree(root, revision) as path:
        # Never resolve executable or source argv through another claim/trunk.
        _safe_source_args(argv)
        executable = argv[0].replace('\\','/').rsplit('/',1)[-1].casefold()
        if _project_python(argv):
            binary = python_executable or root / ".venv" / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not binary.is_file():
                raise ClaimReviewError("project Python venv unavailable")
            # `py -3` is a Windows *launcher* selector, not a CPython flag.
            # A sealed manifest must keep the original argv for its digest
            # while the isolated runner substitutes a project interpreter.
            # Passing -3 through would fail even for a green acceptance.
            command = _project_python_argv(binary,argv)
            # A caller's inherited PYTHONPATH may contain trunk/another claim.
            # The snapshot is the only first-party import root for Python.
            env = python_environment(
                root, path, {k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
            env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        elif executable in {"node", "node.exe"}:
            # Node's test runner is a legitimate registered acceptance command
            # for the extension. Do not allow -e/-r/--import or arbitrary flags
            # to turn the sealed adapter into an unbounded command runner.
            if len(argv) < 3 or argv[1] != "--test":
                raise ClaimReviewError("Node acceptance requires --test and a source file")
            for source in argv[2:]:
                if source.startswith("-") or Path(source).suffix.lower() not in {
                        ".js", ".mjs", ".cjs"}:
                    raise ClaimReviewError("Node acceptance requires relative JS test files")
                target = (path / source).resolve(strict=False)
                if (not target.is_file() or
                        not target.is_relative_to(path.resolve())):
                    raise ClaimReviewError("Node test source must be inside sealed snapshot")
            installed = shutil.which("node")
            if not installed:
                raise ClaimReviewError("Node.js runtime unavailable")
            binary = Path(installed).resolve(strict=True)
            if binary.is_relative_to(path.resolve()) or binary.is_relative_to(root.resolve()):
                raise ClaimReviewError("Node.js runtime must be installed outside project tree")
            command = [str(binary), *argv[1:]]
            # NODE_OPTIONS and NODE_PATH can inject preloads or foreign source
            # into an otherwise exact manifest command.
            excluded = {"PYTHONPATH", "NODE_OPTIONS", "NODE_PATH", "NODE_V8_COVERAGE"}
            env = {k: v for k, v in os.environ.items() if k.upper() not in excluded}
            env["NODE_DISABLE_COLORS"] = "1"
        else:
            raise ClaimReviewError("only project Python or Node.js test acceptance is supported by isolated adapter")
        with tempfile.TemporaryFile() as output:
            try:
                completed = subprocess.run(command, cwd=path, stdout=output,
                                           stderr=subprocess.STDOUT, env=env,
                                           timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ClaimReviewError(f"acceptance execution failed: {type(exc).__name__}") from exc
            output.seek(0)
            sample = output.read(4096).decode("utf-8", errors="replace")
            _snapshot_unchanged(path)
            return int(completed.returncode), sample


def reviewer_run(store: Any, claim_id: int, reviewer: str,
                 argv: str | Sequence[str], *, python_executable: Path | None = None,
                 timeout: float = 300) -> RunEvidence:
    sub, ctx, row = _submission(store, claim_id)
    if row["task_state"] != "submitted":
        raise ClaimReviewError("review only submitted claims")
    review_claim_id = _review_claim(store, sub, reviewer)
    command = _argv(argv)
    if command not in sub.commands:
        raise ClaimReviewError("command not present in sealed acceptance manifest")
    rc, output = _execute(ctx.project_root, sub.revision, command,
                          python_executable=python_executable, timeout=timeout)
    _fresh(ctx, sub)
    evidence = RunEvidence(claim_id, sub.revision, reviewer, command,
                           rc, sub.tree_sha, _now(), output)
    with store.tx() as tx:
        active = tx.one("SELECT state,worker_id FROM claims WHERE id=?", (review_claim_id,))
        if not active or active["state"] != "active" or active["worker_id"] != reviewer:
            raise ClaimReviewError("review claim changed during command")
        tx.record_event(kind="claim_review_run", project=sub.project, tz=sub.tz,
                        worker_id=reviewer, rc=rc, payload={
                            **vars(evidence), "argv": list(command),
                            "submission_id": sub.submission_id,
                            "review_claim_id": review_claim_id,
                        })
    return evidence


def _rejected_previous(store: Any, fix: ClaimSubmission, body: str) -> ClaimSubmission:
    line = body.splitlines()[0].strip() if body else ""
    prefix = "[fleet-fix] source="
    if not line.startswith(prefix):
        raise ClaimReviewError("fix source is not trusted")
    address = line[len(prefix):]
    project, sep, origin_tz = address.rpartition("/")
    expected_name = f"fix:{origin_tz}"
    if (not sep or not origin_tz or project != fix.project
            or (fix.tz != expected_name and not re.fullmatch(
                re.escape(expected_name) + r":\d+", fix.tz))):
        raise ClaimReviewError("fix source does not match the fix task")
    with store.read() as tx:
        # The server's reject event links the precise predecessor to THIS fix.
        # The newest reject of the root task is not enough: fix:TZ:2 must bite
        # the rejected fix:TZ, not the original root revision. A text marker
        # by itself cannot establish that relationship.
        rejects = tx.query(
            "SELECT id,tz,payload FROM events WHERE kind='reject' AND project=? "
            "ORDER BY id DESC", (project,))
        rejection = next((event for event in rejects
                          if _decode(event["payload"]).get("fix_task") == fix.tz), None)
        if rejection is None:
            raise ClaimReviewError("no trusted rejection linked to this fix")
        rejected_tz = str(rejection["tz"])
        if (rejected_tz != origin_tz and rejected_tz != expected_name
                and not re.fullmatch(re.escape(expected_name) + r":\d+", rejected_tz)):
            raise ClaimReviewError("rejection is for an unrelated source")
        rejected_event_id = int(rejection["id"])
        events = tx.query("SELECT id,payload FROM events WHERE kind='claim_submission' "
                        "AND project=? AND tz=? AND id<? ORDER BY id DESC",
                        (project, rejected_tz, rejected_event_id))
    if not events:
        raise ClaimReviewError("rejected revision has no sealed snapshot")
    previous = _parse_submission(events[0]["payload"])
    if previous.project != project or previous.tz != rejected_tz:
        raise ClaimReviewError("rejected revision has foreign claim identity")
    return previous


def check_fix_bite(store: Any, claim_id: int, reviewer: str,
                   argv: str | Sequence[str] | None, *,
                   regression_commands: Sequence[Sequence[str]] = (),
                   na_reason: str = "", python_executable: Path | None = None,
                   timeout: float = 300) -> BiteEvidence:
    new, ctx, row = _submission(store, claim_id)
    if row["kind"] != "fix" or row["task_state"] != "submitted":
        raise ClaimReviewError("bite requires submitted fix")
    review_claim_id = _review_claim(store, new, reviewer)
    old = _rejected_previous(store, new, str(row["body"]))
    _g(ctx.project_root, "rev-parse", "--verify", old.revision + "^{commit}")
    if argv is None:
        if not na_reason.strip():
            raise ClaimReviewError("visual/document n/a requires structured reason")
        result = BiteEvidence(claim_id, old.revision, new.revision, reviewer,
                              "n/a", None, None, na_reason.strip())
    else:
        command = _argv(argv)
        _safe_source_args(command)
        with ExitStack() as stack:
            old_path = stack.enter_context(_worktree(ctx.project_root, old.revision))
            new_path = stack.enter_context(_worktree(ctx.project_root, new.revision))
            # Both immutable trees coexist throughout the test. Runs still resolve
            # Python from root venv and sources from their own snapshot trees.
            def run(path: Path) -> int:
                binary = python_executable or ctx.project_root / ".venv" / (
                    "Scripts/python.exe" if os.name == "nt" else "bin/python")
                if not binary.is_file() or not _project_python(command):
                    raise ClaimReviewError("bite requires project Python")
                with tempfile.TemporaryFile() as out:
                    try:
                        rc = int(subprocess.run(
                            _project_python_argv(binary,command), cwd=path,
                            stdout=out, stderr=subprocess.STDOUT,
                            env={**python_environment(
                                    ctx.project_root, path,
                                    {k: v for k, v in os.environ.items()
                                     if k != "PYTHONPATH"}),
                                 "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
                            timeout=timeout).returncode)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise ClaimReviewError("bite execution unavailable") from exc
                    _snapshot_unchanged(path)
                    return rc
            old_rc = run(old_path)
            new_rc = run(new_path)
            regressions = []
            # Old acceptance cases which were already passing are a regression
            # suite even if the fix author supplies no extra tests.
            extras = [(_argv(c), False) for c in old.commands if _argv(c) != command]
            extras.extend((_argv(c), True) for c in regression_commands)
            for reg_cmd, required_before in extras:
                if not _project_python(reg_cmd):
                    raise ClaimReviewError("regression command requires project Python")
                # _execute creates its own detached snapshots; never mutates these.
                before, _ = _execute(ctx.project_root, old.revision, reg_cmd,
                                     python_executable=python_executable, timeout=timeout)
                after, _ = _execute(ctx.project_root, new.revision, reg_cmd,
                                    python_executable=python_executable, timeout=timeout)
                regressions.append((before, after, required_before))
        status = "passed" if old_rc != 0 and new_rc == 0 and all(
            (before != 0 and not mandatory) or before == after == 0
            for before, after, mandatory in regressions) else "failed"
        result = BiteEvidence(claim_id, old.revision, new.revision, reviewer,
                              status, old_rc, new_rc)
    _fresh(ctx, new)
    with store.tx() as tx:
        tx.record_event(kind="claim_bite", project=new.project, tz=new.tz,
                        worker_id=reviewer, payload={
                            **vars(result), "submission_id": new.submission_id,
                            "review_claim_id": review_claim_id,
                        })
    return result


def _accepted_proofs(store: Any, sub: ClaimSubmission, reviewer: str,
                     *, require_bite: bool,
                     reviewer_claim_id: int | None = None) -> tuple[tuple[int, ...], int | None]:
    with store.read() as tx:
        rows = tx.query("SELECT id,rc,payload,worker_id FROM events WHERE "
                        "kind='claim_review_run' AND project=? AND tz=? ORDER BY id DESC",
                        (sub.project, sub.tz))
        bites = tx.query("SELECT id,payload,worker_id FROM events WHERE "
                         "kind='claim_bite' AND project=? AND tz=? ORDER BY id DESC",
                         (sub.project, sub.tz))
    found: dict[tuple[str, ...], int] = {}
    for row in rows:
        p = _decode(row["payload"])
        if (p.get("submission_id") == sub.submission_id
                and p.get("submission_revision") == sub.revision
                and p.get("tree_sha") == sub.tree_sha
                and (reviewer_claim_id is None
                     or p.get("review_claim_id") == reviewer_claim_id)
                and row["worker_id"] == reviewer and row["rc"] == 0):
            cmd = _argv(p.get("argv", []))
            found.setdefault(cmd, int(row["id"]))
    missing = set(sub.commands) - set(found)
    if missing:
        raise ClaimReviewError("missing fresh independent successful acceptance: "
                               + repr(sorted(missing)))
    bite_id = None
    if require_bite:
        for row in bites:
            p = _decode(row["payload"])
            if (p.get("submission_id") == sub.submission_id and p.get("revision") == sub.revision
                    and p.get("verdict") in {"passed", "n/a"}
                    and (p.get("verdict") != "n/a" or str(p.get("reason", "")).strip())
                    and (reviewer_claim_id is None or p.get('review_claim_id') == reviewer_claim_id)
                    and row["worker_id"] == reviewer):
                bite_id = int(row["id"])
                break
        if bite_id is None:
            raise ClaimReviewError("fix has no verified successful bite (or structured n/a)")
    return tuple(found[c] for c in sub.commands), bite_id


def accept_claim(store: Any, claim_id: int, reviewer: str, *,
                 reviewer_claim_id: int | None = None) -> AcceptedClaimEvidence | None:
    """Guard called before review.decide; legacy shared claims use existing route."""
    row, ctx = _claim(store, claim_id)
    if ctx.mode == "shared":
        return None
    sub, ctx, row = _submission(store, claim_id)
    if row["task_state"] != "submitted":
        raise ClaimReviewError("claim no longer submitted")
    review_id = _review_claim(store, sub, reviewer)
    if reviewer_claim_id is not None and review_id != reviewer_claim_id:
        raise ClaimReviewError('FLEET_STALE_CLAIM: reviewer claim changed before trusted accept')
    run_ids, bite_id = _accepted_proofs(
        store, sub, reviewer, require_bite=row["kind"] == "fix",
        reviewer_claim_id=review_id)
    _fresh(ctx, sub)
    proof = AcceptedClaimEvidence(
        claim_id, sub.revision, sub.submission_id, sub.author,
        reviewer, sub.project, sub.tz, sub.tree_sha, sub.commands,
        run_ids, _now(), bite_id, review_id,
    )
    with store.tx() as tx:
        active = tx.one("SELECT state FROM claims WHERE id=?", (review_id,))
        source = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?",
                        (sub.project, sub.tz))
        if not active or active["state"] != "active" or not source or source["state"] != "submitted":
            raise ClaimReviewError("review state changed before trusted accept")
        tx.record_event(kind="claim_accept", project=sub.project, tz=sub.tz,
                        worker_id=reviewer, payload={
                            **vars(proof), "commands": [list(c) for c in proof.commands],
                        })
    return proof


def verify_accepted_claim(store: Any, claim_id: int,
                          submission_revision: str) -> AcceptedClaimEvidence:
    """TZ-42 typed, fail-closed contract. Never interpret author note as acceptance."""
    # This contract must survive integration and claim worktree cleanup. Check
    # immutable submitted Git objects and durable server evidence, not the
    # checkout: a successfully integrated branch may have moved or vanished.
    with store.read() as tx:
        row = tx.one(
            "SELECT c.worker_id,t.state AS task_state,t.kind,t.project,t.tz "
            "FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.id=?", (claim_id,))
    if row is None:
        raise ClaimReviewError("unknown claim")
    _, sub = _latest(store, claim_id)
    if (sub.author != row["worker_id"] or sub.project != row["project"]
            or sub.tz != row["tz"]):
        raise ClaimReviewError("submitted claim identity changed")
    if sub.revision != submission_revision:
        raise ClaimReviewError("wrong submission revision")
    if row["task_state"] != "accepted":
        raise ClaimReviewError("claim not accepted by fleet review")
    with store.read() as tx:
        project = tx.one("SELECT root FROM projects WHERE name=?", (sub.project,))
    if project is None:
        raise ClaimReviewError("project is missing")
    root = Path(project["root"]).resolve(strict=True)
    if (_g(root, "rev-parse", "--verify", f"refs/fleet/submissions/{sub.submission_id}")
            != sub.revision or _g(root, "rev-parse", sub.revision + "^{tree}") != sub.tree_sha):
        raise ClaimReviewError("sealed accepted revision invalid")
    with store.read() as tx:
        events = tx.query("SELECT id,payload,worker_id FROM events WHERE "
                          "kind='claim_accept' AND project=? AND tz=? ORDER BY id DESC",
                          (sub.project, sub.tz))
    for event in events:
        data = _decode(event["payload"])
        if (data.get("claim_id") == claim_id
                and data.get("submission_revision") == submission_revision
                and data.get("submission_id") == sub.submission_id
                and data.get("tree_sha") == sub.tree_sha
                and data.get("reviewer") == event["worker_id"]
                and data.get("author") == sub.author
                and event["worker_id"] != sub.author):
            review_claim_id = data.get("reviewer_claim_id")
            if type(review_claim_id) is not int or review_claim_id <= 0:
                raise ClaimReviewError("accept evidence lacks trusted reviewer claim")
            with store.read() as tx:
                rev = tx.one("SELECT worker_id FROM claims WHERE id=?", (review_claim_id,))
            if not rev or rev["worker_id"] != event["worker_id"]:
                raise ClaimReviewError("reviewer claim identity changed")
            run_ids, bite_id = _accepted_proofs(
                store, sub, str(event["worker_id"]), require_bite=row["kind"] == "fix",
                reviewer_claim_id=review_claim_id)
            if tuple(data.get("run_ids", ())) != run_ids or data.get("bite_id") != bite_id:
                raise ClaimReviewError("accept evidence does not match reviewed runs")
            return AcceptedClaimEvidence(
                claim_id, submission_revision, sub.submission_id, sub.author,
                str(event["worker_id"]), sub.project, sub.tz, sub.tree_sha,
                sub.commands, run_ids, str(data.get("accepted_at")), bite_id,
                review_claim_id,
            )
    raise ClaimReviewError("missing trusted accept event")


class ClaimReviewBite:
    """Store-bound interface; shared project caller keeps legacy handling."""

    def __init__(self, store: Any):
        self.store = store

    def verify_accepted_claim(self, claim_id: int,
                              submission_revision: str) -> AcceptedClaimEvidence:
        return verify_accepted_claim(self.store, claim_id, submission_revision)
