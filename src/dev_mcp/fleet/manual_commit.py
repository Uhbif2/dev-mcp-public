"""TZ-63: accepted-only Git commit helper, intentionally not wired into integration.

Trust boundary
--------------
The caller supplies *both* the accepted path list and receipt_loader from the
server-side review store, in the same verified transaction. receipt_loader(task_id)
must return a durable receipt bound to a terminal independent accept (not author
notes or a caller-provided "approved" boolean), with:
  receipt_id: nonempty server-issued identifier
  task_id: exact task identity
  review_state: "accepted"
  review_event_id: positive integer identifying the independent review event
  base_sha: full SHA of the reviewed Git parent commit
  files: {relative_posix_file_path: {"sha256": reviewed-bytes hex digest,
                                     "mode": "100644" | "100755"}}
The helper rechecks exact path membership, bytes, modes, HEAD and path safety.
It cannot itself authenticate an arbitrary receipt_loader: production wiring
MUST use the authoritative server store and independently validate its receipt.
Deletion, submodules and symlinks are deliberately unsupported (fail closed).

The shared Git index is NEVER read for content or updated: a private temporary
index starts from base_sha, receives *only* the verified blobs, and produces a
commit-tree. Atomic update-ref HEAD <new> <expected-base> is the only ref write.
There is no Git add -A, checkout, reset, commit -a or production integration.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class CommitRefused(ValueError):
    """No accepted-only commit can be proven; never fall back to broad staging."""


@dataclass(frozen=True)
class CommitResult:
    commit_sha: str
    paths: tuple[str, ...]
    repeated: bool = False


_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z")
_PRIVATE_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def _git(root: Path, *args: str, env: dict[str, str] | None = None,
         data: bytes | None = None) -> bytes:
    clean_env = dict(os.environ)
    # Never let a parent Git operation redirect this subprocess to a foreign
    # index, worktree, object database, or injected config values.
    for key in list(clean_env):
        if key.startswith("GIT_"):
            clean_env.pop(key)
    clean_env.update(env or {})
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, env=clean_env, input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommitRefused(f"git unavailable: {exc}") from exc
    if result.returncode:
        reason = result.stderr.decode("utf-8", errors="replace").strip()[-280:]
        raise CommitRefused(f"git {args[0]} failed: {reason}")
    return result.stdout.strip()


def _safe_name(rel: object) -> str:
    if not isinstance(rel, str) or not rel or len(rel) > 1000:
        raise CommitRefused("empty or invalid accepted path")
    if any(ch in rel for ch in "\\\0\r\n:*?[]") or rel.startswith("/"):
        raise CommitRefused("unsafe or non-posix accepted path")
    parts = rel.split("/")
    if any(not p or p in (".", "..") or p.endswith((".", " "))
           or "\t" in p for p in parts):
        raise CommitRefused("non-canonical accepted path")
    for part in parts:
        lowered = part.casefold()
        if (lowered == ".git" or _RESERVED.fullmatch(lowered)
                or lowered.startswith(".env") or lowered.startswith("id_rsa")
                or lowered.startswith("id_ed25519")
                or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
                or any(term in lowered for term in (
                    "password", "secret", "credential", "private_key", "token"
                ))):
            raise CommitRefused("secret/reserved path rejected")
    return rel


def _read_verified(root: Path, rel: str, info: Mapping[str, object]) -> tuple[bytes, str]:
    if not isinstance(info, Mapping):
        raise CommitRefused("missing accepted file manifest")
    mode = info.get("mode")
    digest = info.get("sha256")
    if mode not in ("100644", "100755") or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise CommitRefused("invalid reviewed file mode/digest")
    candidate = root
    try:
        for part in rel.split("/"):
            candidate = candidate / part
            # Check every ancestor: junctions on Windows can escape a root even
            # if the final file is not itself marked as a symbolic link.
            if candidate.is_symlink() or (
                hasattr(os.path, "isjunction") and os.path.isjunction(candidate)
            ):
                raise CommitRefused("symlink/junction accepted path")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if os.path.normcase(str(candidate.absolute())) != os.path.normcase(str(resolved)):
            raise CommitRefused("accepted file resolved through an alias")
        if not stat.S_ISREG(candidate.stat().st_mode):
            raise CommitRefused("accepted path is not a regular file")
        data = candidate.read_bytes()
    except (OSError, ValueError) as exc:
        raise CommitRefused(f"accepted path not safe/present: {rel}") from exc
    if hashlib.sha256(data).hexdigest() != digest:
        raise CommitRefused("accepted file bytes changed after independent review")
    if _PRIVATE_KEY.search(data):
        raise CommitRefused("private key material in accepted file")
    return data, str(mode)


def _proof(root: Path, task_id: str, accepted_paths: Sequence[str],
           receipt_loader: Callable[[str], Mapping[str, object] | None]
           ) -> tuple[str, str, tuple[str, ...], dict[str, tuple[bytes, str]]]:
    if (not isinstance(task_id, str) or not _ID.fullmatch(task_id)
            or not callable(receipt_loader)):
        raise CommitRefused("invalid server receipt lookup")
    if isinstance(accepted_paths, (str, bytes)) or not isinstance(accepted_paths, Sequence):
        raise CommitRefused("accepted paths must be an explicit list")
    paths = tuple(_safe_name(p) for p in accepted_paths)
    if not paths or len(paths) > 400 or len({p.casefold() for p in paths}) != len(paths):
        raise CommitRefused("empty/duplicate/oversized accepted set")
    try:
        receipt = receipt_loader(task_id)
    except Exception as exc:
        raise CommitRefused("server receipt lookup failed") from exc
    if not isinstance(receipt, Mapping):
        raise CommitRefused("server receipt missing")
    receipt_id = receipt.get("receipt_id")
    base = receipt.get("base_sha")
    files = receipt.get("files")
    event_id = receipt.get("review_event_id")
    if (receipt.get("task_id") != task_id
            or receipt.get("review_state") != "accepted"
            or type(event_id) is not int or event_id <= 0
            or not isinstance(receipt_id, str) or not _ID.fullmatch(receipt_id)
            or not isinstance(base, str) or not _SHA.fullmatch(base)
            or not isinstance(files, Mapping)
            or set(files) != set(paths)):
        raise CommitRefused("no matching terminal server-side accept receipt")
    verified: dict[str, tuple[bytes, str]] = {}
    for rel in paths:
        verified[rel] = _read_verified(root, rel, files[rel])
    return str(base), str(receipt_id), paths, verified


def commit_accepted(root: str | Path, *, task_id: str,
                    accepted_paths: Sequence[str],
                    receipt_loader: Callable[[str], Mapping[str, object] | None]
                    ) -> CommitResult:
    """Commit exactly server-reviewed regular files, without touching shared index.

    The caller must serialize this operation with its existing integration
    controller; update-ref's expected-old SHA additionally fails closed on races.
    A repeat with the same server receipt is idempotent. An unrelated HEAD move
    is never silently rebased. No production entry point invokes this helper yet.
    """
    try:
        repo = Path(root).absolute()
        if repo.is_symlink() or repo.resolve(strict=True) != repo:
            raise CommitRefused("repository root is not canonical")
        top = Path(_git(repo, "rev-parse", "--show-toplevel").decode("utf-8")).resolve(strict=True)
        if os.path.normcase(str(top)) != os.path.normcase(str(repo)):
            raise CommitRefused("root is not the Git worktree top-level")
    except (OSError, ValueError) as exc:
        raise CommitRefused("repository root unavailable") from exc

    base, rid, paths, files = _proof(repo, task_id, accepted_paths, receipt_loader)
    current = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii")
    # A prior successful commit is identified by the exact durable receipt
    # identity AND the original parent. Do not create a second commit.
    receipt_marker = f"Fleet-Receipt: {rid}"
    task_marker = f"Fleet-Task: {task_id}"
    if current != base:
        parent = _git(repo, "rev-parse", "--verify", "HEAD^").decode("ascii")
        message = _git(repo, "log", "-1", "--format=%B").decode("utf-8")
        if parent == base and receipt_marker in message.splitlines() and task_marker in message.splitlines():
            return CommitResult(current, paths, repeated=True)
        raise CommitRefused("base SHA drift; independent review must be repeated")

    # Verify a full parent commit before writing objects.
    if _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").decode("ascii") != base:
        raise CommitRefused("accepted base is not a full commit")
    with tempfile.TemporaryDirectory(prefix="fleet-accepted-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", base, env=env)
        for rel, (content, mode) in files.items():
            blob = _git(repo, "hash-object", "-w", "--stdin", data=content).decode("ascii")
            _git(repo, "update-index", "--add", "--cacheinfo", mode, blob, rel, env=env)
        tree = _git(repo, "write-tree", env=env).decode("ascii")
        # An unchanged reviewed tree needs no commit. A genuinely empty
        # accepted-path list was already rejected by _proof.
        original_tree = _git(repo, "rev-parse", f"{base}^{{tree}}").decode("ascii")
        if tree == original_tree:
            return CommitResult(base, paths, repeated=True)
        changed = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only",
                           "-r", "-z", base, tree).decode("utf-8").split("\0"))
        changed.discard("")
        if not changed or not changed.issubset(set(paths)):
            raise CommitRefused("candidate tree changed files outside accepted list")
        author = {
            "GIT_AUTHOR_NAME": "Fleet Accepted",
            "GIT_AUTHOR_EMAIL": "fleet@localhost",
            "GIT_COMMITTER_NAME": "Fleet Accepted",
            "GIT_COMMITTER_EMAIL": "fleet@localhost",
        }
        message = (f"Fleet accepted {task_id}\n\n"
                   f"{receipt_marker}\n{task_marker}\nFleet-Base: {base}")
        commit = _git(repo, "commit-tree", tree, "-p", base, "-m", message,
                      env=author).decode("ascii")
        # CAS: no implicit merge, rebase, index update, or overwriting others.
        _git(repo, "update-ref", "-m", "fleet accepted-only integration",
             "HEAD", commit, base)
    return CommitResult(commit, paths)
