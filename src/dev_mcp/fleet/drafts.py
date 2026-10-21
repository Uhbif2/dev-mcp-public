"""Promote validated ToDo/tz-drafts/TZ-*.md into the fleet queue.

Called by the controller with its trusted Store, Policy and isolated preflight
runner. No agent, Claude Code or Codex participates in the registration path.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import goals
from .model import norm_path
from .spec import SpecError, parse_spec, spec_sha, sync_selected, validate_spec

_ID = re.compile(r"^(TZ-[A-Za-z0-9]+)(?:-|$)", re.I)
_OPEN_FIX = ("ready", "frozen")


class _DraftDependencyPending(Exception):
    """A valid draft must wait for another task to release its files."""

    def __init__(self, dependencies: tuple[str, ...]) -> None:
        super().__init__("pending owner dependencies")
        self.dependencies = dependencies


class _DraftRetryPending(Exception):
    """A concurrent registry transition requires a new probe, not a planner fix."""


def _link_free(root: Path, path: Path) -> bool:
    """Check the entire in-project path before EVERY read or move.

    resolve() by itself is unsafe: it follows a symlink before the caller
    can reject it, including symlinks in the draft/target directories.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    if current.is_symlink():
        return False
    for name in relative.parts:
        current /= name
        if current.is_symlink():
            return False
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _safe_draft(root: Path, path: Path) -> bool:
    return _link_free(root, path) and path.is_file()


def _identifier(path: Path) -> str:
    found = _ID.match(path.stem)
    return found.group(1) if found else path.stem


def _registered_ids(store: Any, project: str, root: Path) -> set[str]:
    """A TZ number is taken by a file or registry row, including past work."""
    result: set[str] = set()
    with store.read() as tx:
        result.update(str(r["tz"]).casefold() for r in tx.query(
            "SELECT tz FROM tasks WHERE project=?", (project,)
        ) if str(r["tz"]).casefold().startswith("tz-"))
    for directory in (root / "ToDo" / "tz", root / "ToDo" / "fleet"):
        for path in directory.glob("TZ-*.md") if _link_free(root, directory) and directory.is_dir() else ():
            if not _safe_draft(root, path):
                continue
            result.add(_identifier(path).casefold())
            try:
                result.add(parse_spec(str(path.resolve())).tz.casefold())
            except (OSError, SpecError, ValueError):
                pass
    return result


def _record_invalid(store: Any, project: str, root: Path, path: Path, reason: str) -> bool:
    """One obstacle per (draft contents + cause), and one repair work per path."""
    rel = path.relative_to(root).as_posix()
    # Invalid drafts may themselves be symlinks. Never fingerprint bytes from
    # outside the registered project, including through a symlink directory.
    data = path.read_bytes() if _safe_draft(root, path) else rel.encode("utf-8")
    fingerprint = hashlib.sha256(data + b"\\0" + reason.encode("utf-8")).hexdigest()
    tz = _identifier(path)
    fix_id = "fix:draft:" + hashlib.sha256(rel.casefold().encode()).hexdigest()[:16]
    with store.tx() as tx:
        prior = tx.query(
            "SELECT payload FROM events WHERE kind='draft_invalid' AND project=? AND tz=?",
            (project, tz),
        )
        if any(json.loads(row["payload"]).get("fingerprint") == fingerprint for row in prior):
            return False
        tx.record_event(kind="draft_invalid", project=project, tz=tz, tool="drafts",
                        payload={"draft": rel, "reason": reason, "fingerprint": fingerprint})
        body = (
            f"[fleet-draft-fix] draft={rel} fingerprint={fingerprint}\\n"
            f"Исправить черновик планировщика: {reason}\\n"
            "После исправления контроллер сам проверит и поставит ТЗ в очередь."
        )
        old = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, fix_id))
        if old is None:
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES(?,?,?,'ready',1,'fix',?,?,?,?,'[]','[]','planner',?,"
                "datetime('now'),datetime('now'))",
                (project, fix_id, "Исправить черновик " + path.name, rel, fingerprint,
                 "[]", json.dumps([norm_path(str(path))], ensure_ascii=False), body),
                event={"kind": "draft_fix_open", "project": project, "tz": fix_id,
                       "payload": {"draft": rel, "fingerprint": fingerprint}},
            )
        elif old["state"] in _OPEN_FIX:
            tx.change(
                "UPDATE tasks SET state='ready', spec_sha=?,body=?,updated_at=datetime('now') "
                "WHERE project=? AND tz=?",
                (fingerprint, body, project, fix_id),
                event={"kind": "draft_fix_refresh", "project": project, "tz": fix_id,
                       "payload": {"draft": rel, "fingerprint": fingerprint}},
            )
    return True


