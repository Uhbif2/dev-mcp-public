"""MCP facade for fleet coordination.

This module owns the model-facing fleet surface.  Neighbouring fleet modules keep
their own contracts; imports are deliberately lazy so the shared dev-mcp server
continues to start while fleet zones are being deployed independently.
"""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import anyio

from .model import Decision, Task, norm_path, paths_overlap
from .jev import OWNER_GATE_MIN, owner_gate_probability
from .park import Parking
from . import bite, checkpoint, probe, wake, world

NUDGE_EVERY_S = 600  # руль «поставь контрольную точку» — не чаще раза в 10 минут
ADVISORY_EVERY_S = 30


ResponderOK = Callable[[str, dict[str, Any], str], str]
ResponderFail = Callable[[str, dict[str, Any], Exception], str]


@dataclass
class FleetContext:
    store: Any
    policy: Any | None = None
    ok: ResponderOK | None = None
    fail: ResponderFail | None = None
    modules: dict[str, Any] = field(default_factory=dict)
    owner: Any | None = None  # fleet/owner.py OwnerSessions; None — роль владельца выключена
    nudged: dict[str, float] = field(default_factory=dict)
    sealed_read_views: dict[str, tuple[str, tempfile.TemporaryDirectory, Any]] = field(
        default_factory=dict,
    )
    sealed_read_lock: Any = field(default_factory=threading.Lock)
    sealed_pruned_at: float = 0.0
    advisory_slots: Any = field(default_factory=lambda: threading.BoundedSemaphore(2))
    advisory_lock: Any = field(default_factory=threading.Lock)
    advisory_at: dict[str, float] = field(default_factory=dict)

    def is_owner(self, agent: str) -> bool:
        if self.owner is None:
            return False
        try:
            return bool(self.owner.is_owner(agent))
        except Exception:
            return False

    def module(self, name: str) -> Any | None:
        if name in self.modules:
            return self.modules[name]
        full_name = f"dev_mcp.fleet.{name}"
        try:
            module = importlib.import_module(full_name)
        except ModuleNotFoundError as exc:
            if exc.name == full_name:
                return None
            raise
        self.modules[name] = module
        return module

    def touch(self, agent: str) -> None:
        worker_id = str(agent or "").strip()
        if not worker_id:
            return
        try:
            self.store.touch_worker(worker_id)
        except Exception:
            # CONTRACT §11: coordination must never stop ordinary work.
            return

    def touch_resolved(self, agent: str, decision: Decision) -> None:
        """Heartbeat the worker the guard inferred when the caller named none.

        The guard allows a write into a path held by exactly one active claim even
        without `agent`, and reports that claim's owner in `decision.worker_id`;
        the server already attributes the event to it. Liveness has to follow the
        same inference, otherwise `last_seen` stays frozen while the worker is
        demonstrably working, and fleet_list reports it as silent for days.
        """
        resolved = str(getattr(decision, "worker_id", "") or "").strip()
        if resolved and resolved != str(agent or "").strip():
            self.touch(resolved)

    def check_write(self, agent: str, abs_path: str) -> Decision:
        self.touch(agent)
        guard = self.module("guard")
        if guard is None:
            return Decision(True, "FLEET_DEGRADED", "fleet guard module is unavailable", agent or None, True)
        try:
            decision = guard.check_write(self.store, agent or None, abs_path)
        except Exception as exc:
            return Decision(True, "FLEET_DEGRADED", f"fleet guard unavailable: {exc}", agent or None, True)
        decision = self._confine_chat(agent, decision, abs_path)
        self.touch_resolved(agent, decision)
        if not decision.allow:
            # Отказ стража — в журнал флота: без этого «вне зоны» в report и metrics всегда
            # 0, а попытки видны только в аудите (22.09.2026: 0/511 при реальном нарушении).
            self.record_event(
                "deny", str(decision.worker_id or agent or ""), tool="guard", path=abs_path,
                payload={"code": decision.code},
            )
        return decision

    def _confine_chat(self, agent: str, decision: Decision, abs_path: str) -> Decision:
        """Свободный путь того же проекта автоматически присоединяется к активному claim.

        Это сохраняет главный инвариант — два воркера не пишут один путь одновременно — но
        не заставляет большую задачу дробиться на request:* только потому, что автор ТЗ не
        перечислил каждый соседний файл заранее. Review остаётся read-only; чужой проект или
        уже занятый путь по-прежнему требует отдельной координации.
        """
        if not decision.allow or decision.code != "FLEET_OUT_OF_CLAIM" or not agent:
            return decision
        try:
            path_norm = norm_path(abs_path)
            with self.store.tx() as tx:
                worker = tx.one("SELECT kind FROM workers WHERE id=?", (agent,))
                if worker is None or str(worker["kind"] or "") != "chatgpt":
                    return decision
                claim = tx.one(
                    "SELECT c.id,c.project,c.tz,t.kind,t.owns,p.root FROM claims c "
                    "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                    "JOIN projects p ON p.name=c.project "
                    "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
                    (agent,),
                )
                if claim is None:
                    return decision
                zone = [str(r["path_norm"]) for r in tx.query(
                    "SELECT path_norm FROM claim_paths WHERE claim_id=? ORDER BY path_norm", (claim["id"],)
                )]
                task = f"{claim['project']}/{claim['tz']}"
                if str(claim["kind"]) == "review":
                    return Decision(
                        False,
                        "FLEET_OUT_OF_CLAIM",
                        f"Путь не входит в зону review {task}. Во время ревью файлы не правят: "
                        "вердикт — fleet_review, замечания — в его note.",
                        agent,
                        False,
                    )

                root_norm = norm_path(str(claim["root"]))
                same_project = path_norm == root_norm or path_norm.startswith(root_norm.rstrip("/") + "/")
                if not same_project:
                    shown = ", ".join(zone[:6]) + (" …" if len(zone) > 6 else "")
                    return Decision(
                        False,
                        "FLEET_OUT_OF_CLAIM",
                        f"Путь вне проекта активной задачи {task}. Зона: {shown or '—'}. "
                        "Для другого проекта/чужой зоны создай dependency через request_note.",
                        agent,
                        False,
                    )

                # Guard уже видел путь свободным, но повторяем проверку внутри той же транзакции:
                # между snapshot и auto-expand другой воркер мог успеть взять пересекающуюся зону.
                foreign = tx.query(
                    "SELECT c.worker_id,c.project,c.tz,cp.path_norm FROM claims c "
                    "JOIN claim_paths cp ON cp.claim_id=c.id "
                    "WHERE c.state='active' AND c.id<>?",
                    (claim["id"],),
                )
                conflict = next(
                    (row for row in foreign if paths_overlap(path_norm, str(row["path_norm"]))),
                    None,
                )
                if conflict is not None:
                    return Decision(
                        False,
                        "FLEET_FOREIGN_PATH",
                        f"Путь уже занял {conflict['worker_id']}, "
                        f"{conflict['project']}/{conflict['tz']}; auto-expand отменён.",
                        agent,
                        False,
                    )

                owns = [str(x) for x in json.loads(str(claim["owns"] or "[]")) if str(x)]
                if not any(paths_overlap(path_norm, owned) for owned in owns):
                    owns.append(path_norm)
                now = datetime.now(timezone.utc).isoformat()
                tx.changes(
                    [
                        (
                            "INSERT INTO claim_paths(claim_id,path_norm) "
                            "SELECT ?,? WHERE NOT EXISTS("
                            "SELECT 1 FROM claim_paths WHERE claim_id=? AND path_norm=?)",
                            (claim["id"], path_norm, claim["id"], path_norm),
                        ),
                        (
                            "UPDATE tasks SET owns=?,updated_at=? WHERE project=? AND tz=?",
                            (json.dumps(owns, ensure_ascii=False), now, claim["project"], claim["tz"]),
                        ),
                    ],
                    event={
                        "kind": "claim_expand",
                        "worker_id": agent,
                        "project": str(claim["project"]),
                        "tz": str(claim["tz"]),
                        "path": path_norm,
                        "payload": {"automatic": True},
                    },
                )

            evidence = self.module("evidence")
            if evidence is not None and hasattr(evidence, "extend_capture"):
                evidence.extend_capture(self.store, int(claim["id"]), [path_norm])
            return Decision(
                True,
                "",
                f"Зона {task} автоматически расширена на свободный путь {path_norm}.",
                agent,
                False,
            )
        except Exception as exc:
            return Decision(
                False,
                "FLEET_OUT_OF_CLAIM",
                f"Не удалось безопасно расширить зону активной задачи: {type(exc).__name__}: {exc}",
                agent,
                False,
            )

    def check_exec(self, agent: str, cwd: str) -> Decision:
        self.touch(agent)
        guard = self.module("guard")
        if guard is None:
            return Decision(True, "FLEET_DEGRADED", "fleet guard module is unavailable", agent or None, True)
        try:
            decision = guard.check_exec(self.store, agent or None, cwd)
        except Exception as exc:
            return Decision(True, "FLEET_DEGRADED", f"fleet guard unavailable: {exc}", agent or None, True)
        self.touch_resolved(agent, decision)
        return decision

    def _prune_sealed_views(self, *, force: bool = False) -> None:
        if not force and time.monotonic()-self.sealed_pruned_at<30:
            return
        from .sealed_cache import sweep
        sweep(Path(self.store.path).resolve().parent/'sealed-reads')
        if not self.sealed_read_views:
            self.sealed_pruned_at=time.monotonic()
            return
        with self.store.read() as tx:
            live={str(r['worker_id']) for r in tx.query(
                "SELECT c.worker_id FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                "JOIN workers w ON w.id=c.worker_id "
                "WHERE c.state='active' AND t.kind='review' AND w.revoked_at IS NULL")}
        with self.sealed_read_lock:
            retired=[]
            for reviewer in list(self.sealed_read_views):
                if reviewer not in live:
                    retired.append(self.sealed_read_views.pop(reviewer)[1])
            held={id(v[1]) for v in self.sealed_read_views.values()}
            for scratch in {id(v):v for v in retired}.values():
                if id(scratch) not in held:
                    try: scratch.cleanup()
                    except OSError: pass  # a closing Windows reader may still hold its file
            self.sealed_pruned_at=time.monotonic()

    def route_claim_path(self, agent: str, path: str, *, read_only: bool = False) -> tuple[Any, Any, Any]:
        """Trusted source→checkout mapping, before Policy and the original guard.

        The guard still checks the source coordinate (the task's declared owns);
        the adapter separately proves that the actual file is inside this claim's
        canonical checkout. Missing isolated bindings must never use the trunk.
        """
        from pathlib import Path
        from . import goals
        from .claim_take_routing import ClaimTakeRouting, project_path_to_claim
        from .claim_context import ClaimRoutingError

        requested = Path(path)
        if ".." in requested.parts:
            raise ClaimRoutingError("claim path cannot contain parent traversal")
        source = requested.resolve(strict=False)
        self._prune_sealed_views()
        # Server-generated projection snapshots are implementation storage, not
        # user-addressable paths. A copied physical path cannot bypass the
        # per-review authorization or expose a foreign review's files.
        with self.sealed_read_lock:
            for _, _, private_root in self.sealed_read_views.values():
                if source == private_root or private_root in source.parents:
                    raise ClaimRoutingError("sealed review storage is not user-addressable")
        if read_only and agent:
            # Existing read_file/search_files/list_dir entrypoints call this
            # before Policy.resolve. A review must see precisely the immutable
            # submitted source used by its sealed acceptance, not its own stale
            # checkout. Non-review and non-owned reads retain normal routing.
            from .claim_review_bite import ClaimReviewError
            try:
                sealed = self._sealed_review_read_path(agent, source)
            except ClaimReviewError as exc:
                raise ClaimRoutingError(f"sealed review source unavailable: {exc}") from exc
            if sealed is not None:
                return sealed
        if not agent:
            # A legacy call without agent must not mutate an isolated project's
            # shared trunk merely because the fleet guard can infer a worker.
            with self.store.read() as tx:
                projects = list(tx.query("SELECT name,root FROM projects"))
            for project in projects:
                root = Path(str(project["root"])).resolve(strict=False)
                info = goals.project_info(str(project["name"]))
                trees = root.parent / ".fleet-trees" / root.name
                if info.get("claim_trees") is True and info.get("commit") is True and (
                    source == root or root in source.parents
                    or source == trees or trees in source.parents
                ):
                    raise ClaimRoutingError("isolated project requires explicit agent")
            return source, source, None
        with self.store.read() as tx:
            held = tx.one(
                "SELECT c.id,c.project FROM claims c WHERE c.worker_id=? AND c.state='active' "
                "ORDER BY c.id DESC LIMIT 1",
                (agent,),
            )
        if held is None:
            if not read_only:
                with self.store.read() as tx:
                    projects = tuple(tx.query("SELECT name,root FROM projects"))
                for item in projects:
                    settings = goals.project_info(str(item["name"]))
                    if settings.get("claim_trees") is not True or settings.get("commit") is not True:
                        continue
                    root = Path(str(item["root"])).resolve(strict=False)
                    trees = root.parent / ".fleet-trees" / root.name
                    if (source == root or root in source.parents or
                            source == trees or trees in source.parents):
                        raise ClaimRoutingError("isolated write requires active claim")
            return source, source, None
        project_settings = goals.project_info(str(held["project"]))
        with self.store.read() as tx:
            saved = tx.query(
                "SELECT payload FROM events WHERE kind='take' AND worker_id=? AND project=? "
                "ORDER BY id DESC", (agent, str(held["project"])),
            )
        durable_isolated = False
        for row in saved:
            try:
                value = json.loads(str(row["payload"] or "{}"))
            except ValueError:
                continue
            if value.get("claim_id") == int(held["id"]):
                durable_isolated = (value.get("claim_context") or {}).get("mode") == "isolated"
                break
        if not durable_isolated and (
            project_settings.get("claim_trees") is not True
            or project_settings.get("commit") is not True
        ):
            return source, source, None
        context = ClaimTakeRouting(self.store).resolve_claim_context(agent, int(held["id"]))
        if context.mode != "isolated":
            return source, source, None
        with self.store.read() as tx:
            paths = tuple(str(row["path_norm"]) for row in tx.query(
                "SELECT path_norm FROM claim_paths WHERE claim_id=?", (context.claim_id,),
            ))
        actual = project_path_to_claim(
            str(source), context, read_only=read_only,
            owns=paths if not read_only else None,
        )
        tree = context.tree_path
        if actual != tree and tree not in actual.parents:
            if read_only:
                return actual, actual, context
            raise ClaimRoutingError("write escaped the isolated claim")
        original = context.project_root / actual.relative_to(tree)
        return actual, original, context

    def check_claim_write(self, agent: str, source: str, context: Any | None) -> Decision:
        """The old cross-worker guard cannot compare identical source owns
        across *different isolated Git trees*. Enforce that guard's ownership
        predicate inside the verified active checkout instead.

        All non-isolated calls still pass through the original fleet guard.
        """
        if context is None or context.mode != "isolated":
            return self.check_write(agent, source)
        from .model import paths_overlap
        with self.store.read() as tx:
            claim = tx.one(
                "SELECT c.state,w.revoked_at FROM claims c JOIN workers w ON w.id=c.worker_id "
                "WHERE c.id=? AND c.worker_id=?", (context.claim_id, agent),
            )
            owned = tuple(str(row["path_norm"]) for row in tx.query(
                "SELECT path_norm FROM claim_paths WHERE claim_id=?", (context.claim_id,),
            ))
        if claim is None or claim["state"] != "active" or claim["revoked_at"] is not None:
            return Decision(False, "FLEET_REVOKED", "claim inactive or worker revoked", agent, False)
        if not any(paths_overlap(norm_path(source), own) for own in owned):
            return Decision(False, "FLEET_OUT_OF_CLAIM", "path outside isolated claim owns", agent, False)
        return Decision(True, "", f"verified isolated claim {context.claim_id}", agent, False)

    def check_claim_exec(self, agent: str, source: str, context: Any | None) -> Decision:
        if context is None or context.mode != "isolated":
            return self.check_exec(agent, source)
        if Path(source).resolve(strict=False) != context.project_root:
            # CWD may be an owned subdirectory, never outside the project.
            cwd = Path(source).resolve(strict=False)
            if context.project_root not in cwd.parents:
                return Decision(False, "FLEET_OUT_OF_CLAIM", "cwd outside claim project", agent, False)
        with self.store.read() as tx:
            claim = tx.one(
                "SELECT c.state,w.revoked_at FROM claims c JOIN workers w ON w.id=c.worker_id "
                "WHERE c.id=? AND c.worker_id=?", (context.claim_id, agent),
            )
        if claim is None or claim["state"] != "active" or claim["revoked_at"] is not None:
            return Decision(False, "FLEET_REVOKED", "claim inactive or worker revoked", agent, False)
        return Decision(True, "", f"verified isolated claim {context.claim_id}", agent, False)

    def _sealed_review_read_path(self, reviewer: str, requested: Any) -> tuple[Any, Any, Any] | None:
        """Read-only projection of the authenticated submitted Git revision.

        A reviewer never supplies a revision, author checkout or foreign tree.
        The server verifies active review -> source claim -> sealed submission
        (including pinned Git ref, tree SHA and unchanged author) on every read.
        Only paths in source owns are projected. An explicit project-root search
        sees only those files, never another worker's unowned private files.
        """
        from pathlib import Path
        from .claim_review_bite import _review_claim, _submission, ClaimReviewError
        from .claim_take_routing import ClaimTakeRouting
        from .model import norm_path

        trusted = self.sealed_review_source(reviewer)
        if trusted is None:
            return None
        claim_id, root = trusted
        requested = Path(requested).resolve(strict=False)
        if requested != root and root not in requested.parents:
            return None
        submission, _, _ = _submission(self.store, claim_id)
        _review_claim(self.store, submission, reviewer)
        own_roots = tuple(
            norm_path(str(p)[:-3] if str(p).endswith("/**") else str(p))
            for p in submission.owns
        )
        normalized = norm_path(str(requested))
        if requested != root and not any(
            normalized == own or normalized.startswith(own.rstrip("/") + "/")
            for own in own_roots
        ):
            # Unowned reads keep the existing review checkout path; they are
            # not silently presented as author evidence.
            return None
        key = f"{claim_id}:{submission.submission_id}:{submission.revision}"
        self._prune_sealed_views(force=True)
        with self.sealed_read_lock:
            existing = self.sealed_read_views.get(reviewer)
            shared=next((v for v in self.sealed_read_views.values() if v[0]==key),None)
            if shared is not None:
                self.sealed_read_views[reviewer]=shared
                existing=shared
            if existing is None or existing[0] != key:
                if existing is not None:
                    del self.sealed_read_views[reviewer]
                    if not any(v[1] is existing[1] for v in self.sealed_read_views.values()):
                        existing[1].cleanup()
                cache=Path(self.store.path).resolve().parent/'sealed-reads'
                cache.mkdir(parents=True,exist_ok=True)
                if cache.is_symlink() or cache.is_junction():
                    raise ClaimReviewError('sealed projection storage cannot be a link')
                scratch = tempfile.TemporaryDirectory(
                    prefix=f"view-{os.getpid()}-", dir=str(cache),
                )
                view_root = Path(scratch.name)
                try:
                    process = subprocess.Popen(
                        ["git", "-C", str(root), "archive", "--format=tar",
                         submission.revision],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    try:
                        if process.stdout is None:
                            raise ClaimReviewError("sealed archive has no stdout")
                        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                            total = count = 0
                            for member in archive:
                                rel = Path(member.name.replace("\\", "/"))
                                if (rel.is_absolute() or ".." in rel.parts
                                        or not rel.parts or ":" in member.name):
                                    raise ClaimReviewError("invalid sealed archive path")
                                original = (root / rel).resolve(strict=False)
                                original_norm = norm_path(str(original))
                                if not any(
                                    original_norm == own or original_norm.startswith(
                                        own.rstrip("/") + "/")
                                    for own in own_roots
                                ):
                                    continue
                                if member.isdir():
                                    continue
                                if not member.isfile():
                                    raise ClaimReviewError("sealed source contains non-file entry")
                                count += 1
                                total += member.size
                                if count > 20000 or total > 256 * 1024 * 1024:
                                    raise ClaimReviewError("sealed projection capacity exceeded")
                                destination = view_root.joinpath(*rel.parts)
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                blob = archive.extractfile(member)
                                if blob is None:
                                    raise ClaimReviewError("unreadable sealed archive member")
                                with blob, destination.open("xb") as out:
                                    shutil.copyfileobj(blob, out, 1024 * 1024)
                                os.chmod(destination, stat.S_IREAD)
                        stderr = process.stderr.read() if process.stderr else b""
                        if process.wait(timeout=60):
                            raise ClaimReviewError(
                                f"sealed archive unavailable: {stderr[:120]!r}"
                            )
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=10)
                except BaseException:
                    scratch.cleanup()
                    raise
                self.sealed_read_views[reviewer] = (key, scratch, view_root)
            else:
                view_root = existing[2]

        with self.store.read() as tx:
            held = tx.one(
                "SELECT id FROM claims WHERE worker_id=? AND state='active' "
                "ORDER BY id DESC LIMIT 1", (reviewer,),
            )
        if held is None:
            raise ClaimReviewError("review claim no longer active")
        context = ClaimTakeRouting(self.store).resolve_claim_context(
            reviewer, int(held["id"])
        )
        relative = requested.relative_to(root)
        projected = view_root / relative
        return projected, requested, context

    def sealed_review_source(self, reviewer: str) -> tuple[int, Path] | None:
        """Resolve author claim only from an active review and durable server evidence.

        None means this is not an isolated review; it never authorizes a trunk
        fallback for an isolated review with missing evidence.
        """
        from . import goals, review
        from .claim_review_bite import ClaimReviewError

        with self.store.read() as tx:
            active = tx.one(
                "SELECT c.id,t.project,t.tz,t.kind,t.body,p.root FROM claims c "
                "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                "JOIN projects p ON p.name=t.project "
                "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
                (reviewer,),
            )
            if active is None or active["kind"] != "review":
                return None
            project = str(active["project"])
            flags = goals.project_info(project)
            feature_on = (flags.get("claim_trees") is True
                          and flags.get("commit") is True)
            source_project, source_tz = review._review_target(
                str(active["tz"]), str(active["body"] or ""), project,
            )
            task = tx.one(
                "SELECT state,author_worker FROM tasks WHERE project=? AND tz=?",
                (source_project, source_tz),
            )
            if task is None or task["state"] != "submitted" or not task["author_worker"]:
                raise ClaimReviewError("trusted isolated review has no submitted source")
            author = str(task["author_worker"])
            source_claim = tx.one(
                "SELECT id FROM claims WHERE project=? AND tz=? AND worker_id=? "
                "ORDER BY id DESC LIMIT 1", (source_project, source_tz, author))
            matched_source = None
            submitted = tx.query(
                "SELECT payload FROM events WHERE kind='claim_submission' "
                "AND project=? AND tz=? ORDER BY id DESC",
                (source_project, source_tz),
            )
            for event in submitted:
                try:
                    item = json.loads(str(event["payload"]))
                except (ValueError, TypeError):
                    continue
                if (isinstance(item, dict) and item.get("author") == author
                        and type(item.get("claim_id")) is int):
                    claim_id = item["claim_id"]
                    held = tx.one(
                        "SELECT worker_id,project,tz FROM claims WHERE id=?",
                        (claim_id,),
                    )
                    if (held and held["worker_id"] == author
                            and held["project"] == source_project
                            and held["tz"] == source_tz):
                        matched_source = (claim_id, Path(str(active["root"])).resolve(strict=True))
                        break
        known_isolated = False
        if source_claim is not None:
            from .claim_review_bite import _claim
            _, source_context = _claim(self.store,int(source_claim['id']))
            if source_context.mode == 'shared':
                return None
            known_isolated = True
        if matched_source is not None:
            return matched_source
        if not feature_on and not known_isolated:
            return None
        raise ClaimReviewError("missing sealed author claim for isolated review")

    def trusted_reviewer_run(self, reviewer: str, root: str, cwd: str,
                             argv: list[str], *, timeout: float,
                             background: bool = False) -> Any | None:
        """Registered run tool's immutable review path; no generic cwd fallback."""
        source = self.sealed_review_source(reviewer)
        if source is None:
            return None
        from .claim_review_bite import ClaimReviewError, reviewer_run

        claim_id, trusted_root = source
        if (background or cwd or Path(root).resolve(strict=False) != trusted_root):
            raise ClaimReviewError("review run must use source project root, foreground only")
        result = reviewer_run(self.store, claim_id, reviewer, argv, timeout=timeout)
        # Legacy review.decide requires run evidence. Emit it ONLY after the
        # immutable reviewer_run has recorded its typed, independently verified
        # claim_review_run (and do not count nonzero as green).
        self.record_event(
            "run", reviewer, tool="run", path=str(trusted_root), rc=result.rc,
            payload={"argv": list(argv), "sealed_claim_id": claim_id,
                     "sealed_revision": result.submission_revision,
                     "sealed_tree": result.tree_sha},
        )
        return result

    def verify_accepted_claim(self, claim_id: int, revision: str) -> Any:
        """Expose TZ-42's sealed acceptance verifier, never an author note."""
        from .claim_review_bite import verify_accepted_claim
        return verify_accepted_claim(self.store, claim_id, revision)

    def record_event(self, kind: str, agent: str, *, tool: str = "", path: str = "", rc: int | None = None,
                     payload: dict[str, Any] | None = None, declared: bool | None = None) -> None:
        """Record a coordination event.

        `declared` separates "the client named its worker" from "the guard inferred
        the worker from path ownership". Both end up attributed to the same id, so
        without this flag the registry cannot tell protocol use from protocol bypass
        — which is the one number that says whether the fleet is actually adopted.
        """
        worker_id = str(agent or "").strip()
        if not worker_id:
            return
        try:
            path_norm = norm_path(path) if path else ""
            body = dict(payload or {})
            if declared is not None:
                body["declared"] = bool(declared)
            run_context = None
            if kind in {"write", "run"}:
                try:
                    from .claim_take_routing import ClaimTakeRouting, claim_command_environment
                    with self.store.read() as view:
                        held = view.one(
                            "SELECT id FROM claims WHERE worker_id=? AND state='active' "
                            "ORDER BY id DESC LIMIT 1", (worker_id,),
                        )
                    if held is not None:
                        context = ClaimTakeRouting(self.store).resolve_claim_context(
                            worker_id, int(held["id"]))
                        if context.mode == "isolated":
                            run_context = context
                            body["claim_id"] = context.claim_id
                            body["tree_revision"] = claim_command_environment(context, {}).tree_revision
                except Exception:
                    # Telemetry cannot create an alternative authorization path.
                    pass
            project = tz = None
            if kind in {"run", "write", "deny"}:
                # Без project/tz событие не связано с задачей: «приёмка красная
                # дважды» и сводки по задаче его просто не видели.
                subject = self._subject(worker_id)
                if subject is not None:
                    project, tz = subject["project"], subject["tz"]
                    if kind == "run" and subject["paths"]:
                        # Прогон доказывает конкретное состояние файлов зоны; accept
                        # сверяет этот хеш с текущим (см. fleet.evidence).
                        paths = subject["paths"]
                        if run_context is not None:
                            from .claim_take_routing import project_path_to_claim
                            paths = [
                                str(project_path_to_claim(p, run_context, read_only=True))
                                for p in paths
                            ]
                        tree = self._tree(paths)
                        if tree:
                            body["tree"] = tree
            with self.store.tx() as tx:
                tx.record_event(
                    kind=kind,
                    worker_id=worker_id,
                    project=project,
                    tz=tz,
                    tool=tool,
                    path_norm=path_norm,
                    rc=rc,
                    payload=body,
                )
        except Exception:
            # Event telemetry is coordination metadata, not permission to break
            # a successful read/write when the registry is degraded.
            return

    def is_acceptance_run(self, agent: str, argv: list[str]) -> bool:
        """argv — команда приёмки активной задачи агента (или гейт её проекта).

        Такой прогон не обрывается на умолчании `run` (300 с): гейт Beta App идёт ~880 с,
        и стык, принимаемый гейтом, в руках агента не мог стать зелёным никогда (07.10.2026).
        """
        worker_id = str(agent or "").strip()
        if not worker_id or not argv:
            return False
        try:
            from . import goals
            from .review import _norm_command

            with self.store.read() as tx:
                row = tx.one(
                    "SELECT t.project,t.acceptance FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                    "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
                    (worker_id,),
                )
            if row is None:
                return False
            commands = [str(x) for x in json.loads(str(row["acceptance"] or "[]"))]
            gate = str(goals.project_info(str(row["project"])).get("gate") or "").strip()
            wanted = {_norm_command(c) for c in [*commands, gate] if c}
            return _norm_command(" ".join(str(a) for a in argv)) in wanted
        except Exception:
            return False

    def _subject(self, worker_id: str) -> dict[str, Any] | None:
        evidence = self.module("evidence")
        if evidence is None:
            return None
        try:
            return evidence.subject(self.store, worker_id)
        except Exception:
            return None

    def _tree(self, paths: list[str]) -> str | None:
        evidence = self.module("evidence")
        if evidence is None:
            return None
        try:
            return evidence.tree_hash(paths)
        except Exception:
            return None

    def envelope(self, agent: str) -> str:
        worker_id = str(agent or "").strip()
        if not worker_id:
            return ""
        inbox = self.module("inbox")
        if inbox is None:
            return ""
        try:
            hazard = self.module("hazard")
            if hazard is not None:
                try:
                    self.schedule_hazard(hazard, worker_id)
                except Exception:
                    # Stale-read detection is advisory coordination metadata.
                    # It must not hide the actual tool response when degraded.
                    pass
            messages = inbox.pending_for(self.store, worker_id)
            text = inbox.render_envelope(messages, worker_id)
        except Exception:
            text = ""
        steer = self.steering(worker_id)
        return f"{steer}\n{text}" if steer and text else steer or text

    def schedule_hazard(self, hazard: Any, worker_id: str) -> None:
        """Bound advisory work independently of the response path and traffic."""
        import time
        now = time.monotonic()
        with self.advisory_lock:
            if now - self.advisory_at.get(worker_id, -1e9) < ADVISORY_EVERY_S:
                return
            if not self.advisory_slots.acquire(blocking=False):
                return
            self.advisory_at[worker_id] = now
        def run() -> None:
            try:
                hazard.stale_reads(self.store, worker_id)
            except Exception:
                pass  # next advisory cycle retries; actual tool result is safe
            finally:
                self.advisory_slots.release()
        threading.Thread(target=run, name="fleet-advisory", daemon=True).start()

    def steering(self, worker_id: str) -> str:
        """Руль: правила над состоянием захвата, вшитые в ответ любого инструмента (v2 §4)."""
        import time as _time

        now = _time.monotonic()
        if now - self.nudged.get(worker_id, -1e9) < NUDGE_EVERY_S:
            return ""
        try:
            text = checkpoint.due(self.store, worker_id)
        except Exception:
            return ""
        if text:
            self.nudged[worker_id] = now
        return text

    def with_envelope(self, agent: str, payload: str) -> str:
        envelope = self.envelope(agent)
        if not envelope:
            return payload
        return f"{envelope}\n--- dev-mcp · ответ инструмента ---\n{payload}"


