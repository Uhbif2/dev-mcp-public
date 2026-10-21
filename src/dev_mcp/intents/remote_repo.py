"""Fail-closed, best-effort GitHub delivery of server-proven intent projects.

This is called by the trusted stage prover, never by a task or a test project.
The fleet journal is the durable throttle; a network error must not reopen an
already proven stage. Neither credentials nor detected secret contents are logged.
"""
from __future__ import annotations

from dev_mcp import machine

import fnmatch
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


LOG = logging.getLogger(__name__)
OWNER = machine.GITHUB_OWNER  # machine.toml: github_owner; empty = remote publishing off
POLICY_PATH = machine.HOME / 'policy.toml'
REMOTE = "GitHub"
THROTTLE = timedelta(minutes=10)
_LIVE_DB = machine.FLEET_DB
_PARENTS = machine.WORKSPACES
_BASE_DENY = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "*secret*", "*token*", "*credential*",
    "*password*", "keys", "keys.*", "*.keys", "mcp-keys", "gateway.env",
    ".ssh", ".claude", ".codex", ".mcp.json", ".mcp.json.*",
)
_CONTENT = (
    re.compile(rb"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(rb"(?:ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
)
_MAX_FILE = 8 * 1024 * 1024
_MAX_OBJECTS = 5000
_LOCK = threading.Lock()
_SCHEDULE_LOCK = threading.Lock()
_PUBLISHING = False


def retry_pending(store: Any) -> int:
    """Retry delivery only for active, server-proven intent projects."""
    if Path(store.path).resolve() != _LIVE_DB.resolve(): return 0
    with store.read() as tx:
        rows = tx.query("SELECT p.name,p.root FROM projects p WHERE p.active=1 AND EXISTS "
                        "(SELECT 1 FROM outcomes o JOIN evidence e ON e.outcome_id=o.id "
                        "WHERE o.project=p.name AND o.parent_id IS NOT NULL AND o.state='done' "
                        "AND e.producer='intent_stage_prover' AND e.verdict='pass') ORDER BY p.name")
    pushed = 0
    for row in rows:
        try:
            result = push_after_proven_stage(store,row['name'],Path(row['root']))
            pushed += result.get('status') == 'pushed'
        except Exception:
            LOG.exception('intent publication deferred for %s',row['name'])
    return pushed


def schedule_retry(store: Any) -> None:
    """Network delivery has its own bounded worker, outside the stage prover."""
    global _PUBLISHING
    if Path(store.path).resolve() != _LIVE_DB.resolve(): return
    with _SCHEDULE_LOCK:
        if _PUBLISHING: return
        _PUBLISHING = True
    def run():
        global _PUBLISHING
        try:
            retry_pending(store)
        except Exception:
            LOG.exception('intent publication retry failed')
        finally:
            with _SCHEDULE_LOCK: _PUBLISHING = False
    try:
        threading.Thread(target=run,name='intent-remote-delivery',daemon=True).start()
    except Exception:
        with _SCHEDULE_LOCK: _PUBLISHING = False
        raise


class UnsafeRepository(RuntimeError):
    """An unscannable object or an identified secret blocks all network writes."""


def _exec(args: list[str], root: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, cwd=root, capture_output=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                          check=False)


def _cmd(args: list[str], root: Path) -> bytes:
    result = _exec(args, root)
    if result.returncode != 0:
        # Never copy stderr: gh/git may include private repository URLs/tokens.
        raise RuntimeError(f"command failed: {args[0]} {args[1]} (rc={result.returncode})")
    return result.stdout


def _is_live_project(store: Any, project: str, root: Path) -> bool:
    """In particular, pytest Store.open(tmp_path) must never reach gh."""
    if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project, re.I)
            or not root.is_dir() or root.is_symlink()):
        return False
    try:
        actual = root.resolve(strict=True)
        if (Path(store.path).resolve() != _LIVE_DB.resolve()
                or root.name.casefold() != project.casefold()
                or not any(actual.parent == parent.resolve() for parent in _PARENTS)):
            return False
        manifest = root / "component.json"
        if manifest.is_symlink():
            return False
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data.get("kind") == "intent-outcome-tree" and data.get("name") == project
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _has_proof(store: Any, project: str) -> bool:
    with store.read() as tx:
        row = tx.one("""
            SELECT o.id FROM outcomes o JOIN evidence e ON e.outcome_id=o.id
            WHERE o.project=? AND o.parent_id IS NOT NULL AND o.state='done'
              AND e.producer='intent_stage_prover' AND e.verdict='pass'
            LIMIT 1""", (project,))
        return row is not None


