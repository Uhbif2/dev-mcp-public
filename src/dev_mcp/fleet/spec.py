from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from dev_mcp.policy import Policy, PolicyError

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .model import Task, norm_path
from .mode_spec import ModeSpecError, mode_from_header


_HEADER_RE = re.compile(
    r"\A\s*\x60\x60\x60ya?ml[ \t]*\r?\n(.*?)\r?\n\x60\x60\x60[ \t]*(?:\r?\n|\Z)",
    re.S | re.I,
)
# Planner output may use ordinary YAML frontmatter instead of a fenced block.
# In both forms the machine-readable header must be the FIRST element; only
# its contents (not delimiters or trailing prose) participate in spec_sha.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S,
)
_ANY_HEADER_RE = re.compile(r"\x60\x60\x60ya?ml[ \t]*\r?\n", re.I)
_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)


class SpecError(ValueError):
    pass


_ACCEPTANCE_REJECTIONS: dict[tuple[str, str, str], str] = {}


def _ownership_snapshot(tx: Any) -> tuple[tuple[str, str, str, str], ...]:
    """Retired, proved transfers release stale reservations, never live claims.

    Inactive alone is insufficient: an owner may have paused a real project.
    Only the recorded operator/server transfer plus no live claims excludes it.
    Use the same predicate before the probe and at registration commit.
    """
    return tuple((str(r['project']),str(r['tz']),str(r['state']),str(r['owns']))
                 for r in tx.query(
        "SELECT t.project,t.tz,t.state,t.owns FROM tasks t JOIN projects p ON p.name=t.project "
        "WHERE t.state NOT IN ('accepted','rejected') AND (p.active=1 OR EXISTS (SELECT 1 FROM claims c WHERE c.project=p.name "
        "AND c.state IN ('active','preparing')) OR NOT EXISTS (SELECT 1 FROM events e "
        "WHERE e.project=p.name AND e.kind IN ('intent.work_transferred','intent.placeholder_retired'))) "
        "ORDER BY t.project,t.tz"))


def _freeze_registered_acceptance(
    store: Any, project: str, parsed: dict[str, Task],
    errors: dict[str, str], warnings: dict[str, list[str]],
) -> dict[str, str]:
    """Keep registered commands authoritative when a backlog YAML changes.

    Only the owner-side set-acceptance path may update tasks.acceptance.  A
    scanner can refresh task prose or metadata, never the execution contract.
    """
    read = store.read if hasattr(store, "read") else store.tx
    with read() as tx:
        registered = {
            str(row["tz"]).casefold(): str(row["acceptance"])
            for row in tx.query("SELECT tz,acceptance FROM tasks WHERE project=?", (project,))
        }
    for key, item in list(parsed.items()):
        registered_key = item.tz.casefold()
        if registered_key not in registered:
            continue
        try:
            old_commands = json.loads(registered[registered_key])
            if not isinstance(old_commands, list) or not all(
                isinstance(command, str) for command in old_commands
            ):
                raise ValueError("registered acceptance must be a list of strings")
        except (TypeError, ValueError):
            errors[item.spec_path] = "invalid_registered_acceptance"
            parsed.pop(key)
            continue
        if item.acceptance != tuple(old_commands):
            parsed[key] = dataclasses.replace(item, acceptance=tuple(old_commands))
            warnings.setdefault(item.spec_path, []).append(
                "registered acceptance preserved; change only via owner set-acceptance"
            )
    return registered


def _heavy_static_launch(command: str, root: Path) -> str | None:
    """None: not a heavy command, run the normal preflight. "": heavy and launchable.
    Otherwise the reason it cannot launch (interpreter or script missing)."""
    import shutil
    from . import world
    from .exec_preflight import parse_command

    try:
        argv = list(parse_command(command))
        cfg = world.config()
        heavy = [n for n in world._run_needs(cfg, argv) if world._scan_pattern(cfg[n])]
    except Exception:
        return None
    if not heavy or not argv:
        return None
    exe = argv[0]
    if not (shutil.which(exe) or (root / exe).is_file() or Path(exe).is_file()):
        return f"interpreter not found: {exe}"
    script = next((a for a in argv[1:] if a.lower().endswith(".py")), None)
    if script and not (root / script).is_file():
        return f"script not found: {script}"
    return ""


def _preflight_log(project: str, tz: str, command: str, result: Any) -> str:
    """Full launch/collection output of a refused registration; the reply keeps only
    350 chars (Beta App TZ-547, 19.10.2026: the cause was cut off)."""
    try:
        folder = Path(__file__).resolve().parents[3] / "logs" / "preflight"
        folder.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{project}-{tz}")
        path = folder / f"{safe}.txt"
        parts = [f"command: {command}", f"reason: {getattr(result, 'reason', '')}"]
        for name in ("launch", "collection"):
            run = getattr(result, name, None)
            if run is not None:
                parts.append(f"--- {name} rc={run.returncode}\n--- stdout\n{run.stdout}"
                             f"\n--- stderr\n{run.stderr}")
        path.write_text("\n".join(parts), encoding="utf-8")
        return str(path)
    except OSError:
        return ""


