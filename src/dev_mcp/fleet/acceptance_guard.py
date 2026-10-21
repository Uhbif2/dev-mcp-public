"""Read-only acceptance and ownership verification for a candidate checkout.

The caller supplies the registered task row, not agent-edited YAML as authority.
This module never writes to the queue and never enforces its own findings: the
queue integration must first journal them in TZ-49 shadow mode for 24 hours.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Iterable

from .model import norm_path, paths_overlap
from .spec import parse_spec


@dataclass(frozen=True)
class RegisteredProject:
    """Trusted registration plus the checkout whose actual Git diff is checked.

    root is the project's canonical root; checkout may be a claim worktree.
    acceptance and owns MUST come from the registration, not from the candidate.
    """

    root: Path
    checkout: Path
    acceptance: tuple[str, ...]
    owns: tuple[str, ...]
    spec_path: str

    @classmethod
    def from_store(
        cls, store: Any, project_name: str, tz: str, *,
        checkout: str | Path | None = None,
    ) -> "RegisteredProject":
        """Bind a check to authoritative project/task rows before inspecting files."""
        with store.read() as tx:
            row = tx.one(
                "SELECT p.root,t.acceptance,t.owns,t.spec_path FROM tasks t "
                "JOIN projects p ON p.name=t.project "
                "WHERE t.project=? AND t.tz=?", (project_name, tz),
            )
        if row is None:
            raise ValueError(f"unregistered task: {project_name}/{tz}")
        acceptance = json.loads(row["acceptance"])
        owns = json.loads(row["owns"])
        if (not isinstance(acceptance, list) or
                not all(isinstance(c, str) for c in acceptance) or
                not isinstance(owns, list) or
                not all(isinstance(p, str) for p in owns)):
            raise ValueError("invalid registered acceptance/owns")
        root = Path(row["root"]).resolve(strict=True)
        return cls(root, Path(checkout or root).resolve(strict=True),
                   tuple(acceptance), tuple(owns), str(row["spec_path"]))


class _GitError(RuntimeError):
    pass


def _git(checkout: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    if process.returncode:
        raise _GitError(f"git {args[0]} failed (rc={process.returncode})")
    return process.stdout


def _relative(checkout: Path, raw: str) -> str:
    path = Path(raw)
    resolved = (path if path.is_absolute() else checkout / path).resolve(strict=False)
    try:
        relative = resolved.relative_to(checkout)
    except ValueError as exc:
        raise ValueError("candidate path escapes checkout") from exc
    if not relative.parts or ".." in relative.parts:
        raise ValueError("invalid candidate path")
    return relative.as_posix()


def _test_targets(commands: tuple[str, ...]) -> tuple[str, ...]:
    """Extract pytest file/directory/glob operands, never shell-execute them."""
    operands: set[str] = set()
    for command in commands:
        try:
            tokens = shlex.split(command, posix=False)
        except ValueError:
            continue
        if not any("pytest" in x.lower() for x in tokens):
            continue
        found = False
        for token in tokens:
            token = token.strip("'\"").replace("\\", "/").split("::", 1)[0]
            if token.startswith("-") or "=" in token or token.startswith("/"):
                continue
            if (token.endswith(".py") or token == "tests" or
                    token.startswith("tests/") or token.startswith("test_")):
                # A selector cannot authorize tests from outside the checkout.
                if token != ".." and not token.startswith("../") and "/../" not in token:
                    operands.add(token.removeprefix("./"))
                    found = True
        if not found:
            operands.add("tests")
    return tuple(sorted(operands))


def _matches_test(path: str, target: str) -> bool:
    if target.endswith(".py") or any(c in target for c in "*?["):
        return fnmatch.fnmatchcase(path, target)
    return path.startswith(target.rstrip("/") + "/")


def _test_files(
    checkout: Path, base_sha: str, commands: tuple[str, ...],
) -> tuple[set[str], set[str]]:
    targets = _test_targets(commands)
    before = {
        x for x in _git(checkout, "ls-tree", "-r", "--name-only", base_sha).splitlines()
        if x.endswith(".py")
    }
    current = {
        p.relative_to(checkout).as_posix()
        for p in (checkout / "tests").rglob("*.py")
        if p.is_file() and not p.is_symlink()
    } if (checkout / "tests").is_dir() else set()
    # Explicit non-tests/ test modules are permitted.
    for target in targets:
        if target.endswith(".py") and "*" not in target and "?" not in target:
            if (checkout / target).is_file():
                current.add(target)
    selected_before = {p for p in before if any(_matches_test(p, t) for t in targets)}
    selected_current = {p for p in current if any(_matches_test(p, t) for t in targets)}
    return selected_before, selected_current


def _test_functions(tree: ast.AST) -> set[str]:
    result: set[str] = set()

    def visit(items: list[ast.stmt], prefix: str = "") -> None:
        for item in items:
            if isinstance(item, ast.ClassDef):
                visit(item.body, prefix + item.name + ".")
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name.startswith("test_"):
                    result.add(prefix + item.name)
    if isinstance(tree, ast.Module):
        visit(tree.body)
    return result


def _skip_markers(tree: ast.AST) -> Counter[str]:
    """Compare AST signatures; detect decorators, pytestmark and imperative skips."""
    result: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = node.attr.casefold()
            if name.startswith("skip") or name == "xfail":
                result[ast.dump(node, include_attributes=False)] += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id.casefold()
            if name.startswith("skip") or name == "xfail":
                result[ast.dump(node, include_attributes=False)] += 1
    return result


def _ast_checks(
    checkout: Path, base_sha: str, acceptance: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    if not _test_targets(acceptance):
        return errors
    old_paths, new_paths = _test_files(checkout, base_sha, acceptance)
    if not old_paths and not new_paths:
        return ["acceptance: pytest test files not found"]
    for relative in sorted(old_paths | new_paths):
        try:
            baseline = _git(checkout, "show", f"{base_sha}:{relative}") if relative in old_paths else ""
            current = (checkout / relative).read_text(encoding="utf-8") if relative in new_paths else ""
            old_ast = ast.parse(baseline, filename=relative)
            new_ast = ast.parse(current, filename=relative)
        except (SyntaxError, UnicodeError, OSError, _GitError) as exc:
            errors.append(f"test parse failed: {relative}: {type(exc).__name__}")
            continue
        lost_tests = _test_functions(old_ast) - _test_functions(new_ast)
        if lost_tests:
            errors.append(f"test functions removed: {relative}: {', '.join(sorted(lost_tests))}")
        old_asserts = Counter(
            ast.dump(node, include_attributes=False)
            for node in ast.walk(old_ast) if isinstance(node, ast.Assert)
        )
        new_asserts = Counter(
            ast.dump(node, include_attributes=False)
            for node in ast.walk(new_ast) if isinstance(node, ast.Assert)
        )
        if old_asserts - new_asserts:
            errors.append(f"assert removed or weakened: {relative}")
        if _skip_markers(new_ast) - _skip_markers(old_ast):
            errors.append(f"skip/xfail introduced: {relative}")
    return errors


def check(
    project: RegisteredProject, tz: str, base_sha: str,
    candidate_files: Iterable[str | Path],
) -> list[str]:
    """Return refusals against registered acceptance and the REAL candidate diff.

    candidate_files is an advisory legacy list, deliberately NOT used as the
    source of truth for owns. Neither a truncated list nor an agent-supplied
    absence of changes can hide tracked, staged, untracked or renamed files.
    The caller must keep the checkout stable while checking and running tests.
    """
    del candidate_files
    errors: list[str] = []
    root, checkout = project.root.resolve(), project.checkout.resolve()
    try:
        if Path(_git(checkout, "rev-parse", "--show-toplevel").strip()).resolve() != checkout:
            return ["candidate checkout is not a Git worktree root"]
        actual_sha = _git(checkout, "rev-parse", "--verify", f"{base_sha}^{{commit}}").strip()
        if len(actual_sha) != 40 or actual_sha != base_sha:
            return ["base_sha must identify a pinned full Git commit"]
        # This is the spec from the candidate, never the stored registration.
        spec = (checkout / project.spec_path).resolve(strict=False)
        if checkout not in spec.parents:
            errors.append("registered spec path escapes checkout")
        else:
            try:
                item = parse_spec(str(spec))
                if item.tz != tz or item.acceptance != project.acceptance:
                    errors.append("acceptance changed after registration without owner set-acceptance")
            except (OSError, ValueError) as exc:
                errors.append(f"registered spec unavailable or invalid: {type(exc).__name__}")
        changed = {
            x for x in _git(checkout, "diff", "--name-only", "-z", base_sha, "--").split("\0") if x
        }
        changed.update(
            x for x in _git(checkout, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if x
        )
        allowed = tuple(norm_path(own, project_root=str(root)) for own in project.owns)
        for name in sorted(changed):
            try:
                rel = _relative(checkout, name)
                source = norm_path(str(root / rel))
                if not any(paths_overlap(source, own) for own in allowed):
                    errors.append(f"outside owns: {rel}")
            except ValueError:
                errors.append(f"unsafe candidate path: {name}")
        errors.extend(_ast_checks(checkout, base_sha, project.acceptance))
    except (_GitError, OSError, ValueError) as exc:
        errors.append(f"acceptance guard cannot verify candidate: {type(exc).__name__}: {exc}")
    return errors


def blocking_findings(findings: Iterable[str], *, shadow: bool = True) -> list[str]:
    """Shadow is read-only journal input, NEVER a submit veto by itself."""
    return [] if shadow else list(findings)