def _patterns() -> tuple[str, ...]:
    policy = POLICY_PATH
    try:
        with policy.open("rb") as handle:
            configured = tomllib.load(handle)["deny_patterns"]
        if not isinstance(configured, list) or not all(isinstance(p, str) for p in configured):
            raise ValueError("invalid deny_patterns")
        return tuple(set(_BASE_DENY).union(configured))
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        # An unreadable policy may contain newly added masks: never guess.
        raise UnsafeRepository("security policy unavailable for secret scan") from exc


def _path_secret(parts: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(part.casefold(), pat.casefold())
               for part in parts for pat in patterns)


def _content_secret(data: bytes) -> bool:
    return any(expr.search(data) is not None for expr in _CONTENT)


def _scan_tree(root: Path, patterns: tuple[str, ...]) -> None:
    def unreadable(error: OSError) -> None:
        raise UnsafeRepository("unreadable directory during tree scan") from error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        current = Path(directory)
        for name in list(dirs):
            path = current / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise UnsafeRepository(f"linked directory during secret scan: {path.relative_to(root)}")
            if current == root and name == ".git":
                dirs.remove(name)  # Git metadata is checked as history instead.
                continue
            if _path_secret(path.relative_to(root).parts, patterns):
                raise UnsafeRepository(f"unsafe tree path: {path.relative_to(root)}")
        for name in files:
            file = current / name
            rel = file.relative_to(root)
            if file.is_symlink() or _path_secret(rel.parts, patterns):
                raise UnsafeRepository(f"unsafe tree path: {rel}")
            try:
                if file.stat().st_size > _MAX_FILE:
                    raise UnsafeRepository(f"file too large to scan: {rel}")
                if _content_secret(file.read_bytes()):
                    raise UnsafeRepository(f"secret content in tree: {rel}")
            except OSError as exc:
                raise UnsafeRepository(f"unreadable tree file: {rel}") from exc


def _scan_history(root: Path, patterns: tuple[str, ...]) -> None:
    objects = _cmd(["git", "rev-list", "--objects", "--all"], root).splitlines()
    if len(objects) > _MAX_OBJECTS:
        raise UnsafeRepository("git history too large to scan safely")
    seen: set[bytes] = set()
    for entry in objects:
        sha, _, raw_path = entry.partition(b" ")
        if not re.fullmatch(rb"[a-fA-F0-9]{40,64}", sha):
            raise UnsafeRepository("malformed git history object")
        if raw_path:
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if _path_secret(tuple(path.replace("\\", "/").split("/")), patterns):
                raise UnsafeRepository(f"unsafe git history path: {path}")
        if sha in seen:
            continue
        seen.add(sha)
        kind = _cmd(["git", "cat-file", "-t", sha.decode("ascii")], root).strip()
        if kind != b"blob":
            continue
        size = _cmd(["git", "cat-file", "-s", sha.decode("ascii")], root).strip()
        if int(size) > _MAX_FILE:
            raise UnsafeRepository("git history blob too large to scan")
        if _content_secret(_cmd(["git", "cat-file", "blob", sha.decode("ascii")], root)):
            raise UnsafeRepository("secret content in git history")


def _record(store: Any, kind: str, project: str, payload: dict[str, str]) -> None:
    with store.tx() as tx:
        tx.record_event(kind=kind, project=project, payload=payload)


def _last(store: Any, project: str, kind: str) -> Any:
    with store.read() as tx:
        return tx.one("SELECT ts,payload FROM events WHERE project=? AND kind=? "
                      "ORDER BY id DESC LIMIT 1", (project, kind))


def _obstacle_id(project: str) -> str:
    return "intent-remote-" + hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]


def _owner_action(store: Any, project: str, reason: str) -> None:
    """Make a durable actionable owner obstacle without changing stage proof."""
    from dev_mcp.fleet import outcome

    with store.read() as tx:
        proven = tx.one("SELECT id FROM outcomes WHERE project=? AND parent_id IS NOT NULL "
                        "AND state='done' ORDER BY closed_ts DESC LIMIT 1", (project,))
        existing = tx.one("SELECT id,state FROM obstacles WHERE id=?", (_obstacle_id(project),))
    if proven is None:
        return
    action = ("Перед GitHub push устранить риск: " + reason[:220] +
              ". Проверить и очистить дерево и всю Git-историю; затем повторить доставку.")
    if existing is not None:
        if existing["state"] == "resolved":
            with store.tx() as tx:
                tx.change("UPDATE obstacles SET state='open',owner_action=?,ts=? WHERE id=?",
                          (action, datetime.now(timezone.utc).isoformat(), _obstacle_id(project)),
                          event={"kind": "intent.remote_owner_reopened", "project": project})
        return
    outcome.open_obstacle(
        store, outcome_id=proven["id"], type="owner_action",
        cause_code="owner_private_data",
        predicate={"remote_repository": project, "safe_push": False},
        owner_action=action,
        obstacle_id=_obstacle_id(project),
    )


