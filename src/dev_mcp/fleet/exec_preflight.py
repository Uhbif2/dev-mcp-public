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

import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .gate_snapshot import (_MAX_BYTES, _MAX_FILES, SnapshotError, SnapshotStore,
                            _restricted)

# Media is never imported by pytest collection or a gate launch; it is dropped
# only when the git-visible tree would not fit the snapshot limits.
_MEDIA = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".psd",
                    ".ai", ".wav", ".ogg", ".mp3", ".flac", ".mp4", ".mov", ".webm",
                    ".avi", ".pdf", ".zip", ".7z", ".ttf", ".otf", ".woff", ".woff2"})


_SIDE_MEDIA_MAX_BYTES = 512 * 1024 * 1024


def _snapshot_files(root: Path) -> list[str] | None:
    return _snapshot_split(root)[0]


def _snapshot_split(root: Path) -> tuple[list[str] | None, list[str]]:
    """(snapshot files, media left out of the snapshot) of a git tree.

    The media that did not fit is not dropped from the gate: preflight copies it
    beside the materialized snapshot, unhashed. Beta App's gate builds the
    site from assets/beta_app.png and failed `gate_launch_failed` on every
    integration for a day (19.10.2026) while the snapshot omitted all media.
    """
    names = _git_visible(root)
    if names is None:
        return None, []
    sizes = {n: (root / n).stat().st_size for n in names}
    if len(names) <= _MAX_FILES and sum(sizes.values()) <= _MAX_BYTES:
        return names, []
    media = [n for n in names if Path(n).suffix.casefold() in _MEDIA]
    dropped = set(media)
    return [n for n in names if n not in dropped], media


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

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    command: str | Sequence[str], *, cwd: str | Path,
    binary_root: str | Path | None = None,
    changed: Sequence[str] = (), timeout_s: float = 180.0,
    runner: Callable[..., Any] = subprocess.run,
    windows: bool | None = None,
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
    release = None
    if runner is subprocess.run:
        # Гейт/приёмка берут тот же host-мьютекс, что и run агентов (fleet/world.py).
        try:
            from . import world
            release, busy = world.hold_blocking(argv, f"гейт/приёмка: {' '.join(argv)[:80]}",
                                                wait_s=max(1.0, float(timeout_s)))
        except Exception:  # noqa: BLE001 — сбой учёта ресурса не должен ронять гейт
            release, busy = None, ""
        if busy:
            return ExecutionResult(tuple(argv), 124, "", "resource busy: " + busy)
    try:
        proc = runner(argv, cwd=str(cwd), shell=False, capture_output=True,
                      text=True, encoding="utf-8", errors="replace",
                      timeout=timeout_s, env=env)
        return ExecutionResult(tuple(argv), int(proc.returncode),
                               _output(proc.stdout), _output(proc.stderr))
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(tuple(argv), 124,
                               _output(exc.stdout), f"timeout: {timeout_s}s")
    except (OSError, ValueError) as exc:
        return ExecutionResult(tuple(argv), 127, "", f"{type(exc).__name__}: {exc}")
    finally:
        if release is not None:
            release()


def _output(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


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
    try:
        if link.is_symlink() or getattr(link, "is_junction", lambda: False)():
            os.rmdir(link) if os.name == "nt" else link.unlink()
    except OSError:
        pass


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
        if source.is_dir():
            out[rel.as_posix()] = source
    return out


def link_deps(work: Path, deps: Mapping[str, Path] | None) -> list[Path]:
    links: list[Path] = []
    for rel, source in (deps or {}).items():
        target = work.joinpath(*PurePosixPath(rel).parts)
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        links.append(link_dir(source, target))
    return links


def preflight_gate(
    command: str, *, source_root: str | Path,
    snapshots: SnapshotStore, include: Sequence[str] | None = None,
    binary_root: str | Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
    python: str = sys.executable,
    timeout_s: float = 180.0,
    windows: bool | None = None,
    deps: Mapping[str, Path] | None = None,
) -> PreflightResult:
    """Probe gate launch AND pytest collection in a verified immutable snapshot.

    ``deps`` maps a relative directory of the work tree to the project's own
    ignored dependency folder (node_modules, vendor): linked read-only, never
    copied into the snapshot and never hashed.

    Never run tests in the mutable source tree. On a collection error (including
    duplicate module names) the candidate is rejected even when the gate passes.
    """
    root = Path(source_root)
    try:
        side: list[str] = []
        if include is None:
            include, side = _snapshot_split(root)
        snapshot = snapshots.create(root, include=include)
        with tempfile.TemporaryDirectory(prefix="fleet-preflight-") as scratch:
            work = snapshots.materialize(snapshot.snapshot_id, Path(scratch) / "work")
            _copy_side_media(root, work, side)
            # Decided before linking deps: vendored packages carry their own tests.
            tests_present = any(p.is_file() for p in work.rglob("test_*.py"))
            links = link_deps(work, deps)
            try:
                return _probe(command, work, root, binary_root, runner, python,
                              timeout_s, windows, snapshot.snapshot_id, tests_present,
                              deps_ignore_args(deps))
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
           ignore: Sequence[str] = ()) -> PreflightResult:
    gate = run_command(command, cwd=work, binary_root=binary_root or root, timeout_s=timeout_s,
                       runner=runner, windows=windows)
    if not gate.ok:
        return PreflightResult(False, "gate_launch_failed", gate, snapshot_id=snapshot_id)
    # A tree without tests has no pytest collector to run; a tree with
    # any test file must collect successfully, including imports.
    if not tests_present:
        return PreflightResult(True, "ok", gate, snapshot_id=snapshot_id)
    collect = run_command([*_collector_python(command, python), "-m", "pytest",
                           "--collect-only", "-q", "-p", "no:cacheprovider", *ignore],
                          cwd=work, binary_root=root,
                          timeout_s=timeout_s, runner=runner, windows=windows)
    if not collect.ok:
        return PreflightResult(False, "pytest_collection_failed", gate, collect, snapshot_id)
    return PreflightResult(True, "ok", gate, collect, snapshot_id)


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