def _respond_ok(ctx: FleetContext, tool: str, args: dict[str, Any], payload: str) -> str:
    return ctx.ok(tool, args, payload) if ctx.ok is not None else payload


def _respond_fail(ctx: FleetContext, tool: str, args: dict[str, Any], exc: Exception) -> str:
    if ctx.fail is not None:
        return ctx.fail(tool, args, exc)
    return f"Отказано: {exc}"


def _required(ctx: FleetContext, name: str) -> Any:
    module = ctx.module(name)
    if module is None:
        raise RuntimeError(f"fleet module {name!r} is not available yet")
    return module


def _next_worker_id(tx: Any) -> str:
    maximum = 0
    for row in tx.query("SELECT id FROM workers"):
        worker_id = str(row["id"])
        if worker_id.startswith("W") and worker_id[1:].isdigit():
            maximum = max(maximum, int(worker_id[1:]))
    return f"W{maximum + 1}"


_AUTOSTART_LABEL = re.compile(r"^f\d{4}-\d{4}[a-z0-9]{3}$", re.I)


def _worker_number(worker_id: str) -> int:
    value = str(worker_id or "")
    return int(value[1:]) if value.startswith("W") and value[1:].isdigit() else 0


def _dedupe_autostart_workers(ctx: FleetContext, only_label: str = "") -> list[str]:
    """Collapse accidental duplicate worker_ids created by repeated fleet_join in one browser tab.

    Autostart labels are generated per tab and therefore are session identity, not a display
    nickname.  A retry/reload must reuse that identity.  If old versions already created
    several workers for one label, keep the worker with the newest active claim (or newest
    heartbeat when none is active), requeue older simultaneous claims, and merge historical
    claims into the canonical worker so chat-budget/accounting stays truthful.
    """

    now = datetime.now(timezone.utc).isoformat()
    merged: list[str] = []
    with ctx.store.tx() as tx:
        workers = [dict(row) for row in tx.query(
            """SELECT id,label,kind,last_seen FROM workers
               WHERE revoked_at IS NULL AND kind='chatgpt'
               ORDER BY last_seen,id"""
        )]
        groups: dict[str, list[dict[str, Any]]] = {}
        for worker in workers:
            label = str(worker.get("label") or "")
            if not _AUTOSTART_LABEL.fullmatch(label):
                continue
            if only_label and label != only_label:
                continue
            groups.setdefault(label, []).append(worker)

        for label, group in groups.items():
            if len(group) < 2:
                continue
            ids = [str(worker["id"]) for worker in group]
            marks = ",".join("?" for _ in ids)
            active = [dict(row) for row in tx.query(
                f"""SELECT id,project,tz,worker_id,started_at FROM claims
                    WHERE state='active' AND worker_id IN ({marks})""",
                ids,
            )]
            newest_active: dict[str, dict[str, Any]] = {}
            for claim in active:
                wid = str(claim["worker_id"])
                current = newest_active.get(wid)
                if current is None or str(claim["started_at"]) > str(current["started_at"]):
                    newest_active[wid] = claim

            def rank(worker: dict[str, Any]) -> tuple[int, str, str, int]:
                wid = str(worker["id"])
                claim = newest_active.get(wid)
                return (
                    1 if claim else 0,
                    str(claim["started_at"]) if claim else "",
                    str(worker.get("last_seen") or ""),
                    _worker_number(wid),
                )

            canonical = max(group, key=rank)
            canonical_id = str(canonical["id"])
            duplicates = [str(worker["id"]) for worker in group if str(worker["id"]) != canonical_id]
            statements: list[tuple[str, tuple[Any, ...]]] = []

            for duplicate_id in duplicates:
                for claim in [row for row in active if str(row["worker_id"]) == duplicate_id]:
                    statements.extend([
                        (
                            "UPDATE claims SET state='requeued',released_at=? WHERE id=? AND state='active'",
                            (now, int(claim["id"])),
                        ),
                        (
                            "UPDATE tasks SET state='ready',updated_at=? "
                            "WHERE project=? AND tz=? AND state='taken'",
                            (now, str(claim["project"]), str(claim["tz"])),
                        ),
                    ])
                # Same browser session = same logical worker.  Merge released history too,
                # otherwise repeated fleet_join resets the per-chat task budget.
                statements.extend([
                    (
                        "UPDATE claims SET worker_id=? WHERE worker_id=?",
                        (canonical_id, duplicate_id),
                    ),
                    (
                        "UPDATE tasks SET author_worker=? WHERE author_worker=?",
                        (canonical_id, duplicate_id),
                    ),
                    (
                        "UPDATE messages SET to_worker=? WHERE to_worker=? AND acked_at IS NULL",
                        (canonical_id, duplicate_id),
                    ),
                    (
                        "UPDATE workers SET revoked_at=?,revoke_reason=? WHERE id=? AND revoked_at IS NULL",
                        (now, f"merged duplicate browser label {label} into {canonical_id}", duplicate_id),
                    ),
                ])

            # Maintenance identity cleanup must NOT heartbeat the surviving worker.
            # Otherwise a tab from yesterday suddenly looks active and occupies maxWorking.
            tx.changes(
                statements,
                event={
                    "kind": "worker_dedupe",
                    "worker_id": canonical_id,
                    "payload": {"label": label, "merged": duplicates},
                },
            )
            merged.append(f"{label}: {', '.join(duplicates)} → {canonical_id}")
    return merged


