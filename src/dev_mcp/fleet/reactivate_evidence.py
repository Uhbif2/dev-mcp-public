"""TZ-69: read-only evidence policy for a *candidate* automatic block release.

This is not a queue transition API. The caller must construct the snapshots
from one consistent, trusted read of the task/event ledger, and must verify
probe, acceptance or deployment receipts *before* constructing evidence.
Untrusted block reason text cannot supply an authority or a positive fact.

An ELIGIBLE result is advisory: an integrator must atomically re-read the
latest transition event, predicate, and current task state before changing
anything, and append an auditable auto-reactivate event in that transaction.
This module never reads a store, executes a probe, or changes anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class DecisionStatus(str, Enum):
    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TaskSnapshot:
    project: str
    tz: str
    state: str
    latest_transition_id: int | None


@dataclass(frozen=True)
class BlockingEvent:
    event_id: int
    project: str
    tz: str
    kind: str
    origin: str  # fleet_auto | owner | guard | manual | unknown
    gate: str  # operational | dependency | repair | security | manual | unknown
    occurred_at: str


@dataclass(frozen=True)
class ResolutionPredicate:
    block_event_id: int
    kind: str  # until_probe | accepted_work | platform_deploy_commit | platform_deploy_after | world_resource_up
    target: str  # registered probe ID, project/TZ, exact SHA or UTC timestamp
    authority: str  # probe_registry | fleet_task_events | platform_guard


@dataclass(frozen=True)
class VerifiedEvidence:
    block_event_id: int
    kind: str  # probe_pass | work_accepted | platform_deployed
    subject: str  # probe ID, work reference, or deployed SHA
    reference: str  # durable event/guard receipt identifier for the audit log
    issuer: str  # fleet_probe | fleet_acceptance | platform_guard
    satisfied: bool
    event_id: int  # durable fleet evidence event ID, later than the block
    occurred_at: str  # time of the successful probe/acceptance/deployment
    checked_at: str  # time the trusted reader verified the present receipt


@dataclass(frozen=True)
class Evaluation:
    status: DecisionStatus
    reason: str
    block_event_id: int | None = None
    evidence_refs: tuple[str, ...] = ()


# Only trusted, *structured* predicates have automation authority. A string
# extracted from a free-form reason never becomes a predicate by itself.
_AUTHORITIES = {
    "until_probe": ("probe_registry", "probe_pass", "fleet_probe"),
    "accepted_work": ("fleet_task_events", "work_accepted", "fleet_acceptance"),
    "platform_deploy_commit": ("platform_guard", "platform_deployed", "platform_guard"),
    "platform_deploy_after": ("platform_guard", "platform_deployed", "platform_guard"),
    # The server's own sensor (world.py), not an agent's note: target is a resource name.
    "world_resource_up": ("world_sensor", "resource_up", "fleet_world"),
}
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_PROBE = re.compile(r"probe:[0-9a-f]{64}\Z")
_WORK = re.compile(r"[^/]+/[^/]+\Z")


def _utc(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _result(
    status: DecisionStatus, reason: str, event: BlockingEvent | None,
    evidence: VerifiedEvidence | None = None,
) -> Evaluation:
    return Evaluation(
        status, reason, event.event_id if event else None,
        (evidence.reference,) if evidence and evidence.reference else (),
    )


def evaluate_reactivation(
    task: TaskSnapshot,
    event: BlockingEvent | None,
    predicate: ResolutionPredicate | None,
    evidence: VerifiedEvidence | None,
    *,
    now: datetime,
    max_evidence_age: timedelta = timedelta(minutes=5),
) -> Evaluation:
    """Classify a verified automatic block without performing any action.

    'blocked' denotes a known prohibition or a known unsatisfied prerequisite.
    'unknown' denotes incomplete, stale, conflicting, or untrusted information.
    Only 'eligible' may be considered by a separately authorized integrator.
    """
    if event is None:
        return _result(DecisionStatus.UNKNOWN, "missing_transition_event", None)
    if event.kind == "freeze":
        return _result(DecisionStatus.BLOCKED, "explicit_freeze", event)
    if event.kind != "block":
        return _result(DecisionStatus.UNKNOWN, "not_a_block", event)
    if event.origin in {"owner", "guard", "manual"}:
        return _result(DecisionStatus.BLOCKED, "nonautomatic_block", event)
    if event.gate in {"security", "manual"}:
        return _result(DecisionStatus.BLOCKED, "protected_gate", event)
    if event.origin != "fleet_auto" or event.gate not in {"operational", "dependency", "repair"}:
        return _result(DecisionStatus.UNKNOWN, "untrusted_block_origin", event)
    if task.state != "frozen":
        return _result(DecisionStatus.UNKNOWN, "task_not_frozen", event)
    if (not task.project or not task.tz or task.project != event.project
            or task.tz != event.tz or event.event_id <= 0):
        return _result(DecisionStatus.UNKNOWN, "task_event_mismatch", event)
    # The snapshot's latest transition includes block/freeze/thaw/reactivate/
    # approve/auto_reactivate, not the latest event of any kind.
    if task.latest_transition_id != event.event_id:
        return _result(DecisionStatus.UNKNOWN, "transition_changed", event)
    blocked_at = _utc(event.occurred_at)
    if blocked_at is None or now.tzinfo is None or now.utcoffset() is None:
        return _result(DecisionStatus.UNKNOWN, "invalid_clock", event)
    current = now.astimezone(timezone.utc)
    if blocked_at > current:
        return _result(DecisionStatus.UNKNOWN, "future_block_event", event)

    if predicate is None or predicate.block_event_id != event.event_id:
        return _result(DecisionStatus.UNKNOWN, "missing_or_unbound_predicate", event)
    expected = _AUTHORITIES.get(predicate.kind)
    if expected is None or predicate.authority != expected[0]:
        return _result(DecisionStatus.UNKNOWN, "unknown_or_untrusted_predicate", event)
    if predicate.kind == "until_probe" and not _PROBE.fullmatch(predicate.target):
        return _result(DecisionStatus.UNKNOWN, "unregistered_probe_target", event)
    if predicate.kind == "accepted_work":
        if (not _WORK.fullmatch(predicate.target)
                or predicate.target == f"{task.project}/{task.tz}"):
            return _result(DecisionStatus.BLOCKED, "nonindependent_work", event)
    if (predicate.kind == "platform_deploy_commit"
            and not _SHA40.fullmatch(predicate.target)):
        return _result(DecisionStatus.UNKNOWN, "invalid_deployment_commit", event)
    if predicate.kind == "platform_deploy_after" and _utc(predicate.target) is None:
        return _result(DecisionStatus.UNKNOWN, "invalid_deployment_boundary", event)

    if evidence is None:
        return _result(DecisionStatus.UNKNOWN, "missing_evidence", event)
    if (evidence.block_event_id != event.event_id
            or evidence.event_id <= event.event_id
            or evidence.kind != expected[1] or evidence.issuer != expected[2]
            or not evidence.reference.strip()):
        return _result(DecisionStatus.UNKNOWN, "untrusted_or_unbound_evidence", event, evidence)
    observed = _utc(evidence.occurred_at)
    checked = _utc(evidence.checked_at)
    if (observed is None or checked is None or max_evidence_age <= timedelta(0)
            or observed <= blocked_at or observed > checked or checked > current
            or current - checked > max_evidence_age):
        return _result(DecisionStatus.UNKNOWN, "stale_or_invalid_evidence_time", event, evidence)

    if predicate.kind == "platform_deploy_after":
        if not _SHA40.fullmatch(evidence.subject):
            return _result(DecisionStatus.UNKNOWN, "invalid_deployment_receipt", event, evidence)
        threshold = _utc(predicate.target)
        if threshold is None or observed <= threshold:
            return _result(DecisionStatus.BLOCKED, "deployment_predicate_unmet", event, evidence)
    elif evidence.subject != predicate.target:
        return _result(DecisionStatus.UNKNOWN, "evidence_subject_mismatch", event, evidence)
    if not evidence.satisfied:
        return _result(DecisionStatus.BLOCKED, "predicate_not_satisfied", event, evidence)
    return _result(DecisionStatus.ELIGIBLE, "verified_block_resolved", event, evidence)
