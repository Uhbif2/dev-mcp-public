"""Create a versioned outcome tree and child-project safely and idempotently.

This is trusted server-side code, not an agent-facing path or permission API.
The server supplies allowed_roots. The source's raw intent supplies no rights.
"""
from __future__ import annotations

from dev_mcp import machine

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.project_identity import (
    ProjectIdentityError, projects_at, require_available,
)

from .sources.model import IntentEnvelope
from .sources.registry import IntentChange
from . import inputs, stages, stage_check
from .template import OutcomeTree, build_template


DEFAULT_ALLOWED_ROOTS = machine.WORKSPACES
_SAFE_NAME = re.compile(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9_-]{0,62}\Z")
_WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL"} |
                               {f"{kind}{i}" for kind in ("COM", "LPT") for i in range(1, 10)})
_SCHEMA = 1


class SpawnError(ValueError):
    """Unsafe project creation or a conflicting idempotency key."""


@dataclass(frozen=True)
class SpawnResult:
    project: str
    directory: Path
    tree: OutcomeTree
    created: bool
    actionable_stage: str
    manifest_sha256: str


def _canonical(root: Path) -> Path:
    # A normalized/aliased root is not acceptable: caller must pass a literal
    # allowed directory. Resolve also detects Windows reparse-point aliases.
    raw = Path(root)
    if not raw.is_absolute() or ".." in raw.parts or "." in raw.parts:
        raise SpawnError("root must be an absolute canonical directory")
    if not raw.is_dir() or raw.is_symlink():
        raise SpawnError("root does not exist or is a symlink")
    if os.path.normcase(str(raw.absolute())) != os.path.normcase(str(raw.resolve(strict=True))):
        raise SpawnError("root path aliases another directory")
    return raw.resolve(strict=True)


def _project_name(name: str) -> str:
    if (not isinstance(name, str) or not _SAFE_NAME.fullmatch(name)
            or name.upper() in _WINDOWS_RESERVED or name.endswith((".", " "))):
        raise SpawnError("invalid project name")
    return name


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _manifest(envelope: IntentEnvelope, project: str) -> dict[str, object]:
    unique = hashlib.sha256(
        _canonical_json({"source": envelope.source_id,
                         "external": envelope.external_id}).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": _SCHEMA,
        "name": project,
        "component_id": unique,
        "origin": {"source_id": envelope.source_id, "external_id": envelope.external_id},
        "kind": "intent-outcome-tree",
    }


