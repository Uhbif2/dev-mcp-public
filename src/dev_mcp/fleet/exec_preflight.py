"""Execution preflight for server-controlled commands.

All execution paths must use :func:`run_command` for BOTH the candidate probe
and the production invocation.  A successful parser check is not a proof that
the executable starts; collection is independent of a green unit-test run.

Production integration: integrate._gate_locked calls run_command; goals._load_doc
holds last-known-good gates AND resource commands; spec registrars preflight
new/changed acceptance commands; guard.install(register=True) calls
install_verified and rolls back failed Scheduler installs. Resource installer
commands without a transactional heartbeat/rollback adapter are rejected.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from dev_mcp.process_execution import bounded_run

from .gate_snapshot import (_MAX_BYTES, _MAX_FILES, SnapshotError, SnapshotStore,
                            _is_link,
                            _restricted, _relative)

# Media is never imported by pytest collection or a gate launch; it is dropped
# only when the git-visible tree would not fit the snapshot limits.
_DOCS = frozenset({".md", ".txt", ".pdf", ".docx", ".rtf", ".log"})
_ARCHIVE_DIRS = frozenset({"_attic", "todo", "archive", "_archive", "logs"})
_MEDIA = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".psd",
                    ".ai", ".wav", ".ogg", ".mp3", ".flac", ".mp4", ".mov", ".webm",
                    ".avi", ".pdf", ".zip", ".7z", ".ttf", ".otf", ".woff", ".woff2"})


_SIDE_MEDIA_MAX_BYTES = 512 * 1024 * 1024
_DEP_CHECK_MAX_FILES = 400_000
_PREFLIGHT_CACHE_ROOT = Path(__file__).resolve().parents[3] / "data" / "fleet" / "preflight-cache"
_WORKSPACE_PARALLEL_MIN_FILES = 512
_WORKSPACE_CHECK_WORKERS = 8


def _snapshot_files(root: Path) -> list[str] | None:
    return _snapshot_split(root)[0]


def _snapshot_split(root: Path, *,
                    versions: dict[str, tuple[int, int, int]] | None = None
                    ) -> tuple[list[str] | None, list[str]]:
    """(snapshot files, media left out of the snapshot) of a git tree.

    The media that did not fit is not dropped from the gate: preflight copies it
    beside the materialized snapshot, unhashed. Beta App's gate builds the
    site from assets/beta_app.png and failed `gate_launch_failed` on every
    integration for a day (19.10.2026) while the snapshot omitted all media.
    """
    names = _git_visible(root)
    if names is None:
        return None, []
    sizes: dict[str, int] = {}
    for name in names:
        stat = (root / name).stat()
        sizes[name] = stat.st_size
        if versions is not None:
            versions[name] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if len(names) <= _MAX_FILES and sum(sizes.values()) <= _MAX_BYTES:
        return names, []
    # 15.11: Beta App — 10 230 файлов, без медиа всё ещё 4451 файл / 141,7 МБ > лимитов, и ни одно
    # новое ТЗ с pytest-приёмкой не регистрировалось. Ступени «рядом, без хеша» (как медиа): медиа →
    # документы и архив → самые крупные, пока снимок не влезет. Проверке файлы доступны все.
    tiers = (
        lambda n: Path(n).suffix.casefold() in _MEDIA,
        lambda n: (Path(n).suffix.casefold() in _DOCS
                   or n.split("/", 1)[0].casefold() in _ARCHIVE_DIRS),
    )
    kept = list(names)
    beside: list[str] = []
    for tier in tiers:
        moved = [n for n in kept if tier(n)]
        if moved:
            out = set(moved)
            kept = [n for n in kept if n not in out]
            beside += moved
        if len(kept) <= _MAX_FILES and sum(sizes[n] for n in kept) <= _MAX_BYTES:
            return kept, beside
    total = sum(sizes[n] for n in kept)
    by_size = sorted(kept, key=lambda n: -sizes[n])
    out = set()
    for n in by_size:
        if len(kept) - len(out) <= _MAX_FILES and total <= _MAX_BYTES:
            break
        out.add(n)
        total -= sizes[n]
    return [n for n in kept if n not in out], beside + [n for n in by_size if n in out]


def _git_visible(root: Path) -> list[str] | None:
    """Files of a git tree that a preflight snapshot should capture.

    A whole-root walk captures docs, screenshots and brand assets: Beta App
    (18.10.2026) had 6382 such files, 178 MB, past the snapshot limits, so no
    new task of that project could be registered. Git-visible files honour
    .gitignore; media goes only if the rest would still not fit.
    None means "not a git tree" — the caller keeps the plain walk.
    """
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    names = []
    for raw in listed.stdout.split(b"\0"):
        name = raw.decode("utf-8", errors="surrogateescape")
        path = root / name
        if not name or _restricted(name) or path.is_symlink() or not path.is_file():
            continue
        names.append(name)
    return names


def _copy_side_media(root: Path, work: Path, names: Sequence[str]) -> None:
    """Copy media omitted from the snapshot into the gate's work tree (read-only data)."""
    total = 0
    for name in names:
        source, target = root / name, work / name
        if _restricted(name) or source.is_symlink() or not source.is_file() or target.exists():
            continue
        total += source.stat().st_size
        if total > _SIDE_MEDIA_MAX_BYTES:
            break
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

EXECUTABLE_SOURCES = (
    "fleet-projects.toml:projects.*.gate",
    "TZ YAML:acceptance",
    "fleet-projects.toml:resources.*.command",
    "installed process:command",
    "guard:entrypoint",
)


@dataclass(frozen=True)
class Executable:
    source: str
    identity: str
    command: str
    root: Path


def inventory(
    config: Mapping[str, Any], *, project_roots: Mapping[str, str | Path],
    acceptances: Mapping[str, tuple[str | Path, Sequence[str]]] | None = None,
    installations: Mapping[str, tuple[str | Path, str]] | None = None,
    guard_entry: tuple[str | Path, str] | None = None,
) -> tuple[Executable, ...]:
    """Enumerate command-bearing surfaces; never execute during discovery."""
    found: list[Executable] = []
    for project, info in (config.get("projects") or {}).items():
        if not isinstance(info, Mapping):
            continue
        command = info.get("gate")
        if isinstance(command, str) and command.strip():
            if project not in project_roots:
                raise ValueError(f"project root unavailable: {project}")
            found.append(Executable(EXECUTABLE_SOURCES[0], str(project), command,
                                    Path(project_roots[project])))
    for task, (root, commands) in (acceptances or {}).items():
        for number, command in enumerate(commands):
            found.append(Executable(EXECUTABLE_SOURCES[1], f"{task}:{number}",
                                    command, Path(root)))
    for resource, info in (config.get("resources") or {}).items():
        if isinstance(info, Mapping):
            for key in ("command", "run", "install_command"):
                if isinstance(info.get(key), str) and info[key].strip():
                    # Resources with commands must declare the directory, not
                    # silently inherit the long-running server's cwd.
                    root = info.get("cwd")
                    if not isinstance(root, str) or not root.strip():
                        raise ValueError(f"resource command missing cwd: {resource}")
                    found.append(Executable(EXECUTABLE_SOURCES[2], f"{resource}:{key}",
                                            info[key], Path(root)))
    for name, (root, command) in (installations or {}).items():
        found.append(Executable(EXECUTABLE_SOURCES[3], name, command, Path(root)))
    if guard_entry is not None:
        root, command = guard_entry
        found.append(Executable(EXECUTABLE_SOURCES[4], "guard", command, Path(root)))
    return tuple(found)


