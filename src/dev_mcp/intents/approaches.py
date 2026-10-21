"""Measured competition of approaches, independent second-model critique.

The code-work permit depends on signed scenario baseline AND measured spikes.
Only trusted orchestration invokes measurement runners and critic adapters;
user/agent declarations alone cannot produce an admitted decision.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

from .objective import (
    BaselineEvidence, EvidenceAuthority, GoldenCorpus, ObjectiveError, ObjectiveReady,
    _digest, _nonempty, code_work_ready,
)

MAX_APPROACHES = 3
MIN_SAMPLES = 3
MAX_SAMPLES = 12
MAX_SPREAD_FRACTION = 0.20
MIN_WIN_MARGIN_FRACTION = 0.05


@dataclass(frozen=True)
class Approach:
    approach_id: str
    hypothesis: str
    author_id: str

    def validate(self) -> None:
        _nonempty(self.approach_id, "approach_id")
        _nonempty(self.hypothesis, "hypothesis", limit=4096)
        _nonempty(self.author_id, "approach author")


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str
    direction: Literal["min", "max"]

    def validate(self) -> None:
        _nonempty(self.name, "metric")
        _nonempty(self.unit, "unit")
        if self.direction not in {"min", "max"}:
            raise ObjectiveError("metric direction must be min or max")


@dataclass(frozen=True)
class SpikeResult:
    approach_id: str
    values: tuple[float, ...]
    median: float
    spread: float


@dataclass(frozen=True)
class SpikeEvidence:
    objective_version: str
    corpus_sha256: str
    baseline_digest: str
    precode_subject_version: str
    env_version: str
    runner_id: str
    metric: Metric
    approaches: tuple[Approach, ...]
    results: tuple[SpikeResult, ...]
    digest: str
    signature: str


@dataclass(frozen=True)
class Critique:
    model_id: str
    proposal_digest: str
    verdict: Literal["approve", "reject"]
    reasoning: str


@dataclass(frozen=True)
class ApproachDecision:
    objective_version: str
    baseline_digest: str
    spike_digest: str
    chosen_approach: str
    alternatives: tuple[str, ...]
    metric: Metric
    scores: tuple[tuple[str, float], ...]
    rationale: str
    proposer_model: str
    critique: Critique
    digest: str
    signature: str


@dataclass(frozen=True)
class CodeWorkPermit:
    objective_version: str
    corpus_sha256: str
    baseline_digest: str
    spike_digest: str
    decision_digest: str
    chosen_approach: str
    ready: bool = True


def _spike_payload(spike: SpikeEvidence) -> dict[str, object]:
    return {
        "objective_version": spike.objective_version,
        "corpus_sha256": spike.corpus_sha256,
        "baseline_digest": spike.baseline_digest,
        "precode_subject_version": spike.precode_subject_version,
        "env_version": spike.env_version,
        "runner_id": spike.runner_id,
        "metric": vars(spike.metric),
        "approaches": [vars(a) for a in spike.approaches],
        "results": [vars(s) for s in spike.results],
    }


def _decision_payload(decision: ApproachDecision) -> dict[str, object]:
    return {
        "objective_version": decision.objective_version,
        "baseline_digest": decision.baseline_digest,
        "spike_digest": decision.spike_digest,
        "chosen_approach": decision.chosen_approach,
        "alternatives": decision.alternatives,
        "metric": vars(decision.metric),
        "scores": decision.scores,
        "rationale": decision.rationale,
        "proposer_model": decision.proposer_model,
        "critique": vars(decision.critique),
    }


def _samples(values: tuple[float, ...]) -> tuple[float, float]:
    if not MIN_SAMPLES <= len(values) <= MAX_SAMPLES:
        raise ObjectiveError("each spike needs 3..12 independent repeat measurements")
    if any(not isinstance(x, (int, float)) or isinstance(x, bool)
           or not math.isfinite(x) for x in values):
        raise ObjectiveError("metric must be a finite numeric measurement")
    median = statistics.median(values)
    spread = max(values) - min(values)
    if spread > MAX_SPREAD_FRACTION * max(abs(median), 1e-9):
        raise ObjectiveError("unstable/random spike measurements")
    return float(median), float(spread)


def measure_spikes(
    authority: EvidenceAuthority,
    corpus: GoldenCorpus, ready: ObjectiveReady,
    baseline: BaselineEvidence, approaches: Sequence[Approach],
    metric: Metric, *,
    runner_id: str, implementation_author: str,
    measure: Callable[[Approach, Metric, int], float], repeats: int = 3,
) -> SpikeEvidence:
    """Run all approaches against the same versioned objective with the same metric."""
    if not code_work_ready(permit=ready, corpus=corpus, evidence=baseline,
                           authority=authority, implementation_author=implementation_author):
        raise ObjectiveError("objective is not ready: golden red baseline required")
    choices = tuple(approaches)
    if not 2 <= len(choices) <= MAX_APPROACHES or len({a.approach_id for a in choices}) != len(choices):
        raise ObjectiveError("provide exactly 2 or 3 distinct approaches")
    for choice in choices:
        choice.validate()
    metric.validate()
    _nonempty(runner_id, "runner_id")
    if runner_id == implementation_author or runner_id == corpus.test_author:
        raise ObjectiveError("spike runner must be independent of tests and implementation")
    if not callable(measure) or not MIN_SAMPLES <= repeats <= MAX_SAMPLES:
        raise ObjectiveError("trusted measured spike runner and bounded repeats required")
    rows: list[SpikeResult] = []
    for choice in choices:
        values: list[float] = []
        for i in range(repeats):
            try:
                value = measure(choice, metric, i)
            except Exception as exc:
                raise ObjectiveError("spike measurement failed: " + type(exc).__name__) from exc
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ObjectiveError("non-numeric spike measurement")
            values.append(float(value))
        median, spread = _samples(tuple(values))
        rows.append(SpikeResult(choice.approach_id, tuple(values), median, spread))
    unsigned = SpikeEvidence(
        ready.objective_version, ready.corpus_sha256, ready.baseline_digest,
        ready.precode_subject_version, ready.env_version,
        runner_id, metric, choices, tuple(rows), "", "",
    )
    digest, signature = authority.sign(_spike_payload(unsigned))
    return SpikeEvidence(**{**vars(unsigned), "digest": digest, "signature": signature})


def verify_spikes(authority: EvidenceAuthority, ready: ObjectiveReady,
                  spike: SpikeEvidence) -> None:
    if (spike.objective_version != ready.objective_version
            or spike.corpus_sha256 != ready.corpus_sha256
            or spike.baseline_digest != ready.baseline_digest
            or spike.precode_subject_version != ready.precode_subject_version
            or spike.env_version != ready.env_version):
        raise ObjectiveError("spike belongs to another requirement or environment")
    spike.metric.validate()
    if not 2 <= len(spike.approaches) <= MAX_APPROACHES:
        raise ObjectiveError("invalid spike approach count")
    if len({x.approach_id for x in spike.approaches}) != len(spike.approaches):
        raise ObjectiveError("duplicate measured approach")
    if tuple(x.approach_id for x in spike.approaches) != tuple(x.approach_id for x in spike.results):
        raise ObjectiveError("missing measurements")
    for a, r in zip(spike.approaches, spike.results):
        a.validate()
        m, s = _samples(r.values)
        if r.median != m or r.spread != s:
            raise ObjectiveError("fabricated spike summary")
    authority.verify(_spike_payload(spike), spike.digest, spike.signature)


def _proposal(spike: SpikeEvidence, selected: str, alternatives: tuple[str, ...],
              scores: tuple[tuple[str, float], ...], rationale: str,
              proposer_model: str) -> dict[str, object]:
    return {
        "objective_version": spike.objective_version, "baseline_digest": spike.baseline_digest,
        "spike_digest": spike.digest, "chosen_approach": selected,
        "alternatives": alternatives, "metric": vars(spike.metric),
        "scores": scores, "rationale": rationale, "proposer_model": proposer_model,
    }


def choose_approach(
    authority: EvidenceAuthority, ready: ObjectiveReady, spike: SpikeEvidence, *,
    proposer_model: str, critic_model: str, rationale: str,
    critic: Callable[[dict[str, object]], Critique],
) -> ApproachDecision:
    """Record a measured Decision; trusted adapter solicits a DIFFERENT model."""
    verify_spikes(authority, ready, spike)
    _nonempty(proposer_model, "proposer_model")
    _nonempty(critic_model, "critic_model")
    _nonempty(rationale, "rationale", limit=4096)
    if proposer_model == critic_model:
        raise ObjectiveError("a different critique model is required")
    if not callable(critic):
        raise ObjectiveError("external second-model critique required")
    descending = spike.metric.direction == "max"
    ranked = sorted(spike.results, key=lambda x: (-x.median if descending else x.median,
                                                    x.approach_id))
    # Choosing lexicographically on a statistical tie would masquerade as
    # evidence-based selection. An unresolved comparison needs another spike.
    win_margin = abs(ranked[0].median - ranked[1].median)
    if win_margin < MIN_WIN_MARGIN_FRACTION * max(abs(ranked[1].median), 1e-9):
        raise ObjectiveError("approaches are indistinguishable by measured margin")
    selected = ranked[0].approach_id
    alternatives = tuple(x.approach_id for x in ranked[1:])
    scores = tuple((x.approach_id, x.median) for x in ranked)
    proposal = _proposal(spike, selected, alternatives, scores, rationale, proposer_model)
    proposal_digest = _digest(proposal)
    result = critic(proposal)
    if (not isinstance(result, Critique) or result.model_id != critic_model
            or result.proposal_digest != proposal_digest
            or result.verdict not in {"approve", "reject"}):
        raise ObjectiveError("critic did not attest this exact proposal")
    _nonempty(result.reasoning, "critic reasoning", limit=4096)
    if result.verdict != "approve":
        raise ObjectiveError("independent model rejected measured approach")
    unsigned = ApproachDecision(
        spike.objective_version, spike.baseline_digest, spike.digest, selected,
        alternatives, spike.metric, scores, rationale, proposer_model,
        result, "", "",
    )
    digest, signature = authority.sign(_decision_payload(unsigned))
    return ApproachDecision(**{**vars(unsigned), "digest": digest, "signature": signature})


def permit_code_work(
    authority: EvidenceAuthority, corpus: GoldenCorpus, baseline: BaselineEvidence,
    objective_ready: ObjectiveReady, spike: SpikeEvidence, decision: ApproachDecision, *,
    implementation_author: str,
) -> CodeWorkPermit:
    """Fail closed until objective AND independently critiqued decision are ready."""
    if not code_work_ready(permit=objective_ready, corpus=corpus, evidence=baseline,
                           authority=authority, implementation_author=implementation_author):
        raise ObjectiveError("objective not ready")
    verify_spikes(authority, objective_ready, spike)
    if implementation_author == spike.runner_id:
        raise ObjectiveError("code author must be independent from measured spike runner")
    if (decision.objective_version != spike.objective_version
            or decision.baseline_digest != spike.baseline_digest
            or decision.spike_digest != spike.digest or
            decision.metric != spike.metric):
        raise ObjectiveError("decision is stale or based on a different spike")
    if (decision.proposer_model == decision.critique.model_id or
            decision.critique.verdict != "approve"):
        raise ObjectiveError("independent approval missing")
    choices = {x.approach_id for x in spike.approaches}
    if (decision.chosen_approach not in choices or
            set(decision.alternatives) != choices - {decision.chosen_approach}):
        raise ObjectiveError("decision alternatives incomplete")
    scores = tuple((x.approach_id, x.median) for x in
                   sorted(spike.results, key=lambda x: (
                       -x.median if spike.metric.direction == "max" else x.median, x.approach_id)))
    if decision.scores != scores or decision.chosen_approach != scores[0][0]:
        raise ObjectiveError("decision is not selected by measured evidence")
    proposal = _proposal(spike, decision.chosen_approach, decision.alternatives,
                         decision.scores, decision.rationale, decision.proposer_model)
    if decision.critique.proposal_digest != _digest(proposal):
        raise ObjectiveError("critique for a different decision")
    authority.verify(_decision_payload(decision), decision.digest, decision.signature)
    return CodeWorkPermit(
        spike.objective_version, spike.corpus_sha256, spike.baseline_digest,
        spike.digest, decision.digest, decision.chosen_approach,
    )