def _manifest_hash(manifest: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _safe_manifest(directory: Path, expected: dict[str, object]) -> str:
    """Check an existing child folder without following unknown file links."""
    if not directory.is_dir() or directory.is_symlink():
        raise SpawnError("project child is not an ordinary directory")
    target = directory / "component.json"
    if not target.is_file() or target.is_symlink():
        raise SpawnError("existing project has no safe component.json")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SpawnError("invalid existing component.json") from exc
    if data != expected:
        raise SpawnError("project belongs to a different intent or has changed")
    return _manifest_hash(expected)


def _work_tz(tree: OutcomeTree, stage: str) -> str:
    return f"{tree.root_id}-{stage}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist_tree(store: Store, directory: Path, tree: OutcomeTree,
                  manifest_hash: str, *, inherited_understood: bool = False,
                  new_workspace: bool = False) -> bool:
    """One transaction for project, outcomes, jobs and observable wait edges."""
    tree.validate()
    inserted = False
    first_stamp = datetime.now(timezone.utc)
    if new_workspace:
        from dev_mcp.fleet.queue_intake import install_schema
        install_schema(store)
    with store.tx() as tx:
        try:
            require_available(tx, tree.project, str(directory))
        except ProjectIdentityError as exc:
            raise SpawnError(str(exc)) from exc
        p = tx.one("SELECT root,active FROM projects WHERE name=?", (tree.project,))
        if p is None:  # -100: интенция владельца раньше самообслуживания платформы (5)
            tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,-100,1)",
                      (tree.project, str(directory)), event="intent.spawn_project")
        elif os.path.normcase(str(Path(p["root"]).absolute())) != os.path.normcase(str(directory)):
            raise SpawnError("registered project uses a different directory")
        elif not p["active"]:
            raise SpawnError("disabled intent project cannot receive new work")
        if new_workspace:
            tx.change('INSERT OR IGNORE INTO queue_sources VALUES(?,?,?,?,?)',
                      (tree.project, norm_path(str(directory)),
                       '{}', '[]', _now()), event={'kind':'queue_source_baseline','project':tree.project,
                           'payload':{'files':0,'fresh_workspace':True}})
        for index, node in enumerate(tree.nodes):
            # next_step traverses children in created_ts order: retain the
            # template's explicit stage ordering, not alphabetical stage IDs.
            # Windows clock resolution can be 1 ms: derive unique values.
            stamp = (first_stamp + timedelta(microseconds=index)).isoformat()
            spec = _canonical_json(node.spec)
            evidence_spec = _canonical_json(node.evidence_spec)
            authority = _canonical_json(node.authority)
            old = tx.one("SELECT parent_id,project,intent_id,spec,evidence_spec,authority "
                         "FROM outcomes WHERE id=?", (node.identifier,))
            if old is not None:
                actual = (old["parent_id"], old["project"], old["intent_id"],
                          old["spec"], old["evidence_spec"], old["authority"])
                expected = (node.parent, tree.project, tree.intent_id,
                            spec, evidence_spec, authority)
                if actual != expected:
                    raise SpawnError("existing outcome version conflicts with template")
                continue
            inserted = True
            tx.change(
                "INSERT INTO outcomes "
                "(id,parent_id,project,title,intent_id,spec,evidence_spec,authority,"
                "state,rev,created_ts,closed_ts) "
                "VALUES(?,?,?,?,?,?,?,?,'open',1,?,NULL)",
                (node.identifier, node.parent, tree.project, node.stage, tree.intent_id,
                 spec, evidence_spec, authority, stamp),
                event="intent.outcome_created",
            )
            if node.parent is None:
                continue
            tz = _work_tz(tree, node.stage)
            step_ready = not node.depends_on
            triple = tree.triple and stages.triple_stage(node.stage) and not (
                inherited_understood and node.stage == "understood")
            body = stages.task_body(node.identifier, node.stage,
                                    node.evidence_spec['required'], triple=triple)
            stage_acceptance = json.dumps(stages.acceptance(node.stage), ensure_ascii=False)
            tx.change(
                "INSERT INTO tasks "
                "(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,"
                "owns,resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES(?,?,?, ?,5,'triage',?,?, '[]',?,'[]',?,NULL,?,?,?)",
                (tree.project, tz, f"Стадия {node.stage}",
                 "accepted" if inherited_understood and node.stage == "understood" else (
                     "frozen" if triple else "ready" if step_ready else "frozen"),
                 str(directory / "component.json"), manifest_hash,
                 json.dumps(stages.stage_owns(directory, node.stage), ensure_ascii=False),
                 stage_acceptance,
                 body, stamp, stamp),
                event="intent.work_created",
            )
            if triple:
                for index in (1, 2, 3):
                    candidate = stages.candidate_tz(tree.root_id, node.stage, index)
                    tx.change(
                        "INSERT INTO tasks "
                        "(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,"
                        "owns,resources,acceptance,author_worker,body,created_at,updated_at) "
                        "VALUES(?,?,?, ?,4,'triage',?,?, '[]',?,'[]',?,NULL,?,?,?)",
                        (tree.project, candidate, f"Независимая версия {index}: {node.stage}",
                         "ready" if step_ready else "frozen",
                         str(directory / "component.json"), manifest_hash,
                         json.dumps([str(directory / "intent" / "candidates" /
                                          node.stage / f"{index}.md")], ensure_ascii=False),
                         json.dumps([f"py -3 .intent/check.py candidate:{node.stage}:{index}"]),
                         stages.candidate_body(node.stage, index), stamp, stamp),
                        event="intent.candidate_created",
                    )
            tx.change("INSERT INTO outcome_work(outcome_id,project,tz) VALUES(?,?,?)",
                      (node.identifier, tree.project, tz), event="intent.work_linked")
            if node.stage == "owner_approved":
                tx.change(
                    "INSERT INTO obstacles (id,outcome_id,type,cause_code,predicate,"
                    "change_source,resolver_work,owner_action,state,ts) "
                    "VALUES(?,?,'owner_action','owner_approval',?,'owner',NULL,?,'open',?)",
                    ("owner-consent-" + hashlib.sha256(node.identifier.encode()).hexdigest()[:24],
                     node.identifier, _canonical_json({"owner_approval_required": True}),
                     "Одобрить операционный план с откатом; по умолчанию НЕ исполнять",
                     stamp), event="intent.owner_approval_required")
            for supplier in node.depends_on:
                obstacle = "wait-" + hashlib.sha256(
                    f"{node.identifier}|{supplier}".encode("utf-8")).hexdigest()[:24]
                tx.change(
                    "INSERT INTO obstacles "
                    "(id,outcome_id,type,cause_code,predicate,change_source,"
                    "resolver_work,owner_action,state,ts) "
                    "VALUES(?,?,'needs_result','procedure_unknown',?, ?,NULL,NULL,'open',?)",
                    (obstacle, node.identifier,
                     _canonical_json({"requires_outcome": supplier,
                                      "requires_proof": True}),
                     f"outcome:{supplier}", stamp),
                    event="intent.wait_dependency",
                )
    return inserted