def parse_command(command: str, *, windows: bool | None = None) -> tuple[str, ...]:
    """The one argv decoder for live execution and preflight.

    Windows shlex's non-POSIX mode retains backslashes, but also retains outer
    quotes; strip only paired token-level quotes. Shell metacharacters are not
    interpreted: commands are argv, never arbitrary shell programs.
    """
    if windows is None:
        windows = os.name == "nt"
    if not isinstance(command, str) or not command.strip():
        raise ValueError("empty executable command")
    pieces = shlex.split(command, posix=not windows)
    if windows:
        pieces = [
            s[1:-1] if len(s) > 1 and s[0] == s[-1] and s[0] in ('"', "'") else s
            for s in pieces
        ]
    if not pieces or not pieces[0] or any(not p for p in pieces):
        raise ValueError("invalid executable command")
    return tuple(pieces)


@dataclass(frozen=True)
class ExecutionResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    status: str = ''

    @property
    def outcome(self) -> str:
        if self.status:
            return self.status
        return 'passed' if self.returncode == 0 else 'tests_failed'

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    command: str | Sequence[str], *, cwd: str | Path,
    binary_root: str | Path | None = None,
    changed: Sequence[str] = (), timeout_s: float = 180.0,
    runner: Callable[..., Any] = subprocess.run,
    windows: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ExecutionResult:
    """Launch via the SAME direct subprocess path in production and in probes.

    For a materialized source snapshot, relative *executables* can refer to
    the original environment (e.g. .venv/Scripts/python.exe); scripts and
    modules continue to resolve relative to the snapshot cwd. The caller
    specifies binary_root only for such snapshots.
    """
    argv = list(parse_command(command, windows=windows) if isinstance(command, str)
                else command)
    if not argv or any(not isinstance(p, str) or not p for p in argv):
        raise ValueError("invalid argv")
    if not Path(argv[0]).is_absolute():
        # Bare PATH executables (python/py/cmd) must retain PATH resolution.
        # A relative path with a separator is resolved against binary_root (snapshots) or
        # cwd: Windows CreateProcess resolves it against the *parent's* cwd, not `cwd=`.
        if "/" in argv[0] or "\\" in argv[0]:
            candidate = Path(binary_root if binary_root is not None else cwd) / argv[0]
            if candidate.is_file():
                argv[0] = str(candidate.resolve())
        elif (os.name == "nt" if windows is None else windows):
            # 17.10: `npm test` failed — npm is npm.cmd, and CreateProcess without a
            # shell does not apply PATHEXT. Resolve batch shims to their full path.
            found = shutil.which(argv[0])
            if found and Path(found).suffix.lower() in (".cmd", ".bat"):
                argv[0] = found
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DEV_MCP_FLEET_CHANGED": "\n".join(changed),
    }
    env.update(environment or {})
    deadline = time.monotonic() + max(0, float(timeout_s))
    release = None
    if runner is subprocess.run or runner is bounded_run:
        # Гейт/приёмка берут тот же host-мьютекс, что и run агентов (fleet/world.py).
        try:
            from . import world
            release, busy = world.hold_blocking(argv, f"гейт/приёмка: {' '.join(argv)[:80]}",
                                                wait_s=min(5.0, max(0, deadline - time.monotonic())))
        except Exception as exc:  # fail closed: resource accounting is execution authority
            return ExecutionResult(tuple(argv), 127, '', f'resource accounting: {exc}', 'launch_error')
        if busy:
            return ExecutionResult(tuple(argv), 124, "", "resource busy: " + busy, 'not_started_busy')
    try:
        remaining = max(0, deadline - time.monotonic())
        if remaining <= 0:
            return ExecutionResult(tuple(argv), 124, '', 'execution budget exhausted before launch', 'not_started_busy')
        execute = bounded_run if runner is subprocess.run else runner
        proc = execute(argv, cwd=str(cwd), shell=False, capture_output=True,
                      text=True, encoding="utf-8", errors="replace",
                      timeout=remaining, env=env)
        return ExecutionResult(tuple(argv), int(proc.returncode),
                               _output(proc.stdout), _output(proc.stderr),
                               getattr(proc,'execution_outcome',''))
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(tuple(argv), 124,
                               _output(exc.stdout), _output(exc.stderr) + f"\ntimeout: {timeout_s}s", 'timed_out')
    except (OSError, ValueError) as exc:
        return ExecutionResult(tuple(argv), 127, "", f"{type(exc).__name__}: {exc}", 'launch_error')
    finally:
        if release is not None:
            release()