def _launch_failure(result: Any, root: Path | None = None) -> str:
    """Registration checks that acceptance *launches*, not that it passes (11.10.2026).

    A new TZ's acceptance is red by construction: its tests are written during the
    work. Rejecting rc!=0 made the fleet weaken acceptance (TZ-52 -> `tests/fleet`).
    Unlaunchable: timeout/not executable/not found, or the interpreter lacks the module.
    """
    launch = getattr(result, "launch", None)
    if launch is None:
        return str(getattr(result, "reason", "") or "launch_unknown")
    text = (launch.stdout or "") + (launch.stderr or "")
    # pytest rc=4 also means an invalid CLI option. Missing future test files
    # are allowed at registration, but a malformed command never becomes runnable.
    bad_args = launch.returncode == 4 and any(
        phrase in text.casefold() for phrase in (
            "unrecognized arguments", "unrecognized option", "no such option",
            "ambiguous option", "expected one argument",
        )
    )
    missing = "No module named" in text and not _future_project_module(text, root)
    stderr = launch.stderr or ""
    if launch.returncode == 124 and stderr.startswith("resource busy:"):
        # Transient: another heavy run holds the host lock. Not cached, retried
        # on the next bootstrap (Beta App TZ-544, 18.10.2026).
        return "resource_busy: " + stderr[-220:]
    if launch.returncode == 124 and stderr.startswith("timeout:") and not missing:
        # It launched and outran the registration budget: Beta App's
        # check_all takes 13-87 min. Unlaunchable commands fail in seconds.
        return ""
    if launch.returncode in (124, 126, 127) or missing or bad_args:
        return "gate_launch_failed: " + text[-220:]
    return ""


def _future_project_module(text: str, root: Path | None) -> bool:
    """unittest names a test module the TZ will write: ``No module named 'tests.test_x'``.

    Its package exists in the project but the module file does not yet — that is a
    red acceptance of new work, not an unlaunchable command (deep-research TZ-11,
    13.10.2026). A missing interpreter module (``No module named pytest``) stays fatal.
    """
    if root is None:
        return False
    names = re.findall(r"No module named '([\w.]+)'", text)
    if not names:
        return False
    for name in names:
        parts = name.split(".")
        if len(parts) < 2 or not (Path(root) / parts[0]).is_dir():
            return False
        if (Path(root).joinpath(*parts).with_suffix(".py")).is_file():
            return False
    return True