def _close_fix(store: Any, project: str, root: Path, path: Path) -> None:
    rel = path.relative_to(root).as_posix()
    fix_id = "fix:draft:" + hashlib.sha256(rel.casefold().encode()).hexdigest()[:16]
    with store.tx() as tx:
        row = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, fix_id))
        if row is not None and row["state"] in _OPEN_FIX:
            tx.change(
                "UPDATE tasks SET state='accepted',updated_at=datetime('now') WHERE project=? AND tz=?",
                (project, fix_id),
                event={"kind": "draft_fix_resolved", "project": project, "tz": fix_id,
                       "payload": {"draft": rel}},
            )


def _git_commit(root: Path, old: Path, new: Path, *, runner: Callable[..., Any]) -> str:
    """Commit only this rename; unrelated staged modifications are excluded."""
    before = old.relative_to(root).as_posix()
    after = new.relative_to(root).as_posix()
    # --only refuses an untracked destination until git knows the path.
    intent = runner(["git", "add", "-N", "--", after], cwd=str(root),
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=10)
    if intent.returncode:
        raise RuntimeError("git add -N: " + intent.stderr.strip()[-350:])
    # Planner drafts may be new, untracked files. Only name the old source
    # in the commit when it was tracked (otherwise Git rejects the pathspec).
    tracked = runner(["git", "ls-files", "--error-unmatch", "--", before],
                     cwd=str(root), capture_output=True, text=True,
                     encoding="utf-8", errors="replace", timeout=10).returncode == 0
    paths = [before, after] if tracked else [after]
    args = ["git", "-c", "core.quotepath=false", "commit", "--only", "-m",
            "fleet: register draft " + new.stem, "--", *paths]
    result = runner(args, cwd=str(root), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30)
    if result.returncode:
        raise RuntimeError("git commit: " + (result.stderr or result.stdout).strip()[-350:])
    head = runner(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True,
                  text=True, encoding="utf-8", errors="replace", timeout=10)
    if head.returncode:
        raise RuntimeError("git rev-parse: " + head.stderr.strip()[-200:])
    return head.stdout.strip()