def spawn_project(
    store: Store,
    change: IntentChange | IntentEnvelope,
    *,
    parent: Path,
    project_name: str,
    size: str,
    allowed_roots: Iterable[Path] = DEFAULT_ALLOWED_ROOTS,
    triple: bool = False,
) -> SpawnResult:
    """Materialize a project beneath a configured root and its outcome graph.

    allowed_roots is supplied by trusted server configuration. An adapter's
    payload and an agent must never be allowed to choose it themselves.
    """
    if isinstance(change, IntentChange):
        if change.kind not in {"created", "updated"}:
            raise SpawnError("only created or updated intents may spawn")
        envelope = change.envelope
        # Source snapshots are authoritative: an old unacknowledged 'created'
        # may not resurrect an intent already cancelled by a later scan.
        try:
            with store.read() as tx:
                revision = tx.one(
                    "SELECT state FROM intent_source_revisions "
                    "WHERE source_id=? AND external_id=? AND revision=?",
                    (envelope.source_id, envelope.external_id, envelope.revision))
        except Exception as exc:
            if "no such table: intent_source_revisions" not in str(exc):
                raise
            revision = None  # older test/legacy Store without a source registry
        if revision is not None and revision["state"] == "cancelled":
            raise SpawnError("intent cancelled by owner; refused stale spawn")
    else:
        envelope = change
    name = _project_name(project_name)
    tree = build_template(envelope, project=name, size=size, triple=triple)
    allowed = tuple(_canonical(Path(item)) for item in allowed_roots)
    root = _canonical(Path(parent))
    if not any(os.path.normcase(str(root)) == os.path.normcase(str(item)) for item in allowed):
        raise SpawnError("project parent is outside trusted roots")
    auth = envelope.authority
    # This check is in ADDITION to the trusted server roots, not a replacement.
    if auth.get("create_project") is not True:
        raise SpawnError("source lacks explicit creation authority")
    scope = auth.get("project_roots")
    if not isinstance(scope, (tuple, list)) or str(root) not in scope:
        raise SpawnError("source authority does not include the selected parent")
    directory = root / name
    if directory.resolve(strict=False) != directory.absolute():
        raise SpawnError("project child resolves through an alias")
    with store.read() as tx:
        try:
            require_available(tx, name, str(directory))
        except ProjectIdentityError as exc:
            raise SpawnError(str(exc)) from exc
    manifest = _manifest(envelope, name)
    manifest_hash = _manifest_hash(manifest)
    created_dir = False
    outcome.install_schema(store)
    try:
        # Publish component.json together with its directory via atomic
        # rename. A concurrent spawn never observes a half-written manifest.
        staging = root / f".spawn-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            with (staging / "component.json").open("x", encoding="utf-8") as file:
                file.write(_canonical_json(manifest) + "\n")
                file.flush()
                os.fsync(file.fileno())
            try:
                staging.rename(directory)
                created_dir = True
            except FileExistsError:
                manifest_hash = _safe_manifest(directory, manifest)
        finally:
            if staging.exists():
                if (staging.is_dir() and not staging.is_symlink() and
                        set(staging.iterdir()) == {staging / "component.json"}):
                    (staging / "component.json").unlink()
                    staging.rmdir()
        if directory.is_symlink():
            raise SpawnError("project child is a symlink")
        created_outcomes = _persist_tree(store, directory, tree, manifest_hash, new_workspace=created_dir)
        inputs.record_inputs(store, name, directory, envelope.raw_intent)
        stages.materialize(directory, envelope.raw_intent, triple=triple)
    except Exception:
        if created_dir:
            # Only clean the newly created directory with our exact manifest.
            # Refuse to follow symlinks/reparse points or delete foreign files.
            try:
                if (directory.is_dir() and not directory.is_symlink()
                        and set(directory.iterdir()) == {directory / "component.json"}
                        and _safe_manifest(directory, manifest) == manifest_hash):
                    (directory / "component.json").unlink()
                    directory.rmdir()
            except (OSError, SpawnError):
                pass
        raise
    return SpawnResult(name, directory, tree, created_outcomes, "understood", manifest_hash)