def _resolve_owner_action(store: Any, project: str) -> None:
    from dev_mcp.fleet import outcome

    with store.read() as tx:
        row = tx.one("SELECT state FROM obstacles WHERE id=?", (_obstacle_id(project),))
    if row is not None and row["state"] == "open":
        outcome.resolve_obstacle(store, _obstacle_id(project))


def _publish(root: Path, project: str) -> None:
    repo = f"{OWNER}/{project}"
    url = f"https://github.com/{repo}.git"
    remote = _exec(["git", "remote", "get-url", REMOTE], root)
    if remote.returncode == 0:
        existing = remote.stdout.decode("utf-8", errors="replace").strip()
        if existing not in (url, f"git@github.com:{repo}.git"):
            raise UnsafeRepository("GitHub remote points to another repository")
    else:
        # The view is authoritative about whether this repo already exists.
        viewed = _exec(["gh", "repo", "view", repo, "--json", "visibility", "--jq", ".visibility"], root)
        if viewed.returncode != 0:
            _cmd(["gh", "repo", "create", repo, "--private"], root)
    # A pre-existing remote must be private as well: never push to a public repo.
    visibility = _cmd(["gh", "repo", "view", repo, "--json", "visibility", "--jq", ".visibility"], root)
    if visibility.strip().upper() != b"PRIVATE":
        raise UnsafeRepository("GitHub repository is not confirmed private")
    if remote.returncode != 0:
        _cmd(["git", "remote", "add", REMOTE, url], root)
    _cmd(["git", "push", "-u", REMOTE, "HEAD"], root)


def push_after_proven_stage(store: Any, project: str, root: Path) -> dict[str, str]:
    """Push committed proof, at most once per ten minutes; never fail a stage.

    Return status: skipped, unchanged, throttled, pushed, blocked or retry.
    Failures are recorded without secret bytes; retry happens on a later prover tick.
    """
    if not OWNER:
        return {"status": "skipped"}
    root = Path(root)
    if not _is_live_project(store, project, root):
        return {"status": "skipped", "reason": "not a live intent project"}
    with _LOCK:
        try:
            if not _has_proof(store, project):
                return {"status": "skipped", "reason": "no proven stage"}
            head = _cmd(["git", "rev-parse", "HEAD"], root).decode("ascii").strip()
            last_push = _last(store, project, "intent.remote_pushed")
            if last_push and json.loads(last_push["payload"]).get("head") == head:
                return {"status": "unchanged"}
            last_attempt = _last(store, project, "intent.remote_attempt")
            now = datetime.now(timezone.utc)
            if last_attempt:
                since = now - datetime.fromisoformat(last_attempt["ts"])
                if since < THROTTLE:
                    return {"status": "throttled"}
            _record(store, "intent.remote_attempt", project, {"head": head})
            patterns = _patterns()
            _scan_tree(root, patterns)
            _scan_history(root, patterns)
            _publish(root, project)
            _record(store, "intent.remote_pushed", project, {"head": head})
            try:
                _resolve_owner_action(store, project)
            except Exception:
                LOG.exception("failed to resolve owner action for %s", project)
            return {"status": "pushed"}
        except UnsafeRepository as exc:
            # One durable owner-facing action, not merely a swallowed log line.
            reason = str(exc)[:300]
            LOG.warning("intent remote blocked for %s: %s", project, reason)
            try:
                _record(store, "intent.remote_owner_action", project,
                        {"reason": reason, "action": "Resolve unsafe repository and scan entire Git history before push"})
                _owner_action(store, project, reason)
            except Exception:
                LOG.exception("failed to record remote owner action")
            return {"status": "blocked", "reason": reason}
        except Exception as exc:
            LOG.warning("intent remote retry for %s: %s", project, type(exc).__name__)
            try:
                _record(store, "intent.remote_retry", project, {"error": type(exc).__name__})
            except Exception:
                LOG.exception("failed to record remote retry")
            return {"status": "retry", "reason": type(exc).__name__}
