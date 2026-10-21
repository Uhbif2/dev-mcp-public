"""Pure terminal rules for fleet claims, accepted work and failed application.

There are deliberately no Store writes here. Queue/judgment adapters must record
the returned events and create repair tasks *atomically* with the state change.
Existing claims have no outcome/reason columns: record a `claim_terminal`
event with `claim_id`, `outcome`, `reason` when closing one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


CLAIM_OUTCOMES = frozenset({"submitted", "released", "expired", "failed"})
CLAIM_REASONS = {
    "submitted": frozenset({"acceptance_passed", "review_accepted", "review_rejected", "review_escalated"}),
    "released": frozenset({"work_finished", "blocked_external", "handoff", "voluntary_release"}),
    "expired": frozenset({"lease_timeout", "worker_lost"}),
    "failed": frozenset({"execution_failed", "guard_revoked", "platform_failed"}),
}


@dataclass(frozen=True)
class ClaimViolation:
    code: str
    claim_id: int
    detail: str


def terminal_event(claim_id: int, outcome: str, reason: str) -> dict[str, Any]:
    """Validate a typed closure; callers persist this in the same transaction."""
    if not isinstance(claim_id, int) or claim_id <= 0:
        raise ValueError("claim_id must be a positive integer")
    if outcome not in CLAIM_OUTCOMES:
        raise ValueError(f"invalid claim outcome: {outcome!r}")
    if reason not in CLAIM_REASONS[outcome]:
        raise ValueError(f"invalid {outcome} reason: {reason!r}")
    return {"kind": "claim_terminal", "claim_id": claim_id, "outcome": outcome, "reason": reason}


def _event_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return {}
    if isinstance(payload, Mapping):
        return payload
    return row


def check_invariant(
    claims: Iterable[Mapping[str, Any]],
    terminal_events: Iterable[Mapping[str, Any]] = (),
) -> list[ClaimViolation]:
    """Diagnose every claim closure; never infer an outcome from legacy state.

    `claims` may be SQLite Row converted via dict(row). `terminal_events`
    accepts both `terminal_event` payloads and Store events containing a
    JSON `payload`. An active claim must have neither release time nor outcome.
    """
    terminal: dict[int, dict[str, str]] = {}
    duplicates: set[int] = set()
    for row in terminal_events:
        if row.get("kind", "claim_terminal") != "claim_terminal":
            continue
        data = _event_payload(row)
        try:
            claim_id = int(data.get("claim_id"))
        except (TypeError, ValueError):
            continue
        if claim_id in terminal:
            duplicates.add(claim_id)
        terminal[claim_id] = {
            "outcome": str(data.get("outcome") or ""),
            "reason": str(data.get("reason") or ""),
        }

    violations: list[ClaimViolation] = []
    for row in claims:
        claim_id = int(row["id"])
        active = str(row.get("state") or "") == "active"
        closed_at = row.get("released_at")
        end = terminal.get(claim_id, {})
        outcome = str(row.get("outcome") or end.get("outcome") or "")
        reason = str(row.get("reason") or end.get("reason") or "")
        if active:
            if closed_at or outcome or reason:
                violations.append(ClaimViolation("active_has_terminal", claim_id, "active claim has closure evidence"))
            continue
        if not closed_at:
            violations.append(ClaimViolation("missing_release_time", claim_id, "closed claim has no released_at"))
        if not outcome or not reason:
            violations.append(ClaimViolation("missing_terminal", claim_id, "closed claim has no typed outcome and reason"))
        elif outcome not in CLAIM_OUTCOMES or reason not in CLAIM_REASONS[outcome]:
            violations.append(ClaimViolation("invalid_terminal", claim_id, f"{outcome}:{reason}"))
        if claim_id in duplicates:
            violations.append(ClaimViolation("duplicate_terminal", claim_id, "multiple closure records for a claim"))
    return violations


@dataclass(frozen=True)
class AcceptedFacts:
    acceptance_output_sha: str = ""
    review_reject_id: int = 0
    spec_sha: str = ""


@dataclass(frozen=True)
class ReopenDecision:
    allowed: bool
    event: str
    changes: tuple[str, ...]


def reopen_decision(state: str, accepted: AcceptedFacts, current: AcceptedFacts) -> ReopenDecision:
    """An accepted item is sticky unless previously unseen, server-verified facts exist.

    The caller supplies the last accepted evidence snapshot and latest verified
    evidence. Review rejection IDs are monotonically increasing event IDs.
    Missing/stale fields are *not* evidence of change.
    """
    if state != "accepted":
        return ReopenDecision(True, "reopened", ())
    changed = []
    if current.acceptance_output_sha and current.acceptance_output_sha != accepted.acceptance_output_sha:
        changed.append("acceptance_output_sha")
    if current.review_reject_id > accepted.review_reject_id:
        changed.append("review_reject_id")
    if current.spec_sha and current.spec_sha != accepted.spec_sha:
        changed.append("spec_sha")
    return ReopenDecision(bool(changed), "reopened" if changed else "reopen_refused", tuple(changed))


@dataclass(frozen=True)
class PlatformRepair:
    project: str
    tz: str
    dedup_key: str
    title: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class ApplyFailureDecision:
    state: str
    event: str
    repair: PlatformRepair | None


def apply_failure_decision(
    *, state: str, project: str, tz: str, errors: Iterable[str],
    existing_keys: Iterable[str] = (), accepted_version: str = "", phase: str = "triage_apply",
) -> ApplyFailureDecision:
    """Return a repair request, not a retry or reopening of an accepted decision.

    The platform must commit the `dedup_key` under a UNIQUE constraint or in
    the same serialized transaction as repair creation. Repeated observations
    of the same accepted version cannot produce a second platform task.
    """
    if state != "accepted":
        raise ValueError("application failure belongs to an accepted decision")
    if not project or not tz or not phase:
        raise ValueError("project, tz and phase are required")
    problems = tuple(str(error).strip() for error in errors if str(error).strip())
    if not problems:
        raise ValueError("application failure requires error evidence")
    raw = json.dumps([project, tz, phase, accepted_version], ensure_ascii=False, separators=(",", ":"))
    key = "apply:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if key in set(existing_keys):
        return ApplyFailureDecision("accepted", "apply_failure_duplicate", None)
    return ApplyFailureDecision(
        "accepted", "apply_failure_repair",
        PlatformRepair(
            project="Fleet Platform",
            tz=f"repair:{key[6:22]}",
            dedup_key=key,
            title=f"Исправить применение {project}/{tz} ({phase})",
            evidence={
                "source_project": project, "source_tz": tz, "phase": phase,
                "accepted_version": accepted_version, "errors": list(problems),
            },
        ),
    )