def route_proven_understood(store: Store, tree: OutcomeTree) -> OutcomeTree:
    """Rebase a proved provisional owner intent onto an existing safe target.

    The old outcome and its evidence remain as an audit trail; the new tree
    receives a server-issued carryover proof, not a user-asserted completion.
    """
    # 14.10: перенацеливание было привязано к тройке; без неё change-интенции
    # (fast-jev, mill-works-finish) строили бы с нуля в папке-заготовке.
    if tree.kind != "build":
        return tree
    understood = next((n for n in tree.nodes if n.stage == "understood"), None)
    if understood is None:
        return tree
    with store.read() as tx:
        row = tx.one("SELECT state FROM outcomes WHERE id=?", (understood.identifier,))
        proven = tx.scalar(
            "SELECT COUNT(*) FROM evidence WHERE outcome_id=? AND verdict='pass' "
            "AND kind=? AND subject_version=?",
            (understood.identifier, understood.evidence_spec["kind"], tree.revision))
        root = tx.one("SELECT root,active FROM projects WHERE name=?", (tree.project,))
        aggregate = tx.one("SELECT state FROM outcomes WHERE id=?", (tree.root_id,))
        # The old root is dropped after a successful route. Replaying the
        # event must yield the same routed tree, not resurrect the old one.
        try:
            cancelled = tx.scalar(
                "SELECT COUNT(*) FROM intent_source_events "
                "WHERE kind='cancelled' AND source_id=? AND external_id=? "
                "AND revision=?",
                (tree.nodes[0].spec["source_id"], tree.nodes[0].spec["external_id"],
                 tree.revision))
        except Exception as exc:
            if "no such table: intent_source_events" not in str(exc):
                raise
            cancelled = 0
    if (not row or row["state"] != "done" or not proven or not root
            or not root["active"] or not aggregate
            or aggregate["state"] not in ("open", "dropped") or cancelled):
        return tree
    staging = Path(root["root"])
    kind, target = stage_check.routing(staging)
    size = stage_check.size(staging)
    # Тройка нужна неясным большим задачам; small/medium идут одной версией.
    triple = tree.triple and size == "large"
    if kind == "build":
        if size == tree.size:
            return tree
        return _resize_in_place(store, tree, understood, staging, size, triple)
    raw = Path(target)
    if not raw.is_absolute() or ".." in raw.parts or raw.is_symlink() or not raw.is_dir():
        raise SpawnError("target must be an existing canonical directory")
    destination = raw.resolve(strict=True)
    if os.path.normcase(str(destination)) != os.path.normcase(str(raw.absolute())):
        raise SpawnError("target contains a path alias")
    allowed = tuple(_canonical(Path(p)) for p in tree.nodes[0].authority.get("project_roots", ()))
    if not any(destination != parent and destination.is_relative_to(parent) for parent in allowed):
        raise SpawnError("target outside trusted roots or target is the root itself")
    origin = {"source_id": str(tree.nodes[0].spec["source_id"]),
              "external_id": str(tree.nodes[0].spec["external_id"])}
    component = destination / "component.json"
    existing_manifest = None
    if component.exists():
        if component.is_symlink() or not component.is_file():
            raise SpawnError("target component manifest unsafe")
        try:
            existing_manifest = json.loads(component.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise SpawnError("target component manifest invalid") from exc
        if not isinstance(existing_manifest, dict):
            raise SpawnError("target component manifest invalid")
    with store.read() as tx:
        projects = projects_at(tx, str(destination))
    matching = [p for p in projects if p["active"]]
    if len(matching) > 1:
        raise SpawnError("ambiguous canonical target: multiple active projects")
    previous_origin = (existing_manifest.get("origin")
                       if existing_manifest and existing_manifest.get("kind") ==
                       "intent-outcome-tree" else None)
    takeover = False
    if previous_origin is not None and previous_origin != origin:
        if matching:
            raise SpawnError("target is owned by another active intent; retarget blocked")
        with store.read() as tx:
            cancelled = tx.scalar(
                "SELECT COUNT(*) FROM intent_source_events "
                "WHERE kind='cancelled' AND source_id=? AND external_id=?",
                (previous_origin.get("source_id"), previous_origin.get("external_id")))
        if not cancelled or not projects:
            raise SpawnError("target belongs to another intent, not cancelled")
        takeover = True
    elif projects and not matching:
        raise SpawnError("target project is disabled; retarget blocked")
    if matching and not takeover:
        project = matching[0]["name"]
    elif not takeover and _free_plain_name(store, destination):
        # 17.10.2026: the target folder's own name, as the owner sees it. The
        # "<folder>-intent-<file>-<hash>" form is only for a real name clash.
        project = _free_plain_name(store, destination)
    else:
        # Stable, source-identifying name; a raw target hash alone made a
        # live retarget indistinguishable from the cancelled old intent.
        slug = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "-", origin["external_id"]).strip("-")
        prefix = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "-", destination.name).strip("-_") or "component"
        slug = slug.strip("-_") or "source"
        suffix = hashlib.sha256(
            _canonical_json(origin).encode("utf-8")).hexdigest()[:8]
        project = _project_name(
            prefix[:18] + "-intent-" + slug[:22] + "-" + suffix)
        with store.read() as tx:
            named = tx.one("SELECT root,active FROM projects WHERE name=?", (project,))
        if named and (named["root"] != str(destination) or not named["active"]):
            raise SpawnError("retarget project name conflicts or is disabled")
    # Never overwrite application files. Only copy identical/missing intent
    # infrastructure. A pre-existing conflicting intent must be resolved first.
    copies = []
    for folder in ("intent", ".intent"):
        source = staging / folder
        if not source.exists():
            continue
        for original in sorted(source.rglob("*")):
            if original.is_symlink():
                raise SpawnError("intent workspace contains an alias")
            if not original.is_file():
                continue
            relative = original.relative_to(staging)
            target_file = destination / relative
            if not target_file.resolve(strict=False).is_relative_to(destination):
                raise SpawnError("target intent directory resolves outside trusted target")
            if target_file.exists():
                if target_file.is_symlink() or target_file.read_bytes() != original.read_bytes():
                    raise SpawnError(f"existing target contains conflicting intent data: {relative}")
            else:
                copies.append((original, target_file))
    # Validate the entire transfer first: a later conflicting file must not
    # leave earlier files copied and become a second source of future drift.
    for original, target_file in copies:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target_file)
    previous_manifest_bytes = None
    if takeover:
        # Reuse is an explicit, journalled transfer, never a silent merge.
        previous_manifest_bytes = component.read_bytes()
        next_manifest = _manifest(
            IntentEnvelope(origin["source_id"], origin["external_id"], tree.revision,
                           (staging / ".intent/intent.md").read_text(encoding="utf-8"),
                           tree.nodes[0].authority, _now()), project)
        component.write_text(_canonical_json(next_manifest) + "\n", encoding="utf-8")
    elif not component.exists():
        component.write_text(_canonical_json(_manifest(
            IntentEnvelope(str(tree.nodes[0].spec["source_id"]),
                           str(tree.nodes[0].spec["external_id"]), tree.revision,
                           (staging / ".intent/intent.md").read_text(encoding="utf-8"),
                           tree.nodes[0].authority, _now()), project)) + "\n",
                             encoding="utf-8")
    elif component.is_symlink() or not component.is_file():
        raise SpawnError("target component manifest unsafe")
    digest = hashlib.sha256(component.read_bytes()).hexdigest()
    envelope = IntentEnvelope(str(tree.nodes[0].spec["source_id"]),
                              str(tree.nodes[0].spec["external_id"]), tree.revision,
                              (staging / ".intent/intent.md").read_text(encoding="utf-8"),
                              tree.nodes[0].authority, _now())
    next_tree = build_template(envelope, project=project, size=size,
                               kind=kind, target=str(destination), triple=triple)
    try:
        _persist_tree(store, destination, next_tree, digest, inherited_understood=True)
    except Exception:
        if previous_manifest_bytes is not None:
            component.write_bytes(previous_manifest_bytes)
        raise
    if takeover:
        with store.tx() as tx:
            tx.record_event(kind="intent.target_reused", project=project,
                            payload={"retired_projects": [p["name"] for p in projects],
                                     "source_id": origin["source_id"],
                                     "external_id": origin["external_id"],
                                     "target": str(destination)})
    _retire_staging(store, tree.project, staging, destination)
    return _supersede(store, tree, understood, next_tree)


