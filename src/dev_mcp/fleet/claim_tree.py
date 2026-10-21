"""Isolated Git worktrees for accepted-only fleet integration.

This is Git plumbing, not the queue integration. A trusted caller must verify
the accepted event, commit ONLY reviewed paths in the claim worktree, and
serialize the post-merge project gate through the provided callback.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
import time
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Callable, Iterable, Iterator, Literal


class ClaimTreeError(RuntimeError):
    """A worktree operation is unsafe or Git rejected it."""


@dataclass(frozen=True)
class Claim:
    project_root: Path
    claim_id: str
    accepted: bool = False  # asserted by the trusted fleet controller, not the agent


@dataclass(frozen=True)
class IntegrationResult:
    status: Literal["integrated", "already_integrated", "needs_result", "gate_failed"]
    commit: str
    conflict_paths: tuple[str, ...] = ()
    gate_rc: int | None = None


def _slug(claim_id: str) -> str:
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise ValueError("claim_id must be nonempty")
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", claim_id)
        and ".." not in claim_id and not claim_id.endswith((".lock", "."))
        and claim_id.upper().split(".")[0] not in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
    ):
        return claim_id
    prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", claim_id).strip("-")[:36] or "claim"
    digest = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "-c", "core.quotepath=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaimTreeError(f"git operation unavailable: {exc}") from exc
    if check and completed.returncode:
        raise ClaimTreeError(
            f"git {args[0]} rejected ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout).strip()[:500]}"
        )
    return completed


def _clean(cwd: Path) -> bool:
    return not _git(cwd, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()


def _ancestor(cwd: Path, earlier: str, later: str) -> bool:
    completed = _git(cwd, "merge-base", "--is-ancestor", earlier, later, check=False)
    if completed.returncode not in (0, 1):
        raise ClaimTreeError("git merge-base failed")
    return completed.returncode == 0


def _snapshot_path(relative: str, *, allow_root: bool = False) -> str:
    """A Git-tree path, never an OS path or a revision/pathspec expression."""
    if not isinstance(relative, str) or "\x00" in relative or "\\" in relative:
        raise ClaimTreeError("invalid pinned source path")
    if not relative and allow_root:
        return ""
    if (not relative or relative.startswith("/") or ":" in relative
            or any(ch in relative for ch in "*?[]")
            or any(part in {"", ".", "..", ".git"} for part in relative.split("/"))):
        raise ClaimTreeError("unsafe pinned source path")
    if PurePosixPath(relative).as_posix() != relative:
        raise ClaimTreeError("noncanonical pinned source path")
    return relative


LOCK_STALE_S = 15 * 60


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_stale(lock: Path) -> bool:
    try:
        age = time.time() - lock.stat().st_mtime
        parts = lock.read_text(encoding="ascii", errors="ignore").split()
    except OSError:
        return False
    if parts and parts[0].isdigit():
        return not _pid_alive(int(parts[0]))  # живой держатель держит сколько нужно
    return age > LOCK_STALE_S  # старый формат без pid — только по возрасту


class ClaimWorktrees:
    """Isolated worktree manager bound to a checked-out, canonical project root."""

    def __init__(self, project_root: str | Path) -> None:
        root = Path(project_root).resolve(strict=True)
        reported = _git(root, "rev-parse", "--show-toplevel").stdout.strip()
        if Path(reported).resolve(strict=True) != root:
            raise ClaimTreeError("project_root must be the top-level trunk worktree")
        if _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).returncode:
            raise ClaimTreeError("trunk must have a checked-out branch")
        self.root = root
        self.workspace = root.parent / ".fleet-trees" / root.name

    def path(self, claim_id: str) -> Path:
        return self.workspace / _slug(claim_id)

    def branch(self, claim_id: str) -> str:
        return f"claim/{_slug(claim_id)}"

    def pinned_submission_tree(self, submission_id: str, revision: str,
                               tree_sha: str) -> str:
        """Verify server-provided sealed evidence without using the reviewer's HEAD.

        Authentication/authorization of the reviewer and lookup of all three
        values from the durable submission event belong to the Store caller.
        This low-level Git method must never be exposed to an agent as an
        arbitrary-ref reader. The private evidence ref pins the commit through
        worktree cleanup and ordinary Git garbage collection.
        """
        if (not isinstance(submission_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", submission_id)
                or not isinstance(revision, str)
                or not re.fullmatch(r"[0-9a-f]{40}", revision)
                or not isinstance(tree_sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", tree_sha)):
            raise ClaimTreeError("invalid sealed submission identity")
        pinned = _git(self.root, "rev-parse", "--verify",
                      f"refs/fleet/submissions/{submission_id}^{{commit}}").stdout.strip()
        if pinned != revision:
            raise ClaimTreeError("sealed submission ref mismatch")
        actual = _git(self.root, "rev-parse", "--verify",
                      f"{revision}^{{tree}}").stdout.strip()
        if actual != tree_sha:
            raise ClaimTreeError("sealed submission tree mismatch")
        return tree_sha

    def pinned_paths(self, submission_id: str, revision: str, tree_sha: str,
                     prefix: str = "", *, max_entries: int = 1000) -> tuple[str, ...]:
        """List regular files in immutable author bytes, not reviewer/trunk files.

        The caller must first prove an independent active review for the
        matching submission. Symlinks, Gitlinks and untrusted paths are omitted.
        """
        tree = self.pinned_submission_tree(submission_id, revision, tree_sha)
        path = _snapshot_path(prefix, allow_root=True)
        if type(max_entries) is not int or not 1 <= max_entries <= 1000:
            raise ClaimTreeError("invalid pinned listing limit")
        args = ["ls-tree", "-r", "-z", "--full-tree", tree]
        if path:
            args.extend(("--", path))
        rows = _git(self.root, *args).stdout.split("\x00")
        files: list[str] = []
        for entry in rows:
            if not entry:
                continue
            header, sep, name = entry.partition("\t")
            if not sep:
                raise ClaimTreeError("malformed sealed tree entry")
            mode, _, object_type = header.partition(" ")
            if mode not in {"100644", "100755"} or not object_type.startswith("blob "):
                continue
            if name != path and path and not name.startswith(path.rstrip("/") + "/"):
                continue
            if any(segment == ".git" for segment in name.split("/")):
                continue
            files.append(name)
            if len(files) > max_entries:
                raise ClaimTreeError("sealed source listing exceeds limit")
        return tuple(files)

    def pinned_file(self, submission_id: str, revision: str, tree_sha: str,
                    relative: str, *, max_bytes: int = 2_000_000) -> bytes:
        """Read exactly one regular Git blob from a server-pinned author tree.

        No worktree is mounted, no Git ref is moved, and neither a symlink nor
        a filesystem path can redirect the read into another claim or the OS.
        The reviewer must be authorized by the Store layer before calling.

        Synthetic regression: the reviewer can read pinned author bytes even
        after trunk advances, but cannot escape to another path/ref.

        >>> import tempfile
        >>> scratch = tempfile.TemporaryDirectory()
        >>> repo = Path(scratch.name) / "sample"
        >>> repo.mkdir()
        >>> def git(*args):
        ...     return subprocess.run(["git", "-C", str(repo), *args],
        ...         capture_output=True, text=True, check=True).stdout.strip()
        >>> _ = git("init", "-b", "main")
        >>> _ = git("config", "user.name", "Fixture")
        >>> _ = git("config", "user.email", "fixture@example.invalid")
        >>> _ = (repo / "source.txt").write_bytes(b"original\\n")
        >>> _ = git("add", "source.txt")
        >>> _ = git("commit", "-m", "original")
        >>> revision = git("rev-parse", "HEAD")
        >>> tree = git("rev-parse", "HEAD^{tree}")
        >>> _ = git("update-ref", "refs/fleet/submissions/" + "a"*32, revision)
        >>> _ = (repo / "source.txt").write_bytes(b"trunk later\\n")
        >>> _ = git("commit", "-am", "later")
        >>> manager = ClaimWorktrees(repo)
        >>> manager.pinned_file("a"*32, revision, tree, "source.txt")
        b'original\\n'
        >>> manager.pinned_paths("a"*32, revision, tree)
        ('source.txt',)
        >>> try:
        ...     manager.pinned_file("a"*32, revision, tree, "../source.txt")
        ... except ClaimTreeError as exc:
        ...     print(type(exc).__name__)
        ClaimTreeError
        >>> try:
        ...     manager.pinned_file("b"*32, revision, tree, "source.txt")
        ... except ClaimTreeError as exc:
        ...     print(type(exc).__name__)
        ClaimTreeError
        >>> scratch.cleanup()
        """
        tree = self.pinned_submission_tree(submission_id, revision, tree_sha)
        path = _snapshot_path(relative)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2_000_000:
            raise ClaimTreeError("invalid pinned file limit")
        entries = [entry for entry in _git(
            self.root, "ls-tree", "-r", "-z", "--full-tree", tree, "--", path,
        ).stdout.split("\x00") if entry]
        if len(entries) != 1:
            raise ClaimTreeError("pinned source file not found")
        header, sep, filename = entries[0].partition("\t")
        fields = header.split(" ")
        if (not sep or filename != path or len(fields) != 3
                or fields[0] not in {"100644", "100755"}
                or fields[1] != "blob"
                or not re.fullmatch(r"[0-9a-f]{40}", fields[2])):
            raise ClaimTreeError("pinned source is not a regular file")
        blob = fields[2]
        try:
            size = int(_git(self.root, "cat-file", "-s", blob).stdout.strip())
        except ValueError as exc:
            raise ClaimTreeError("invalid sealed blob size") from exc
        if size > max_bytes:
            raise ClaimTreeError("pinned source file exceeds limit")
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "cat-file", "blob", blob],
                capture_output=True, timeout=90, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaimTreeError("sealed blob unavailable") from exc
        if result.returncode or len(result.stdout) != size:
            raise ClaimTreeError("sealed blob could not be verified")
        return result.stdout

    @contextmanager
    def _lock(self, name: str = ".integration.lock") -> Iterator[None]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        lock = self.workspace / name
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            # 13.10: перезапуск сервера посреди интеграции оставил замок на 6,5 ч — каждая
            # выдача Fleet Platform отказывала (38 тыс. отказов в час), сервер захлебнулся.
            # Замок умершего процесса (или без pid и старше 15 мин) снимается.
            if not _lock_stale(lock):
                raise ClaimTreeError("integration lock held; retry after owner checks its holder") from exc
            lock.unlink(missing_ok=True)
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as again:
                raise ClaimTreeError("integration lock held; retry after owner checks its holder") from again
        try:
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode("ascii"))
            os.close(fd)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _verified_path(self, claim_id: str) -> Path:
        path = self.path(claim_id)
        if path.is_symlink() or not path.is_dir():
            raise ClaimTreeError("claim worktree unavailable or symlinked")
        active = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        if active != self.branch(claim_id):
            raise ClaimTreeError("claim worktree is on another branch")
        return path

    def open(self, claim_id: str, base: str = "HEAD") -> Path:
        """Create claim/<id> at the *current trunk HEAD*; never copy dirty files."""
        # Своё дерево задачи не ждёт интеграцию: её замок держит полный гейт ствола (~13 мин),
        # и 13.10 каждая выдача Fleet Platform отказывала «integration lock held» (~3 тыс./ч).
        # worktree add от текущего HEAD безопасен рядом с ff-слиянием — git сам блокирует refs.
        with self._lock(".open.lock"):
            path = self.path(claim_id)
            if path.exists() or path.is_symlink():
                return self._verified_path(claim_id)  # retry is idempotent
            current = _git(self.root, "rev-parse", "--verify", "HEAD").stdout.strip()
            requested = _git(self.root, "rev-parse", "--verify", f"{base}^{{commit}}").stdout.strip()
            if current != requested:
                raise ClaimTreeError("base is not the current trunk HEAD")
            branch_ref = f"refs/heads/{self.branch(claim_id)}"
            if _git(self.root, "show-ref", "--verify", "--quiet", branch_ref, check=False).returncode == 0:
                # 13-14.10: the folder of a claim tree vanished (cleanup, prune) while its
                # branch with the agent's commits stayed; every take of that claim's review
                # was refused (36/h). Re-attach the existing branch: commits are preserved.
                _git(self.root, "worktree", "prune", check=False)
                path.parent.mkdir(parents=True, exist_ok=True)
                attached = _git(self.root, "worktree", "add", str(path), self.branch(claim_id),
                                check=False)
                if attached.returncode != 0:
                    raise ClaimTreeError("claim branch exists without its expected worktree")
                return self._verified_path(claim_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            _git(self.root, "worktree", "add", "-b", self.branch(claim_id), str(path), current)
            return path

    def integrate(self, claim_id: str, *, accepted: bool = False,
                  gate: Callable[[Path], int] | None = None) -> IntegrationResult:
        """Rebase reviewed commits onto trunk and fast-forward it.

        On a content conflict, restore the claim branch and return needs_result
        with conflicted paths. Nothing is merged in that case. The queue owner
        creates the repair task; this module never invents queue state.
        """
        if not accepted:
            raise ClaimTreeError("claim has no verified acceptance")
        with self._lock():
            tree = self._verified_path(claim_id)
            if not _clean(tree):
                raise ClaimTreeError("claim has uncommitted changes; commit only reviewed paths")
            if not _clean(self.root):
                raise ClaimTreeError("trunk worktree is dirty; do not merge unrelated work")
            trunk = _git(self.root, "rev-parse", "HEAD").stdout.strip()
            branch = self.branch(claim_id)
            tip = _git(tree, "rev-parse", "HEAD").stdout.strip()
            if _ancestor(self.root, tip, trunk):
                status: Literal["integrated", "already_integrated"] = "already_integrated"
            else:
                if not _ancestor(self.root, trunk, tip):
                    rebased = _git(tree, "rebase", trunk, check=False)
                    if rebased.returncode:
                        unresolved = _git(
                            tree, "diff", "--name-only", "--diff-filter=U", "-z",
                            check=False,
                        ).stdout
                        _git(tree, "rebase", "--abort")
                        if unresolved:
                            return IntegrationResult(
                                "needs_result", trunk,
                                tuple(sorted(name for name in unresolved.split("\0") if name)),
                            )
                        raise ClaimTreeError("rebase failed without a content conflict")
                _git(self.root, "merge", "--ff-only", branch)
                status = "integrated"
            merged = _git(self.root, "rev-parse", "HEAD").stdout.strip()
            # The gate runs on the updated trunk, never on unfinished trees.
            if gate is not None:
                try:
                    rc = int(gate(self.root))
                except Exception as exc:
                    raise ClaimTreeError(
                        f"merged commit {merged}, but trunk gate raised: {exc}"
                    ) from exc
                if rc:
                    return IntegrationResult("gate_failed", merged, gate_rc=rc)
                return IntegrationResult(status, merged, gate_rc=0)
            return IntegrationResult(status, merged)

    def close(self, claim_id: str, *, abandon: bool = False) -> None:
        """Remove a merged claim, or explicitly discard a confirmed abandoned claim."""
        with self._lock():
            tree = self._verified_path(claim_id)
            tip = _git(tree, "rev-parse", "HEAD").stdout.strip()
            trunk = _git(self.root, "rev-parse", "HEAD").stdout.strip()
            if not abandon and not _ancestor(self.root, tip, trunk):
                raise ClaimTreeError("unmerged work: explicit abandon required")
            if not abandon and not _clean(tree):
                raise ClaimTreeError("uncommitted work: explicit abandon required")
            args = ["worktree", "remove"]
            if abandon:
                args.append("--force")
            _git(self.root, *args, str(tree))
            _git(self.root, "branch", "-D" if abandon else "-d", self.branch(claim_id))

    def cleanup(self, finished: Iterable[tuple[str, str, datetime]], *,
                older_than: datetime) -> tuple[str, ...]:
        """Maintenance entry: only terminal claims older than a trusted cutoff."""
        removed: list[str] = []
        for claim_id, state, finished_at in finished:
            if state not in {"accepted", "abandoned"} or finished_at >= older_than:
                continue
            if not self.path(claim_id).exists():
                continue
            self.close(claim_id, abandon=(state == "abandoned"))
            removed.append(claim_id)
        return tuple(removed)


def open(project_root: str | Path, claim_id: str, base: str = "HEAD") -> Path:
    return ClaimWorktrees(project_root).open(claim_id, base)


def integrate(claim: Claim, *, gate: Callable[[Path], int] | None = None) -> IntegrationResult:
    return ClaimWorktrees(claim.project_root).integrate(
        claim.claim_id, accepted=claim.accepted, gate=gate,
    )


def close(claim: Claim, *, abandon: bool = False) -> None:
    ClaimWorktrees(claim.project_root).close(claim.claim_id, abandon=abandon)


def python_environment(project_root: str | Path, claim_tree: str | Path,
                       environment: dict[str, str] | None = None) -> dict[str, str]:
    """Use project .venv's executable, but import source code from the claim."""
    env = dict(os.environ if environment is None else environment)
    source = str(Path(claim_tree) / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source, env.get("PYTHONPATH", ""))))
    env["VIRTUAL_ENV"] = str(Path(project_root) / ".venv")
    return env