def _output(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def managed_run(argv: Sequence[str], *, cwd: str | Path, timeout: float = 180,
                env: Mapping[str,str] | None = None, check: bool = False,
                **kwargs: Any) -> subprocess.CompletedProcess:
    """Adapter for candidate wrappers: retain resource admission through env adapters."""
    if kwargs.get('shell'):
        raise ValueError('managed execution never uses a shell')
    result=run_command(argv,cwd=cwd,timeout_s=timeout,runner=bounded_run,environment=env)
    text=kwargs.get('text') or kwargs.get('encoding') or kwargs.get('universal_newlines')
    output=result.stdout if text else result.stdout.encode('utf-8')
    error=result.stderr if text else result.stderr.encode('utf-8')
    completed=subprocess.CompletedProcess(argv,result.returncode,output,error)
    completed.execution_outcome=result.outcome
    if check:
        completed.check_returncode()
    return completed


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reason: str
    launch: ExecutionResult | None = None
    collection: ExecutionResult | None = None
    snapshot_id: str = ""


def _collector_python(command: str | Sequence[str], python: str) -> list[str]:
    """Collect tests with the interpreter the gate itself runs, not dev-mcp's own.

    17.10.2026: local-library tests import mcp 1.x; dev-mcp's venv has mcp 2.x,
    so collection failed for every TZ although `py -3 ...` acceptance was green.
    """
    argv = command.split() if isinstance(command, str) else list(command)
    if argv and Path(argv[0]).stem.lower() == "py":
        return [argv[0], *(a for a in argv[1:2] if a.startswith("-") and a[1:2].isdigit())]
    return [python]


def link_dir(source: Path, link: Path) -> Path:
    """Directory junction on Windows (no admin rights needed), symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(source), str(link))
    else:
        os.symlink(source, link, target_is_directory=True)
    return link


def unlink_dir(link: Path) -> None:
    """Remove only the link itself, never what it points to (lesson 14.10)."""
    if _is_link(link):
        # A failed unlink must never be hidden: a later recursive worktree
        # cleanup must not encounter an unknown still-attached dependency.
        os.rmdir(link) if os.name == "nt" else link.unlink()
    elif link.exists():
        raise SnapshotError("refusing to unlink a real dependency directory")


def project_deps(root: str | Path, names: Sequence[str] = ()) -> dict[str, Path]:
    """Ignored dependency folders of a project, to link (not copy) into a work tree.

    node_modules always; others from `deps` in fleet-projects.toml (Beta App:
    vendor/, vendor_qt/ — comtypes and PySide6 that its gate imports, 18.10.2026).
    """
    out: dict[str, Path] = {}
    for name in ("node_modules", *names):
        rel = PurePosixPath(str(name).replace("\\", "/"))
        if rel.is_absolute() or not rel.parts or ".." in rel.parts:
            continue
        source = Path(root).joinpath(*rel.parts)
        # 05.11: a single ignored file is an input too — Nova app/android/local.properties
        # (sdk.dir) was absent in the candidate, Gradle failed only there, and the accept
        # re-ran its red acceptance all night while the shared tree stayed green.
        if source.is_dir() or (source.is_file() and not source.is_symlink()):
            out[rel.as_posix()] = source
    return out


def link_deps(work: Path, deps: Mapping[str, Path] | None) -> list[Path]:
    links: list[Path] = []
    try:
        for rel, source in (deps or {}).items():
            target = work.joinpath(*PurePosixPath(_relative(rel)).parts)
            if target.exists() or target.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                # Files are copied, not linked: a small config, and nothing the work
                # tree does to its copy reaches the project. The temp tree removes it.
                shutil.copy2(source, target)
                continue
            links.append(link_dir(source, target))
    except BaseException:
        for link in reversed(links):
            unlink_dir(link)
        raise
    return links


def _dependency_fingerprints(deps: Mapping[str, Path] | None) -> dict[str, str]:
    """Fingerprint linked dependency trees by metadata (name, size, mode, mtime).

    Junctions are writable even in a materialized snapshot. Detect added,
    removed, or rewritten dependency files. Never follow links that escape the
    tree. Content is not read (cost: node_modules of 100k+ files on every
    preflight); a tree over the file budget is marked unverified, not rejected.
    """
    result: dict[str, str] = {}
    files = 0
    for rel, source in sorted((deps or {}).items()):
        _relative(rel)
        if source.is_file() and not _is_link(source):
            continue  # These are copied into the workspace, not linked.
        if _is_link(source) or not source.is_dir():
            raise SnapshotError(f"unsafe dependency source: {rel}")
        source_root = source.resolve(strict=True)
        digest = hashlib.sha256()
        pending = [(source, "")]
        while pending:
            folder, prefix = pending.pop()
            with os.scandir(folder) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    path = Path(entry.path)
                    name = f"{prefix}/{entry.name}" if prefix else entry.name
                    if _is_link(path):
                        # npm .bin and similar internal links are common. Their
                        # targets are already scanned by their real paths. Reject
                        # links pointing outside the checked directory instead.
                        try:
                            destination = path.resolve(strict=True)
                            internal = destination.is_relative_to(source_root)
                        except (OSError, RuntimeError):
                            internal = False
                        if not internal:
                            raise SnapshotError(f"dependency link escapes verified tree: {rel}/{name}")
                        digest.update(b"L\0" + name.encode("utf-8", "surrogateescape") + b"\0")
                        digest.update(str(destination).encode("utf-8", "surrogateescape") + b"\0")
                        continue
                    encoded = name.encode("utf-8", "surrogateescape")
                    if entry.is_dir(follow_symlinks=False):
                        digest.update(b"D\0" + encoded + b"\0")
                        pending.append((path, name))
                    elif entry.is_file(follow_symlinks=False):
                        # Только метаданные: запись через ссылку меняет размер/mtime.
                        # Чтение содержимого (до 2 ГБ дважды на прогон) убивало бы время
                        # preflight; переполнение бюджета — «не проверено», не отказ.
                        before = entry.stat(follow_symlinks=False)
                        files += 1
                        if files > _DEP_CHECK_MAX_FILES:
                            pending.clear()
                            digest = hashlib.sha256(b"unverified-budget")
                            break
                        digest.update(b"F\0" + encoded + b"\0")
                        digest.update(str((before.st_size, before.st_mode,
                                           before.st_mtime_ns)).encode("ascii") + b"\0")
                    else:
                        raise SnapshotError(f"non-regular dependency entry: {rel}/{name}")
        result[rel] = digest.hexdigest()
    return result


def _incremental_side(root: Path, names: Sequence[str], cache_root: Path,
                      previous: Path, versions: Mapping[str, Sequence[int]],
                      current_versions: Mapping[str, Sequence[int]]) -> Path:
    """Content-addressed side overlay: store only changed files, not 7k hardlinks.

    .fleet-side-origins.json is an internal lookup table; each manifest entry
    still carries the digest of the actual bytes. Origin IDs are restricted to
    this cache's content-addressed side directory.
    """
    import hashlib

    try:
        old_raw = (previous / ".fleet-side-manifest.json").read_text(encoding="utf-8")
        if hashlib.sha256(old_raw.encode("utf-8")).hexdigest() != previous.name:
            raise SnapshotError("side manifest digest mismatch")
        old = {name: (size, digest) for name, size, digest in json.loads(old_raw)}
        origins_file = previous / ".fleet-side-origins.json"
        old_origins = json.loads(origins_file.read_text(encoding="utf-8")) if origins_file.exists() else {}
        if not isinstance(old_origins, dict):
            raise SnapshotError("invalid side origins")
    except (OSError, ValueError, TypeError) as exc:
        raise SnapshotError("side base manifest unavailable") from exc
    stage = Path(tempfile.mkdtemp(prefix=".side-", dir=cache_root))
    entries: list[tuple[str, int, str]] = []
    origins: dict[str, str] = {}
    changes: set[str] = set()
    total = 0
    try:
        for name in sorted(names):
            source = root / name
            if _restricted(name):
                continue
            current = current_versions.get(name)
            if current is None:
                continue
            total += current[0]
            if total > _SIDE_MEDIA_MAX_BYTES:
                break
            out = stage / name
            version = versions.get(name)
            previous_entry = old.get(name)
            unchanged = (previous_entry is not None and version is not None
                         and tuple(version) == tuple(current)
                         and previous_entry[0] == current[0])
            if unchanged:
                origin = old_origins.get(name, previous.name)
                if not isinstance(origin, str) or len(origin) != 64 or any(c not in "0123456789abcdef" for c in origin):
                    raise SnapshotError("invalid side origin id")
                # Unchanged content is referenced by verified digest, not
                # read or linked. The workspace validates the resolved bytes
                # whenever a file must be copied from this origin.
                digest = previous_entry[1]
                origins[name] = origin
            else:
                if source.is_symlink() or not source.is_file():
                    continue
                before = source.stat()
                out.parent.mkdir(parents=True, exist_ok=True)
                for attempt in range(6):
                    shutil.copyfile(source, out)
                    after = source.stat()
                    if (before.st_size == after.st_size == out.stat().st_size
                            and before.st_mtime_ns == after.st_mtime_ns
                            and before.st_ctime_ns == after.st_ctime_ns):
                        break
                    if attempt == 5:
                        raise SnapshotError(f"side source changed during capture: {name}")
                    before = after
                with out.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                changes.add(name)
            entries.append((name, current[0] if unchanged else before.st_size, digest))
        manifest = json.dumps(entries, ensure_ascii=False)
        side_id = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
        target = cache_root / "side" / side_id
        if not target.is_dir():
            for name in changes:
                origins[name] = side_id
            (stage / ".fleet-side-manifest.json").write_text(manifest, encoding="utf-8")
            (stage / ".fleet-side-origins.json").write_text(
                json.dumps(origins, ensure_ascii=False), encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                stage.rename(target)
            except FileExistsError:
                pass
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _cached_source(root: Path, snapshots: SnapshotStore,
                   include: Sequence[str] | None,
                   critical_paths: Sequence[str] = (),
                   cache_status: dict[str, Any] | None = None) -> tuple[SnapshotStore, str, Path | None]:
    """Capture complete source bytes despite concurrent edits outside this TZ.

    The index is only an optimization, valid after a stable full-tree metadata pass.
    Owned/acceptance files are strict; unrelated tracked files can fall back to
    HEAD if rewritten continuously. Ignored dependencies remain linked by deps.
    """
    import hashlib
    import json

    if include is not None:
        if cache_status is not None:
            cache_status["source"] = "cold"
        return snapshots, snapshots.create(root, include=include).snapshot_id, None
    listed_versions: dict[str, tuple[int, int, int]] = {}
    names, side = _snapshot_split(root, versions=listed_versions)
    if names is None:
        # Non-git projects still benefit from the same indexed snapshot cache.
        # Match SnapshotStore's restricted walk rather than caching a whole
        # tree with potentially linked secrets or ignored dependencies.
        names = []
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                name for name in dirs
                if not _restricted(name) and not (Path(folder) / name).is_symlink())
            for name in sorted(files):
                path = Path(folder) / name
                rel = path.relative_to(root).as_posix()
                if not _restricted(rel) and not path.is_symlink():
                    names.append(rel)
                    stat = path.stat()
                    listed_versions[rel] = (stat.st_size, stat.st_mtime_ns,
                                            stat.st_ctime_ns)
    source_names = sorted([*names, *side])
    critical = tuple(str(name).replace("\\", "/").rstrip("/") for name in critical_paths)
    def is_critical(name: str) -> bool:
        return any(name == path or name.startswith(path + "/")
                   for path in critical if path)

    try:
        head_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
            capture_output=True, timeout=10, check=False,
        )
        lines = head_result.stdout.decode("utf-8", "replace").splitlines()
        head = lines[1].strip() if (
            head_result.returncode == 0 and len(lines) == 2
            and Path(lines[0]).resolve() == root.resolve()) else ""
    except (OSError, subprocess.SubprocessError):
        head = ""
    def file_version(name: str) -> tuple[int, int, int] | None:
        try:
            # Name enumeration already checked symlink components. Do not
            # repeat O(depth) lstat syscalls for 10k paths on each pass.
            path = root / name
            if path.is_symlink():
                return None
            st = path.stat()
            return st.st_size, st.st_mtime_ns, st.st_ctime_ns
        except OSError:
            return None

    def fingerprint(stats: list[tuple[str, tuple[int, int, int] | None]] | None = None) -> str:
        if stats is None:
            stats = [(name, file_version(name)) for name in source_names]
        return hashlib.sha256(json.dumps(
            [str(root.resolve()), head, stats], ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest()

    before_stats = [(name, listed_versions.get(name) or file_version(name))
                    for name in source_names]
    before_versions = dict(before_stats)
    before = fingerprint(before_stats)
    critical_before = {name: before_versions[name] for name in source_names
                       if is_critical(name)}
    fallback_used = False
    fallback_names: set[str] = set()
    def fallback(name: str) -> bytes | None:
        nonlocal fallback_used
        if is_critical(name) or not head:
            return None
        try:
            # Immutable HEAD bytes only if this is an actual tracked file.
            result = subprocess.run(
                ["git", "-C", str(root), "show", f"{head}:{name}"],
                capture_output=True, timeout=20, check=False)
            if result.returncode == 0:
                fallback_used = True
                fallback_names.add(name)
                return result.stdout
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    # Preflight snapshots live in dev-mcp's data directory, not in the source project.
    cache_root = _PREFLIGHT_CACHE_ROOT
    cache = SnapshotStore(cache_root / "objects")
    index = cache_root / "index" / before
    project_key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    latest = cache_root / "latest" / (project_key + ".json")
    try:
        info = json.loads(index.read_text(encoding="utf-8"))
        snapshot_id = info["snapshot"]
        side_id = info.get("side")
        cached_side = cache_root / "side" / side_id if side_id else None
        if (info["names"] == source_names and
                (not side or (cached_side is not None and cached_side.is_dir()))
                and fingerprint() == before):
            cache.manifest(snapshot_id)
            if cache_status is not None:
                cache_status["source"] = "cache"
            return cache, snapshot_id, cached_side if side else None
    except (OSError, ValueError, KeyError, TypeError, SnapshotError):
        pass

    if cache_status is not None:
        cache_status["source"] = "cold"
    try:
        previous = json.loads(latest.read_text(encoding="utf-8"))
        if (not isinstance(previous, dict)
                or previous.get("root") != str(root.resolve())
                or not isinstance(previous.get("versions"), dict)):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    old_versions = previous.get("versions", {})
    capture_started = time.perf_counter()
    if previous.get("snapshot") and old_versions:
        try:
            snapshot = cache.create_incremental(
                root, names, base_snapshot=previous["snapshot"],
                base_versions=old_versions,
                current_versions=before_versions,
                volatile_fallback=fallback)
            if cache_status is not None:
                cache_status["source"] = "incremental"
        except SnapshotError as exc:
            # Corrupt or stale base: rebuild from the live source, never trust
            # unverifiable data. A volatile live file remains a hard failure.
            if "changed during capture" in str(exc):
                raise
            snapshot = cache.create(root, include=names, volatile_fallback=fallback)
    else:
        snapshot = cache.create(root, include=names, volatile_fallback=fallback)
    if cache_status is not None:
        cache_status["create_s"] = round(time.perf_counter() - capture_started, 3)
    # A separate duration: side_s must not include snapshot creation time.
    side_started = time.perf_counter()
    side_dir = None
    if side:
        cache_root.mkdir(parents=True, exist_ok=True)
        old_side_id = previous.get("side")
        old_side = cache_root / "side" / old_side_id if isinstance(old_side_id, str) else None
        if old_versions and old_side is not None and old_side.is_dir():
            # Most commits change only Python/JS; all 7k excluded media files
            # are identical. Reusing the side object avoids 7k NTFS hardlinks.
            try:
                old_raw = (old_side / ".fleet-side-manifest.json").read_text(encoding="utf-8")
                old_entries = json.loads(old_raw)
                old_side_valid = hashlib.sha256(old_raw.encode("utf-8")).hexdigest() == old_side.name
                actual_names: list[str] = []
                total_side = 0
                for name in side:
                    version = before_versions.get(name)
                    if version is None:
                        continue
                    total_side += version[0]
                    if total_side > _SIDE_MEDIA_MAX_BYTES:
                        break
                    actual_names.append(name)
                side_same = (old_side_valid
                             and sorted(actual_names) == sorted(row[0] for row in old_entries)
                             and all(name in old_versions
                                     and tuple(old_versions[name]) == before_versions[name]
                                     for name in actual_names))
            except (OSError, ValueError, TypeError, IndexError):
                side_same = False
            if cache_status is not None:
                cache_status["side"] = "hit" if side_same else "incremental"
            side_dir = (old_side if side_same else
                        _incremental_side(root, side, cache_root, old_side,
                                          old_versions, before_versions))
        else:
            if cache_status is not None:
                cache_status["side"] = "cold"
            stage = Path(tempfile.mkdtemp(prefix=".side-", dir=cache_root))
            try:
                _copy_side_media(root, stage, side)
                # Side cache identity is its actual bytes, not timestamps.
                entries = []
                for path in sorted(stage.rglob("*")):
                    if path.is_file():
                        data = path.read_bytes()
                        entries.append((path.relative_to(stage).as_posix(),
                                        len(data), hashlib.sha256(data).hexdigest()))
                manifest = json.dumps(entries, ensure_ascii=False)
                (stage / ".fleet-side-manifest.json").write_text(
                    manifest, encoding="utf-8")
                side_id = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
                side_dir = cache_root / "side" / side_id
                side_dir.parent.mkdir(parents=True, exist_ok=True)
                if not side_dir.exists():
                    try:
                        stage.rename(side_dir)
                    except FileExistsError:
                        pass
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
    if any(file_version(name) != version
           for name, version in critical_before.items()):
        raise SnapshotError("owned/acceptance source changed during snapshot capture")
    # Concurrent edits to unrelated files must not reject this capture.
    # Never index a mixed-time source under the wrong metadata fingerprint.
    if cache_status is not None:
        cache_status["side_s"] = round(time.perf_counter() - side_started, 3)
    after_stats = [(name, file_version(name)) for name in source_names]
    after = fingerprint(after_stats)
    if not fallback_used and after == before:
        index.parent.mkdir(parents=True, exist_ok=True)
        temp_index = index.with_name(index.name + "." + str(os.getpid()))
        try:
            temp_index.write_text(json.dumps({
                "snapshot": snapshot.snapshot_id, "names": source_names,
                "side": side_dir.name if side_dir is not None else None,
            }), encoding="utf-8")
            temp_index.replace(index)
        finally:
            temp_index.unlink(missing_ok=True)
    # Even under concurrent unrelated edits, preserve a usable base. A version
    # is reusable only for a file stable across the capture and not from HEAD.
    after_versions = dict(after_stats)
    stable_versions = {name: list(version) for name, version in before_stats
                       if (version is not None and version == after_versions[name]
                           and name not in fallback_names)}
    latest.parent.mkdir(parents=True, exist_ok=True)
    staged_latest = latest.with_name(latest.name + "." + str(os.getpid()))
    try:
        staged_latest.write_text(json.dumps({
            "root": str(root.resolve()), "snapshot": snapshot.snapshot_id,
            "side": side_dir.name if side_dir else None,
            "versions": stable_versions,
        }, ensure_ascii=False), encoding="utf-8")
        staged_latest.replace(latest)
    finally:
        staged_latest.unlink(missing_ok=True)
    return cache, snapshot.snapshot_id, side_dir


def _session_source_unchanged(root: Path, session: dict[str, Any],
                              critical_paths: Sequence[str]) -> bool:
    """Reuse a captured immutable snapshot within one bootstrap batch only.

    All owned/acceptance inputs for the next TZ must still match the captured
    bytes. Unrelated live edits do not invalidate the batch's source view.
    """
    import hashlib

    if session.get("root") != str(root.resolve()):
        return False
    expected = session.get("digests")
    if not isinstance(expected, dict):
        return False
    for raw in critical_paths:
        name = str(raw).replace("\\", "/").rstrip("/")
        if not name or any(char in name for char in "*?[]"):
            return False
        try:
            name = _relative(name)
        except SnapshotError:
            return False
        try:
            path = SnapshotStore._safe_source(root, name)
        except (SnapshotError, OSError):
            # A missing future owned file is stable only when it was also
            # absent at capture time.
            if (name in expected or (root / name).exists()
                    or (root / name).is_symlink()):
                return False
            continue
        if name not in expected:
            return False
        try:
            before = path.stat()
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
        except OSError:
            return False
        if (digest != expected[name]
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            return False
    return True


def _save_source_session(session: dict[str, Any], root: Path,
                         snapshots: SnapshotStore, snapshot_id: str,
                         side_dir: Path | None) -> None:
    """A batch-local reference, never persisted as a bypass for verification."""
    manifest = json.loads((snapshots._object(snapshot_id) / "manifest.json").read_bytes())
    digests = {row["path"]: row["sha256"] for row in manifest["files"]}
    if side_dir is not None:
        entries = json.loads((side_dir / ".fleet-side-manifest.json").read_text(encoding="utf-8"))
        digests.update({name: sha for name, _, sha in entries})
    session.clear()
    session.update(root=str(root.resolve()), snapshots=snapshots,
                   snapshot_id=snapshot_id, side_dir=side_dir, digests=digests)


@functools.lru_cache(maxsize=1)
def _usn_api():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    ioctl = kernel.DeviceIoControl
    ioctl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                      wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    return create, ioctl, close


def _file_usn(path: Path) -> str | None:
    """NTFS journal change token; None falls back to full SHA-256 validation.

    USN cannot be reset by a gate restoring mtime/size. Identical token means
    the same file ID and no writes since a previously verified worktree.
    """
    if os.name != "nt":
        return None
    import ctypes
    import struct
    from ctypes import wintypes

    create, ioctl, close = _usn_api()
    create_err = ctypes.c_void_p(-1).value
    handle = create(str(path), 0x80, 7, None, 3, 0, None)  # READ_ATTRIBUTES
    if handle in (None, create_err):
        return None
    try:
        payload = (wintypes.WORD * 2)(2, 3)
        buffer = ctypes.create_string_buffer(1024)
        written = wintypes.DWORD()
        if not ioctl(handle, 0x900EB, ctypes.byref(payload), ctypes.sizeof(payload),
                     buffer, len(buffer), ctypes.byref(written), None):
            return None
        raw = buffer.raw[:written.value]
        if len(raw) < 48:
            return None
        major = struct.unpack_from("<H", raw, 4)[0]
        if major == 2:
            file_id = raw[8:16]
            usn = struct.unpack_from("<q", raw, 24)[0]
        elif major == 3:
            file_id = raw[8:24]
            usn = struct.unpack_from("<q", raw, 40)[0]
        else:
            return None
        return file_id.hex() + ":" + str(usn)
    finally:
        close(handle)


def _prepare_cached_work(snapshots: SnapshotStore, snapshot_id: str,
                         side_dir: Path | None, fingerprint: str) -> Path:
    """Repair one reusable worktree from captured bytes, never from live source.

    Gate code may change its workspace. Check every file and remove extras before
    the next gate; only dirty files get copied again. Bootstrap owns a host lock,
    so no two cached acceptance probes mutate this workspace concurrently.
    """
    import hashlib
    import json

    base = _PREFLIGHT_CACHE_ROOT / "work" / fingerprint
    if _is_link(base):
        raise SnapshotError("cached workspace must not be a link")
    manifest = json.loads((snapshots._object(snapshot_id) / "manifest.json").read_bytes())
    expected: dict[str, tuple[Path, int, str]] = {}
    for row in manifest["files"]:
        expected[row["path"]] = (
            snapshots._object(snapshot_id) / "tree" / row["path"],
            row["size"], row["sha256"])
    if side_dir is not None:
        side_manifest = side_dir / ".fleet-side-manifest.json"
        try:
            entries = json.loads(side_manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotError("side manifest missing or invalid") from exc
        side_origins_file = side_dir / ".fleet-side-origins.json"
        try:
            side_origins = (json.loads(side_origins_file.read_text(encoding="utf-8"))
                            if side_origins_file.is_file() else {})
            if not isinstance(side_origins, dict):
                raise ValueError("invalid side origins")
        except (OSError, ValueError) as exc:
            raise SnapshotError("side origins missing or invalid") from exc
        validated_origins: set[str] = set()
        for name, size, digest in entries:
            _relative_name = _relative(name)
            if _restricted(_relative_name) or _relative_name in expected:
                raise SnapshotError("unsafe side entry")
            origin = side_origins.get(_relative_name, side_dir.name)
            if (not isinstance(origin, str) or len(origin) != 64
                    or any(c not in "0123456789abcdef" for c in origin)):
                raise SnapshotError("unsafe side origin")
            if origin not in validated_origins:
                origin_dir = side_dir.parent / origin
                if _is_link(origin_dir):
                    raise SnapshotError("linked side origin")
                validated_origins.add(origin)
            expected[_relative_name] = (side_dir.parent / origin / _relative_name,
                                        size, digest)
    base.mkdir(parents=True, exist_ok=True)
    token_file = _PREFLIGHT_CACHE_ROOT / "work-usn" / (fingerprint + ".json")
    try:
        previous_state = json.loads(token_file.read_text(encoding="utf-8"))
        if not isinstance(previous_state, dict):
            previous_state = {}
    except (OSError, ValueError):
        previous_state = {}
    # An unchanged NTFS USN alone does NOT justify reusing bytes under a new
    # snapshot: the expected digest must also match the one last checked.
    known_tokens = previous_state.get("tokens", {})
    known_hashes = previous_state.get("hashes", {})
    if not isinstance(known_tokens, dict) or not isinstance(known_hashes, dict):
        known_tokens, known_hashes = {}, {}
    next_tokens: dict[str, str] = {}
    expected_dirs = {"/".join(PurePosixPath(name).parts[:depth])
                     for name in expected
                     for depth in range(1, len(PurePosixPath(name).parts))}
    # Never follow untrusted symlinks/junctions left by a preceding gate.
    # Keep visited directories in traversal order: a second os.walk() costs
    # another full NTFS enumeration on large Beta App worktrees.
    walked_dirs: list[Path] = []
    for folder, dirs, files in os.walk(base, topdown=True, followlinks=False):
        here = Path(folder)
        if here != base:
            walked_dirs.append(here)
        for name in list(dirs):
            path = here / name
            if _is_link(path):
                unlink_dir(path)
                dirs.remove(name)
        for name in files:
            path = here / name
            rel = path.relative_to(base).as_posix()
            if rel not in expected or _is_link(path):
                path.unlink()
    # Remove empty untracked directories deepest first without enumerating
    # the tree again. Every directory here was visited without following links.
    for here in reversed(walked_dirs):
        if _is_link(here):
            unlink_dir(here)
        elif here.relative_to(base).as_posix() not in expected_dirs:
            here.rmdir()
    def _check_workspace_file(item: tuple[str, tuple[Path, int, str]]) -> tuple[bool, str | None]:
        name, (_, size, digest) = item
        target = base.joinpath(*PurePosixPath(name).parts)
        # lstat checks type, size and reparse attributes in one syscall.
        # Path.is_file + is_symlink + stat made three calls per item across
        # 11k+ Beta App files, dominating the warm-workspace pass on NTFS.
        try:
            target_stat = target.lstat()
            clean_target = (stat.S_ISREG(target_stat.st_mode)
                            and target_stat.st_size == size
                            and not (getattr(target_stat, "st_file_attributes", 0)
                                     & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
        except OSError:
            clean_target = False
        if clean_target:
            usn = _file_usn(target)
            if (usn is not None and known_tokens.get(name) == usn
                    and known_hashes.get(name) == digest):
                return True, usn
            with target.open("rb") as stream:
                actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_digest == digest:
                return True, usn
        return False, None

    items = list(expected.items())
    # Read-only checks are independent. Parallel NTFS metadata probes avoid
    # holding the bootstrap lock for 30+ seconds on an unchanged 11k-file tree.
    # Repairs below remain strictly sequential and validate source bytes.
    if len(items) >= _WORKSPACE_PARALLEL_MIN_FILES:
        with ThreadPoolExecutor(max_workers=_WORKSPACE_CHECK_WORKERS) as pool:
            checks = list(pool.map(_check_workspace_file, items))
    else:
        checks = [_check_workspace_file(item) for item in items]
    for (name, (source, size, digest)), (clean, usn) in zip(items, checks):
        if clean:
            if usn is not None:
                next_tokens[name] = usn
            continue
        target = base.joinpath(*PurePosixPath(name).parts)
        # The gate changed these bytes (or a new snapshot expects new bytes).
        # Only dirty files are restored from the source snapshot.
        # The hashed source is read again only when a gate dirtied/missed a file.
        # On an unchanged workspace, this avoids a second read of 10k files.
        # An immutable snapshot file is only needed for repair. Validated
        # unchanged workspace bytes do not need a separate source-file stat.
        if source.is_symlink() or not source.is_file():
            raise SnapshotError("cached source symlink or missing")
        if source.stat().st_size != size:
            raise SnapshotError("cached source size mismatch")
        # Revalidate every component on actual reads; an injected junction in
        # an otherwise unused cached parent must not escape the cache root.
        parts = PurePosixPath(name).parts
        parent = source
        for _ in parts:
            parent = parent.parent
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise SnapshotError("linked cached source origin")
        for part in parts:
            parent = parent / part
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise SnapshotError("linked cached source parent")
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise SnapshotError("cached source digest mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.chmod(0o666)
        shutil.copyfile(source, target)
        token = _file_usn(target)
        if token is not None:
            next_tokens[name] = token
    token_file.parent.mkdir(parents=True, exist_ok=True)
    staged = token_file.with_name(token_file.name + "." + str(os.getpid()))
    try:
        staged.write_text(json.dumps({
            "tokens": next_tokens,
            "hashes": {name: digest for name, (_, _, digest) in expected.items()},
        }, sort_keys=True), encoding="utf-8")
        staged.replace(token_file)
    finally:
        staged.unlink(missing_ok=True)
    return base


@contextlib.contextmanager
def _cached_workspace_slot():
    """Serialize cache capture, workspace repair and gate execution across hosts."""
    lock_path = _PREFLIGHT_CACHE_ROOT / "workspace.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.25)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _serialized_cached_gate(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if kwargs.get("cache_source", False):
            started = time.perf_counter()
            with _cached_workspace_slot():
                if kwargs.get("timing") is not None:
                    kwargs["timing"]("workspace_lock", time.perf_counter() - started, {})
                return fn(*args, **kwargs)
        return fn(*args, **kwargs)
    return wrapped


@_serialized_cached_gate
def preflight_gate(
    command: str, *, source_root: str | Path,
    snapshots: SnapshotStore, include: Sequence[str] | None = None,
    binary_root: str | Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
    python: str = sys.executable,
    timeout_s: float = 180.0,
    windows: bool | None = None,
    deps: Mapping[str, Path] | None = None,
    acceptance_red: bool = False,
    cache_source: bool = False,
    critical_paths: Sequence[str] = (),
    collection_targets: Sequence[str] | None = None,
    timing: Callable[[str, float, dict[str, Any]], None] | None = None,
    source_session: dict[str, Any] | None = None,
) -> PreflightResult:
    """Probe gate launch AND pytest collection in a verified immutable snapshot.

    ``deps`` maps relative directories to ignored project dependencies. Large
    directories remain linked, NOT read-only: hash them before and after the
    probe and reject any mutation or unverified tree. This detects changes but
    cannot undo a gate's writes to the source. Files are copied, not linked.

    Never run tests in the mutable source tree. On a collection error (including
    duplicate module names) the candidate is rejected even when the gate passes.
    """
    root = Path(source_root)
    try:
        started = time.perf_counter()
        cache_status: dict[str, Any] = {}
        side: list[str] = []
        if cache_source:
            if (source_session is not None and include is None
                    and _session_source_unchanged(root, source_session, critical_paths)):
                snapshots = source_session["snapshots"]
                snapshot_id = source_session["snapshot_id"]
                side_dir = source_session["side_dir"]
                cache_status["source"] = "session"
            else:
                snapshots, snapshot_id, side_dir = _cached_source(
                    root, snapshots, include, critical_paths, cache_status)
                if source_session is not None and include is None:
                    _save_source_session(source_session, root, snapshots, snapshot_id, side_dir)
        else:
            side: list[str] = []
            if include is None:
                include, side = _snapshot_split(root)
            snapshot_id = snapshots.create(root, include=include).snapshot_id
            side_dir = None
            cache_status["source"] = "cold"
        if timing is not None:
            timing("snapshot", time.perf_counter() - started, cache_status)
        with tempfile.TemporaryDirectory(prefix="fleet-preflight-") as scratch:
            started = time.perf_counter()
            if cache_source:
                work = _prepare_cached_work(
                    snapshots, snapshot_id, side_dir,
                    __import__("hashlib").sha256(
                        str(root.resolve()).encode("utf-8")).hexdigest())
            else:
                work = snapshots.materialize(snapshot_id, Path(scratch) / "work")
                _copy_side_media(root, work, side)
            # Decided before linking deps: vendored packages carry their own tests.
            tests_present = (bool(collection_targets) if collection_targets is not None
                             else any(p.is_file() for p in work.rglob("test_*.py")))
            dependency_before = _dependency_fingerprints(deps)
            links = link_deps(work, deps)
            if timing is not None:
                timing("workspace", time.perf_counter() - started,
                       {"collection_targets": list(collection_targets or ())})
            try:
                outcome = _probe(command, work, root, binary_root, runner, python,
                                 timeout_s, windows, snapshot_id, tests_present,
                                 deps_ignore_args(deps), acceptance_red=acceptance_red,
                                 collection_cache=snapshots.root if cache_source else None,
                                 side_version=side_dir.name if side_dir is not None else "",
                                 collection_targets=collection_targets, timing=timing)
                if _dependency_fingerprints(deps) != dependency_before:
                    return PreflightResult(False, "dependency_source_changed",
                                           outcome.launch, outcome.collection, snapshot_id)
                return outcome
            finally:
                for link in links:
                    unlink_dir(link)
    except (SnapshotError, OSError, ValueError) as exc:
        return PreflightResult(False, f"snapshot_failed: {type(exc).__name__}: {exc}")


def deps_ignore_args(deps: Mapping[str, Path] | None) -> list[str]:
    """pytest must not collect linked dependency folders: they are not the project's
    tests. Beta App (19.10.2026) has no pytest config; collection walked into
    vendor_qt (PySide6) and the Android SDK and died with 0xC0000005."""
    return [f"--ignore={rel}" for rel in sorted(deps or {})]


def _probe(command: str, work: Path, root: Path, binary_root: str | Path | None,
           runner: Callable[..., Any], python: str, timeout_s: float,
           windows: bool | None, snapshot_id: str, tests_present: bool,
           ignore: Sequence[str] = (), *, acceptance_red: bool = False,
           collection_cache: Path | None = None,
           side_version: str = "",
           collection_targets: Sequence[str] | None = None,
           timing: Callable[[str, float, dict[str, Any]], None] | None = None) -> PreflightResult:
    started = time.perf_counter()
    gate = run_command(command, cwd=work, binary_root=binary_root or root, timeout_s=timeout_s,
                       runner=runner, windows=windows)
    if timing is not None:
        timing("acceptance", time.perf_counter() - started,
               {"command": command, "rc": gate.returncode})
    # Only a pytest baseline may count as RED. A broken custom command
    # returning rc=1 must not be mistaken for a failing future test.
    argv = parse_command(command)
    pytest_command = (
        Path(argv[0]).stem.casefold() in ("pytest", "py.test")
        or any(argv[i:i + 2] == ("-m", "pytest")
               for i in range(len(argv) - 1))
    )
    baseline_red = (acceptance_red and pytest_command and not gate.ok
                    and gate.outcome == "tests_failed"
                    and gate.returncode in (1, 4, 5)
                    and not any(marker in (gate.stdout + gate.stderr).lower()
                                for marker in ("importerror", "modulenotfounderror",
                                               "syntaxerror", "no module named pytest",
                                               "unrecognized arguments", "unknown command")))
    if not gate.ok and not baseline_red:
        return PreflightResult(False, "gate_launch_failed", gate, snapshot_id=snapshot_id)
    # A tree without tests has no pytest collector to run; a tree with
    # any test file must collect successfully, including imports.
    if not tests_present or collection_targets is not None and not collection_targets:
        if timing is not None:
            timing("collection", 0.0, {"skipped": True})
        return PreflightResult(True, "baseline_red" if baseline_red else "ok",
                               gate, snapshot_id=snapshot_id)
    # Collection depends on the immutable tree and interpreter, not on the TZ.
    import hashlib
    import json
    collect_argv = [*_collector_python(command, python), "-m", "pytest",
                    "--collect-only", "-q", "-p", "no:cacheprovider", *ignore,
                    *(collection_targets or ())]
    cache_file = None
    if collection_cache is not None and runner is subprocess.run:
        digest = hashlib.sha256(json.dumps(
            [snapshot_id, collect_argv, str(root.resolve()),
             side_version],
            ensure_ascii=False).encode("utf-8")).hexdigest()
        cache_file = collection_cache.parent / "collection" / digest
    collect = None
    started = time.perf_counter()
    if cache_file is not None and cache_file.is_file():
        try:
            recorded = json.loads(cache_file.read_text(encoding="utf-8"))
            collect = ExecutionResult(tuple(collect_argv),
                                      int(recorded["returncode"]),
                                      recorded["stdout"], recorded["stderr"])
        except (OSError, ValueError, KeyError, TypeError):
            collect = None
    cache_hit = collect is not None
    if collect is None:
        collect = run_command(collect_argv, cwd=work, binary_root=root,
                              timeout_s=timeout_s, runner=runner, windows=windows)
        # A deterministic pytest collection failure also belongs to this
        # immutable snapshot. Do not cache resource/launch/timeout failures.
        if cache_file is not None and collect.outcome in ("passed", "tests_failed"):
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({
                "returncode": collect.returncode, "stdout": collect.stdout,
                "stderr": collect.stderr,
            }), encoding="utf-8")
    if timing is not None:
        timing("collection", time.perf_counter() - started,
               {"rc": collect.returncode, "cache": "hit" if cache_hit else "miss",
                "targets": list(collection_targets or ())})
    if not collect.ok:
        if baseline_red and collect.returncode == 5:
            # A new owned test file may be the only future test in the tree.
            return PreflightResult(True, "baseline_red", gate, collect, snapshot_id)
        return PreflightResult(False, "pytest_collection_failed", gate, collect, snapshot_id)
    return PreflightResult(True, "baseline_red" if baseline_red else "ok",
                           gate, collect, snapshot_id)


class ConfigActivation:
    """Last-known-good config: publish only if *all* changed commands pass.

    event and author_task are service callbacks. For a rejected reload, both
    are called; a reporting failure cannot accidentally activate a bad value.
    """

    def __init__(
        self, active: Mapping[str, str], *,
        verify: Callable[[str, str], PreflightResult],
        event: Callable[[str, str, str], None] | None = None,
        author_task: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._active = dict(active)
        self._verify = verify
        self._event = event
        self._author_task = author_task

    @property
    def active(self) -> dict[str, str]:
        return dict(self._active)

    def activate(self, proposed: Mapping[str, str]) -> bool:
        next_config = dict(proposed)
        for identity, command in next_config.items():
            if not isinstance(command, str):
                raise ValueError(f"command is not text: {identity}")
            if self._active.get(identity) == command:
                continue
            try:
                result = self._verify(identity, command)
                reason = result.reason
                passed = result.ok
            except Exception as exc:
                reason = f"preflight_exception: {type(exc).__name__}: {exc}"
                passed = False
            if not passed:
                for reporter in (self._event, self._author_task):
                    if reporter is not None:
                        try:
                            reporter(identity, command, reason)
                        except Exception:
                            pass  # failure already rejected, never fail open
                return False
        self._active = next_config
        return True


def install_verified(
    install: Callable[[], Any], *, alive: Callable[[], bool],
    pulse: Callable[[], Any], rollback: Callable[[], None],
    duration_s: float, poll_s: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """An installer succeeds only after continuous process + fresh heartbeat.

    Pulse is a monotonic generation/sequence/timestamp (or None if missing).
    Require >=2 *new* beats, throughout duration_s; a stale pre-install
    heartbeat is insufficient. On all failures, invoke rollback.
    """
    if duration_s <= 0 or poll_s <= 0 or poll_s > duration_s:
        raise ValueError("invalid heartbeat observation window")
    previous = pulse()
    start = clock()
    beats = 0
    last_beat = start
    try:
        install()
        while True:
            now = clock()
            if not alive():
                break
            current = pulse()
            if current is not None and current != previous:
                previous = current
                last_beat = now
                beats += 1
            if now - last_beat > max(2 * poll_s, duration_s / 2):
                break
            if now - start >= duration_s:
                if beats >= 2:
                    return True
                break
            sleep(min(poll_s, max(0, duration_s - (now - start))))
    except Exception:
        pass
    rollback()
    return False


# In the working source tree, share the exact stdlib-only primitive used by
# the independently installed guard. dev_mcp's wheel intentionally contains
# only src/dev_mcp (not guard), so retain the local fallback above when running
# from a wheel without a guard package.
try:
    from guard.verified import install_verified as install_verified
except ModuleNotFoundError as exc:
    if exc.name not in {"guard", "guard.verified"}:
        raise