def _resize_in_place(store: Store, tree: OutcomeTree, understood: Any, staging: Path,
                     size: str, triple: bool) -> OutcomeTree:
    """understood выбрал другой размер для новой сборки: то же место, дерево короче/длиннее.
    Старые стадии снимаются, понимание переносится доказательством сервера."""
    component = staging / "component.json"
    if component.is_symlink() or not component.is_file():
        raise SpawnError("staging component manifest unsafe")
    envelope = IntentEnvelope(str(tree.nodes[0].spec["source_id"]),
                              str(tree.nodes[0].spec["external_id"]), tree.revision,
                              (staging / ".intent/intent.md").read_text(encoding="utf-8"),
                              tree.nodes[0].authority, _now())
    next_tree = build_template(envelope, project=tree.project, size=size, triple=triple)
    _persist_tree(store, staging, next_tree,
                  hashlib.sha256(component.read_bytes()).hexdigest(), inherited_understood=True)
    old_ids = [n.identifier for n in tree.nodes if n.stage != "understood"]
    with store.tx() as tx:
        marks = ",".join("?" * len(old_ids))
        tx.change("UPDATE tasks SET state='rejected',updated_at=? "
                  "WHERE state IN ('ready','frozen','submitted') AND project=? AND "
                  f"tz IN (SELECT tz FROM outcome_work WHERE outcome_id IN ({marks}))",
                  (_now(), tree.project, *old_ids),
                  event={"kind": "intent.resized", "project": tree.project,
                         "payload": {"from": tree.size, "to": size,
                                     "stages": [n.stage for n in next_tree.nodes[1:]]}})
        # кандидаты тройки и их review не привязаны к outcome_work — по префиксу корня;
        # understood уже доказан и перенесён, его хвосты тоже не нужны
        tx.change("UPDATE tasks SET state='rejected',updated_at=? "
                  "WHERE state IN ('ready','frozen','submitted') AND project=? "
                  "AND (tz LIKE ? OR tz LIKE ?)",
                  (_now(), tree.project, f"{tree.root_id}-%", f"review:{tree.root_id}-%"),
                  event="intent.resized_candidates")
    return _supersede(store, tree, understood, next_tree)


