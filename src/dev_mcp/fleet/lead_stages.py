"""Typed Nova-style delivery stages for an O-ID, separate from witness cell levels.

L0: reproduced symptom; L1: tested patch; L2: independently reviewed clean
main SHA; L3: authorised signed and published artifact; L4: installed version,
native user action and negative control.  An accept event is NOT a receipt.
Only structured, O-ID-bound receipts are admissible, never text in a note.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class LeadStage(IntEnum):
    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4

    @classmethod
    def parse(cls, value: str) -> "LeadStage":
        try:
            return cls[str(value).upper()]
        except KeyError as exc:
            raise ValueError(f"неизвестная стадия {value!r}") from exc


@dataclass(frozen=True)
class StageReceipt:
    """Server-recorded proof; all fields must be explicitly present in event data."""

    stage: LeadStage
    obligation: str
    sha: str = ""
    source_sha: str = ""  # Proven parent, when merge/release changes the artifact hash.
    reference: str = ""
    test_passed: bool = False
    review_accepted: bool = False
    integrated: bool = False
    signed: bool = False
    published: bool = False
    authorised: bool = False
    installed_version: str = ""
    native_action: str = ""
    negative_control: bool = False
    works_live: bool = False


@dataclass(frozen=True)
class StageVerdict:
    reached: LeadStage
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def _receipt(data: dict[str, Any], *, event_kind: str) -> StageReceipt | None:
    obligation = data.get("obligation")
    if not isinstance(obligation, str) or not obligation.startswith("O-"):
        return None
    try:
        stage = LeadStage.L4 if event_kind == "witness_result" else LeadStage.parse(data["stage"])
    except (KeyError, ValueError, TypeError):
        return None
    # A native witness can provide L4 evidence but cannot stand in for a
    # separately authorised signed publication (L3).
    if event_kind == "witness_result" and data.get("verdict") != "works_live":
        return None
    return StageReceipt(
        stage=stage, obligation=obligation,
        sha=str(data.get("sha") or data.get("artifact_sha") or ""),
        source_sha=str(data.get("source_sha") or ""),
        reference=str(data.get("receipt") or data.get("reference") or ""),
        test_passed=data.get("test_passed") is True,
        review_accepted=data.get("review_accepted") is True,
        integrated=data.get("integrated") is True,
        signed=data.get("signed") is True,
        published=data.get("published") is True,
        authorised=data.get("authorised") is True,
        installed_version=str(data.get("installed_version") or ""),
        native_action=str(data.get("native_action") or ""),
        negative_control=data.get("negative_control") is True,
        works_live=data.get("works_live") is True or
                   (event_kind == "witness_result" and data.get("verdict") == "works_live"),
    )


def receipts(tx: Any, project: str, obligation: str) -> list[StageReceipt]:
    """Read only bound structured evidence, ignoring malformed/foreign witnesses."""
    rows = tx.query(
        "SELECT kind,payload FROM events WHERE project=? AND kind IN "
        "('lead_stage_evidence','witness_result') ORDER BY id", (project,))
    out: list[StageReceipt] = []
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        proof = _receipt(data, event_kind=str(row["kind"]))
        if proof is not None and proof.obligation == obligation:
            out.append(proof)
    return out


def validate_transition(source: str, target: str, evidence: list[StageReceipt]) -> StageVerdict:
    """Validate the entire ordered chain, including stages before 'source'.

    The previous level in a submitted note is a claim, not proof. All receipts
    must refer to the same exact integrated artifact SHA.
    """
    try:
        start, stop = LeadStage.parse(source or "L0"), LeadStage.parse(target)
    except ValueError as exc:
        return StageVerdict(LeadStage.L0, str(exc))
    if stop <= start:
        return StageVerdict(start, f"переход {start.name}→{stop.name} не повышает уровень")
    sha = ""
    reached = LeadStage.L0
    subjects = {r.obligation for r in evidence}
    if len(subjects) > 1:
        return StageVerdict(reached, "квитанции относятся к разным O-ID")
    for level in list(LeadStage)[1: stop.value + 1]:
        candidates = [r for r in evidence if r.stage == level and r.sha and r.reference
                      and (not sha or (r.source_sha == sha if r.source_sha else r.sha == sha))]
        if level == LeadStage.L1:
            candidates = [r for r in candidates if r.test_passed]
            description = "L1: patch SHA, успешный тест и квитанция"
        elif level == LeadStage.L2:
            candidates = [r for r in candidates if r.review_accepted and r.integrated]
            description = "L2: независимый review и clean main с exact SHA"
        elif level == LeadStage.L3:
            candidates = [r for r in candidates if r.signed and r.published and r.authorised]
            description = "L3: разрешённый подписанный опубликованный артефакт (без works_live)"
        else:
            candidates = [r for r in candidates if r.installed_version and r.native_action
                          and r.negative_control and r.works_live]
            description = "L4: installed version, native works_live и отрицательный контроль"
        if not candidates:
            return StageVerdict(reached, f"нет проверяемой квитанции {description}")
        sha = candidates[-1].sha
        reached = level
    return StageVerdict(reached)


def verified_stage(tx: Any, project: str, obligation: str) -> LeadStage:
    """Highest continuously proven stage, independent of submit/accept counts."""
    evidence = receipts(tx, project, obligation)
    for stage in (LeadStage.L4, LeadStage.L3, LeadStage.L2, LeadStage.L1):
        if validate_transition("L0", stage.name, evidence).ok:
            return stage
    return LeadStage.L0
