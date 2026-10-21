"""Read-only, versioned pre-registration checks for a proposed fleet task.

This module does not change the task registry or create Outcome.evidence.
It checks declared structured paths and command shapes; an optional trusted
fixture runner can probe a copied workspace, NEVER the live project.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import re
import shlex
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal

from .model import Task, norm_path, paths_overlap


CauseCode = Literal["needs_file_outside_owns", "acceptance_impossible"]
Action = Literal["register", "register_with_dependencies", "reject"]
DryRun = Literal["not_executed", "passed", "baseline_red", "interrupted"]
FixtureRunner = Callable[[tuple[str, ...], Path], int]

_SHELL = re.compile(r"[&|><;\r\n\x00]")
_SAFE_MODULES = frozenset({"pytest", "unittest", "py_compile", "compileall", "ruff"})
# Real open task states in the fleet registry. A review is a task *kind*,
# while an author awaiting review is still the owner in state "submitted".
_OPEN_OWNER_STATES = frozenset({"draft", "ready", "taken", "submitted", "frozen"})
_CLOSED_OWNER_STATES = frozenset({"accepted", "rejected", "done"})  # урок 50


@dataclass(frozen=True)
class PreflightObstacle:
    cause_code: CauseCode
    obstacle_type: Literal["needs_result", "defect"]
    predicate: dict[str, str]
    change_source: str | None
    resolver_work: str | None
    detail: str


@dataclass(frozen=True)
class PreflightCheck:
    command: str
    argv: tuple[str, ...]
    executable_found: bool
    dry_run: DryRun
    detail: str
    snapshot_version: str
    admissible_as_evidence: bool = False


@dataclass(frozen=True)
class PreflightReport:
    project: str
    tz: str
    subject_version: str
    snapshot_version: str
    action: Action
    obstacles: tuple[PreflightObstacle, ...]
    dependencies: tuple[str, ...]
    checks: tuple[PreflightCheck, ...]

    @property
    def ok(self) -> bool:
        return self.action != "reject"

    def to_dict(self) -> dict:
        return asdict(self)



class ProbeNotApplicable(ValueError):
    """The dry-run probe does not run this command shape; it is not a defect of the task."""

def _hash(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _within(path: str, root: str) -> bool:
    path = path.removesuffix("/**").rstrip("/")
    root = root.removesuffix("/**").rstrip("/")
    return path == root or path.startswith(root + "/")


def _covered(path: str, owned: str) -> bool:
    """A file is covered by exact owns or an ancestor /** (not by any parent file)."""
    if owned.endswith("/**"):
        return _within(path, owned)
    return path == owned


def _canonical(raw: str, root: Path) -> str:
    return norm_path(raw, project_root=str(root))


def _unsafe_path(
    raw: str, root: Path, *, allowed_roots: Iterable[Path] | None = None
) -> str | None:
    """Reject lexical escape and symlinks before canonicalization hides them."""
    if not raw or not isinstance(raw, str):
        return "empty_path"
    plain = raw.replace("\\", "/").removesuffix("/**")
    if any(ch in plain for ch in "*?["):
        return "invalid_glob"
    native = Path(plain)
    if not native.is_absolute():
        native = root / native
    # An ancestor symlink is unsafe even when it currently points inside root.
    current = native
    while True:
        if current.is_symlink():
            return "symlink_traversal"
        if current == current.parent:
            break
        current = current.parent
    resolved = native.resolve(strict=False)
    for allowed in allowed_roots if allowed_roots is not None else (root,):
        try:
            resolved.relative_to(allowed.resolve())
            return None
        except ValueError:
            continue
    return "outside_authorized_roots"


def _command(raw: str, root: Path | None = None) -> tuple[tuple[str, ...], str | None]:
    if not raw or not raw.strip():
        return (), "empty_acceptance"
    if _SHELL.search(raw) or "\x60" in raw:
        return (), "shell_syntax_forbidden"
    try:
        argv = tuple(arg.strip('"') for arg in shlex.split(raw, posix=False))
    except ValueError:
        return (), "invalid_quoting"
    if not argv:
        return (), "empty_acceptance"
    exe = Path(argv[0].replace("\\", "/")).name.casefold()
    if exe in {"python", "python.exe", "py", "py.exe"}:
        try:
            midx = argv.index("-m")
        except ValueError:
            # An existing project-local script is a valid acceptance target,
            # not an inline Python program. The trusted fixture probe must
            # still decline to execute it automatically.
            start = 2 if exe in {"py", "py.exe"} and len(argv) > 1 and argv[1] == "-3" else 1
            doc_check = (len(argv) > start + 1
                         and argv[start].replace('\\', '/').casefold() == 'tools/tzcheck.py'
                         and _safe_document_check(argv[start + 1:]))
            if (len(argv) < start + 1 or not _safe_relative_file(argv[start], (".py",))
                    or len(argv) != start + 1 and not doc_check):
                return argv, "python_without_allowlisted_module"
        else:
            if midx + 1 >= len(argv) or argv[midx + 1] not in _SAFE_MODULES:
                return argv, "unsupported_python_module"
            if any(a in {"-c", "-i"} for a in argv[1:]):
                return argv, "interactive_or_inline_python"
    elif exe in {"node", "node.exe"}:
        # No node -e, --import, --require, --test-reporter-destination, etc.
        # Only explicit test files (or the standard zero-argument test suite).
        if len(argv) < 2 or argv[1] != "--test" or any(
            not _safe_relative_file(arg, (".js", ".cjs", ".mjs")) for arg in argv[2:]
        ):
            return argv, "unsupported_node_test_args"
    elif exe in {'npm', 'npm.cmd', 'npm.exe'}:
        test_args = argv[3:] if len(argv) > 3 and argv[1:3] == ('test', '--') else ()
        safe_test_args = bool(test_args) and all(
            _safe_relative_file(arg, ('.ts', '.tsx', '.js', '.mjs', '.cjs'))
            and (root is None or _unsafe_path(arg, root) is None) for arg in test_args)
        script = ('test' if argv[1:] == ('test',) or safe_test_args else
                  argv[2] if len(argv) == 3 and argv[1] == 'run'
                  and argv[2] in {'test', 'typecheck'} else None)
        if script is None:
            return argv, 'unsupported_npm_args'
        if root is not None:
            problem = _npm_script_error(root, script)
            if problem:
                return argv, problem
    elif exe not in {"pytest", "pytest.exe", "ruff", "ruff.exe"}:
        return argv, "unsupported_executable"
    if any(a == ".." or a.startswith("../") or "/../" in a.replace("\\", "/") for a in argv[1:]):
        return argv, "argument_traversal"
    return argv, None


def _safe_document_check(args: tuple[str, ...]) -> bool:
    """The project's read-only document checker has a closed argv grammar.

    This grants no fixture execution: the trusted pytest probe still declines
    scripts. The ordinary immutable gate preflight remains mandatory.
    """
    if len(args) < 2 or args[0] != 'doc' or not _safe_relative_file(args[1], ('.md', '.txt')):
        return False
    index = 2
    while index < len(args):
        option = args[index]
        index += 1
        if option == '--sections':
            start = index
            while index < len(args) and not args[index].startswith('-'):
                if any(x in args[index] for x in ('/', '\\', ':', '@')):
                    return False
                index += 1
            if index == start:
                return False
        elif option == '--min-bytes':
            if index >= len(args) or not args[index].isdigit() or not 1 <= int(args[index]) <= 1_000_000:
                return False
            index += 1
        else:
            return False
    return True


def _npm_script_error(root: Path, name: str) -> str | None:
    """Validate test/typecheck scripts without executing npm or installation.

    Only explicit Vitest runs, local Node tests and non-emitting TypeScript
    checks qualify. Nested typecheck scripts and their lifecycle hooks must
    satisfy the same rules; shell programs and arbitrary npm flags do not.
    """
    package = root / 'package.json'
    try:
        if _unsafe_path('package.json', root) or package.stat().st_size > 1_048_576:
            return 'unsafe_npm_package'
        scripts = json.loads(package.read_text(encoding='utf-8')).get('scripts', {})
        if not isinstance(scripts, dict):
            return 'invalid_npm_package'
    except (OSError, ValueError, AttributeError):
        return 'npm_package_missing_or_invalid'

    def check(script: str, seen: frozenset[str]) -> bool:
        if script in seen or len(seen) >= 16 or not isinstance(scripts.get(script), str):
            return False
        seen = seen | {script}
        for hook in ('pre'+script, 'post'+script):
            if hook in scripts and not check(hook, seen):
                return False
        for part in scripts[script].split('&&'):
            if not part.strip() or _SHELL.search(part) or '\x60' in part:
                return False
            try:
                args = tuple(x.strip('"') for x in shlex.split(part.strip(), posix=False))
            except ValueError:
                return False
            if args == ('vitest', 'run'):
                continue
            if args and args[0] == 'node' and _command(part)[1] is None:
                continue
            if (len(args) == 3 and args[:2] == ('npm', 'run')
                    and re.fullmatch(r'typecheck(?::[A-Za-z0-9_-]+)?', args[2])):
                if not check(args[2], seen):
                    return False
                continue
            if args and args[0] == 'tsc':
                rest = list(args[1:])
                no_emit = '--noEmit' in rest
                if no_emit:
                    rest.remove('--noEmit')
                config = 'tsconfig.json'
                if len(rest) == 2 and rest[0] in ('-p', '--project'):
                    config = rest[1]
                    rest = []
                if rest or not _safe_relative_file(config, ('.json',)) or _unsafe_path(config, root):
                    return False
                if not no_emit:
                    try:
                        no_emit = json.loads((root/config).read_text(encoding='utf-8')).get('compilerOptions',{}).get('noEmit') is True
                    except (OSError, ValueError, AttributeError):
                        return False
                if no_emit:
                    continue
            return False
        return True

    return None if check(name, frozenset()) else 'unsafe_npm_script'


def _safe_relative_file(raw: str, suffixes: tuple[str, ...]) -> bool:
    """Lexically allow only local files, independent of the host OS syntax."""
    path = raw.replace("\\", "/")
    parts = path.split("/")
    return bool(
        path and not path.startswith(("/", "~", "@", "-"))
        and not ntpath.splitdrive(raw)[0]
        and path.endswith(suffixes)
        and all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
                and part not in {".", ".."} for part in parts)
    )


def _executable_exists(argv: tuple[str, ...], root: Path) -> bool:
    token = argv[0].replace("\\", "/")
    if "/" in token or token.startswith("."):
        path = Path(token)
        if not path.is_absolute():
            path = root / path
        if _unsafe_path(token, root):
            return False
        return path.is_file()
    if shutil.which(token) is None:
        return False
    exe = Path(token).name.casefold()
    if exe in {"python", "python.exe", "py", "py.exe"} and "-m" not in argv:
        # For a direct script acceptance, unlike a future test module, the
        # script is an existing verifier and must already live under root.
        start = 2 if exe in {'py', 'py.exe'} and len(argv) > 1 and argv[1] == '-3' else 1
        if len(argv) <= start:
            return False
        script = argv[start]
        if _unsafe_path(script, root):
            return False
        return (root / script.replace("\\", "/")).is_file()
    return True


def _snapshot(root: Path, fixture_paths: Iterable[str], destination: Path) -> str:
    manifest: list[tuple[str, str]] = []
    for raw in sorted(set(fixture_paths)):
        # Path(destination) / absolute_path would discard destination and write
        # straight into the live project: fixture inputs MUST be relative.
        native = Path(raw.replace("\\", "/"))
        if native.is_absolute() or native.drive:
            raise ValueError("fixture:absolute_path")
        error = _unsafe_path(raw, root)
        if error:
            raise ValueError(f"fixture:{error}")
        source = root / native
        if not source.is_file():
            raise ValueError("fixture:missing")
        data = source.read_bytes()
        target = destination / native
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest.append((raw.casefold(), hashlib.sha256(data).hexdigest()))
    return _hash(manifest)


def preflight_task(
    task: Task,
    project_root: str | Path,
    *,
    existing_tasks: Iterable[Task] = (),
    required_paths: Iterable[str] = (),
    fixture_paths: Iterable[str] = (),
    dry_runner: FixtureRunner | None = None,
    require_dry_run: bool = False,
    allowed_roots: Iterable[str | Path] | None = None,
) -> PreflightReport:
    """Validate a task BEFORE registration, without touching the task registry.

    - existing_tasks is trusted structured registry data, not parsed log prose.
    - required_paths names planned writes, never inferred from error messages.
    - dry_runner is a server-owned test probe; omitted means no code execution.
      require_dry_run fails closed when no trusted runner is installed.
      Even a passing fixture probe is NOT acceptance evidence.
    - allowed_roots must come from trusted policy; defaults to project_root.
      Fixture files always remain inside project_root regardless of authority.
    """
    root = Path(project_root).resolve()
    permitted = (
        tuple(Path(p).resolve() for p in allowed_roots)
        if allowed_roots is not None else (root,)
    )
    owners = tuple(existing_tasks)
    needed = tuple(required_paths)
    fixtures = tuple(fixture_paths)
    obstacles: list[PreflightObstacle] = []
    dependencies: set[str] = set()
    invalid_owns: set[str] = set()

    def blocked_path(path: str, reason: str, owner: Task | None = None) -> None:
        work = f"{owner.project}/{owner.tz}" if owner else None
        if work and owner.state in _OPEN_OWNER_STATES:
            dependencies.add(work)
        else:
            work = None
        obstacles.append(PreflightObstacle(
            cause_code="needs_file_outside_owns",
            obstacle_type="needs_result" if work else "defect",
            predicate={"path": path, "condition": reason},
            change_source=f"work:{work}" if work else None,
            resolver_work=work,
            detail=f"Путь {path}: {reason}",
        ))

    canonical_owns: list[str] = []
    for raw in task.owns:
        reason = _unsafe_path(raw, root, allowed_roots=permitted)
        if reason:
            invalid_owns.add(raw)
            blocked_path(raw, reason)
            continue
        canonical_owns.append(_canonical(raw, root))

    for raw in needed:
        reason = _unsafe_path(raw, root, allowed_roots=permitted)
        if reason:
            blocked_path(raw, reason)
            continue
        path = _canonical(raw, root)
        if not any(_covered(path, owned) for owned in canonical_owns):
            blocked_path(raw, "outside_declared_owns")

    for owner in owners:
        if (owner.project.casefold(), owner.tz.casefold()) == (
            task.project.casefold(), task.tz.casefold()
        ):
            continue
        # Урок 50: закрытая задача (accepted/rejected) зону не держит. Иначе две
        # принятые заявки с owns=tests/fleet навсегда отвергали любой черновик,
        # касающийся tests/fleet, а owns=src/dev_mcp — любой черновик ядра.
        if owner.state in _CLOSED_OWNER_STATES:
            continue
        for raw_owned in owner.owns:
            # Registry owns are already canonical; norm_path accepts their absolute form.
            other = _canonical(raw_owned, root)
            for raw, current in zip(
                (x for x in task.owns if x not in invalid_owns), canonical_owns
            ):
                if paths_overlap(current, other):
                    blocked_path(raw, "foreign_owner_overlap", owner)

    version = _hash({
        "spec_sha": task.spec_sha, "body": task.body,
        "owns": task.owns, "required": needed, "acceptance": task.acceptance,
        "other_owners": sorted(
            (x.project, x.tz, x.state, x.owns) for x in owners
        ),
    })
    checks: list[PreflightCheck] = []
    with tempfile.TemporaryDirectory(prefix="fleet-preflight-") as tmp:
        snapshot = Path(tmp)
        try:
            snapshot_version = _snapshot(root, fixtures, snapshot)
        except (ValueError, OSError) as exc:
            snapshot_version = _hash(["fixture_invalid", str(exc)])
            obstacles.append(PreflightObstacle(
                "acceptance_impossible", "defect",
                {"condition": "fixture_invalid"}, None, None,
                "Не удаётся создать изолированный fixture-снимок",
            ))

        commands = task.acceptance or ("",)
        for raw in commands:
            argv, error = _command(raw, root)
            exists = bool(argv) and not error and _executable_exists(argv, root)
            if not error and not exists:
                error = "executable_missing"
            status: DryRun = "not_executed"
            if not error and require_dry_run and dry_runner is None:
                error = "dry_run_unavailable"
            if not error and dry_runner is not None and not obstacles:
                try:
                    rc = dry_runner(argv, snapshot)
                    status = "passed" if rc == 0 else "baseline_red"
                except ProbeNotApplicable:
                    # 13.10: probe умеет только узкий pytest; команда другого вида (unittest,
                    # свой скрипт) — не «невозможная приёмка», а просто не прогоняется заранее.
                    status = "not_executed"
                except (Exception, KeyboardInterrupt):
                    status = "interrupted"
                    error = "dry_run_interrupted"
            if error:
                obstacles.append(PreflightObstacle(
                    cause_code="acceptance_impossible",
                    obstacle_type="defect",
                    predicate={"command": raw, "condition": error},
                    change_source=None,
                    resolver_work=None,
                    detail=f"Невозможно проверить acceptance: {error}",
                ))
            checks.append(PreflightCheck(
                command=raw, argv=argv, executable_found=bool(exists),
                dry_run=status, detail=error or "fixture_probe_only",
                snapshot_version=snapshot_version,
            ))
    dependent = obstacles and all(x.resolver_work for x in obstacles)
    action: Action = (
        "register_with_dependencies" if dependent
        else "reject" if obstacles
        else "register"
    )
    return PreflightReport(
        project=task.project, tz=task.tz, subject_version=version,
        snapshot_version=snapshot_version, action=action,
        obstacles=tuple(obstacles), dependencies=tuple(sorted(dependencies)),
        checks=tuple(checks),
    )