def _supersede(store: Store, tree: OutcomeTree, understood: Any,
               next_tree: OutcomeTree) -> OutcomeTree:
    migrated = next(n for n in next_tree.nodes if n.stage == "understood")
    with store.read() as tx:
        done = tx.one("SELECT state FROM outcomes WHERE id=?", (migrated.identifier,))
    if done["state"] != "done":
        outcome.add_evidence(
            store, writer=outcome.server_evidence_writer(store),
            outcome_id=migrated.identifier, kind=migrated.evidence_spec["kind"],
            ref=f"intent-route://{understood.identifier}", subject_version=tree.revision,
            env_version="route-proven-v1", verdict="pass", producer="intent_route_prover")
        outcome.close_outcome(store, migrated.identifier)
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET state='dropped',rev=rev+1 "
                  "WHERE id=? AND state='open'", (tree.root_id,),
                  event="intent.old_tree_superseded")
        tx.change("UPDATE outcomes SET state='dropped',rev=rev+1 "
                  "WHERE parent_id=? AND state='open'", (tree.root_id,),
                  event="intent.old_stages_superseded")
    return next_tree


def _free_plain_name(store: Store, destination: Path) -> str | None:
    plain = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "-", destination.name).strip("-_")
    try:
        plain = _project_name(plain)
    except SpawnError:
        return None
    with store.read() as tx:
        taken = tx.one("SELECT 1 AS x FROM projects WHERE name=?", (plain,))
    return None if taken else plain


