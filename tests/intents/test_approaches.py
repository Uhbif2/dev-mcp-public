"""Independent measured spike comparison, synthetic only."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.intents.objective import (
    EvidenceAuthority, ObjectiveError, ObjectiveSpec, Scenario, make_corpus,
)
from dev_mcp.intents.approaches import (
    Approach, Critique, Metric, choose_approach, measure_spikes, permit_code_work,
    verify_spikes, _digest,
)


def dataset():
    obj = ObjectiveSpec("intent:executable_goal", "req-sha-v1", "Run scenarios against source",
                        "requirement-owner", hashlib.sha256(b"snapshot").hexdigest())
    cases = (
        Scenario("pass", "positive", {"input": 7}, {"ok": 7}, "independent-test-writer"),
        Scenario("deny", "negative", {"input": -1}, {"error": "negative"}, "independent-test-writer"),
    )
    corpus = make_corpus(obj, cases, test_author="independent-test-writer")
    issuer = EvidenceAuthority(b"only-for-unit-tests-server-key-123456789")
    baseline = issuer.run_baseline(
        corpus, precode_subject_version=corpus.objective.subject_sha256, env_version="env-v1",
        verifier_id="server-baseline-runner", implementation_author="coder",
        runner=lambda case: {"unimplemented": True},
    )
    ready = issuer.ready(corpus, baseline, implementation_author="coder")
    approaches = (
        Approach("indexed", "Use a stable local index", "research-A"),
        Approach("scan", "Sequential comparison", "research-B"),
    )
    return corpus, issuer, baseline, ready, approaches


def measured():
    corpus, issuer, baseline, ready, approaches = dataset()
    spike = measure_spikes(
        issuer, corpus, ready, baseline, approaches, Metric("latency", "milliseconds", "min"),
        runner_id="independent-measurement-runner", implementation_author="coder",
        measure=lambda a, m, i: (20.0 if a.approach_id == "indexed" else 50.0)
                                + [-0.5, 0.0, 0.5][i],
    )
    return corpus, issuer, baseline, ready, approaches, spike


def attested_critic(proposal):
    return Critique("model-B", _digest(proposal), "approve",
                    "Comparison uses independent stable measurements")


def decision_ready():
    corpus, issuer, baseline, ready, approaches, spike = measured()
    decision = choose_approach(
        issuer, ready, spike, proposer_model="model-A",
        critic_model="model-B", rationale="The median is lower under matched workload",
        critic=attested_critic,
    )
    return corpus, issuer, baseline, ready, spike, decision


def test_choose_measured_best_spike_and_issue_structured_permit():
    corpus, issuer, baseline, ready, spike, decision = decision_ready()
    assert decision.chosen_approach == "indexed"
    assert decision.alternatives == ("scan",)
    assert decision.scores == (("indexed", 20.0), ("scan", 50.0))
    assert decision.spike_digest == spike.digest
    assert decision.critique.model_id == "model-B"
    assert decision.digest and decision.signature
    permit = permit_code_work(
        issuer, corpus, baseline, ready, spike, decision,
        implementation_author="coder",
    )
    assert permit.ready and permit.chosen_approach == "indexed"
    assert permit.objective_version == corpus.objective.version
    assert permit.baseline_digest == baseline.run_digest
    assert permit.decision_digest == decision.digest


def test_two_or_three_approaches_not_one_or_four():
    corpus, issuer, baseline, ready, approaches = dataset()
    for bad in ((approaches[0],), approaches * 2):
        with pytest.raises(ObjectiveError):
            measure_spikes(issuer, corpus, ready, baseline, bad,
                           Metric("latency", "ms", "min"),
                           runner_id="measure-runner", implementation_author="coder",
                           measure=lambda *_: 20.0)
    third = Approach("parallel", "Two workers", "research-C")
    result = measure_spikes(
        issuer, corpus, ready, baseline, (*approaches, third), Metric("quality", "score", "max"),
        runner_id="measure-runner", implementation_author="coder",
        measure=lambda a, m, i: {"indexed": 50.0, "scan": 70.0, "parallel": 90.0}[a.approach_id],
    )
    decision = choose_approach(
        issuer, ready, result, proposer_model="model-A", critic_model="model-B",
        rationale="Highest stable quality score wins", critic=attested_critic,
    )
    assert decision.chosen_approach == "parallel"


def test_random_or_unstable_spike_is_not_accepted():
    corpus, issuer, baseline, ready, approaches = dataset()
    with pytest.raises(ObjectiveError, match="unstable"):
        measure_spikes(
            issuer, corpus, ready, baseline, approaches, Metric("duration", "ms", "min"),
            runner_id="measurer", implementation_author="coder",
            measure=lambda a, m, i: (10.0, 100.0, 3.0)[i],
        )
    with pytest.raises(ObjectiveError, match="finite"):
        measure_spikes(
            issuer, corpus, ready, baseline, approaches, Metric("duration", "ms", "min"),
            runner_id="measurer", implementation_author="coder",
            measure=lambda a, m, i: float("nan"),
        )


def test_tied_measurements_do_not_choose_an_arbitrary_winner():
    corpus, issuer, baseline, ready, approaches = dataset()
    spike = measure_spikes(
        issuer, corpus, ready, baseline, approaches, Metric("latency", "ms", "min"),
        runner_id="measurer", implementation_author="coder",
        measure=lambda a, m, i: 100.0 if a.approach_id == "scan" else 98.0,
    )
    with pytest.raises(ObjectiveError, match="indistinguishable"):
        choose_approach(
            issuer, ready, spike, proposer_model="model-A", critic_model="model-B",
            rationale="A very small unverified difference", critic=attested_critic,
        )


def test_measurement_runner_not_self_author_and_min_repeats_enforced():
    corpus, issuer, baseline, ready, approaches = dataset()
    with pytest.raises(ObjectiveError, match="independent"):
        measure_spikes(
            issuer, corpus, ready, baseline, approaches, Metric("latency", "ms", "min"),
            runner_id="coder", implementation_author="coder",
            measure=lambda *_: 3,
        )
    with pytest.raises(ObjectiveError, match="repeats"):
        measure_spikes(
            issuer, corpus, ready, baseline, approaches, Metric("latency", "ms", "min"),
            runner_id="measurement-runner", implementation_author="coder",
            measure=lambda *_: 3, repeats=1,
        )


def test_critique_required_from_different_model_and_exact_digest():
    corpus, issuer, baseline, ready, approaches, spike = measured()
    args = dict(proposer_model="model-A", critic_model="model-B", rationale="objective median")
    with pytest.raises(ObjectiveError, match="different critique model"):
        choose_approach(issuer, ready, spike, proposer_model="model-A", critic_model="model-A",
                        rationale="not independent", critic=attested_critic)
    with pytest.raises(ObjectiveError, match="exact proposal"):
        choose_approach(issuer, ready, spike, **args,
                        critic=lambda p: Critique("model-B", "wrong", "approve", "unsupported"))
    with pytest.raises(ObjectiveError, match="rejected"):
        choose_approach(issuer, ready, spike, **args,
                        critic=lambda p: Critique("model-B", _digest(p), "reject", "bad fit"))


def test_spike_and_decision_tampering_fail_closed():
    corpus, issuer, baseline, ready, spike, decision = decision_ready()
    with pytest.raises(ObjectiveError):
        verify_spikes(issuer, ready, replace(spike, signature="broken"))
    with pytest.raises(ObjectiveError, match="fabricated"):
        verify_spikes(issuer, ready, replace(spike, results=(
            replace(spike.results[0], median=100.0), spike.results[1])))
    for changed in (
        replace(decision, chosen_approach="scan"),
        replace(decision, scores=(("scan", 10.0), ("indexed", 20.0))),
        replace(decision, signature="broken"),
        replace(decision, critique=replace(decision.critique, model_id="model-A")),
    ):
        with pytest.raises(ObjectiveError):
            permit_code_work(issuer, corpus, baseline, ready, spike, changed,
                             implementation_author="coder")
    with pytest.raises(ObjectiveError, match="signature"):
        permit_code_work(issuer, corpus, baseline, ready, replace(spike, digest="unknown"),
                         decision, implementation_author="coder")
    with pytest.raises(ObjectiveError, match="independent"):
        permit_code_work(issuer, corpus, baseline, ready, spike, decision,
                         implementation_author=spike.runner_id)


def test_changed_requirement_is_not_valid_for_prior_measurement():
    corpus, issuer, baseline, ready, spike, decision = decision_ready()
    new_obj = replace(corpus.objective, requirement_version="req-v2")
    changed = make_corpus(new_obj, corpus.scenarios, test_author=corpus.test_author)
    with pytest.raises(ObjectiveError, match="objective"):
        permit_code_work(issuer, changed, baseline, ready, spike, decision,
                         implementation_author="coder")


def test_empty_goal_blocked_before_any_spike():
    corpus, issuer, baseline, ready, approaches = dataset()
    from dev_mcp.intents.objective import code_work_ready
    assert not code_work_ready(permit=None, corpus=corpus, evidence=baseline,
                               authority=issuer, implementation_author="coder")
    with pytest.raises(ObjectiveError, match="objective is not ready"):
        measure_spikes(issuer, corpus, replace(ready, ready=False), baseline,
                       approaches, Metric("latency", "ms", "min"),
                       runner_id="server-runner", implementation_author="coder",
                       measure=lambda *_: 1)