def _checked_acceptance_updates(
    store: Any, project: str, root: Path,
    parsed: dict[str, Task], errors: dict[str, str],
    dry_runner: Any | None = None,
) -> set[str]:
    """Fail closed on changed registered acceptance; never run under a DB lock.

    Every newly activated acceptance command (including first registration)
    requires a REAL trial run and collection on an immutable source snapshot.
    Ownership/spec validation is independent of this runtime requirement.
    """
    from .exec_preflight import preflight_gate, project_deps
    from .gate_snapshot import SnapshotStore
    from .goals import project_info
    from . import review

    read = store.read if hasattr(store, "read") else store.tx
    with read() as tx:
        sql = "SELECT tz,acceptance,author_worker FROM tasks WHERE project=?"
        rows = (tx.query(sql, (project,)) if hasattr(tx, "query")
                else tx.execute(sql, (project,)).fetchall())
        old = {
            str(row["tz"]).casefold(): dict(row)
            for row in rows
        }
    rejected: set[str] = set()
    for key, item in list(parsed.items()):
        previous = old.get(item.tz.casefold())
        try:
            before = (tuple(json.loads(previous["acceptance"] or "[]"))
                      if previous is not None else ())
        except (TypeError, ValueError):
            before = ()
        if before == item.acceptance:
            continue
        cache_key = (project, item.tz, item.spec_sha)
        log_path = ""

        reason = _ACCEPTANCE_REJECTIONS.get(cache_key, "")
        if not reason:
            try:
                with tempfile.TemporaryDirectory(prefix="fleet-acceptance-preflight-") as d:
                    snapshots = SnapshotStore(Path(d) / "objects")
                    python = root / ".venv" / "Scripts" / "python.exe"
                    deps = project_deps(root, tuple(project_info(project).get("deps") or ()))
                    for command in item.acceptance:
                        if dry_runner is not None:
                            # Drafts already probed this command with the trusted isolated
                            # fixture runner (drafts.register_drafts); its rc is a baseline.
                            continue
                        static = _heavy_static_launch(command, root)
                        if static is not None:
                            # A heavy gate (check_all, product_release_check) holds the
                            # host lock for 15-30 min; registration waited 180 s and
                            # starved (Beta App TZ-547, 19.10.2026). Launchability is
                            # proved statically; the run itself is the gate's business.
                            if static:
                                reason = "gate_launch_failed: " + static
                                break
                            continue
                        result = preflight_gate(
                            command, source_root=root, snapshots=snapshots,
                            binary_root=root, deps=deps,
                            python=str(python) if python.is_file() else sys.executable,
                            timeout_s=180,
                        )
                        if not result.ok:
                            log_path = _preflight_log(project, item.tz, command, result)
                        if not result.ok and result.reason == "gate_launch_failed":
                            reason = _launch_failure(result, root)
                            if reason:
                                break
                        elif (not result.ok and result.reason == "pytest_collection_failed"
                              and result.launch is not None and result.launch.ok
                              and "import file mismatch" not in (
                                  (result.collection.stdout or "") +
                                  (result.collection.stderr or "")
                                  if result.collection else "")):
                            # Registration proves the acceptance launches and refuses
                            # duplicate test module names. Other whole-tree collection
                            # errors are the gate's business at integration: Before
                            # Send TZ-547 (19.10.2026) was refused because backend/tests
                            # do not collect from the root even in the live tree.
                            continue
                        elif not result.ok:
                            reason = f"{result.reason}: " + (
                                (result.collection.stderr or result.collection.stdout)
                                if result.collection else
                                (result.launch.stderr or result.launch.stdout)
                                if result.launch else ""
                            )[-220:]
                            break
                if not item.acceptance and previous is not None:
                    reason = "empty_acceptance_requires_explicit_revision"
            except (OSError, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
        if not reason:
            continue
        parsed.pop(key)
        rejected.add(item.tz.casefold())
        errors[item.spec_path or item.tz] = "exec_preflight: " + reason[:350] + (
            f" [полный вывод: {log_path}]" if log_path else "")
        if cache_key in _ACCEPTANCE_REJECTIONS or reason.startswith("resource_busy"):
            continue
        _ACCEPTANCE_REJECTIONS[cache_key] = reason
        try:
            with store.tx() as tx:
                tx.record_event(kind="exec_preflight_reject", project=project, tz=item.tz,
                                payload={"source": "task.acceptance", "reason": reason[:350],
                                         "spec_sha": item.spec_sha})
            ident = hashlib.sha256((project + item.tz + item.spec_sha).encode()).hexdigest()[:8]
            review.open_request(
                store, ident, kind="other", to="queue", project=project,
                worker_id=(str(previous["author_worker"] or "") or None
                           if previous is not None else None),
                source_tz=item.tz,
                path=str(root / item.spec_path),
                summary=f"Исправить неприменимую acceptance {project}/{item.tz}",
                detail=f"Runtime preflight отклонил изменённую команду: {reason[:300]}",
            )
        except (ValueError, OSError):
            # Rejected executable remains rejected independent of reporting.
            pass
    return rejected


def _header(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    match = _HEADER_RE.search(text) or _FRONTMATTER_RE.search(text)
    if match:
        return text, match.group(1)
    if _ANY_HEADER_RE.search(text):
        raise SpecError(f"{path.name}: машиночитаемая шапка должна быть первой в файле")
    raise SpecError(f"{path.name}: нет машиночитаемой YAML-шапки в начале файла")


def _scalar(value: str) -> Any:
    if value in {"[]", "{}"}:
        return ast.literal_eval(value)
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        if not body:
            return []
        return [part.strip().strip("'\"") for part in body.split(",")]
    if value.lower() in {"null", "none", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value.strip("'\"")


def _fallback_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current is None:
                raise SpecError("элемент списка без ключа")
            result.setdefault(current, []).append(_scalar(stripped[2:].strip()))
            continue
        if ":" not in stripped:
            raise SpecError(f"не удалось разобрать YAML-строку: {raw_line}")
        key, value = stripped.split(":", 1)
        key, value = key.strip(), value.strip()
        current = key
        result[key] = [] if not value else _scalar(value)
    return result


def _load_header(path: Path, header_text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(header_text) if yaml is not None else _fallback_yaml(header_text)
    except Exception as exc:
        raise SpecError(f"{path.name}: ошибка YAML в шапке: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise SpecError(f"{path.name}: YAML-шапка должна быть mapping")
    return dict(data)


def _as_tuple(value: Any, *, field: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise SpecError(f"{path.name}: поле {field} должно быть списком")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _project_root(path: Path) -> Path:
    parts = list(path.resolve().parts)
    lowered = [part.casefold() for part in parts]
    if "todo" in lowered:
        index = lowered.index("todo")
        if index > 0:
            return Path(*parts[:index])
    return path.resolve().parent


def _relative_spec_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _body_after_header(text: str) -> str:
    match = _HEADER_RE.search(text) or _FRONTMATTER_RE.search(text)
    return text[match.end():].lstrip() if match else text


def parse_spec(abs_path: str) -> Task:
    path = Path(abs_path)
    if not path.is_absolute():
        raise SpecError(f"{path}: parse_spec ожидает абсолютный путь")
    text, header_text = _header(path)
    head = _load_header(path, header_text)
    try:
        mode_from_header(head)  # reject unsafe/unknown metadata at backlog sync
    except ModeSpecError as exc:
        raise SpecError(f"{path.name}: {exc}") from exc

    for key in ("tz", "owns"):
        if key not in head or head[key] in (None, ""):
            raise SpecError(f"{path.name}: в шапке нет обязательного поля {key}")

    project_root = _project_root(path)
    project = str(head.get("project") or project_root.name).strip()
    owns_raw = _as_tuple(head.get("owns"), field="owns", path=path)
    owns: list[str] = []
    for raw in owns_raw:
        try:
            lexical = raw.replace("\\", "/")
            if (lexical.startswith(("//?/", "//./", "//")) or
                    "\x00" in raw):
                raise ValueError("недопустимый device/UNC путь или нулевой байт")
            owns.append(norm_path(raw, project_root=str(project_root)))
        except ValueError as exc:
            raise SpecError(f"{path.name}: owns {raw!r}: {exc}") from exc

    body = _body_after_header(text)
    heading = _HEADING_RE.search(body)
    title = heading.group(1).strip() if heading else ""

    try:
        priority = int(head.get("priority", 5))
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{path.name}: priority должна быть целым числом") from exc

    return Task(
        project=project,
        tz=str(head["tz"]).strip(),
        state=str(head.get("state") or "ready").strip(),
        priority=priority,
        kind=str(head.get("kind") or "work").strip(),
        spec_path=_relative_spec_path(path, project_root),
        spec_sha=hashlib.sha256(header_text.encode("utf-8")).hexdigest(),
        needs=_as_tuple(head.get("needs"), field="needs", path=path),
        owns=tuple(owns),
        resources=_as_tuple(head.get("resources"), field="resources", path=path),
        acceptance=_as_tuple(head.get("acceptance"), field="acceptance", path=path),
        title=title,
        body=body,
    )


def spec_sha(abs_path: str) -> str:
    path = Path(abs_path)
    if not path.is_absolute():
        raise SpecError(f"{path}: spec_sha ожидает абсолютный путь")
    _, header_text = _header(path)
    return hashlib.sha256(header_text.encode("utf-8")).hexdigest()


def _policy_values(policy: Any, name: str) -> list[str]:
    value = policy.get(name, []) if isinstance(policy, Mapping) else getattr(policy, name, [])
    if value is None:
        return []
    if isinstance(value, (str, os.PathLike)):
        value = [value]
    return [os.fspath(item) for item in value]


def _under(path: str, root: str) -> bool:
    p = path[:-3] if path.endswith("/**") else path
    p = os.path.realpath(p).replace("\\", "/").rstrip("/").casefold()
    r = os.path.realpath(root).replace("\\", "/").rstrip("/").casefold()
    return p == r or p.startswith(r + "/")


def _validation_policy(policy: Any) -> Policy:
    """One resolver for Policy objects and legacy mapping/attribute policies.

    Construct a policy facade for the legacy shapes so that every safety
    decision comes from Policy.resolve rather than parallel string-prefix code.
    """
    if isinstance(policy, Policy):
        return policy
    roots = _policy_values(policy, "roots")
    writes = _policy_values(policy, "write_roots")
    trees = _policy_values(policy, "tree_roots")
    denied = _policy_values(policy, "tree_deny")
    if not roots:
        roots = list(dict.fromkeys([*writes, *trees]))
    setting = (policy.get("allow_write", False) if isinstance(policy, Mapping)
               else getattr(policy, "allow_write", False))
    return Policy(
        roots=[Policy._real(Path(p)) for p in roots],
        write_roots=[Policy._real(Path(p)) for p in writes],
        tree_roots=[Policy._real(Path(p)) for p in trees],
        tree_deny=[Policy._real(Path(p)) for p in denied],
        deny_patterns=_policy_values(policy, "deny_patterns"),
        allow_write=bool(setting),
    )


def _write_error(path: str, policy: Policy) -> str | None:
    """Use server policy for registration; preserve actionable denial codes."""
    try:
        policy.resolve(path, for_write=True)
    except PolicyError as exc:
        reason = str(exc)
        if "tree_deny" in reason:
            return reason
        if "файлы корня" in reason:
            return reason
        if "маск" in reason:
            return reason
        return "вне периметра: " + reason
    return None


def _find_project_root(task: Task, policy: Any) -> Path | None:
    roots = _policy_values(policy, "roots")
    if not roots:
        roots = _policy_values(policy, "write_roots")
    exact = [Path(root) for root in roots if Path(root).name.casefold() == task.project.casefold()]
    if exact:
        return exact[0]
    # A tree root is a parent of projects, not the project itself. Derive the
    # concrete project from its own TZ file instead of scanning arbitrary roots.
    if _policy_values(policy, "tree_roots") and task.spec_path:
        suffix = "/" + task.spec_path.replace("\\", "/").casefold().lstrip("/")
        matches: set[Path] = set()
        for raw in task.owns:
            source = raw.removesuffix("/**").replace("\\", "/")
            if source.casefold().endswith(suffix):
                candidate = Path(source[:-len(suffix)])
                if _write_error(str(candidate), _validation_policy(policy)) is None:
                    matches.add(candidate)
        if len(matches) == 1:
            return matches.pop()
    # Also support an existing project directory named after the task's project
    # under a tree root (even when the TZ file is not part of owns).
    for root in _policy_values(policy, "tree_roots"):
        candidate = Path(root) / task.project
        if candidate.is_dir() and _write_error(str(candidate), _validation_policy(policy)) is None:
            return candidate
    if _policy_values(policy, "tree_roots"):
        return None
    if len(roots) == 1:
        return Path(roots[0])
    return None


def _dependency_graph(
    project_root: Path,
    project: str,
) -> tuple[dict[str, tuple[str, ...]], set[str]]:
    graph: dict[str, tuple[str, ...]] = {}
    known: set[str] = set()
    candidates: list[Path] = []
    for rel in (Path("ToDo") / "tz", Path("ToDo") / "fleet"):
        folder = project_root / rel
        if folder.is_dir():
            candidates.extend(sorted(folder.glob("TZ-*.md")))
    for path in candidates:
        try:
            item = parse_spec(str(path.resolve()))
        except (OSError, SpecError):
            continue
        # Все ТЗ под корнем проекта — его ТЗ. Сравнивать имя нельзя: parse_spec берёт имя из
        # папки, а проект флота может называться иначе ("Fleet Platform" в dev-mcp-work) —
        # тогда любая needs была «не найдена» (10.10.2026).
        graph[item.tz] = tuple(d.split("#", 1)[0] for d in item.needs)
        known.add(item.tz)
    known |= _registry_task_ids(project_root)
    return graph, known


def _registry_task_ids(project_root: Path) -> set[str]:
    """File-less tasks of this project from the live registry (``request:*``, ``fix:*``).

    They have no ToDo/tz file, so a TZ that legitimately waits for a request chain was
    refused with «ТЗ не найдено» (Beta App TZ-550, 19.10.2026). Matched by project
    root, not name; a missing or locked registry adds nothing.
    """
    import sqlite3

    db = Path(__file__).resolve().parents[3] / "data" / "fleet" / "fleet.sqlite"
    if not db.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            root = str(project_root.resolve()).rstrip("\\/").casefold()
            names = [str(name) for name, path in conn.execute("SELECT name,root FROM projects")
                     if str(path or "").rstrip("\\/").casefold() == root]
            ids: set[str] = set()
            for name in names:
                ids.update(str(row[0]) for row in conn.execute(
                    "SELECT tz FROM tasks WHERE project=? AND tz LIKE '%:%'", (name,)))
            return ids
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


def _has_cycle(start: str, graph: Mapping[str, tuple[str, ...]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dep in graph.get(node, ()):
            if visit(dep):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return visit(start)


def validate_spec(task: Task, policy: Any) -> list[str]:
    issues: list[str] = []
    resolver = _validation_policy(policy)
    for owned in task.owns:
        plain = owned[:-3] if owned.endswith("/**") else owned
        if any(ch in plain for ch in "*?["):
            issues.append(f"owns {owned!r}: допустим только glob каталог/**")
            continue
        error = _write_error(plain, resolver)
        if error is not None:
            if (not resolver.tree_roots and not resolver.allow_write and
                    "вне периметра" in error):
                error = "вне write_roots (" + error + ")"
            issues.append(f"owns {owned!r}: {error}")

    if not task.acceptance:
        issues.append("warning: acceptance пуст")

    project_root = _find_project_root(task, policy)
    if project_root is not None:
        graph, known = _dependency_graph(project_root, task.project)
        graph = dict(graph)
        # `TZ-12#contract` — зависимость от уровня готовности TZ-12 (fleet/world.py).
        graph[task.tz] = tuple(d.split("#", 1)[0] for d in task.needs)
        known.add(task.tz)
        for dep in task.needs:
            if dep.split("#", 1)[0] not in known:
                issues.append(f"needs {dep}: ТЗ не найдено в проекте {task.project}")
        if _has_cycle(task.tz, graph):
            issues.append(f"needs: обнаружен цикл зависимостей от {task.tz}")

    return issues


def _event(project: str, tz: str, action: str) -> dict[str, Any]:
    return {
        "kind": "run",
        "project": project,
        "tz": tz,
        "tool": "sync_backlog",
        "payload": json.dumps({"action": action}, ensure_ascii=False),
    }


def sync_backlog(store: Any, project: str, root: str) -> dict:
    project_root = Path(root).resolve()
    backlog = project_root / "ToDo" / "tz"
    parsed: dict[str, Task] = {}
    errors: dict[str, str] = {}
    warnings: dict[str, list[str]] = {}
    seen_tz: dict[str, str] = {}
    duplicate_tz: set[str] = set()

    paths = sorted(backlog.glob("TZ-*.md")) if backlog.is_dir() else []
    for path in paths:
        rel = path.relative_to(project_root).as_posix()
        try:
            item = parse_spec(str(path.resolve()))
            item = dataclasses.replace(
                item,
                project=project,
                spec_path=rel,
                spec_sha=spec_sha(str(path.resolve())),
            )
            tz_key = item.tz.casefold()
            previous = seen_tz.get(tz_key)
            if previous is not None:
                duplicate_tz.add(tz_key)
                previous_item = parsed.pop(previous.casefold(), None)
                if previous_item is not None:
                    errors[previous] = (
                        f"{Path(previous).name}: дублирующий tz {previous_item.tz}"
                    )
                errors[rel] = f"{path.name}: дублирующий tz {item.tz}"
                continue
            seen_tz[tz_key] = rel
            if tz_key not in duplicate_tz:
                parsed[rel.casefold()] = item
        except (OSError, SpecError, ValueError) as exc:
            errors[rel] = str(exc)

    registered = _freeze_registered_acceptance(store, project, parsed, errors, warnings)
    rejected_updates = _checked_acceptance_updates(store, project, project_root, parsed, errors)
    inserted = updated = frozen = skipped_taken = unchanged = 0
    now_paths = set(parsed)

    with store.tx() as tx:
        existing_rows = [
            dict(row)
            for row in tx.query(
                "SELECT project,tz,state,spec_path,spec_sha,body,acceptance FROM tasks WHERE project=?",
                (project,),
            )
        ]
        existing_by_tz = {
            str(row["tz"]).casefold(): row for row in existing_rows
        }
        parsed_tzs = {item.tz.casefold() for item in parsed.values()}

        for item in parsed.values():
            row = existing_by_tz.get(item.tz.casefold())
            if (row is None and item.tz.casefold() in registered) or (
                row is not None and (
                    item.tz.casefold() not in registered
                    or str(row["acceptance"]) != registered[item.tz.casefold()]
                )
            ):
                errors[item.spec_path] = "registered_acceptance_changed_during_sync"
                continue
            values = (
                item.title,
                item.priority,
                item.kind,
                item.spec_path,
                item.spec_sha,
                json.dumps(list(item.needs), ensure_ascii=False),
                json.dumps(list(item.owns), ensure_ascii=False),
                json.dumps(list(item.resources), ensure_ascii=False),
                registered.get(item.tz.casefold(), json.dumps(list(item.acceptance), ensure_ascii=False)),
                item.body,
            )
            if row is None:
                tx.change(
                    "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                    "needs,owns,resources,acceptance,body,created_at,updated_at) "
                    "VALUES(?,?,?,'ready',?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                    (project, item.tz, *values),
                    event=_event(project, item.tz, "insert"),
                )
                inserted += 1
                continue

            if row["spec_sha"] == item.spec_sha:
                same_path = str(row["spec_path"]).casefold() == item.spec_path.casefold()
                same_body = str(row["body"] or "") == item.body
                if same_path and same_body:
                    unchanged += 1
                    continue
                tx.change(
                    "UPDATE tasks SET title=?,spec_path=?,body=?,updated_at=datetime('now') "
                    "WHERE project=? AND tz=?",
                    (item.title, item.spec_path, item.body, project, row["tz"]),
                    event=_event(
                        project,
                        str(row["tz"]),
                        "move_spec" if same_body else "refresh_body",
                    ),
                )
                updated += 1
                continue

            tx.change(
                "UPDATE tasks SET title=?,priority=?,kind=?,spec_path=?,spec_sha=?,needs=?,owns=?,"
                "resources=?,acceptance=?,body=?,updated_at=datetime('now') "
                "WHERE project=? AND tz=?",
                (*values, project, row["tz"]),
                event=_event(project, str(row["tz"]), "update"),
            )
            updated += 1

        for row in existing_rows:
            rel_key = str(row["spec_path"]).casefold()
            if str(row["tz"]).casefold() in parsed_tzs | rejected_updates:
                continue
            if not rel_key or rel_key in now_paths:
                continue
            if row["state"] == "taken":
                skipped_taken += 1
                continue
            if row["state"] != "frozen":
                tx.change(
                    "UPDATE tasks SET state='frozen',updated_at=datetime('now') "
                    "WHERE project=? AND tz=?",
                    (project, row["tz"]),
                    event=_event(project, str(row["tz"]), "freeze_missing"),
                )
                frozen += 1

    return {
        "found": len(parsed) + len(errors),
        "parsed": len(parsed),
        "inserted": inserted,
        "updated": updated,
        "frozen": frozen,
        "skipped_taken": skipped_taken,
        "unchanged": unchanged,
        "errors": errors,
        "warnings": warnings,
    }


def sync_selected(
    store: Any,
    project: str,
    root: str,
    tzs: Iterable[str],
    *,
    reactivate: bool = False,
    policy: Any | None = None,
    strict_preflight: bool = False,
    required_paths: Mapping[str, Iterable[str]] | None = None,
    fixture_paths: Mapping[str, Iterable[str]] | None = None,
    dry_runner: Any | None = None,
    new_only: bool = False,
) -> dict[str, Any]:
    """Import/update only explicitly selected backlog items.

    Unlike sync_backlog(), this never freezes tasks omitted from the selection and
    therefore is safe for one-time cutover of an old project whose ToDo/tz folder
    also contains historical completed specifications.
    strict_preflight additionally validates the ownership/spec dry-run contract;
    executable acceptance changes are trial-run regardless of that option.
    """

    requested = {str(tz).strip().casefold(): str(tz).strip() for tz in tzs if str(tz).strip()}
    if not requested:
        raise ValueError("sync_selected requires at least one tz")

    project_root = Path(root).resolve()
    from .project_identity import require_available
    read = store.read if hasattr(store, 'read') else store.tx
    with read() as tx:
        require_available(tx, project, str(project_root))
    backlog = project_root / "ToDo" / "tz"
    parsed: dict[str, Task] = {}
    errors: dict[str, str] = {}
    warnings: dict[str, list[str]] = {}
    candidates: dict[str, list[Task]] = {}
    candidate_errors: dict[str, list[tuple[str, str]]] = {}

    for path in sorted(backlog.glob("TZ-*.md")) if backlog.is_dir() else []:
        rel = path.relative_to(project_root).as_posix()
        folded_name = path.stem.casefold()
        filename_keys = [
            key for key in requested
            if folded_name == key or folded_name.startswith(key + "-")
        ]
        try:
            item = parse_spec(str(path.resolve()))
        except (OSError, SpecError, ValueError) as exc:
            for key in filename_keys:
                candidate_errors.setdefault(key, []).append((rel, str(exc)))
            continue
        key = item.tz.casefold()
        if key not in requested:
            continue
        candidates.setdefault(key, []).append(
            dataclasses.replace(
                item,
                project=project,
                spec_path=rel,
                spec_sha=spec_sha(str(path.resolve())),
            )
        )

    for key, shown in requested.items():
        items = candidates.get(key, [])
        if len(items) == 1:
            item = items[0]
            if policy is not None:
                issues = validate_spec(item, policy)
                hard = [issue for issue in issues if not issue.startswith("warning:")]
                soft = [issue for issue in issues if issue.startswith("warning:")]
                if hard:
                    errors[item.spec_path or shown] = "; ".join(hard)
                    continue
                if soft:
                    warnings[item.spec_path or shown] = soft
            parsed[key] = item
            continue
        if len(items) > 1:
            paths = ", ".join(item.spec_path for item in items)
            errors[shown] = f"{shown}: дублирующий tz в валидных ТЗ: {paths}"
            continue
        malformed = candidate_errors.get(key, [])
        if malformed:
            rel, message = malformed[0]
            errors[rel] = message
        else:
            errors[shown] = f"{shown}: ТЗ не найдено в {backlog}"

    # Registered commands are immutable across every scanner entry point.
    registered = _freeze_registered_acceptance(store, project, parsed, errors, warnings)

    preflight_reports: dict[str, dict[str, Any]] = {}
    owner_snapshot: tuple[tuple[str, str, str, str], ...] | None = None
    if strict_preflight:
        from .spec_preflight import preflight_task

        # Read-only snapshot BEFORE any registration. Structured DB fields
        # supply ownership: no identifiers are extracted from error prose.
        with store.read() as tx:
            owner_snapshot = _ownership_snapshot(tx)
        owners: list[Task] = []
        try:
            for name, tz, state, owns_json in owner_snapshot:
                owns = json.loads(owns_json)
                if not isinstance(owns, list) or any(not isinstance(p, str) for p in owns):
                    raise ValueError("registered owns must be a list of paths")
                owners.append(Task(
                    project=name, tz=tz, state=state, priority=5,
                    kind="work", spec_path="", spec_sha="", needs=(),
                    owns=tuple(owns), resources=(), acceptance=(), title="", body="",
                ))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            # Never proceed on a corrupted ownership snapshot.
            errors["preflight"] = f"invalid_registry_owns: {type(exc).__name__}"
            parsed.clear()

        authorized = []
        if policy is not None:
            authorized = (
                _policy_values(policy, "roots") if
                (policy.get("allow_write", False) if isinstance(policy, Mapping)
                 else getattr(policy, "allow_write", False))
                else [*_policy_values(policy, "write_roots"),
                      *_policy_values(policy, "tree_roots")]
            )
        for key, item in list(parsed.items()):
            report = preflight_task(
                item, project_root,
                existing_tasks=owners,
                required_paths=tuple((required_paths or {}).get(item.tz, ())),
                fixture_paths=tuple((fixture_paths or {}).get(item.tz, ())),
                dry_runner=dry_runner,
                require_dry_run=True,
                allowed_roots=authorized or [project_root],
            )
            preflight_reports[item.tz] = report.to_dict()
            if report.action != "register":
                errors[item.spec_path] = (
                    "preflight " + report.action + ": " +
                    ", ".join(o.cause_code for o in report.obstacles)
                )
                parsed.pop(key)
            else:
                owners.append(item)

    _checked_acceptance_updates(store, project, project_root, parsed, errors, dry_runner=dry_runner)
    inserted = updated = reactivated = unchanged = 0
    if strict_preflight and not parsed:
        # A rejected batch must not even register/update the project row.
        return {
            "requested": len(requested), "parsed": 0, "inserted": 0,
            "updated": 0, "reactivated": 0, "unchanged": 0,
            "errors": errors, "warnings": warnings, "preflight": preflight_reports,
        }
    with store.tx() as tx:
        if owner_snapshot is not None:
            # Fail closed on a race between the probe and registry mutation.
            now = _ownership_snapshot(tx)
            if now != owner_snapshot:
                errors["preflight"] = "registry_changed_during_preflight"
                return {
                    "requested": len(requested), "parsed": 0, "inserted": 0,
                    "updated": 0, "reactivated": 0, "unchanged": 0,
                    "errors": errors, "warnings": warnings, "preflight": preflight_reports,
                }
        require_available(tx, project, str(project_root))
        project_row = tx.one("SELECT root,active FROM projects WHERE name=?", (project,))
        if project_row is None:
            tx.change(
                "INSERT INTO projects(name,root,priority,active) VALUES(?,?,5,1)",
                (project, str(project_root)),
                event={"kind": "run", "project": project, "tool": "sync_selected",
                       "payload": json.dumps({"action": "project_insert"}, ensure_ascii=False)},
            )
        elif str(project_row["root"]) != str(project_root) or int(project_row["active"]) != 1:
            tx.change(
                "UPDATE projects SET root=?,active=1 WHERE name=?",
                (str(project_root), project),
                event={"kind": "run", "project": project, "tool": "sync_selected",
                       "payload": json.dumps({"action": "project_update"}, ensure_ascii=False)},
            )

        for item in parsed.values():
            row = tx.one(
                "SELECT state,spec_sha,spec_path,body,acceptance FROM tasks WHERE project=? AND tz=?",
                (project, item.tz),
            )
            if new_only and row is not None:
                unchanged += 1
                continue
            if (row is None and item.tz.casefold() in registered) or (
                row is not None and (
                    item.tz.casefold() not in registered
                    or str(row["acceptance"]) != registered[item.tz.casefold()]
                )
            ):
                errors[item.spec_path] = "registered_acceptance_changed_during_sync"
                continue
            values = (
                item.title,
                item.priority,
                item.kind,
                item.spec_path,
                item.spec_sha,
                json.dumps(list(item.needs), ensure_ascii=False),
                json.dumps(list(item.owns), ensure_ascii=False),
                json.dumps(list(item.resources), ensure_ascii=False),
                registered.get(item.tz.casefold(), json.dumps(list(item.acceptance), ensure_ascii=False)),
                item.body,
            )
            if row is None:
                tx.change(
                    "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                    "needs,owns,resources,acceptance,body,created_at,updated_at) "
                    "VALUES(?,?,?,'ready',?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                    (project, item.tz, *values),
                    event=_event(project, item.tz, "selected_insert"),
                )
                inserted += 1
                continue

            state = str(row["state"])
            new_state = "ready" if reactivate and state == "frozen" else state
            if (
                row["spec_sha"] == item.spec_sha
                and str(row["spec_path"]) == item.spec_path
                and str(row["body"] or "") == item.body
                and new_state == state
            ):
                unchanged += 1
                continue

            tx.change(
                "UPDATE tasks SET title=?,state=?,priority=?,kind=?,spec_path=?,spec_sha=?,"
                "needs=?,owns=?,resources=?,acceptance=?,body=?,updated_at=datetime('now') "
                "WHERE project=? AND tz=?",
                (
                    item.title,
                    new_state,
                    item.priority,
                    item.kind,
                    item.spec_path,
                    item.spec_sha,
                    json.dumps(list(item.needs), ensure_ascii=False),
                    json.dumps(list(item.owns), ensure_ascii=False),
                    json.dumps(list(item.resources), ensure_ascii=False),
                    json.dumps(list(item.acceptance), ensure_ascii=False),
                    item.body,
                    project,
                    item.tz,
                ),
                event=_event(
                    project,
                    item.tz,
                    "selected_reactivate" if new_state != state else "selected_update",
                ),
            )
            if new_state != state:
                reactivated += 1
            else:
                updated += 1

    return {
        "requested": len(requested),
        "parsed": len(parsed),
        "inserted": inserted,
        "updated": updated,
        "reactivated": reactivated,
        "unchanged": unchanged,
        "errors": errors,
        "warnings": warnings,
        "preflight": preflight_reports,
    }