def _retire_staging(store: Store, project: str, staging: Path, destination: Path) -> None:
    """The staging project served only to understand the intent; after a
    retarget it is a duplicate. Freeze its open work so it
    leaves the queue and the owner's alerts (17.10.2026: eight such leftovers)."""
    if os.path.normcase(str(staging)) == os.path.normcase(str(destination)):
        return
    # The project row stays: a repeated route call must still resolve the old
    # tree to the routed one. Only its open work leaves the queue.
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='frozen' WHERE project=? "
                  "AND state IN ('ready','submitted')", (project,),
                  event="intent.staging_tasks_frozen")


def reconcile_tree(store: Store, tree: OutcomeTree) -> tuple[str, ...]:
    """Unfreeze a task ONLY after all supplier outcomes have server-side proof.

    This can be called after a controller reconciliation; it never closes any
    stage and never invents evidence. Future server wiring is a separate task.
    """
    tree.validate()
    if tree.kind == "build":
        with store.read() as tx:
            project = tx.one("SELECT root FROM projects WHERE name=?", (tree.project,))
        document = Path(project["root"]) / "intent/understood.md" if project else None
        if document is not None and document.is_file():
            text = document.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^kind:\s*(?:change|investigate|ops)\s*$", text, re.M | re.I):
                # A failed route must not unlock build work in a staging root.
                # Only route_proven_understood may create its canonical tree.
                return ()
    ready: list[str] = []
    with store.tx() as tx:
        state = tx.one("SELECT active FROM projects WHERE name=?", (tree.project,))
        aggregate = tx.one("SELECT state FROM outcomes WHERE id=?", (tree.root_id,))
        if (state is None or not state["active"] or aggregate is None
                or aggregate["state"] != "open"):
            return ()
        # The tree argument is a *proposal*, not authority over what was
        # persisted. The same source revision can otherwise be rendered as
        # small or large with identical outcome IDs: reconciling a small tree
        # could mark a large root done while four required stages remain open.
        rows = tx.query(
            "SELECT id,parent_id,project,intent_id,spec,evidence_spec,authority "
            "FROM outcomes WHERE id=? OR parent_id=?",
            (tree.root_id, tree.root_id),
        )
        persisted = {str(row["id"]): row for row in rows}
        if set(persisted) != {node.identifier for node in tree.nodes}:
            raise SpawnError("persisted outcome tree differs from supplied stages")
        for node in tree.nodes:
            row = persisted[node.identifier]
            actual = (row["parent_id"], row["project"], row["intent_id"],
                      row["spec"], row["evidence_spec"], row["authority"])
            expected = (node.parent, tree.project, tree.intent_id,
                        _canonical_json(node.spec),
                        _canonical_json(node.evidence_spec),
                        _canonical_json(node.authority))
            if actual != expected:
                raise SpawnError("persisted outcome tree differs from supplied spec")
        for node in tree.nodes[1:]:
            if not node.depends_on:
                continue
            for supplier in node.depends_on:
                row = tx.one("SELECT state,evidence_spec FROM outcomes WHERE id=?", (supplier,))
                if row is None or row["state"] != "done":
                    break
                expected = json.loads(row["evidence_spec"])
                proven = tx.scalar(
                    "SELECT COUNT(*) FROM evidence WHERE outcome_id=? AND verdict='pass' "
                    "AND kind=? AND subject_version=?",
                    (supplier, expected["kind"], expected["subject_version"]),
                )
                if not proven:
                    break
            else:
                # No transition to implementation on task acceptance alone:
                # source outcome must be DONE and independently proven.
                # Размораживаем только при первом снятии ожидания поставщиков. 13.10 полная
                # сверка каждые 60 с размораживала стадию, которую агент заблокировал по своей
                # причине (delivered ждёт владельца) — 22 выдачи за 3 ч по кругу.
                if tx.one("SELECT 1 FROM events WHERE kind='intent.work_unfrozen' AND project=? "
                          "AND tz LIKE ? LIMIT 1", (tree.project, _work_tz(tree, node.stage) + "%")):
                    continue
                if node.stage == "owner_approved":
                    # Only the trusted owner-action receipt can advance this
                    # stage. Do not hand its claim to an ordinary fleet agent.
                    pass
                elif tree.triple and stages.triple_stage(node.stage):
                    for index in (1, 2, 3):
                        candidate = stages.candidate_tz(tree.root_id, node.stage, index)
                        tx.change("UPDATE tasks SET state='ready',updated_at=? "
                                  "WHERE project=? AND tz=? AND state='frozen'",
                                  (_now(), tree.project, candidate),
                                  event={"kind": "intent.work_unfrozen",
                                         "project": tree.project, "tz": candidate})
                    ready.append(node.stage)
                else:
                    tz = _work_tz(tree, node.stage)
                    result = tx.change("UPDATE tasks SET state='ready',updated_at=? "
                                       "WHERE project=? AND tz=? AND state='frozen'",
                                       (_now(), tree.project, tz),
                                       event={"kind": "intent.work_unfrozen",
                                              "project": tree.project, "tz": tz})
                    if result.rowcount:
                        ready.append(node.stage)
                for supplier in node.depends_on:
                    identity = "wait-" + hashlib.sha256(
                        f"{node.identifier}|{supplier}".encode("utf-8")
                    ).hexdigest()[:24]
                    tx.change("UPDATE obstacles SET state='resolved' WHERE id=? AND state='open'",
                              (identity,), event="intent.wait_resolved")
        # The aggregate outcome is only DONE if every required stage has been
        # closed AND each stage's server evidence matches its version and kind.
        all_proven = True
        for node in tree.nodes[1:]:
            state = tx.one("SELECT state FROM outcomes WHERE id=?", (node.identifier,))
            if state is None or state["state"] != "done" or not tx.scalar(
                "SELECT COUNT(*) FROM evidence WHERE outcome_id=? AND verdict='pass' "
                "AND kind=? AND subject_version=?",
                (node.identifier, node.evidence_spec["kind"],
                 node.evidence_spec["subject_version"]),
            ):
                all_proven = False
                break
        root = tx.one("SELECT state FROM outcomes WHERE id=?", (tree.root_id,))
        if root and root["state"] == "open" and all_proven:
            digest = hashlib.sha256(_canonical_json({
                "root": tree.root_id,
                "required": [node.identifier for node in tree.nodes[1:]],
                "revision": tree.revision,
            }).encode("utf-8")).hexdigest()
            tx.change(
                "INSERT INTO evidence "
                "(id,outcome_id,kind,ref,subject_version,env_version,verdict,producer,ts) "
                "VALUES(?,?,?,?,?,?,'pass','trusted_tree_verifier',?)",
                (uuid.uuid4().hex, tree.root_id, "review",
                 f"outcome-tree://{digest}", tree.revision, "template-v1", _now()),
                event="intent.aggregate_evidence",
            )
            tx.change("UPDATE outcomes SET state='done',rev=rev+1,closed_ts=? WHERE id=?",
                      (_now(), tree.root_id),
                      event={"kind": "outcome.closed", "project": tree.project,
                             "payload": {"id": tree.root_id, "state": "done"}})
            ready.append("root_verified")
    return tuple(ready)