def register_drafts(
    store: Any,
    policy: Any,
    *,
    dry_runner: Callable[[tuple[str, ...], Path], int] | None = None,
    project_info: Callable[[str], Mapping[str, Any]] = goals.project_info,
    git_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run one idempotent maintenance tick over every active registered project.

    A dry_runner MUST be a trusted, isolated fixture probe. Absent runner means
    fail-closed strict preflight, exactly as sync_selected(strict_preflight=True).
    """
    result: dict[str, Any] = {
        "registered": [], "invalid": {}, "waiting": {}, "commit_errors": {}, "deferred": {},
    }
    with store.read() as tx:
        projects = [(str(r["name"]), Path(str(r["root"])).resolve())
                    for r in tx.query("SELECT name,root FROM projects WHERE active=1")]
    for project, root in projects:
        drafts = root / "ToDo" / "tz-drafts"
        if not drafts.is_dir():
            continue
        if not _link_free(root, drafts):
            result["invalid"][f"{project}/ToDo/tz-drafts"] = "unsafe_symlink_directory"
            continue
        paths = sorted(drafts.glob("TZ-*.md"))
        # A draft may exist on disk but fail parsing/validation. Dependants
        # must wait for its repair, not report that its TZ is missing.
        # Unsafe symlinks never count as a present draft dependency.
        present_drafts = {
            _identifier(path).casefold(): _identifier(path)
            for path in paths if _safe_draft(root, path) and _ID.match(path.stem)
        }
        occupied = _registered_ids(store, project, root)
        parsed: dict[str, tuple[Path, Any]] = {}
        errors: dict[Path, str] = {}
        for path in paths:
            try:
                if not _safe_draft(root, path):
                    raise SpecError("unsafe_symlink_draft")
                item = parse_spec(str(path.resolve()))
                key = item.tz.casefold()
                if not _ID.match(path.stem) or _identifier(path).casefold() != key:
                    raise SpecError("номер в YAML не совпадает с номером файла")
                if key in occupied:
                    raise SpecError(f"номер {item.tz} уже занят")
                if key in parsed:
                    errors[path] = f"повтор номера {item.tz} в черновиках"
                    errors[parsed[key][0]] = f"повтор номера {item.tz} в черновиках"
                else:
                    parsed[key] = (path, dataclasses.replace(item, project=project))
            except (OSError, SpecError, ValueError) as exc:
                errors[path] = str(exc)
        # Existing specs (not directory names) are the dependency authority.
        known: set[str] = set()
        for folder in (root / "ToDo" / "tz", root / "ToDo" / "fleet"):
            for spec_path in folder.glob("TZ-*.md") if _link_free(root, folder) and folder.is_dir() else ():
                if not _safe_draft(root, spec_path):
                    continue
                try:
                    known.add(parse_spec(str(spec_path.resolve())).tz.casefold())
                except (OSError, SpecError, ValueError):
                    pass
        remaining = {key for key, (p, _) in parsed.items() if p not in errors}
        while remaining:
            progress = False
            for key in sorted(remaining):
                path, item = parsed[key]
                deps = {str(dep).split("#", 1)[0].casefold() for dep in item.needs}
                missing = deps - known - present_drafts.keys()
                if missing:
                    errors[path] = "needs: нет ТЗ " + ", ".join(sorted(missing))
                elif deps & remaining:
                    continue
                elif blocked := (deps & present_drafts.keys()) - known:
                    # Parsed-but-invalid or otherwise unregistered upstream
                    # drafts remain on disk. Their downstreams are pending,
                    # not malformed; a later tick retries registration.
                    result["waiting"][f"{project}/{item.tz}"] = [
                        present_drafts[dep] for dep in sorted(blocked)
                    ]
                    _close_fix(store, project, root, path)
                else:
                    target = root / "ToDo" / "tz" / path.name
                    issues = [issue for issue in validate_spec(item, policy)
                              if not issue.startswith("warning:")]
                    if issues:
                        errors[path] = "; ".join(issues)
                    elif not _link_free(root, target):
                        errors[path] = "unsafe_symlink_target"
                    elif target.exists() or target.is_symlink():
                        errors[path] = "целевой файл уже существует"
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            # Recheck after validation and mkdir to narrow the
                            # race with replacement of either path component.
                            if not _safe_draft(root, path) or not _link_free(root, target):
                                raise ValueError("draft_path_changed_before_move")
                            digest = hashlib.sha256(path.read_bytes()).digest()
                            path.rename(target)
                            # A raced-in source symlink must never reach
                            # sync_selected(), which resolves and opens specs.
                            if (not _safe_draft(root, target)
                                    or hashlib.sha256(target.read_bytes()).digest() != digest):
                                raise ValueError("draft_path_changed_during_move")
                            outcome = sync_selected(
                                store, project, str(root), [item.tz],
                                policy=policy, strict_preflight=True, dry_runner=dry_runner,
                            )
                            if outcome["inserted"] != 1 or outcome["errors"]:
                                if outcome['errors'] == {'preflight': 'registry_changed_during_preflight'}:
                                    raise _DraftRetryPending('registry_changed_during_preflight')
                                reports = list(outcome["preflight"].values())
                                # Ownership claimed by an unfinished task is
                                # a wait, not a malformed planner draft. Never
                                # remove the shared path from owns to appease
                                # preflight, and never emit draft_invalid here.
                                if (outcome["inserted"] == 0
                                        and len(reports) == 1
                                        and reports[0]["action"] == "register_with_dependencies"
                                        and len(outcome["errors"]) == 1
                                        and target.relative_to(root).as_posix() in outcome["errors"]
                                        and reports[0]["dependencies"]):
                                    raise _DraftDependencyPending(
                                        tuple(reports[0]["dependencies"])
                                    )
                                codes = [
                                    obstacle["predicate"].get("condition", obstacle["cause_code"])
                                    for report in outcome["preflight"].values()
                                    for obstacle in report["obstacles"]
                                ]
                                reason = "; ".join([*outcome["errors"].values(), *codes])
                                raise ValueError(reason or "строгий preflight отклонил ТЗ")
                        except _DraftRetryPending as pending:
                            if target.is_file() and not path.exists():
                                target.rename(path)
                            result['deferred'][f'{project}/{item.tz}'] = str(pending)
                            _close_fix(store, project, root, path)
                        except _DraftDependencyPending as pending:
                            if target.is_file() and not path.exists():
                                target.rename(path)
                            result["waiting"][f"{project}/{item.tz}"] = list(pending.dependencies)
                            _close_fix(store, project, root, path)
                        except Exception as exc:
                            if target.is_file() and not path.exists():
                                target.rename(path)
                            errors[path] = str(exc)
                        else:
                            _close_fix(store, project, root, path)
                            commit = ""
                            if project_info(project).get("commit") is True:
                                try:
                                    commit = _git_commit(root, path, target, runner=git_runner)
                                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                                    result["commit_errors"][f"{project}/{item.tz}"] = str(exc)
                            with store.tx() as tx:
                                tx.record_event(
                                    kind="draft_registered", tool="drafts",
                                    project=project, tz=item.tz,
                                    payload={"from": path.relative_to(root).as_posix(),
                                             "to": target.relative_to(root).as_posix(),
                                             "commit": commit,
                                             "commit_error": result["commit_errors"].get(
                                                 f"{project}/{item.tz}", "")},
                                )
                            result["registered"].append(f"{project}/{item.tz}")
                            known.add(key)
                remaining.remove(key)
                progress = True
            if not progress:
                for key in remaining:
                    path, _ = parsed[key]
                    errors[path] = "needs: цикл зависимостей черновиков"
                break
        for path, reason in errors.items():
            result["invalid"][f"{project}/{path.name}"] = reason
            _record_invalid(store, project, root, path, reason)
    return result


def register_owner_approved_legacy(
    store: Any,
    policy: Any,
    *,
    approval_event_id: int,
    dry_runner: Callable[[tuple[str, ...], Path], int] | None = None,
) -> dict[str, str]:
    """Explicit, non-scanning import of ONE old ToDo/tz specification.

    No caller-supplied TZ, file name or "approved=True" is authoritative.
    A trusted owner action must first write a durable
    `owner_legacy_import_approved` event with tool=fleet_owner, no worker_id,
    and payload {project,tz,path,sha256,provenance:'active_backlog',
    action:'approve'}. This API does NOT create such an approval. Unknown
    provenance, archived files, replayed approvals for changed bytes,
    symlinks, registry conflicts and missing strict preflight all fail closed.

    The owner CLI/controller integration that issues this receipt belongs to
    a separate task. This function is never invoked by register_drafts/tick.
    """
    def deny(reason: str) -> dict[str, str]:
        return {"status": "blocked", "reason": reason}

    if not isinstance(approval_event_id, int) or approval_event_id <= 0:
        return deny("invalid_approval_event_id")
    with store.read() as tx:
        receipt = tx.one(
            "SELECT id,project,tz,kind,tool,worker_id,payload FROM events WHERE id=?",
            (approval_event_id,),
        )
        if (receipt is None or receipt["kind"] != "owner_legacy_import_approved"
                or receipt["tool"] != "fleet_owner" or receipt["worker_id"] is not None):
            return deny("missing_trusted_owner_approval")
        try:
            payload = json.loads(receipt["payload"])
        except (TypeError, ValueError):
            return deny("invalid_approval_payload")
        if not isinstance(payload, dict):
            return deny("invalid_approval_payload")
        project, tz = payload.get("project"), payload.get("tz")
        name, digest = payload.get("path"), payload.get("sha256")
        if (not isinstance(project, str) or not isinstance(tz, str)
                or not isinstance(name, str) or not isinstance(digest, str)
                or not project or not re.fullmatch(r"TZ-[A-Za-z0-9]+", tz, re.I)
                or payload.get("action") != "approve"
                or payload.get("provenance") != "active_backlog"
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or receipt["project"] != project or receipt["tz"] != tz):
            return deny("ambiguous_or_unapproved_origin")
        project_row = tx.one(
            "SELECT root,active FROM projects WHERE name=?", (project,),
        )
        if project_row is None or int(project_row["active"]) != 1:
            return deny("unknown_or_inactive_project")
        root = Path(str(project_row["root"])).resolve()
        # The path must be a *single* leaf of the canonical active backlog.
        # Slash normalization never grants path traversal or alternate roots.
        leaf = f"{tz}-"  # allow TZ-12-work.md, not TZ-12-old/subfile
        prefix = "ToDo/tz/"
        if (not name.startswith(prefix) or "\\" in name
                or name.count("/") != 2
                or not name.endswith(".md")
                or not (name[len(prefix):].casefold() == f"{tz}.md".casefold()
                        or name[len(prefix):].casefold().startswith(leaf.casefold()))):
            return deny("noncanonical_or_archived_path")
        path = root / name
        if not _safe_draft(root, path):
            return deny("missing_or_unsafe_spec")
        if _identifier(path).casefold() != tz.casefold():
            return deny("filename_tz_mismatch")
        previous = tx.one(
            "SELECT state,spec_path,spec_sha FROM tasks WHERE project=? AND tz=?",
            (project, tz),
        )
        audited = tx.one(
            "SELECT id,payload FROM events WHERE kind='owner_legacy_import_registered' "
            "AND project=? AND tz=? ORDER BY id DESC LIMIT 1", (project, tz),
        )
    # Preserve *whole-file* SHA, not the header-only spec_sha used by tasks.
    # A changed body is a changed owner authorization even if YAML is identical.
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        return deny("approved_sha_mismatch")
    if previous is not None:
        if audited is not None:
            try:
                record = json.loads(audited["payload"])
            except (TypeError, ValueError):
                record = {}
            if (isinstance(record, dict)
                    and record.get("approval_event_id") == approval_event_id
                    and record.get("sha256") == digest
                    and previous["spec_path"] == name
                    and previous["spec_sha"] == spec_sha(str(path))):
                return {"status": "unchanged", "reason": "already_imported_same_approval"}
        return deny("tz_already_registered")

    if dry_runner is None:
        return deny("missing_trusted_strict_preflight")
    # sync_selected owns the existing strict preflight, acceptance-launch
    # check, holds/owns conflict check, and registry-snapshot CAS. Never use
    # its reactivate=True override for legacy imports.
    response = sync_selected(
        store, project, str(root), [tz], policy=policy, reactivate=False,
        strict_preflight=True, dry_runner=dry_runner,
    )
    if response["inserted"] != 1 or response["errors"]:
        return deny("strict_preflight_or_registry_rejected")
    if not _safe_draft(root, path) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        # Do not audit success when the approved file changed mid-flight.
        # Deployment of this API requires a cooperating file lock/transaction
        # at the source writer boundary; this alone does not resolve a race.
        return deny("spec_changed_during_import_review_required")
    with store.tx() as tx:
        row = tx.one("SELECT spec_path FROM tasks WHERE project=? AND tz=?",
                     (project, tz))
        if row is None or row["spec_path"] != name:
            return deny("registry_changed_during_import")
        tx.record_event(
            kind="owner_legacy_import_registered", project=project, tz=tz,
            tool="drafts", payload={"approval_event_id": approval_event_id,
                                    "path": name, "sha256": digest},
        )
    return {"status": "registered", "reason": "owner_approval_and_preflight_verified"}