def _parse_worker_stamp(raw: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _repair_dedupe_heartbeats(ctx: FleetContext) -> list[str]:
    """Undo the bad maintenance revision that heartbeated historical canonical workers.

    worker_dedupe is coordination metadata, not evidence that the browser tab is alive.
    Reconstruct last_seen from pre-dedupe worker/event/claim activity, but never touch a
    worker that currently has an active claim.
    """

    repaired: list[str] = []
    pending: list[tuple[str, str, Any, dict[str, Any]]] = []
    # Поиск — снимком без замка записи (07.10.2026: этот шаг идёт на каждом fleet_list/take, обходит
    # всех воркеров и почти никогда ничего не пишет, а держал замок записи до 7 с за раз).
    with ctx.store.read() as tx:
        workers = [dict(row) for row in tx.query(
            """SELECT id,label,first_seen,last_seen FROM workers
               WHERE revoked_at IS NULL AND kind='chatgpt'"""
        )]
        for worker in workers:
            worker_id = str(worker["id"])
            label = str(worker.get("label") or "")
            if not _AUTOSTART_LABEL.fullmatch(label):
                continue
            if tx.one(
                "SELECT 1 AS hit FROM claims WHERE worker_id=? AND state='active' LIMIT 1",
                (worker_id,),
            ) is not None:
                continue
            dedupe = tx.one(
                """SELECT ts FROM events
                   WHERE worker_id=? AND kind='worker_dedupe'
                   ORDER BY id DESC LIMIT 1""",
                (worker_id,),
            )
            if dedupe is None:
                continue
            dedupe_at = _parse_worker_stamp(dedupe["ts"])
            current = _parse_worker_stamp(worker.get("last_seen"))
            if dedupe_at is None or current is None:
                continue

            siblings = [dict(row) for row in tx.query(
                "SELECT id,first_seen,last_seen FROM workers WHERE label=?",
                (label,),
            )]
            sibling_ids = [str(row["id"]) for row in siblings]
            marks = ",".join("?" for _ in sibling_ids)
            stamps: list[datetime] = []
            for row in siblings:
                for key in ("first_seen", "last_seen"):
                    stamp = _parse_worker_stamp(row.get(key))
                    if stamp is not None and stamp < dedupe_at - timedelta(seconds=5):
                        stamps.append(stamp)
            if sibling_ids:
                for row in tx.query(
                    f"""SELECT ts FROM events
                        WHERE worker_id IN ({marks})
                          AND kind NOT IN ('worker_dedupe','worker_dedupe_heartbeat_repair')
                        ORDER BY id""",
                    sibling_ids,
                ):
                    stamp = _parse_worker_stamp(row["ts"])
                    if stamp is not None and stamp < dedupe_at - timedelta(seconds=5):
                        stamps.append(stamp)
            for row in tx.query(
                "SELECT started_at,released_at FROM claims WHERE worker_id=?",
                (worker_id,),
            ):
                for key in ("started_at", "released_at"):
                    stamp = _parse_worker_stamp(row[key])
                    if stamp is not None and stamp < dedupe_at - timedelta(seconds=5):
                        stamps.append(stamp)
            if not stamps:
                continue
            real_last_seen = max(stamps)
            # Only undo a heartbeat that is clearly the dedupe timestamp. A real later
            # tool call wins and is never backdated.
            if current < dedupe_at - timedelta(seconds=5):
                continue
            pending.append((worker_id, label, worker.get("last_seen"), {
                "label": label,
                "dedupe_at": dedupe_at.isoformat(),
                "restored_last_seen": real_last_seen.isoformat(),
            }))
    if not pending:
        return repaired
    with ctx.store.tx() as tx:
        for worker_id, label, seen, payload in pending:
            # last_seen не сдвинулся с момента снимка — иначе живой вызов победил, не трогаем.
            if tx.one("SELECT 1 FROM workers WHERE id=? AND last_seen IS ?", (worker_id, seen)) is None:
                continue
            tx.change(
                "UPDATE workers SET last_seen=? WHERE id=?",
                (payload["restored_last_seen"], worker_id),
                event={"kind": "worker_dedupe_heartbeat_repair", "worker_id": worker_id, "payload": payload},
            )
            repaired.append(f"{label}/{worker_id}")
    return repaired


def _register_worker(ctx: FleetContext, label: str, kind: str) -> str:
    now = datetime.now(timezone.utc).isoformat()
    # One extension-created tab has one generated fMMDD-HHMMxxx label.  fleet_join can be
    # retried after a UI/network failure; that retry must be idempotent.
    if kind == "chatgpt" and _AUTOSTART_LABEL.fullmatch(label):
        _dedupe_autostart_workers(ctx, label)
        with ctx.store.tx() as tx:
            existing = tx.one(
                """SELECT id FROM workers
                   WHERE label=? AND kind='chatgpt' AND revoked_at IS NULL
                   ORDER BY last_seen DESC,id DESC LIMIT 1""",
                (label,),
            )
            if existing is not None:
                worker_id = str(existing["id"])
                tx.change(
                    "UPDATE workers SET last_seen=? WHERE id=?",
                    (now, worker_id),
                    event={
                        "kind": "join_reuse",
                        "worker_id": worker_id,
                        "payload": {"label": label, "kind": kind},
                    },
                )
                return worker_id

    with ctx.store.tx() as tx:
        worker_id = _next_worker_id(tx)
        tx.change(
            """INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason)
               VALUES (?,?,?,?,?,NULL,'')""",
            (worker_id, label, kind, now, now),
            event={
                "kind": "join",
                "worker_id": worker_id,
                "payload": {"label": label, "kind": kind},
            },
        )
    return worker_id


def _task_abs_path(ctx: FleetContext, task: Task) -> str:
    try:
        with ctx.store.read() as tx:
            row = tx.one("SELECT root FROM projects WHERE name=?", (task.project,))
    except Exception:
        row = None
    if row is None:
        return task.spec_path
    root = str(row["root"])
    return str(Path(root) / task.spec_path) if task.spec_path else root


def _neighbors(ctx: FleetContext, task: Task, worker_id: str) -> list[str]:
    try:
        with ctx.store.read() as tx:
            rows = tx.query(
                """SELECT c.worker_id,w.label,t.tz,t.title
                   FROM claims c
                   JOIN workers w ON w.id=c.worker_id
                   JOIN tasks t ON t.project=c.project AND t.tz=c.tz
                   WHERE c.state='active' AND c.project=? AND c.worker_id<>?
                   ORDER BY c.started_at,c.worker_id""",
                (task.project, worker_id),
            )
    except Exception:
        return []
    return [
        f'{row["worker_id"]} ({row["label"] or "без метки"}) — {row["tz"]}'
        + (f': {row["title"]}' if row["title"] else "")
        for row in rows
    ]


def _relaxed_needs_note(ctx: FleetContext, task: Task) -> list[str]:
    """Зависимости, выданные «до живого прогона» (world.py), — агент должен знать, на что опирается."""
    try:
        cfg = world.config()
        with ctx.store.read() as tx:
            rows = tx.query("SELECT project,tz,state,needs,resources FROM tasks WHERE project=?",
                            (task.project,))
            provided = world.provided_levels(tx, task.project)
            blocked = world.block_refs(tx, task.project)
            consumers = [
                (str(r["tz"]), d) for r in rows if str(r["state"]) not in ("accepted", "rejected")
                for d in _json_names(r["needs"])
                if world.dep_tz(d) == task.tz and world.dep_level(d)
                and world.dep_level(d) not in provided.get((task.project, task.tz), set())
            ]
        relaxed = world.relaxed_needs(task.project, task.needs, task.resources,
                                      world.build_index(rows, provided, blocked), world.world_names(cfg))
    except Exception:
        return []
    lines: list[str] = []
    levels = [d for d in relaxed if world.dep_level(d)]
    live = [d for d in relaxed if not world.dep_level(d)]
    if levels:
        lines += [
            "Уровень готовности уже есть, задача ещё в работе: " + ", ".join(levels) + ".",
            "  Опирайся на её контракт в дереве (схемы, интерфейсы, решения), а не на всю реализацию. "
            "Контракт расходится с нужным тебе — fleet_block с именем задачи в причине.",
        ]
    if live:
        lines += [
            "Ещё не приняты, но задачу не держат: " + ", ".join(live) + ".",
            "  Это задачи-доказательства на ресурсе мира, которого у тебя нет (телефон, нода, стенд): "
            "их входы готовы, живой прогон впереди. Работай против кода и контрактов в дереве. "
            "Если тебе нужен именно их результат или код, которого ещё нет, — fleet_block с именем "
            "задачи в причине: очередь вернёт тебя, когда её примут.",
        ]
    if consumers:
        by_level: dict[str, list[str]] = {}
        for tz, dep in consumers:
            by_level.setdefault(world.dep_level(dep), []).append(tz)
        for level, waiting in sorted(by_level.items()):
            lines += [
                f"От тебя ждут уровня «{level}»: {', '.join(sorted(waiting))}.",
                f"  Как только эта часть устоялась (её дальше не придётся менять), — fleet_checkpoint со "
                f"словом provide:{level} в done. Не жди сдачи: эти задачи стартуют сразу, а ты "
                f"дорабатываешь своё.",
            ]
    return lines


def _json_names(raw: Any) -> list[str]:
    try:
        val = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


_PROVIDE_RE = re.compile(r"(?<![\w-])provide:([A-Za-z][\w-]{0,40})")


def _record_provides(ctx: FleetContext, worker_id: str, task_label: str, text: str) -> str:
    """`provide:<уровень>` в контрольной точке — уровень готовности задачи объявлен (world.py)."""
    levels = sorted(set(_PROVIDE_RE.findall(text or "")))
    if not levels or "/" not in task_label:
        return ""
    project, tz = task_label.split("/", 1)
    with ctx.store.tx() as tx:
        have = world.provided_levels(tx, project).get((project, tz), set())
        for level in levels:
            if level not in have:
                tx.record_event(kind=world.PROVIDE_KIND, worker_id=worker_id, project=project, tz=tz,
                                payload={"level": level})
        waiting = sum(
            1 for r in tx.query("SELECT needs FROM tasks WHERE project=? AND state='ready'", (project,))
            for d in _json_names(r["needs"]) if world.dep_tz(d) == tz and world.dep_level(d) in levels
        )
    return f" Уровень {', '.join(levels)} объявлен; ждавших его задач: {waiting}."


def _format_task(ctx: FleetContext, task: Task, worker_id: str) -> str:
    def block(title: str, values: tuple[str, ...]) -> list[str]:
        return [f"{title}:"] + ([f"  - {value}" for value in values] if values else ["  (нет)"])

    lines = [
        f"Задача: {task.project}/{task.tz}",
        f"Тип: {task.kind}; приоритет: {task.priority}; состояние: {task.state}",
        f"ТЗ: {_task_abs_path(ctx, task)}",
    ]
    if task.title:
        lines.append(f"Название: {task.title}")
    lines += block("owns", task.owns)
    lines += block("resources", task.resources)
    lines += block("needs", task.needs)
    lines += _relaxed_needs_note(ctx, task)
    lines += block("Приёмка", task.acceptance)
    neighbours = _neighbors(ctx, task, worker_id)
    lines.append("Соседи:")
    lines += [f"  - {value}" for value in neighbours] if neighbours else ["  (сейчас нет)"]
    if task.body:
        lines += ["", "--- тело задания ---", task.body]
    return "\n".join(lines)


def _acceptance_task_hints(ctx: FleetContext, task: Task | None, worker_id: str) -> str:
    """Подсказать run_task, когда acceptance — shell-команда, которую raw run намеренно не разрешает."""
    if task is None or ctx.policy is None or not task.acceptance:
        return ""
    named = getattr(ctx.policy, "tasks", {}) or {}
    by_command = {
        " ".join(str(x) for x in spec.argv).replace("\\", "/").casefold(): name
        for name, spec in named.items()
    }
    hints = []
    for command in task.acceptance:
        norm = " ".join(str(command).replace("\\", "/").split()).casefold()
        name = by_command.get(norm)
        if name:
            hints.append(f"  - {command}  →  run_task(name='{name}', agent='{worker_id}')")
    if not hints:
        return ""
    return "\n\nПриёмка через безопасный named task (не пытайся запускать shell через run):\n" + "\n".join(hints)


def _compact_oversized_neighbors(text: str) -> str:
    """Bound the neighbor section without losing the task or resume evidence.

    The ordinary briefing has ``Соседи по путям``; the emergency fallback
    emits ``Соседи``. A handful of very long entries is just as dangerous as
    hundreds of short ones, so the count itself is never a prerequisite.

    >>> source = ("\\nСоседи по путям:\\n"
    ...           + "\\n".join(f"  - neighbor {n}: " + "x" * 50 for n in range(200))
    ...           + "\\n\\nРабочая памятка:\\nrule\\n\\nТело задачи:\\nBODY"
    ...           + "\\n\\nПричина reject:\\nREJECT\\n\\nКонтрольная точка:\\nCHECKPOINT")
    >>> result = _compact_oversized_neighbors(source)
    >>> all(part in result for part in ("BODY", "REJECT", "CHECKPOINT", "ещё 195 соседей"))
    True
    >>> len(result) < 9_000 and len(result) < len(source)
    True
    >>> huge = ("\\nСоседи по путям:\\n" + "\\n".join("  - " + "x" * 4500 for _ in range(3))
    ...         + "\\n\\nРабочая памятка:\\nrule\\n\\nТело задачи:\\nVISIBLE")
    >>> "VISIBLE" in _compact_oversized_neighbors(huge) and len(_compact_oversized_neighbors(huge)) < 9_000
    True
    >>> fallback = ("\\nСоседи:\\n" + "\\n".join("  - " + "x" * 100 for _ in range(150))
    ...             + "\\n\\n--- тело задания ---\\nFALLBACK BODY")
    >>> "FALLBACK BODY" in _compact_oversized_neighbors(fallback)
    True
    >>> len(_compact_oversized_neighbors(fallback)) < 9_000
    True
    >>> _compact_oversized_neighbors("short briefing")
    'short briefing'
    """
    if len(text) <= 9_000:
        return text
    for marker, terminator in (
        ("\nСоседи по путям:\n", "\n\nРабочая памятка:"),
        ("\nСоседи:\n", "\n\n--- тело задания ---"),
    ):
        start = text.find(marker)
        if start < 0:
            continue
        body_start = start + len(marker)
        body_end = text.find(terminator, body_start)
        if body_end < 0:
            continue
        entries = text[body_start:body_end].splitlines()
        if not entries or not all(entry.startswith("  - ") for entry in entries):
            continue
        prefix, suffix = text[:body_start], text[body_end:]
        for keep in range(min(5, len(entries)), -1, -1):
            hidden = len(entries) - keep
            visible = entries[:keep] + [
                f"  - … ещё {hidden} соседей (полный список — в реестре флота)"
            ]
            candidate = prefix + "\n".join(visible) + suffix
            if len(candidate) <= 9_000 or keep == 0:
                return candidate
    return text


def _compact_task_briefing(text: str) -> str:
    """Keep task/reject/checkpoint content visible in long take/join/resume."""
    return _compact_oversized_neighbors(text)


def _briefing(ctx: FleetContext, worker_id: str, task: Task | None = None) -> str:
    text = _briefing_body(ctx, worker_id, task)
    if task is None:
        return text
    try:
        from dev_mcp.fleet import lessons

        block = lessons.brief(ctx.store, task, worker_id)
        if block:
            text += "\n\n" + block
    except Exception:  # уроки — подсказка, не условие выдачи задачи
        pass
    try:
        from dev_mcp.fleet.goals import goal_block

        return _compact_task_briefing(goal_block(task.project) + text)
    except Exception:  # цель — компас, не условие выдачи задачи
        return _compact_task_briefing(text)


def _briefing_body(ctx: FleetContext, worker_id: str, task: Task | None = None) -> str:
    briefing = ctx.module("briefing")
    if briefing is None:
        if task is not None:
            payload = _format_task(ctx, task, worker_id)
            return payload + _acceptance_task_hints(ctx, task, worker_id)
        return (
            "Памятка fleet.briefing ещё не подключена. "
            f'Вызови fleet_take("{worker_id}") и следуй выданному заданию.'
        )
    try:
        payload = str(briefing.briefing(ctx.store, worker_id, task))
        return payload + _acceptance_task_hints(ctx, task, worker_id)
    except Exception as exc:
        if task is not None:
            payload = _format_task(ctx, task, worker_id)
            return payload + _acceptance_task_hints(ctx, task, worker_id)
        return f"Памятка временно недоступна: {exc}. Вызови fleet_take(\"{worker_id}\")."


def _chatgpt_gate_state(ctx: FleetContext) -> str:
    """Read the sensor's durable resource gate; no I/O or network on list hot path."""
    try:
        with ctx.store.read() as tx:
            state, _ = world.last_state(tx, "chatgpt-reachable")
        return state if state in (world.UP, world.DOWN) else world.UNKNOWN
    except Exception:
        return world.UNKNOWN


def _queue_line(ctx: FleetContext) -> str:
    """Что получит новый воркер. По этой строке расширение решает, открывать ли вкладку."""
    if _chatgpt_gate_state(ctx) == world.DOWN:
        return "очередь: выдавать нечего"
    try:
        task, count = _required(ctx, "queue").peek_next(ctx.store)
    except Exception as exc:
        return f"очередь: неизвестно ({exc})"
    if task is None:
        return "очередь: выдавать нечего"
    return f"очередь: следующая к выдаче — {task.project}/{task.tz} [{task.kind}]; можно выдать сейчас: {count}"


def _project_lines(ctx: FleetContext, projects: list | None = None) -> list[str]:
    """Очередь по каждому активному проекту: из них расширение строит галочки
    автозапуска и выбирает, под какой проект открыть вкладку."""
    if projects is None:
        try:
            projects = _required(ctx, "queue").peek_projects(ctx.store)
        except Exception:
            return []
    lines = []
    chatgpt_down = _chatgpt_gate_state(ctx) == world.DOWN
    for name, task, count in projects:
        if task is None or chatgpt_down:
            lines.append(f"проект: {name} — выдавать нечего")
        else:
            lines.append(f"проект: {name} — можно выдать {count}; следующая {task.tz} [{task.kind}]")
    return lines


def _project_blocker_lines(ctx: FleetContext, projects: list | None = None) -> list[str]:
    """Human explanation for a zero-runnable project.

    Keep the existing machine-readable project line unchanged for old extensions,
    but explain whether work is active, dependency-gated, frozen, or currently
    hidden behind an active resource/path claim.
    """
    if projects is None:
        try:
            projects = _required(ctx, "queue").peek_projects(ctx.store)
        except Exception:
            return []

    empty_projects = [name for name, task, count in projects if task is None and count == 0]
    if not empty_projects:
        return []

    lines: list[str] = []
    with ctx.store.read() as tx:
        has_outcomes = bool(tx.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='outcomes'"))
        current_only = (" AND NOT EXISTS (SELECT 1 FROM outcomes r WHERE r.project=t.project "
                        "AND r.parent_id IS NULL AND r.state IN ('done','dropped') "
                        "AND r.spec LIKE '%intent_ref%' AND t.tz LIKE r.id||'-%')"
                        if has_outcomes else "")
        for name in empty_projects:
            reasons: list[str] = []
            active = tx.one(
                "SELECT COUNT(*) AS n FROM claims WHERE project=? AND state='active'",
                (name,),
            )
            active_count = int(active["n"]) if active is not None else 0
            if active_count:
                reasons.append(f"в работе {active_count}")

            rows = tx.query(
                "SELECT t.tz,t.state,t.needs,t.resources FROM tasks t "
                "WHERE t.project=? AND t.state IN ('ready','frozen')" + current_only +
                " ORDER BY t.priority,t.created_at LIMIT 6",
                (name,),
            )
            index = None
            for row in rows:
                if len(reasons) >= 4:
                    break
                try:
                    needs = [str(x) for x in json.loads(str(row["needs"] or "[]"))]
                    own_res = [str(x) for x in json.loads(str(row["resources"] or "[]"))]
                except (TypeError, ValueError):
                    needs, own_res = [], []
                unmet: list[str] = []
                if needs:
                    if index is None:
                        index = world.build_index(tx.query(
                            "SELECT project,tz,state,needs,resources FROM tasks WHERE project=?", (name,)),
                            world.provided_levels(tx, name), world.block_refs(tx, name))
                    unmet = world.unmet_needs(name, needs, own_res, index, world.world_names())

                tz = str(row["tz"])
                state = str(row["state"])
                if state == "frozen":
                    suffix = f"; ждёт {', '.join(unmet[:3])}" if unmet else ""
                    reasons.append(f"{tz} frozen{suffix}")
                elif unmet:
                    reasons.append(f"{tz} ждёт {', '.join(unmet[:3])}")
                else:
                    reasons.append(f"{tz} перекрыта active resource/path claim")

            if not reasons:
                finished = has_outcomes and tx.one(
                    "SELECT 1 FROM outcomes r WHERE r.project=? AND r.parent_id IS NULL "
                    "AND r.state='done' AND r.spec LIKE '%intent_ref%' "
                    "AND NOT EXISTS (SELECT 1 FROM outcomes x WHERE x.project=r.project "
                    "AND x.parent_id IS NULL AND x.state='open')",(name,))
                if finished:
                    lines.append(f"состояние: {name} — интенция завершена; очередь пуста")
                    continue
                reasons.append("нет зарегистрированных ready/frozen задач")
            lines.append(f"блокер: {name} — " + "; ".join(reasons))
    return lines


def _external_waiting_lines(ctx: FleetContext) -> list[str]:
    """Show server waits without converting them into new author tasks."""
    with ctx.store.read() as tx:
        if not tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_work'"):
            return []
        rows = tx.query("SELECT t.project,t.tz,e.kind,e.payload FROM outcome_work w "
                        "JOIN outcomes o ON o.id=w.outcome_id AND o.state='open' "
                        "JOIN tasks t ON t.project=w.project AND t.tz=w.tz "
                        "JOIN projects p ON p.name=t.project AND p.active=1 "
                        "JOIN events e ON e.id=(SELECT MAX(x.id) FROM events x WHERE x.project=t.project AND x.tz=t.tz "
                        "AND x.kind IN ('intent.stage_waiting_external','intent.stage_external_resumed','intent.stage_platform_error')) "
                        "WHERE t.state IN ('accepted','submitted') AND e.kind!='intent.stage_external_resumed' "
                        "ORDER BY e.id DESC LIMIT 8")
    result = []
    for row in rows:
        payload = json.loads(row['payload'])
        reason = str(payload.get('reason') or payload.get('error') or '')[:200].replace('\n',' ')
        state = 'ожидает внешнюю проверку' if row['kind'].endswith('waiting_external') else 'ожидает ремонта платформы оператором'
        result.append(f"стадия: {row['project']}/{row['tz']} — {state}; {reason}")
    return result


def _attention_lines(ctx: FleetContext) -> list[str]:
    """Legacy draft, который maintenance почему-то не продвинул.

    Это диагностический сигнал автоматики, не очередь ручных approvals владельца.
    В штатном контуре после _fleet_maintenance список пуст.
    """
    try:
        with ctx.store.read() as tx:
            rows = tx.query(
                "SELECT t.project,t.tz,e.payload FROM tasks t "
                "JOIN events e ON e.id=(SELECT MAX(id) FROM events WHERE project=t.project AND tz=t.tz) "
                "WHERE t.state='draft' AND t.kind IN ('review','fix') AND e.kind='escalate' "
                "ORDER BY t.project,t.tz"
            )
    except Exception:
        return []
    lines = []
    for row in rows:
        try:
            note = str(json.loads(str(row["payload"] or "{}")).get("note") or "")
        except ValueError:
            note = ""
        note = " ".join(note.split())[:240] or "ревьюер не может проверить здесь"
        lines.append(f"внимание: {row['project']}/{row['tz']} — legacy draft не продвинут автоматически: {note}")
    return lines


def _pinned_project(ctx: FleetContext, worker_id: str) -> str | None:
    """Project affinity for the FIRST task of an autostarted chat.

    fleet_expect exists to make launcher demand deterministic: a fresh tab opened for
    Nova VPN must not steal the first Beta App task merely because it sorts earlier.
    Keeping that pin forever wastes the chat budget, though: once its project briefly
    empties, the same healthy chat used to stop while another project opened a new tab.
    After at least one claim this chat is already initialized and may consume the global
    queue like any other worker.
    """
    with ctx.store.read() as tx:
        worker = tx.one("SELECT label FROM workers WHERE id=?", (worker_id,))
        label = str(worker["label"] or "").strip() if worker is not None else ""
        if not label:
            return None
        used = tx.one("SELECT 1 AS hit FROM claims WHERE worker_id=? LIMIT 1", (worker_id,))
        if used is not None:
            return None
        row = tx.one(
            "SELECT project FROM events WHERE kind='expect' AND json_extract(payload,'$.label')=? "
            "ORDER BY id DESC LIMIT 1",
            (label,),
        )
    return str(row["project"]) if row is not None and row["project"] else None


def _capture_baseline(ctx: FleetContext, worker_id: str) -> None:
    """Снимок owns в момент взятия — из него ревьюер получит diff сделанного."""
    evidence = ctx.module("evidence")
    if evidence is None:
        return
    try:
        with ctx.store.read() as tx:
            row = tx.one(
                "SELECT id FROM claims WHERE worker_id=? AND state='active' ORDER BY id DESC LIMIT 1",
                (worker_id,),
            )
        if row is not None:
            evidence.capture(ctx.store, int(row["id"]))
    except Exception:
        # CONTRACT §11: снимок — подсказка ревьюеру, а не условие выдачи работы.
        return


def _active_task_info(ctx: FleetContext, worker_id: str,
                      claim_id: int | None = None) -> tuple[str, str, str]:
    with ctx.store.read() as tx:
        row = tx.one(
            """SELECT c.id,c.project,c.tz,t.kind
               FROM claims c
               JOIN tasks t ON t.project=c.project AND t.tz=c.tz
               WHERE c.worker_id=? AND c.state='active'
               ORDER BY c.id DESC LIMIT 1""",
            (worker_id,),
        )
    if row is None:
        raise ValueError(f"{worker_id}: нет активной задачи")
    if claim_id is not None and int(row['id']) != claim_id:
        raise ValueError('FLEET_STALE_CLAIM: active claim changed during action')
    return str(row["project"]), str(row["tz"]), str(row["kind"])


def _action_fingerprint(action: str, *values: str) -> str:
    """Stable signature for repeated effectful calls, without logging notes twice."""
    body = json.dumps([action, *values], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _check_review_revision(ctx: FleetContext, agent: str, revision: str) -> None:
    """Check the source submission before any sealed accept/bite side effects."""
    if not revision:
        return
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("FLEET_STALE_CLAIM: invalid submitted revision")
    from .review import _review_target

    with ctx.store.read() as tx:
        held = tx.one(
            "SELECT t.project,t.tz,t.body,t.kind FROM claims c JOIN tasks t "
            "ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
            (agent,),
        )
        if held is None or held["kind"] != "review":
            raise ValueError("FLEET_STALE_CLAIM: no active review for submitted revision")
        project, source = _review_target(
            str(held["tz"]), str(held["body"] or ""), str(held["project"]),
        )
        author = tx.one("SELECT author_worker FROM tasks WHERE project=? AND tz=?",
                        (project, source))
        seal = tx.one(
            "SELECT payload FROM events WHERE kind='claim_submission' "
            "AND project=? AND tz=? ORDER BY id DESC LIMIT 1", (project, source),
        )
        try:
            payload = json.loads(str(seal["payload"])) if seal is not None else {}
        except (ValueError, TypeError):
            payload = {}
        if (not isinstance(payload, dict)
                or payload.get("revision") != revision
                or author is None or payload.get("author") != author["author_worker"]):
            raise ValueError("FLEET_STALE_CLAIM: submitted revision does not match review")


def _addressed_before(ctx: FleetContext, agent: str, action: str,
                      claim_id: int | None, fingerprint: str) -> tuple[int | None, str | None]:
    """Resolve a claim before side effects; a completed addressed call replays its result."""
    if claim_id is not None and (type(claim_id) is not int or claim_id <= 0):
        raise ValueError("FLEET_STALE_CLAIM: claim_id must be a positive integer")
    with ctx.store.read() as tx:
        active = tx.one(
            "SELECT id,started_at FROM claims WHERE worker_id=? AND state='active' "
            "ORDER BY id DESC LIMIT 1", (agent,),
        )
        receipts = tx.query(
            "SELECT ts,payload FROM events WHERE worker_id=? AND kind='claim_action_result' "
            "ORDER BY id DESC LIMIT 100", (agent,),
        )
        parsed: list[tuple[str, dict[str, Any]]] = []
        completed_claims: set[int] = set()
        for receipt in receipts:
            try:
                data = json.loads(str(receipt["payload"]))
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                if type(data.get('claim_id')) is int:
                    completed_claims.add(data['claim_id'])
                if data.get("action") == action:
                    parsed.append((str(receipt["ts"]), data))
        if claim_id is not None:
            # Exact replay takes priority over the worker's NEW active claim.
            for _, prior in parsed:
                if prior.get("claim_id") == claim_id:
                    if prior.get("fingerprint") == fingerprint and isinstance(prior.get("result"), str):
                        return claim_id, prior["result"]
                    raise ValueError("FLEET_STALE_CLAIM: claim already completed with other arguments")
            if active is None or int(active["id"]) != claim_id:
                raise ValueError("FLEET_STALE_CLAIM: claim_id does not match active claim")
            return claim_id, None
        if active is None:
            return None, None  # Keep legacy no-active finish semantics.
        current = int(active["id"])
        acknowledged = tx.one(
            "SELECT 1 FROM events WHERE worker_id=? AND kind='claim_acknowledged' "
            "AND json_extract(payload,'$.claim_id')=? LIMIT 1", (agent, current))
        if acknowledged:
            return current, None  # an explicit take has bound the new generation
        if completed_claims - {current}:
            raise ValueError(
                "FLEET_STALE_CLAIM: похоже на повтор действия по прошлой задаче; "
                "после автоматической выдачи передай claim_id из памятки; в старой схеме "
                "первая строка note/reason/summary: claim_id: N"
            )
        return current, None


def _addressed_after(ctx: FleetContext, agent: str, claim_id: int | None,
                     action: str, fingerprint: str, result: str) -> str:
    if claim_id is not None:
        with ctx.store.tx() as tx:
            tx.record_event(
                kind="claim_action_result", worker_id=agent,
                payload={"claim_id": claim_id, "action": action,
                         "fingerprint": fingerprint, "result": result},
            )
    return result


def _effect_receipt(action: str, fingerprint: str) -> dict[str, str]:
    return {"action": action, "fingerprint": fingerprint,
            "result": "Действие уже сохранено для указанного claim; повтор не изменил очередь. "
                      "Продолжи через fleet_take/fleet_resume."}


def _claim_from_text(claim_id: int | None, text: str) -> tuple[int | None, str]:
    """Old tool snapshots can bind a claim without a refresh or another tool call."""
    pattern = r"(?m)^[ \t]*claim_id[ \t]*:[ \t]*([1-9][0-9]*)[ \t]*(?:\r?\n|$)"
    match = re.search(pattern, text or '')
    if match:
        tagged = int(match.group(1))
        if claim_id is not None and claim_id != tagged:
            raise ValueError('FLEET_STALE_CLAIM: conflicting claim_id addresses')
        return tagged, (text[:match.start()] + text[match.end():])
    return claim_id, text


def _silence_minutes(raw: str) -> int:
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() // 60))
    except (TypeError, ValueError):
        return 0


# A claim is reclaimed only when its worker never wrote anything and then went
# silent for this long. It must exceed a tab's working window (~27 min), otherwise
# an agent that spends its first window reading loses the task it just took.
ABANDONED_CLAIM_SILENCE_MIN = 30
ABANDONED_WITH_WORK_SILENCE_MIN = 30


def _hold_owner_gated_fix(ctx: FleetContext, agent: str, project: str, p: float | None, note: str) -> str:
    """Отказ, который снимает только владелец или физический мир, не гоняем по кругу fix → review.

    Jev (fleet/jev.py) оценил отказ до записи; при p ≥ порога только что созданная fix-задача
    замораживается с причиной — она уходит в очередь решений владельца (`fleet.cmd decisions`),
    снять — `fleet.cmd reactivate`. Нет оценки — поведение прежнее.
    """
    if p is None or p < OWNER_GATE_MIN:
        return ""
    try:
        with ctx.store.tx() as tx:
            row = tx.one(
                "SELECT payload FROM events WHERE kind='reject' AND worker_id=? AND project=? "
                "ORDER BY id DESC LIMIT 1",
                (agent, project),
            )
            fix_tz = json.loads(row["payload"] or "{}").get("fix_task") if row is not None else None
            if not fix_tz:
                return ""
            cur = tx.change(
                "UPDATE tasks SET state='frozen',updated_at=? WHERE project=? AND tz=? AND state='ready'",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), project, fix_tz),
                event={
                    "kind": "freeze",
                    "worker_id": agent,
                    "project": project,
                    "tz": fix_tz,
                    "payload": {
                        "previous_state": "ready",
                        "reason": "owner_gate",
                        "p": round(p, 3),
                        "note": (note or "")[:1500],
                    },
                },
            )
        if cur.rowcount:
            return (
                f" {fix_tz} заморожена: причина отказа, по оценке, снимается только владельцем "
                f"(p={p:.2f}) — ушла в очередь его решений, а не на новый круг исправлений."
            )
    except Exception:  # noqa: BLE001 — маршрутизация необязательна, вердикт уже сохранён
        return ""
    return ""


def _next_step(ctx: FleetContext, agent: str) -> str:
    """Что делать чату после сданной работы или вердикта: взять следующую или остановиться.

    Ответ инструмента — самое сильное указание для агента: он читает его прямо в момент
    решения. Поэтому «бери следующую» живёт здесь, а не только в инструкции проекта.
    """
    try:
        left = _required(ctx, "queue").budget_left(ctx.store, agent)
    except Exception:
        return ""
    if left is None:
        return ""
    if left <= 0:
        return (
            " Этот чат отработал свой лимит задач: следующую возьмёт новый чат со свежим "
            "контекстом. Коротко отчитайся и остановись."
        )
    return (
        f" Дальше — в этом же чате: сразу вызови fleet_take(agent='{agent}') и работай по новой "
        f"памятке (на этот чат осталось задач: {left}). Задач нет — коротко отчитайся и остановись."
    )


def _chat_lines(ctx: FleetContext) -> list[str]:
    """`чат W33: взято задач 2 из 5` — для расхода чатов в расширении.

    Отдельными строками, а не хвостом строки воркера: старое расширение такие строки
    пропускает, а изменённая строка воркера сломала бы его разбор целиком.
    """
    queue = ctx.module("queue")
    if queue is None or not hasattr(queue, "CHAT_TASK_BUDGET"):
        return []
    budget = queue.chat_budget(ctx.store) if hasattr(queue, "chat_budget") else queue.CHAT_TASK_BUDGET
    try:
        with ctx.store.read() as tx:
            rows = tx.query(
                """SELECT w.id,w.last_seen,COUNT(c.id) AS taken
                   FROM workers w LEFT JOIN claims c ON c.worker_id=w.id
                   WHERE w.revoked_at IS NULL AND w.kind IN ({}) AND w.last_seen>=?
                   GROUP BY w.id ORDER BY w.id""".format(",".join("?" for _ in queue.BUDGETED_KINDS)),
                (*sorted(queue.BUDGETED_KINDS),
                 (datetime.now(timezone.utc)-timedelta(minutes=61)).isoformat()),
            )
    except Exception:
        return []
    return [
        f'чат {row["id"]}: взято задач {row["taken"]} из {budget}'
        for row in rows
        if _silence_minutes(row["last_seen"]) <= 60
    ]


def _fleet_maintenance(ctx: FleetContext, queue: Any) -> None:
    """Самообслуживание очереди перед любым планированием/выдачей.

    Раньше recovery запускался только внутри fleet_take. Если abandoned claim сам
    закрывал последнюю runnable-задачу, расширение видело пустую очередь и новый
    fleet_take вообще не происходил — deadlock. fleet_list вызывается постоянно,
    поэтому maintenance должен быть и там.
    """
    # Каждый шаг независим: одна битая legacy-запись не должна отключать
    # requeue, auto-promote И thaw целиком до следующего ручного вмешательства.
    steps = [
        lambda: _dedupe_autostart_workers(ctx),
        lambda: _repair_dedupe_heartbeats(ctx),
        lambda: queue.recover_preparing(ctx.store) if hasattr(queue, "recover_preparing") else None,
        lambda: _required(ctx, 'launch').release_expired(ctx.store),
        lambda: _required(ctx, 'platform_manual').export(ctx.store),
        lambda: queue.requeue_stale(ctx.store, silence_min=ABANDONED_CLAIM_SILENCE_MIN),
        lambda: queue.recover_stale_with_work(
            ctx.store, silence_min=ABANDONED_WITH_WORK_SILENCE_MIN,
        ) if hasattr(queue, "recover_stale_with_work") else None,
        lambda: queue.close_obsolete_requests(ctx.store)
        if hasattr(queue, "close_obsolete_requests") else None,
        lambda: queue.promote_legacy_drafts(ctx.store)
        if hasattr(queue, "promote_legacy_drafts") else None,
        lambda: queue.reactivate_stale_generated_fixes(ctx.store)
        if hasattr(queue, "reactivate_stale_generated_fixes") else None,
        # Датчики мира (телефон) — до thaw: подключённый ресурс размораживает свои задачи сразу.
        lambda: world.tick(ctx.store),
        # Separate Chrome-route probe, every 60 s in a background thread.
        # Opt-in config must be present; test DBs and flag-off deployments stay offline.
        lambda: _tick_chatgpt_sensor(ctx),
        # Пробы препятствий (fleet/probe.py): агент сказал, как проверить затор, — проверяем сами.
        lambda: probe.tick(ctx.store, ctx.policy),
        # TZ-39: verified platform deployment events release only waits bound
        # to the confirmed candidate. This must precede the generic thaw.
        # The deploy-events module is a separate claim and may land later.
        lambda: _tick_platform_deploy_events(ctx.store),
        lambda: queue.thaw_frozen(ctx.store) if hasattr(queue, "thaw_frozen") else None,
        lambda: queue.close_resolved_styks(ctx.store) if hasattr(queue, "close_resolved_styks") else None,
        lambda: queue.reopen_orphan_reviews(ctx.store) if hasattr(queue, "reopen_orphan_reviews") else None,
    ]
    for i, step in enumerate(steps):
        try:
            step()
        except Exception as exc:
            # CONTRACT §11: coordination hygiene must never block handing out work,
            # и сбой одного maintenance-шагa не должен выключать остальные. Но молча — нельзя:
            # 07.10 шаг закрытия стыка не срабатывал на живом сервере, и следа не было.
            _note_maintenance_error(ctx, i, exc)


def _tick_chatgpt_sensor(ctx: FleetContext) -> None:
    # Only the active server registry may launch a Chrome-route network probe.
    # Controller tests use disposable stores and must never touch live network.
    expected_db = Path(__file__).resolve().parents[3] / "data" / "fleet" / "fleet.sqlite"
    if Path(str(getattr(ctx.store, "path", ""))).resolve() != expected_db.resolve():
        return
    if world.config().get("chatgpt-reachable", {}).get("sensor") != "chatgpt":
        return
    sensor = ctx.module("net_sensor")
    if sensor is not None:
        sensor.tick(ctx.store)


def _tick_platform_deploy_events(store: Any) -> None:
    """Optional TZ-39 consumer; never make missing staged code break fleet_list.

    Once TZ-39 lands, import and execution errors propagate to the independent
    maintenance_error logger rather than silently opening frozen tasks.
    """
    from importlib.util import find_spec

    if find_spec("dev_mcp.fleet.deploy_events") is None:
        return
    from . import deploy_events

    deploy_events.tick(store)
    # TZ-314: never thaw a routed platform dependency without a trusted
    # deployment receipt. Recheck durable events on every tick so a crash
    # between receipt ingestion and routed thaw cannot strand the waiters.
    if find_spec("dev_mcp.fleet.platform_route") is None:
        return
    with store.read() as tx:
        verified = tx.one(
            "SELECT id FROM events WHERE kind IN ('platform_deployed','manual_platform_resolved') "
            "ORDER BY id DESC LIMIT 1"
        )
    if verified is not None:
        from . import platform_route
        platform_route.thaw_deployed(store)


_MAINT_ERR_AT: dict[int, float] = {}


def _note_maintenance_error(ctx: FleetContext, step: int, exc: Exception) -> None:
    """Событие `maintenance_error` — не чаще раза в час на шаг: шаги идут на каждом fleet_list."""
    import time
    now = time.monotonic()
    if now - _MAINT_ERR_AT.get(step, -1e9) < 3600:
        return
    _MAINT_ERR_AT[step] = now
    try:
        with ctx.store.tx() as tx:
            tx.record_event(kind="maintenance_error", payload={
                "step": step, "error": f"{type(exc).__name__}: {exc}"[:500]})
    except Exception:
        pass



# ---- роль владельца (fleet/owner.py) ----------------------------------------------

_REPO = Path(__file__).resolve().parents[3]
_PULT = _REPO / "tools" / "fleet.py"
# Урезанная роль (решение владельца 22.09.2026): только пульт флота. Список закрытый и
# совпадает с подкомандами fleet.cmd; revoke/resume и всё, что пишет или запускает в
# самом dev-mcp, сюда не входит. argv уходит в argparse пульта без оболочки.
OWNER_COMMANDS = ("status", "report", "active", "metrics", "reactivate", "priority",
                  "recover", "say", "bootstrap", "set-acceptance", "chat-budget", "lock", "grant")
OWNER_PULT_TIMEOUT_S = 30  # ниже exec_sync_budget_s и потолка вызова через шлюз (45 с)


def _run_pult(ctx: FleetContext, argv: list[str]) -> str:
    """fleet.cmd отдельным процессом: тот же код и та же проверка, что у владельца локально.

    Реестр — ровно тот, что у этого сервера (`--db`), а не вычисленный пультом заново.
    """
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    db = str(getattr(ctx.store, "path", "") or "")
    prefix = ["--db", db] if db and db != ":memory:" else []
    try:
        done = subprocess.run(
            [sys.executable, str(_PULT), *prefix, *argv],
            cwd=str(_REPO), env=env, capture_output=True, timeout=OWNER_PULT_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return f"пульт не ответил за {OWNER_PULT_TIMEOUT_S} с"
    out = (done.stdout or b"").decode("utf-8", "replace").rstrip()
    err = (done.stderr or b"").decode("utf-8", "replace").rstrip()
    text = "\n".join(part for part in (out, err) if part)
    return text if done.returncode == 0 else f"[rc={done.returncode}] {text}"


def _bootstrap_argv(ctx: FleetContext, args: list[str]) -> list[str]:
    """`bootstrap "Beta App" TZ-72` → с корнем проекта из реестра: пути агенту не нужны."""
    if len(args) >= 2 and args[1].upper().startswith("TZ-"):
        with ctx.store.read() as tx:
            row = tx.one("SELECT root FROM projects WHERE name=?", (args[0],))
        if row is None:
            raise ValueError(f"проекта {args[0]!r} во флоте нет; новый проект ставит владелец локально")
        return [args[0], str(row["root"]), *args[1:]]
    raise ValueError("bootstrap: ПРОЕКТ TZ-NN [TZ-NN ...] [--reactivate] — проект из fleet_list, ТЗ уже лежат в его ToDo/tz")


def _fresh_fleet_lines(text: str, max_silence_min: int = 120) -> str:
    """fleet_list без давно молчащих воркеров: владельцу нужны живые, а не десятки старых чатов."""
    kept = []
    for line in text.splitlines():
        head, sep, tail = line.rpartition("; молчит ")
        if sep and tail.endswith(" мин") and tail[:-4].isdigit() and int(tail[:-4]) > max_silence_min:
            continue
        kept.append(line)
    return "\n".join(kept)


def _holds_claim(ctx: FleetContext, worker_id: str) -> bool:
    with ctx.store.read() as tx:
        return tx.one(
            "SELECT 1 AS x FROM claims WHERE worker_id=? AND state='active' LIMIT 1", (worker_id,),
        ) is not None


def _has_claims(ctx: FleetContext, worker_id: str) -> bool:
    with ctx.store.read() as tx:
        return tx.one("SELECT 1 AS x FROM claims WHERE worker_id=? LIMIT 1", (worker_id,)) is not None


def register(mcp: Any, ctx: FleetContext, parking: Parking | None = None) -> dict[str, Callable[..., str]]:
    """Register the complete fleet MCP surface and return direct callables for tests."""
    parking = parking or Parking()

    @mcp.tool(name='fleet_launch_reserve', description='Launcher: reserve one real task before creating an agent chat.')
    def fleet_launch_reserve(label: str, project: str) -> str:
        if not _AUTOSTART_LABEL.fullmatch(str(label or '')) or not str(project or '').strip():
            raise ValueError('invalid launcher label or project')
        result = _required(ctx, 'launch').reserve(
            ctx.store, lambda: _register_worker(ctx, label, 'chatgpt'), label, project)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool(name='fleet_launch_release', description='Launcher: release an unconfirmed failed chat start by its exact claim.')
    def fleet_launch_release(label: str, claim_id: int) -> str:
        if not _AUTOSTART_LABEL.fullmatch(str(label or '')) or type(claim_id) is not int:
            raise ValueError('invalid launcher address')
        return json.dumps(_required(ctx, 'launch').release(ctx.store,label,claim_id))

    @mcp.tool(
        name="fleet_join",
        description=(
            "Первый вызов воркера флота. Регистрирует эту вкладку/сессию и возвращает worker_id "
            "вместе с актуальной памяткой. Повтор с той же автозапущенной label идемпотентен и возвращает "
            "тот же worker_id, а не создаёт второго воркера. label — понятная владельцу метка, kind обычно 'chatgpt'."
        ),
    )
    def fleet_join(label: str, kind: str = "chatgpt") -> str:
        args = {"label": label, "kind": kind}
        try:
            worker_id = _register_worker(ctx, str(label).strip(), str(kind).strip() or "chatgpt")
            _required(ctx, 'launch').confirm(ctx.store, worker_id)
            payload = f"worker_id: {worker_id}\n\n{_briefing(ctx, worker_id)}"
            if _AUTOSTART_LABEL.fullmatch(str(label).strip()) and not _holds_claim(ctx, worker_id):
                # Чат, открытый расширением, получает задачу сразу (07.10.2026): 35 из 85 новых чатов
                # за сутки так и не взяли задачу, многие кончили ход на join. Без захвата будильник
                # такой чат не будит; с захватом — разбудит, как любой замолчавший.
                status, task = _take(worker_id)
                if status == "task":
                    payload += "\n\n=== ТВОЯ ЗАДАЧА — уже взята за тобой, fleet_take не нужен ===\n" + task
            return _respond_ok(ctx, "fleet_join", {"agent": worker_id, **args}, payload)
        except Exception as exc:
            return _respond_fail(ctx, "fleet_join", args, exc)

    @mcp.tool(
        name="fleet_resume",
        description=(
            "Возобновляет уже взятую этим worker_id задачу после перезапуска вкладки/клиента или "
            "по будильнику. Не создаёт нового claim: возвращает границы, последнюю контрольную точку "
            "и durable-сводку фактов (write/run/request/unacked messages) из общего реестра. "
            "wake — токен будки (w-…), если сообщение будильника его назвало."
        ),
    )
    def fleet_resume(agent: str, wake: str = "") -> str:
        args = {"agent": agent, "wake": wake} if wake else {"agent": agent}
        try:
            ctx.touch(agent)
            _required(ctx, 'launch').confirm(ctx.store, agent)
            if wake:
                _wake_ack(agent, str(wake).strip())
            payload = _required(ctx, "briefing").resume(ctx.store, agent)
            return _respond_ok(ctx, "fleet_resume", args, _compact_task_briefing(str(payload)))
        except Exception as exc:
            return _respond_fail(ctx, "fleet_resume", args, exc)

    @mcp.tool(
        name="fleet_look",
        description=(
            "Проверяет качество видимого интерфейса по 1–12 PNG/JPEG/WebP кадрам, "
            "из owns автора или проекта текущего ревью. Gemini возвращает JSON "
            "{ok, defects:[{severity,where,what}]}; общий лимит 60 вызовов за час. "
            "Зови для любого визуального UI; rubric добавляется к базовым критериям."
        ),
    )
    def fleet_look(agent: str, paths: list[str], rubric: str = "") -> str:
        args = {"agent": agent, "paths": paths, "rubric": rubric}
        try:
            ctx.touch(agent)
            evaluator = _required(ctx, "look")
            payload = evaluator.look_for_agent(ctx.store, agent, paths, rubric, policy=ctx.policy)
            return _respond_ok(ctx, "fleet_look", args, json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            return _respond_fail(ctx, "fleet_look", args, exc)

    @mcp.tool(
        name="fleet_task_body",
        description=(
            "Чистое чтение полного тела задачи по страницам без усечения. "
            "Передай точные project, tz и номер страницы начиная с 1. "
            "Возвращает JSON с body, page, pages, body_sha256 и проверенной mode provenance."
        ),
    )
    def fleet_task_body(project: str, tz: str, page: int = 1) -> str:
        # No ctx.touch or _respond_ok: both would write events and break the
        # read-only contract. Keep each UTF-8 JSON envelope well under 12 KB,
        # even when every source character requires a JSON escape.
        if (not isinstance(project, str) or not 0 < len(project) <= 256
                or not isinstance(tz, str) or not 0 < len(tz) <= 256
                or type(page) is not int or page < 1):
            return "Ошибка: недопустимые project, tz или page"
        with ctx.store.read() as tx:
            row = tx.one("SELECT body, spec_path, spec_sha, kind FROM tasks WHERE project=? AND tz=?",
                         (project, tz))
            project_row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        if row is None:
            return "Ошибка: задача не найдена"
        body = str(row["body"] or "")
        # Bound the *JSON-encoded* body bytes so escaped Unicode and controls
        # cannot breach the response budget; preserve every source character.
        chunks: list[str] = []
        start = 0
        encoded_size = 0
        for position, char in enumerate(body):
            cost = len(json.dumps(char, ensure_ascii=False).encode("utf-8")) - 2
            if encoded_size + cost > 8192 and position > start:
                chunks.append(body[start:position])
                start = position
                encoded_size = 0
            encoded_size += cost
        chunks.append(body[start:])
        if page > len(chunks):
            return "Ошибка: страница вне диапазона"
        chunk = chunks[page - 1]
        # Generated request/review/fix/triage paths address a report/source;
        # without a registered spec SHA they are not an authored UI-mode spec.
        # Preserve their implicit mode rather than strand every continuation.
        generated = str(row['kind']) in {'request','review','fix','triage'} and not row['spec_sha']
        metadata: dict[str, Any] = {
            "mode_status": "unavailable" if row["spec_path"] and not generated else "implicit-default",
        }
        # Read provenance exclusively from the registered project/spec path.
        # A missing/stale spec must never be reported as a verified UI mode.
        if row["spec_path"] and row["spec_sha"] and project_row is not None:
            try:
                from .mode_spec import read_mode_path

                root = Path(str(project_row["root"])).resolve()
                target = (root / str(row["spec_path"])).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("spec path outside project")
                directive = read_mode_path(target)
                if directive.spec_sha == str(row["spec_sha"]):
                    metadata = {
                        "mode_status": "verified",
                        "mode": directive.requested,
                        "mode_explicit": directive.explicit,
                        "outcome_stage": directive.stage,
                        "mode_contract": directive.contract_version,
                        "mode_spec_sha": directive.spec_sha,
                    }
                else:
                    metadata = {"mode_status": "stale"}
            except (OSError, ValueError):
                metadata = {"mode_status": "unavailable"}
        from hashlib import sha256

        return json.dumps({"project": project, "tz": tz, "page": page,
                           "pages": len(chunks), "body": chunk,
                           "body_sha256": sha256(body.encode("utf-8")).hexdigest(),
                           **metadata}, ensure_ascii=False)

    @mcp.tool(
        name="fleet_task_source_page",
        description=(
            "Read-only, lossless page of the ORIGINAL task body for your own active claim. "
            "Pass your worker agent, exact project and tz, zero-based character offset "
            "and character limit (1..4096). JSON gives content, total, next_offset "
            "and body_sha256; continue from next_offset until null. "
            "Other projects/tasks and workers without this active claim are denied."
        ),
    )
    def fleet_task_source_page(
        agent: str, project: str, tz: str, offset: int = 0, limit: int = 2048,
    ) -> str:
        # Do not use ctx.touch / _respond_ok: they record events, violating
        # read-only source retrieval. The call must never mutate fleet.sqlite.
        if (type(agent) is not str or not 0 < len(agent) <= 128
                or type(project) is not str or not 0 < len(project) <= 256
                or type(tz) is not str or not 0 < len(tz) <= 256
                or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 4096):
            return "Ошибка: недопустимые agent, project, tz, offset или limit"
        with ctx.store.read() as tx:
            # Authenticate the exact active claim BEFORE accessing task source;
            # no fallback to stale claims, same-project tasks or owner role.
            held = tx.one(
                "SELECT 1 AS allowed FROM claims WHERE worker_id=? "
                "AND project=? AND tz=? AND state='active' LIMIT 1",
                (agent, project, tz),
            )
            if held is None:
                return "Ошибка: нет активного захвата указанной задачи"
            task = tx.one("SELECT body FROM tasks WHERE project=? AND tz=?",
                          (project, tz))
        if task is None:
            return "Ошибка: задача не найдена"
        body = str(task["body"] or "")
        total = len(body)
        if offset > total:
            return "Ошибка: offset вне диапазона"

        from hashlib import sha256

        packet = {
            "project": project,
            "tz": tz,
            "offset": offset,
            "limit": limit,
            "total": total,
            "next_offset": total,
            "content": "",
            "body_sha256": sha256(body.encode("utf-8")).hexdigest(),
        }
        # Bound the COMPLETE JSON envelope, including worst-case escaping of
        # project/tz names, below the 12 KB MCP response budget.
        def encode(value: dict[str, Any]) -> str:
            return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        budget = 11000 - len(encode(packet).encode("utf-8"))
        piece: list[str] = []
        for ch in body[offset:offset + limit]:
            cost = len(json.dumps(ch, ensure_ascii=True)) - 2
            if cost > budget:
                break
            piece.append(ch)
            budget -= cost
        next_pos = offset + len(piece)
        packet["next_offset"] = next_pos if next_pos < total else None
        packet["content"] = "".join(piece)
        return encode(packet)

    def _wake_ack(agent: str, token: str) -> None:
        """Будка подтверждена: агент пришёл с её токеном. Задержка — от приказа до прихода."""
        delay = None
        try:
            with ctx.store.read() as tx:
                row = tx.one(
                    "SELECT ts FROM events WHERE kind='wake_order' AND payload LIKE ? ORDER BY id DESC LIMIT 1",
                    (f'%"wake":"{token}"%',),
                )
            if row is not None:
                started = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                delay = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        except Exception:
            pass
        ctx.record_event("wake_ack", agent, tool="fleet_resume", payload={"wake": token, "delay_s": delay})

    def _bite_plan(agent: str, project: str, kind: str, note: str, raw: str) -> dict[str, Any] | None:
        """Для fix: команда кусающего теста или явный отказ автора. Проверки — до сдачи."""
        if kind != "fix" or not bite.ENABLED:
            return None
        if not str(raw or "").strip():
            raw = bite.from_note(note)
        if not str(raw or "").strip():
            reason = bite.opted_out(note)
            if not reason:
                raise ValueError(
                    "fix сдаётся с кусающим тестом: bite_test = команда теста (как для run, из корня "
                    "проекта), который падает на отклонённой версии и проходит на исправленной. Добавь "
                    "такой тест на найденный ревьюером дефект. Тест невозможен (визуал, документ, внешний "
                    "конфиг) — первой строкой note: «bite: n/a — почему». Нет поля bite_test в схеме — строкой в note: "
                    "«bite: <команда теста>»."
                )
            return {"skipped": reason}
        argv = bite.parse_command(raw, ctx.policy)
        with ctx.store.read() as tx:
            claim = tx.one("SELECT id FROM claims WHERE worker_id=? AND state='active' ORDER BY id DESC LIMIT 1",
                           (agent,))
            root = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        if claim is None or root is None:
            return None
        return {"argv": argv, "claim_id": int(claim["id"]), "root": str(root["root"])}

    def _bite_launch(agent: str, project: str, tz: str, plan: dict[str, Any] | None) -> None:
        if plan is None:
            return

        def record(payload: dict[str, Any]) -> None:
            try:
                with ctx.store.tx() as tx:
                    tx.record_event(kind="bite", worker_id=agent, project=project, tz=tz, payload=payload)
            except Exception:
                pass

        if "skipped" in plan:
            record({"verdict": "skipped", "reason": plan["skipped"]})
            return
        record({"verdict": "pending", "command": " ".join(plan["argv"]),
                "reason": "проверка идёт в отдельной копии, результат появится здесь"})
        bite.start(ctx.store, project, tz, plan["root"], plan["claim_id"], plan["argv"], record)

    def _handoff(agent: str) -> str:
        """Ответ сдачи/вердикта/блока сразу несёт следующую задачу (v2 §4): решение «брать ли
        дальше» у модели отнято — 14% ходов кончались именно на нём. Брошенную выданную задачу
        подберёт будильник (fleet/wake.py)."""
        base = _next_step(ctx, agent)
        if not base or "лимит" in base:
            return base
        try:
            status, payload = _take(agent)
        except Exception:
            return base
        if status == "task":
            return (
                "\n\nСледующая задача уже выдана тебе — fleet_take не нужен, начинай по этой памятке:\n\n"
                + payload
            )
        if status == "final":
            return " " + payload
        return (
            f" Готовых задач сейчас нет — вызови fleet_take(agent='{agent}'): он подождёт, пока "
            "работа появится."
        )

    def _take(agent: str, _maintain: bool = False) -> tuple[str, str]:
        """Одна попытка выдачи: (status, payload), status — task | empty | cold | final."""
        worker_id = str(agent or "").strip()
        with ctx.store.read() as tx:
            worker = tx.one(
                "SELECT revoked_at,revoke_reason FROM workers WHERE id=?",
                (worker_id,),
            )
        if worker is None:
            return "final", (
                "Этот worker_id не зарегистрирован. Сначала выполни fleet_join и используй возвращённый worker_id."
            )
        if worker["revoked_at"] is not None:
            reason = str(worker["revoke_reason"] or "worker отозван")
            return "final", (
                f"Этот worker_id отозван ({reason}). Повтори fleet_join для текущей вкладки и используй новый/канонический worker_id."
            )
        ctx.touch(agent)
        queue = _required(ctx, "queue")
        # Queue hygiene is owned by the autonomous controller, not a read/take.
        pinned = _pinned_project(ctx, agent)
        left = queue.budget_left(ctx.store, agent) if hasattr(queue, "budget_left") else None
        if left is not None and left <= 0:
            return "final", (
                "Этот чат уже отработал свой лимит задач — следующую возьмёт новый чат со "
                "свежим контекстом. Коротко отчитайся и остановись."
            )
        with ctx.store.read() as tx:
            held = tx.one(
                "SELECT id,project,tz FROM claims WHERE worker_id=? AND state='active' ORDER BY id DESC LIMIT 1",
                (worker_id,),
            )
        if held is not None:
            # Повторный fleet_take с задачей на руках (27.09: W585 через 33 с после выдачи TZ-55)
            # раньше отвечал «очередь пуста», а с парковкой ещё и ждал 30 с. Ответ — сразу.
            return "final", (
                f"У тебя уже есть активная задача {held['project']}/{held['tz']} "
                f"(claim_id={held['id']}) — продолжай её. "
                "Памятка и уже сделанное — fleet_resume; сдача — fleet_submit (review — fleet_review)."
            )
        task = queue.take_next(ctx.store, agent, pinned)
        if task is None and pinned:
            return "final", (
                f"В проекте {pinned} доступных готовых задач сейчас нет (эта вкладка открыта под него). "
                "Коротко отчитайся и остановись."
            )
        if task is None:
            warm = _has_claims(ctx, worker_id)
            return ("empty" if warm else "cold"), "Очередь пуста: доступных готовых задач сейчас нет."
        if task.kind != "review":
            _capture_baseline(ctx, agent)
        payload = _briefing(ctx, agent, task)
        with ctx.store.read() as view:
            identified = view.one(
                "SELECT id FROM claims WHERE worker_id=? AND state='active' ORDER BY id DESC LIMIT 1",
                (agent,),
            )
        if identified is not None:
            payload = (f"claim_id: {identified['id']}\n"
                       "В submit/review/block/finish передавай этот claim_id. Если поля нет "
                       "в схеме инструмента, первая строка note/reason/summary: "
                       f"claim_id: {identified['id']}. Дополнительный вызов не нужен.\n" + payload)
        try:
            from .claim_take_routing import ClaimTakeRouting
            from . import goals
            with ctx.store.read() as view:
                held = view.one(
                    "SELECT id,project FROM claims WHERE worker_id=? AND state='active' "
                    "ORDER BY id DESC LIMIT 1", (agent,),
                )
            if held is not None and (
                goals.project_info(str(held["project"])).get("claim_trees") is True
                and goals.project_info(str(held["project"])).get("commit") is True
            ):
                claim_context = ClaimTakeRouting(ctx.store).resolve_claim_context(agent, int(held["id"]))
                if claim_context.mode == "isolated":
                    payload += (
                        f"\n\nИзолированное дерево claim {claim_context.claim_id}: "
                        f"{claim_context.tree_path}\nИсходный root: {claim_context.project_root}; "
                        f"base: {claim_context.base_sha}. Передавай agent в каждый file/run. "
                        "Пути исходного root автоматически маршрутизируются в твоё дерево."
                    )
        except Exception as exc:
            payload += f"\n\nОШИБКА маршрутизации claim: {type(exc).__name__}: {exc}. Нельзя работать в общем root."
        if left is not None and hasattr(queue, "chat_budget"):
            number = queue.chat_budget(ctx.store) - left + 1
            if number > 1:
                payload = (
                    f"Это задача №{number} в этом чате. Прошлые задачи закрыты: их файлы, "
                    "выводы и приёмка к этой не относятся — работай по этой памятке и "
                    "заново прочитай её ТЗ.\n\n" + payload
                )
        return "task", payload

    def _acknowledge_take(agent: str) -> None:
        with ctx.store.tx() as tx:
            claim = tx.one("SELECT id FROM claims WHERE worker_id=? AND state='active' "
                           "ORDER BY id DESC LIMIT 1", (agent,))
            if claim:
                tx.record_event(kind='claim_acknowledged', worker_id=agent,
                                payload={'claim_id': int(claim['id'])})

    def fleet_take(agent: str) -> str:
        args = {"agent": agent}
        try:
            status, payload = _take(agent)
            if status == 'task':
                _acknowledge_take(agent)
            return _respond_ok(ctx, "fleet_take", args, payload)
        except Exception as exc:
            return _respond_fail(ctx, "fleet_take", args, exc)

    def _park_mail(agent: str, since_iso: str) -> int:
        inbox = ctx.module("inbox")
        if inbox is None or not hasattr(inbox, "unread_since"):
            return 0
        try:
            return int(inbox.unread_since(ctx.store, agent, since_iso))
        except Exception:
            return 0

    def _park_record(agent: str, payload: dict[str, Any]) -> None:
        ctx.record_event("park", agent, tool="fleet_take", payload=payload)

    @mcp.tool(
        name="fleet_take",
        description=(
            "Берёт ОДНУ готовую задачу из общей очереди и возвращает её целиком: путь к ТЗ, owns, "
            "ресурсы, приёмку и соседей. Если очередь пуста, так и сообщает. ВАЖНО: параметр agent "
            "из worker_id передавай в каждый write_file/edit_file/run (и run_task), чтобы стража "
            "могла связать фактическое действие с заявкой."
        ),
    )
    async def fleet_take_tool(agent: str) -> str:
        args = {"agent": agent}
        try:
            status, payload = await anyio.to_thread.run_sync(_take, agent)
            if status == "empty":
                status, payload = await parking.wait(
                    str(agent or "").strip(), payload, _take,
                    lambda a, p: _park_record(a, p),
                    _park_mail,
                )
            if status == 'task':
                await anyio.to_thread.run_sync(_acknowledge_take, agent)
            return await anyio.to_thread.run_sync(_respond_ok, ctx, "fleet_take", args, payload)
        except Exception as exc:
            return await anyio.to_thread.run_sync(_respond_fail, ctx, "fleet_take", args, exc)

    @mcp.tool(
        name="fleet_finish",
        description=(
            "Освобождает незданную задачу. Обычный summary возвращает её в ready. Если работа "
            "объективно заблокирована, начни summary с 'blocked:' / 'заблокировано:' и укажи "
            "request:/TZ-зависимость: задача сразу frozen и не будет раздаваться циклом. "
            "После fleet_submit finish не нужен."
        ),
    )
    def fleet_finish(agent: str, summary: str, claim_id: int | None = None) -> str:
        args = {"agent": agent, "summary": summary, "claim_id": claim_id}
        try:
            claim_id, summary = _claim_from_text(claim_id, summary)
            ctx.touch(agent)
            fingerprint = _action_fingerprint("finish", summary)
            addressed, prior = _addressed_before(ctx, agent, "finish", claim_id, fingerprint)
            if prior is not None:
                return prior
            queue = _required(ctx, "queue")
            blocked = bool(re.match(r"(?i)^\s*(blocked|blocking|заблок(?:ировано|ирован[ао]?|ировано)?|блокер)\s*[:—-]", summary or ""))
            if blocked:
                queue.block(ctx.store, agent, summary, claim_id=addressed,
                            action_receipt=_effect_receipt("finish", fingerprint))
                result = _respond_ok(
                    ctx, "fleet_finish", args,
                    "Claim освобождён; blocker-задача frozen и будет оттаивать автоматически." + _handoff(agent),
                )
                return _addressed_after(ctx, agent, addressed, "finish", fingerprint, result)
            queue.finish(ctx.store, agent, summary, claim_id=addressed,
                         action_receipt=_effect_receipt("finish", fingerprint))
            # Без автовыдачи: задача только что вернулась в ready, и _take отдал бы её обратно.
            result = _respond_ok(ctx, "fleet_finish", args, "Claim и ресурсы освобождены." + _next_step(ctx, agent))
            return _addressed_after(ctx, agent, addressed, "finish", fingerprint, result)
        except Exception as exc:
            return _respond_fail(ctx, "fleet_finish", args, exc)

    @mcp.tool(
        name="fleet_block",
        description=(
            "Текущая work/fix/request-задача объективно заблокирована вне её owns или решением владельца: "
            "освобождает claim и переводит задачу в frozen, чтобы она не выдавалась циклом. reason обязателен. "
            "Для обычного review используй fleet_review(..., 'escalate', note). Исключение: "
            "финальное [fleet-final] review с >=3 точными неуспешными run обязательной приёмки "
            "можно заморозить до принятия инфраструктурного request, указав в reason "
            "одну существующую pending dependency request:<8 hex>; это не verdict. "
            "После block claim освобождается: "
            "maintenance сам вернёт задачу, когда названная зависимость принята, либо после retry-window. "
            "Добавь в reason строку «until: <команда>» — проверку без побочных эффектов, rc=0 = препятствие "
            "снято (until: adb devices): сервер гоняет её сам и вернёт задачу сразу, как только она пройдёт. "
            "Владельца не жди — сразу бери следующую задачу."
        ),
    )
    def fleet_block(agent: str, reason: str, claim_id: int | None = None) -> str:
        args = {"agent": agent, "reason": reason, "claim_id": claim_id}
        try:
            claim_id, reason = _claim_from_text(claim_id, reason)
            ctx.touch(agent)
            fingerprint = _action_fingerprint("block", reason)
            addressed, prior = _addressed_before(ctx, agent, "block", claim_id, fingerprint)
            if prior is not None:
                return prior
            with ctx.store.read() as tx:
                held = tx.one("SELECT project,tz FROM claims WHERE worker_id=? AND state='active' "
                              "ORDER BY id DESC LIMIT 1", (agent,))
            _required(ctx, "queue").block(ctx.store, agent, reason, claim_id=addressed,
                                          action_receipt=_effect_receipt("block", fingerprint))
            probe_note = (probe.register(ctx.store, ctx.policy, agent, str(held["project"]),
                                         str(held["tz"]), reason) if held is not None else "")
            if not probe_note:
                probe_note = ("Совет: строка «until: <команда>» в причине (проверка без побочных "
                              "эффектов, rc=0 — препятствие снято) вернёт задачу сразу, как только "
                              "затор уйдёт, а не по таймеру.")
            result = _respond_ok(
                ctx,
                "fleet_block",
                args,
                "Claim освобождён; задача frozen и будет автоматически проверяться на thaw. "
                "Владельца ждать не нужно. " + probe_note + _handoff(agent),
            )
            return _addressed_after(ctx, agent, addressed, "block", fingerprint, result)
        except Exception as exc:
            return _respond_fail(ctx, "fleet_block", args, exc)

    @mcp.tool(
        name="fleet_checkpoint",
        description=(
            "Контрольная точка текущей задачи — НЕ сдача и НЕ finish: задача остаётся за тобой. "
            "done — что установлено и чем подтверждено; next_step — следующий конкретный шаг; "
            "decided — принятые решения; rejected — что отвергнуто и почему; unknown — что осталось "
            "неизвестным; running — какие процессы ещё идут (job_id). Если ход оборвётся, будильник "
            "или другой агент продолжат ровно с неё. Ставь на переходах и когда руль просит."
        ),
    )
    def fleet_checkpoint(
        agent: str, done: str, next_step: str, decided: str = "", rejected: str = "",
        unknown: str = "", running: str = "",
    ) -> str:
        args = {"agent": agent, "done": done, "next_step": next_step, "decided": decided,
                "rejected": rejected, "unknown": unknown, "running": running}
        try:
            ctx.touch(agent)
            task = checkpoint.record(ctx.store, str(agent or "").strip(), {
                "done": done, "next": next_step, "decided": decided, "rejected": rejected,
                "unknown": unknown, "running": running,
            })
            ctx.nudged.pop(str(agent or "").strip(), None)
            note = _record_provides(ctx, str(agent or "").strip(), task, f"{done} {decided}")
            return _respond_ok(ctx, "fleet_checkpoint", args,
                               f"Контрольная точка {task} сохранена.{note} Продолжай со следующего шага.")
        except Exception as exc:
            return _respond_fail(ctx, "fleet_checkpoint", args, exc)

    @mcp.tool(
        name="fleet_submit",
        description=(
            "Сдаёт work/fix/request/triage-задачу на независимую приёмку, создаёт review-задачу "
            "и СРАЗУ освобождает claim/ресурсы; ответ несёт следующую задачу. Для kind=review "
            "используй fleet_review. Для kind=fix обязателен bite_test — команда теста (как для run, "
            "из корня проекта), который ПАДАЕТ на отклонённой версии и ПРОХОДИТ на исправленной: "
            "сервер проверит это сам в отдельной копии. Тест невозможен (визуал, документ, конфиг "
            "вне кода) — вместо bite_test строка в note: «bite: n/a — почему». Поля bite_test нет в "
            "твоей схеме — команда строкой в note: «bite: <команда теста>»."
        ),
    )
    def fleet_submit(agent: str, note: str, bite_test: str = "",
                     claim_id: int | None = None) -> str:
        args = {"agent": agent, "note": note, "claim_id": claim_id,
                **({"bite_test": bite_test} if bite_test else {})}
        try:
            claim_id, note = _claim_from_text(claim_id, note)
            ctx.touch(agent)
            fingerprint = _action_fingerprint("submit", note, bite_test)
            addressed, prior = _addressed_before(ctx, agent, "submit", claim_id, fingerprint)
            if prior is not None:
                return prior
            project, tz, kind = _active_task_info(ctx, agent, addressed)
            if kind == "review":
                raise ValueError(
                    "активная задача — review; verdict выноси через "
                    "fleet_review(agent, 'accept'|'reject', note), не через fleet_submit"
                )
            bite_plan = _bite_plan(agent, project, kind, note, bite_test)
            from . import goals
            isolated = (goals.project_info(project).get("claim_trees") is True
                        and goals.project_info(project).get("commit") is True)
            # A claim that was taken in an isolated tree never downgrades to
            # shared submission after an operational feature-flag change.
            with ctx.store.read() as tx:
                held = tx.one(
                    "SELECT id,project,tz,started_at FROM claims WHERE worker_id=? AND state='active' "
                    "ORDER BY id DESC LIMIT 1", (agent,),
                )
                if held is not None:
                    takes = tx.query(
                        "SELECT payload FROM events WHERE kind='take' AND worker_id=? "
                        "ORDER BY id DESC", (agent,),
                    )
                    for event in takes:
                        try:
                            taken = json.loads(str(event["payload"]))
                        except (ValueError, TypeError):
                            continue
                        if (taken.get("claim_id") == int(held["id"])
                                and isinstance(taken.get("claim_context"), dict)):
                            isolated = isolated or taken["claim_context"].get("mode") == "isolated"
                            break
            if held is not None and int(held['id']) != addressed:
                raise ValueError('FLEET_STALE_CLAIM: author claim changed before submission capture')
            if kind == "triage" and held is not None:
                from .claim_take_routing import ClaimTakeRouting
                routing = ClaimTakeRouting(ctx.store).resolve_claim_context(
                    agent, int(held["id"]),
                )
                tree = routing.tree_path if routing.mode == "isolated" else None
                refusal = _required(ctx, "judgment").triage_preflight(
                    ctx.store, project, tz, agent, str(held["started_at"]),
                    checkout_root=tree,
                )
                if refusal:
                    raise ValueError("Триаж не сдан: проверка решения до ревью: " + refusal)
            if isolated:
                from .claim_review_bite import _g, capture_submission
                if held is None:
                    raise ValueError("isolated submission requires active author claim")
                claim_id = int(held["id"])
                if kind == "triage":
                    # A decision without a server-recorded edit/request must not
                    # create an orphan immutable Git seal before queue rejects it.
                    # queue.submit repeats this check when releasing the claim.
                    with ctx.store.read() as tx:
                        refusal = _required(ctx, "queue")._triage_submit_reason(
                            tx, agent, held,
                        )
                    if refusal:
                        raise ValueError(refusal)
                sealed = capture_submission(ctx.store, claim_id, agent)
                if sealed is None:
                    raise ValueError("isolated submission has no sealed claim")
                with ctx.store.read() as tx:
                    home = tx.one("SELECT root FROM projects WHERE name=?", (project,))
                if home is None:
                    raise ValueError("isolated project root unavailable")
                base_tree = _g(
                    Path(str(home["root"])), "rev-parse", sealed.base_sha + "^{tree}",
                )
                # A source-root diff would always say 'unchanged' because TZ-40
                # deliberately routes edits into the claimant's private tree.
                # Bind the truthful change bit to the already sealed revision;
                # queue._empty_submit_reason checks this typed server event.
                with ctx.store.tx() as tx:
                    tx.record_event(
                        kind="claim_submission_delta", worker_id=agent,
                        project=project, tz=tz, payload={
                            "claim_id": claim_id,
                            "submission_id": sealed.submission_id,
                            "tree_sha": sealed.tree_sha,
                            "changed": sealed.tree_sha != base_tree,
                        },
                    )
                if kind == "fix":
                    if not bite_plan:
                        raise ValueError("isolated fix requires a reviewed bite command or n/a")
                    with ctx.store.tx() as tx:
                        tx.record_event(
                            kind="claim_bite_plan", worker_id=agent, project=project,
                            tz=tz, payload={"claim_id": claim_id, **bite_plan},
                        )
            _required(ctx, "queue").submit(ctx.store, agent, note, claim_id=addressed,
                                           action_receipt=_effect_receipt("submit", fingerprint))
            if not isolated:
                _bite_launch(agent, project, tz, bite_plan)
            review_tz = _required(ctx, "review").open_review(ctx.store, project, tz)
            admission = ("Работа сдана; проверку выполняет сервер: " + review_tz
                         if review_tz.startswith('server-') else
                         f"Работа сдана на приёмку; создана/найдена задача ревью {project}/{review_tz}")
            result = _respond_ok(
                ctx,
                "fleet_submit",
                args,
                (
                    admission + ". Claim и ресурсы уже освобождены — fleet_finish НЕ нужен."
                    + _handoff(agent)
                ),
            )
            return _addressed_after(ctx, agent, addressed, "submit", fingerprint, result)
        except Exception as exc:
            return _respond_fail(ctx, "fleet_submit", args, exc)

    @mcp.tool(
        name="fleet_review",
        description=(
            "Выносит verdict по активной задаче kind=review. verdict: accept|reject|escalate. "
            "accept разрешён только после собственных успешных run-событий ревьюера для каждой "
            "acceptance-команды, сделанных по ТЕКУЩЕМУ состоянию файлов задачи; reject создаёт "
            "fix-задачу; escalate (note = почему) закрывает текущее ревью и автоматически открывает "
            "финальное ревью другому агенту. Владельца ждать не нужно."
        ),
    )
    def fleet_review(agent: str, verdict: str, note: str = "",
                     claim_id: int | None = None, revision: str = "") -> str:
        args = {"agent": agent, "verdict": verdict, "note": note,
                "claim_id": claim_id, "revision": revision}
        try:
            claim_id, note = _claim_from_text(claim_id, note)
            ctx.touch(agent)
            fingerprint = _action_fingerprint("review", verdict.strip().casefold(), note, revision)
            addressed, prior = _addressed_before(ctx, agent, "review", claim_id, fingerprint)
            if prior is not None:
                return prior
            _check_review_revision(ctx, agent, revision)
            project, tz, kind = _active_task_info(ctx, agent, addressed)
            if kind != "review":
                raise ValueError(
                    f"активная задача {project}/{tz} имеет kind={kind!r}; "
                    "fleet_review допустим только для kind=review"
                )
            tree = None
            desired = verdict.strip().casefold()
            source = (ctx.sealed_review_source(agent)
                      if desired in {"accept", "reject"} else None)
            implicit_accept = False
            if source is not None and desired == "reject":
                # Legacy review.decide intentionally promotes a green FINAL fix
                # rejection to accept + follow-up. That hidden accept path
                # must NEVER bypass independent sealed review and bite proof.
                from . import review as review_impl
                with ctx.store.read() as tx:
                    original = tx.one(
                        "SELECT t.project,t.tz,t.kind,t.body,t.acceptance,t.owns "
                        "FROM claims c JOIN tasks t "
                        "ON t.project=c.project AND t.tz=c.tz WHERE c.id=?",
                        (source[0],),
                    )
                    active_review = tx.one(
                        "SELECT started_at FROM claims WHERE worker_id=? "
                        "AND state='active' ORDER BY id DESC LIMIT 1", (agent,),
                    )
                    if (original is not None and active_review is not None
                            and original["kind"] == "fix"
                            and review_impl.FINAL_MARK in str(original["body"] or "")):
                        commands = json.loads(str(original["acceptance"] or "[]"))
                        implicit_accept = (bool(commands) and not review_impl._missing_runs(
                            tx, agent, str(active_review["started_at"]), commands))
                            # цвет ствола вердикт не держит (п.3 плана консорциума 11.10)
            if desired == "accept" or implicit_accept:
                if source is not None:
                    from .claim_review_bite import ClaimReviewError, accept_claim, check_fix_bite
                    source_claim_id, _ = source
                    with ctx.store.read() as tx:
                        task = tx.one(
                            "SELECT t.kind FROM claims c JOIN tasks t "
                            "ON t.project=c.project AND t.tz=c.tz WHERE c.id=?",
                            (source_claim_id,),
                        )
                        plans = tx.query(
                            "SELECT payload FROM events WHERE kind='claim_bite_plan' "
                            "ORDER BY id DESC",
                        )
                    if task is None:
                        raise ClaimReviewError("sealed source task missing")
                    if task["kind"] == "fix":
                        plan = next((value for p in plans
                                     if (value := json.loads(str(p["payload"]))).get("claim_id")
                                     == source_claim_id), None)
                        if not isinstance(plan, dict):
                            raise ClaimReviewError("fix has no trusted bite plan")
                        check_fix_bite(
                            ctx.store, source_claim_id, agent, plan.get("argv"),
                            na_reason=str(plan.get("skipped") or ""),
                        )
                    accept_claim(ctx.store, source_claim_id, agent,
                                 reviewer_claim_id=addressed)
                    # Typed claim_accept already checks fresh immutable reviewer
                    # runs. Never mix a trunk tree hash into that verified proof.
                elif desired == "accept":
                    evidence = ctx.module("evidence")
                    tree = evidence.subject_tree(ctx.store, agent) if evidence is not None else None
            # Оракул — до записи: сеть внутри транзакции держала бы замок очереди.
            gate_p = owner_gate_probability(note) if verdict.strip().casefold() == "reject" else None
            _required(ctx, "review").decide(
                ctx.store, agent, verdict, note,
                tree=tree, claim_id=addressed, revision=revision,
                action_receipt=_effect_receipt("review", fingerprint),
            )
            held = _hold_owner_gated_fix(ctx, agent, project, gate_p, note)
            result = _respond_ok(
                ctx,
                "fleet_review",
                args,
                f"Вердикт {verdict.strip().lower()} сохранён для {project}/{tz}; review-claim освобождён."
                + held
                + _handoff(agent),
            )
            return _addressed_after(ctx, agent, addressed, "review", fingerprint, result)
        except Exception as exc:
            return _respond_fail(ctx, "fleet_review", args, exc)

    @mcp.tool(
        name="fleet_presence",
        description=(
            "Отдельный write-канал от launcher: сохраняет адреса чатов для серверного будильника. "
            "fleet_list остаётся чистым чтением. Передай строки «label https://chatgpt.com/c/…»."
        ),
    )
    def fleet_presence(launcher_chats: str) -> str:
        args = {"launcher_chats": len(str(launcher_chats or "").splitlines())}
        try:
            book = wake.ChatBook(Path(ctx.store.path).with_name("chats.json"))
            written = book.remember(launcher_chats)
            return _respond_ok(ctx, "fleet_presence", args, f"Сохранено адресов чатов: {written}.")
        except Exception as exc:
            return _respond_fail(ctx, "fleet_presence", args, exc)

    @mcp.tool(
        name="fleet_extension_report",
        description=(
            "Отдельный канал телеметрии расширения FleetChrome: обновляет lease для супервизора "
            "и передаёт сбои composer вместе с текстом страницы датчику ChatGPT. "
            "fleet_list при этом остаётся чистым чтением."
        ),
    )
    def fleet_extension_report(
        version: str = "",
        tabs: int = -1,
        pending: int = -1,
        detail: str = "",
        page_text: str = "",
        runnable: int = -1,
        enabled: bool | None = None,
        tick_completed: bool = False,
        account_event: str = "",
    ) -> str:
        args = {"version": version, "tabs": tabs, "pending": pending,
                "runnable": runnable,
                "detail_len": len(detail), "page_len": len(page_text),
                "enabled": enabled, "tick_completed": tick_completed,
                "account_event_len": len(account_event)}
        try:
            supervisor = _required(ctx, "supervisor")
            supervisor.record_extension_heartbeat(ctx.store, version=str(version)[:80],
                                                  tabs=int(tabs), pending=int(pending),
                                                  runnable=int(runnable),
                                                  enabled=enabled if type(enabled) is bool else None,
                                                  tick_completed=tick_completed is True)
            if account_event:
                from . import account_telemetry
                account_telemetry.record(ctx.store, account_event)
            if isinstance(detail,str) and detail.startswith('activity:'):
                from .activity import record
                encoded=detail[len('activity:'):]
                if len(encoded)>16000:
                    raise ValueError('activity payload too large')
                count=record(ctx.store,json.loads(encoded))
                return _respond_ok(ctx,'fleet_extension_report',args,f'extension lease updated; observations={count}')
            if isinstance(detail, str) and detail.startswith("browser_control:"):
                # Окно расширения = намерение владельца; страница ChatGPT с «подозрительной
                # активностью» = аварийная остановка. Поля: autostart(bool), halt(obj|null).
                encoded = detail[len("browser_control:"):]
                if not 2 <= len(encoded) <= 480:
                    raise ValueError("invalid browser_control payload size")
                request = json.loads(encoded)
                if not isinstance(request, dict):
                    raise ValueError("browser_control must be an object")
                halt: Any = False
                if "halt" in request:
                    raw = request["halt"]
                    halt = None if raw is None else {
                        "reason": str((raw or {}).get("reason", ""))[:200],
                        "source": str((raw or {}).get("source", "extension"))[:40]}
                autostart = request.get("autostart")
                state = supervisor.set_browser_control(
                    ctx.store, autostart=autostart if type(autostart) is bool else None,
                    halt=halt, by=str(request.get("by", "extension"))[:40])
                return _respond_ok(ctx, "fleet_extension_report", args,
                                   "browser control: autostart=" + ("on" if state["autostart"] else "off") +
                                   "; halt=" + ("set" if state["halt"] else "none"))
            if isinstance(detail, str) and detail.startswith("owner_notice_state:"):
                from .owner_notices import record_transport
                encoded = detail[len("owner_notice_state:"):]
                if not 1 <= len(encoded) <= 160:
                    raise ValueError("invalid owner notice receipt size")
                recorded = record_transport(ctx.store, json.loads(encoded))
                return _respond_ok(ctx, "fleet_extension_report", args,
                                   "extension lease updated; owner notice transport " +
                                   ("recorded" if recorded else "obsolete receipt ignored"))
            # Wake-state observations belong to the wake controller, not to the
            # ChatGPT page-failure sensor.  They cannot heartbeat the worker:
            # a composing tab is not proof that the model made progress.
            if isinstance(detail, str) and detail.startswith("wake_state:"):
                encoded = detail[len("wake_state:"):]
                if not 1 <= len(encoded) <= 256:
                    raise ValueError("invalid wake-state payload size")
                try:
                    report = json.loads(encoded)
                except (ValueError, TypeError):
                    raise ValueError("invalid wake-state JSON") from None
                if (not isinstance(report, dict)
                        or set(report) != {"wake", "state"}
                        or not isinstance(report["wake"], str)
                        or re.fullmatch(r"w-[0-9a-f]{6}", report["wake"]) is None
                        or report["state"] not in
                        ("generating", "idle", "error", "not_found")):
                    raise ValueError("invalid wake-state schema")
                wake.report_state(ctx.store, wake_id=report["wake"],
                                  state=report["state"])
                return _respond_ok(ctx, "fleet_extension_report", args,
                                   "extension lease updated; wake state recorded")
            count = 0
            if detail or page_text:
                sensor = _required(ctx, "net_sensor")
                count = sensor.report_extension(ctx.store, str(detail)[:500], str(page_text)[:2000])
            return _respond_ok(ctx, "fleet_extension_report", args,
                               f"extension lease updated; consecutive ChatGPT page failures: {count}")
        except Exception as exc:
            return _respond_fail(ctx, "fleet_extension_report", args, exc)

    @mcp.tool(
        name="fleet_research",
        description=(
            "Заказать Deep Research для текущей задачи флота. Возвращает id сразу, "
            "не ждёт отчёта. project определяется только по активному захвату agent; "
            "результат или unavailable приходит отдельным сообщением fleet_inbox."
        ),
    )
    def fleet_research(agent: str, question: str) -> str:
        args = {"agent": agent, "question_len": len(question) if isinstance(question, str) else -1}
        try:
            if not isinstance(agent, str) or not agent.strip():
                raise ValueError("agent is required")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("question is required")
            # Project provenance is owned by the active fleet claim, never by
            # user-controlled research text or a free-form project argument.
            project, _, _ = _active_task_info(ctx, agent)
            research = _required(ctx, "research")
            result = research.request(ctx.store, agent=agent, question=question, project=project)
            ctx.touch(agent)
            if isinstance(result, dict):
                return _respond_ok(ctx, "fleet_research", args,
                                   json.dumps(result, ensure_ascii=False))
            return _respond_ok(ctx, "fleet_research", args, str(result))
        except Exception as exc:
            return _respond_fail(ctx, "fleet_research", args, exc)

    @mcp.tool(
        name="fleet_research_next",
        description=(
            "Для FleetChrome: JSON-массив ожидающих Deep Research заявок "
            "[{id,question,project,requested_at,deadline_at}]. "
            "Возвращает только JSON, без человекочитаемого конверта."
        ),
    )
    def fleet_research_next() -> str:
        research = _required(ctx, "research")
        requests = research.pending(ctx.store)
        fields = ("id", "question", "project", "requested_at", "deadline_at")
        return json.dumps(
            [{key: row[key] for key in fields} for row in requests],
            ensure_ascii=False,
        )

    @mcp.tool(
        name="fleet_research_finish",
        description=(
            "Для FleetChrome: завершить Deep Research по id. "
            "status=complete|unavailable, ответ только JSON {id,status}."
        ),
    )
    def fleet_research_finish(
        id: str,
        status: str,
        report: str = "",
        reason: str = "",
    ) -> str:
        if not isinstance(id, str) or not id or len(id) > 128:
            raise ValueError("invalid research request id")
        if status not in ("complete", "unavailable"):
            raise ValueError("invalid research finish status")
        if not isinstance(report, str) or not isinstance(reason, str):
            raise ValueError("research report and reason must be strings")
        research = _required(ctx, "research")
        research.finish(
            ctx.store,
            request_id=id,
            status=status,
            report=report,
            reason=reason,
        )
        return json.dumps(
            {"id": id, "status": "completed" if status == "complete" else "unavailable"},
            ensure_ascii=False,
        )

    @mcp.tool(
        name="fleet_list",
        description=(
            "Показывает очередь и воркеров флота. Worker heartbeat означает фактическую MCP-активность "
            "модели, а отдельная строка launcher показывает, живы ли FleetChrome и расширение."
        ),
    )
    async def fleet_list(
        launcher_version: str = "",
        launcher_tabs: int = -1,
        launcher_pending: int = -1,
        launcher_chats: str = "",
    ) -> str:
        # Queue visibility must survive saturation of the shared MCP worker pool
        # by mutating calls waiting for the registry writer or long-running jobs.
        return await anyio.to_thread.run_sync(
            lambda: _fleet_list_snapshot(launcher_version, launcher_tabs,
                                         launcher_pending, launcher_chats),
            limiter=queue_query_slots,
        )

    queue_query_slots = anyio.CapacityLimiter(2)

    def _fleet_list_snapshot(
        launcher_version: str = "",
        launcher_tabs: int = -1,
        launcher_pending: int = -1,
        launcher_chats: str = "",
    ) -> str:
        args = {
            "launcher_version": launcher_version,
            "launcher_tabs": launcher_tabs,
            "launcher_pending": launcher_pending,
            "launcher_chats": len(str(launcher_chats or "").splitlines()),
        }
        try:
            # This endpoint is a pure query.  Controller handles maintenance,
            # wake retries and world sensors in its own independent thread.
            supervisor = ctx.module("supervisor")
            with ctx.store.read() as tx:
                rows = tx.query(
                    """SELECT w.id,w.label,w.kind,w.last_seen,c.project,c.tz
                       FROM workers w
                       LEFT JOIN claims c ON c.worker_id=w.id AND c.state='active'
                       WHERE w.revoked_at IS NULL
                       ORDER BY w.id,c.started_at"""
                )
            # Keep the machine-readable queue header before the unbounded worker
            # roster.  MCP transports/clients may truncate a large text result;
            # the browser extension must still see what can be launched instead
            # of treating the fleet as an old server with an unknown queue.
            from .controller import controller_health
            from .queue_intake import health as queue_intake_health
            from .storage_guard import health as storage_health
            from .owner_notices import lines as owner_notice_lines
            gate = _chatgpt_gate_state(ctx)
            projects = _required(ctx, "queue").peek_projects(ctx.store)
            # The extension consumes queue/worker/attention records. Detailed
            # per-project prose is an operator report, expensive under paging.
            diagnostics = ([*_project_blocker_lines(ctx, projects), *_external_waiting_lines(ctx)]
                           if not launcher_version else [])
            lines = [controller_health(ctx.store),
                     queue_intake_health(ctx.store),
                     storage_health(ctx.store),
                     *owner_notice_lines(ctx.store),
                     f"gate: chatgpt-reachable={gate}; launches={'0' if gate == world.DOWN else 'allowed'}",
                     *_project_lines(ctx, projects),
                     *diagnostics, _queue_line(ctx)]
            # TZ-310: a single classifier shared by fleet.cmd, fleet_list, and popup.
            # Deployment may precede the flow.agent_states module; do not invent
            # an alternative classifier or fail the fleet_list hot path.
            from . import flow as flow_metrics
            classifier = getattr(flow_metrics, "agent_states", None)
            if callable(classifier):
                counts = classifier(ctx.store)
                lines.append(flow_metrics.format_agent_states(counts))
                if hasattr(flow_metrics, "format_silent_breakdown"):
                    lines.append(flow_metrics.format_silent_breakdown(counts))
            if supervisor is not None and hasattr(supervisor, "status_line"):
                lines.append(supervisor.status_line(ctx.store))
            if supervisor is not None and hasattr(supervisor, "browser_control"):
                control = supervisor.browser_control(ctx.store)
                halt = control["halt"]
                reason = " ".join(str((halt or {}).get("reason", "")).split())[:160]
                lines.append("browser: autostart=" + ("on" if control["autostart"] else "off") +
                             "; halt=" + ((reason or "set") if halt else "none"))
            live_rows = [
                row for row in rows
                # История воркеров остаётся в SQLite/report, но fleet_list — hot path
                # расширения каждые 30 секунд. Старый беззадачный чат через несколько
                # часов не влияет ни на очередь, ни на вкладки и не должен раздувать MCP
                # ответ до сотен строк. Активный claim показываем всегда, даже если молчит.
                if row["project"] or _silence_minutes(row["last_seen"]) <= 120
            ]
            for row in live_rows:
                task = f'{row["project"]}/{row["tz"]}' if row["project"] else "без задачи"
                lines.append(
                    f'{row["id"]} ({row["label"] or row["kind"]}) — {task}; '
                    f'молчит {_silence_minutes(row["last_seen"])} мин'
                )
            if not live_rows:
                lines.append("(активных воркеров нет)")
            lines.extend(_chat_lines(ctx))
            lines.extend(_attention_lines(ctx))
            # Wake orders are *created* by the controller.  Reading recent
            # issued tokens here is pure; in particular fleet_list must never
            # issue another order or thaw a task while displaying the queue.
            try:
                from datetime import timedelta as _timedelta
                now = datetime.now(timezone.utc)
                cutoff = (now - _timedelta(minutes=3)).isoformat()
                supplied = {}
                for line in str(launcher_chats or "").splitlines():
                    label, sep, url = line.strip().partition(" ")
                    if sep and url.startswith("https://chatgpt.com/") and "/c/" in url:
                        supplied[label] = url.strip()
                book = wake.ChatBook(Path(ctx.store.path).with_name("chats.json"))
                with ctx.store.read() as tx:
                    recent = tx.query("SELECT ts,payload FROM events WHERE kind='wake_order' "
                                      "AND ts>=? ORDER BY id DESC LIMIT 16", (cutoff,))
                    pending = []
                    for event in recent:
                        meta = json.loads(event["payload"] or "{}")
                        claim = tx.one("SELECT c.project,c.tz,c.worker_id,c.state,w.label,w.last_seen "
                                       "FROM claims c JOIN workers w ON w.id=c.worker_id WHERE c.id=?",
                                       (meta.get("claim_id"),))
                        if (claim is None or claim["state"] != "active" or
                                str(claim["last_seen"]) > str(event["ts"])):
                            continue
                        label = str(claim["label"] or "")
                        url = supplied.get(label) or book.url(label)
                        if not url:
                            continue
                        item = {"label": label, "url": url, "project": claim["project"],
                                "tz": claim["tz"], "worker_id": claim["worker_id"],
                                "wake": str(meta.get("wake") or "")}
                        item["text"] = wake.wake_text(item)
                        pending.append(item)
                lines.extend(wake.order_lines(pending))
            except (ValueError, TypeError):
                pass
            return _respond_ok(ctx, "fleet_list", args, "\n".join(lines))
        except Exception as exc:
            return _respond_fail(ctx, "fleet_list", args, exc)

    @mcp.tool(
        name="fleet_expect",
        description=(
            "Не для агента — для автозапуска вкладок и владельца. Закрепляет проект за будущим "
            "воркером с меткой label: его fleet_take будет брать задачи только из этого проекта."
        ),
    )
    def fleet_expect(label: str, project: str) -> str:
        args = {"label": label, "project": project}
        try:
            label, project = str(label).strip(), str(project).strip()
            if not label:
                raise ValueError("пустая метка")
            with ctx.store.tx() as tx:
                row = tx.one("SELECT active FROM projects WHERE name=?", (project,))
                if row is None:
                    raise ValueError(f"проекта {project!r} во флоте нет")
                if not int(row["active"]):
                    raise ValueError(f"проект {project!r} выключен")
                tx.record_event(kind="expect", project=project, payload={"label": label})
            return _respond_ok(ctx, "fleet_expect", args, f"Метка {label} закреплена за проектом {project}.")
        except Exception as exc:
            return _respond_fail(ctx, "fleet_expect", args, exc)

    @mcp.tool(
        name="fleet_inbox",
        description="Явно читает накопившиеся уведомления воркера сверх малого автоматического конверта ответа.",
    )
    def fleet_inbox(agent: str) -> str:
        args = {"agent": agent}
        try:
            ctx.touch(agent)
            inbox = _required(ctx, "inbox")
            messages = inbox.pending_for(ctx.store, agent, max_items=50, max_bytes=10_000)
            payload = inbox.render_envelope(messages, agent) or "(уведомлений нет)"
            # Do not auto-append the same envelope a second time.
            audit_args = {"worker_id": agent}
            return _respond_ok(ctx, "fleet_inbox", audit_args, payload)
        except Exception as exc:
            return _respond_fail(ctx, "fleet_inbox", args, exc)

    @mcp.tool(
        name="fleet_ack",
        description="Подтверждает приём конкретных уведомлений. Передай agent и список ids из заголовка конверта.",
    )
    def fleet_ack(agent: str, ids: list[str]) -> str:
        args = {"agent": agent, "ids": ids}
        try:
            ctx.touch(agent)
            count = _required(ctx, "inbox").ack(ctx.store, agent, ids)
            return _respond_ok(ctx, "fleet_ack", args, f"Подтверждено уведомлений: {count}.")
        except Exception as exc:
            return _respond_fail(ctx, "fleet_ack", args, exc)

    @mcp.tool(
        name="fleet_send",
        description=(
            "Отправляет соседу или задаче короткий ФАКТ, не вопрос и не новую работу. "
            "to — worker_id или адрес Project/TZ-N; kind обычно note или contract."
        ),
    )
    def fleet_send(agent: str, to: str, kind: str, body: str) -> str:
        args = {"agent": agent, "to": to, "kind": kind, "body": body}
        try:
            ctx.touch(agent)
            message_id = _required(ctx, "inbox").send(
                ctx.store,
                to=to,
                from_kind="worker",
                from_id=agent,
                kind=kind,
                body=body,
            )
            return _respond_ok(ctx, "fleet_send", args, f"Сохранено уведомление {message_id}; это не подтверждение доставки.")
        except Exception as exc:
            return _respond_fail(ctx, "fleet_send", args, exc)


    @mcp.tool(
        name="fleet_owner_unlock",
        description=(
            "Только для агента владельца в его собственной вкладке, по паролю, который дал "
            "владелец. Открывает пульт fleet_owner для этого worker_id: статус флота, постановка "
            "крупных ТЗ в очередь, priority/recover/reactivate/say. Постоянный freeze web-агенту не выдаётся; "
            "ручного approve в throughput-контуре нет: "
            "legacy draft автоматически переводит maintenance, поэтому owner-agent ничего вручную не проталкивает."
        ),
    )
    def fleet_owner_unlock(agent: str, password: str) -> str:
        args = {"agent": agent, "password": "***"}  # пароль не попадает ни в аудит, ни в журнал
        try:
            if ctx.owner is None:
                raise RuntimeError("роль владельца на этом сервере выключена")
            ok, message = ctx.owner.unlock(agent, password)
            try:
                with ctx.store.tx() as tx:
                    tx.record_event(kind="owner_unlock" if ok else "owner_unlock_failed",
                                    worker_id=str(agent or "") or None, payload={})
            except Exception:
                pass
            if not ok:
                raise PermissionError(message)
            return _respond_ok(
                ctx, "fleet_owner_unlock", args,
                f"{message}. Пульт: fleet_owner(agent, command, args); начни с command='status'. "
                f"Команды: {', '.join(OWNER_COMMANDS)}.",
            )
        except Exception as exc:
            return _respond_fail(ctx, "fleet_owner_unlock", args, exc)

    @mcp.tool(
        name="fleet_owner",
        description=(
            "Пульт владельца флота — для worker_id, которому владелец выдал роль локально "
            "(fleet.cmd owner-grant). command: status (платформа, "
            "исключения, кто что держит, живые воркеры, очередь); report [ПРОЕКТ] (заявлено против "
            "сделано); active [ПРОЕКТ]; metrics --since ГГГГ-ММ-ДД; bootstrap ПРОЕКТ TZ-NN [TZ-NN ...] "
            "[--reactivate] — поставить в очередь ТЗ, уже написанные в ToDo/tz проекта. Перед bootstrap "
            "объединяй связанные микро-work в один end-to-end outcome: отдельная TZ нужна только при "
            "реальной dependency, ownership/resource conflict или самостоятельной проверяемой цели; "
            "постоянный freeze из web owner-пульта запрещён — blocker оформляет сам worker через fleet_block, "
            "который оттаивает автоматически; reactivate ПРОЕКТ TZ ПРИЧИНА "
            "(frozen/rejected → ready, включая generated fix:*); "
            "priority ПРОЕКТ N; recover ПРОЕКТ TZ ПРИЧИНА "
            "(длительно молчащий taken → ready с сохранением файлов и отзывом старого worker); "
            "say ТЕКСТ (всем держателям задач); set-acceptance ПРОЕКТ TZ --command CMD "
            "[--command CMD ...] (только без active claim, с audit old/new); "
            "chat-budget [N] (сколько задач подряд берёт один чат, 1–50; без N — показать); "
            "lock (закрыть сессию). args — аргументы команды списком, как у fleet.cmd."
        ),
    )
    def fleet_owner(agent: str, command: str, args: list[str] | None = None) -> str:
        argv = [str(a) for a in (args or [])]
        call = {"agent": agent, "command": command, "args": argv}
        try:
            ctx.touch(agent)
            if not ctx.is_owner(agent):
                raise PermissionError(
                    f"роль владельца не выдана worker_id {agent}. Скажи владельцу: «выдай роль "
                    f"{agent}» — он выполнит у себя fleet.cmd owner-grant {agent}; затем повтори этот "
                    "вызов. Пароль в чат не проси и не передавай."
                )
            command = str(command or "").strip().lower()
            if command not in OWNER_COMMANDS:
                raise ValueError(f"неизвестная команда {command!r}; доступны: {', '.join(OWNER_COMMANDS)}")
            if command == "lock":
                ctx.owner.lock(agent)
                return _respond_ok(ctx, "fleet_owner", call, "Сессия владельца закрыта.")
            if command == "status":
                text = "\n".join([_run_pult(ctx, []), "", _run_pult(ctx, ["active"]), "", _fresh_fleet_lines(_fleet_list_snapshot())])
            else:
                if command == "bootstrap":
                    argv = _bootstrap_argv(ctx, argv)
                text = _run_pult(ctx, [command, *argv])
            with ctx.store.tx() as tx:
                tx.record_event(kind="owner_command", worker_id=agent, payload={"command": command, "args": argv[:6]})
            return _respond_ok(ctx, "fleet_owner", call, text)
        except Exception as exc:
            return _respond_fail(ctx, "fleet_owner", call, exc)

    return {
        'fleet_launch_reserve': fleet_launch_reserve,
        'fleet_launch_release': fleet_launch_release,
        "fleet_join": fleet_join,
        "fleet_resume": fleet_resume,
        "fleet_look": fleet_look,
        "fleet_take": fleet_take,
        "fleet_task_body": fleet_task_body,
        "fleet_task_source_page": fleet_task_source_page,
        "fleet_take_async": fleet_take_tool,
        "fleet_finish": fleet_finish,
        "fleet_block": fleet_block,
        "fleet_checkpoint": fleet_checkpoint,
        "fleet_submit": fleet_submit,
        "fleet_review": fleet_review,
        "fleet_list": fleet_list,
        "fleet_expect": fleet_expect,
        "fleet_inbox": fleet_inbox,
        "fleet_ack": fleet_ack,
        "fleet_send": fleet_send,
        "fleet_owner_unlock": fleet_owner_unlock,
        "fleet_owner": fleet_owner,
    }


__all__ = ["FleetContext", "register"]
