"""Synthetic golden scenario tests; no production sessions or external calls."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dev_mcp.intents.objective import (
    BaselineEvidence, EvidenceAuthority, GoldenStore, ObjectiveError, ObjectiveSpec,
    Scenario, code_work_ready, make_corpus,
)


def data():
    objective = ObjectiveSpec(
        "root:executable_goal", "req-v1", "Output transformed correctly with no prohibited output",
        "objective-owner", hashlib.sha256(b"v1 source").hexdigest(),
    )
    cases = (
        Scenario("positive-1", "positive", {"input": "a"}, {"ok": "a"}, "golden-author"),
        Scenario("negative-1", "negative", {"input": "bad"}, {"error": "denied"}, "golden-author"),
    )
    corpus = make_corpus(objective, cases, test_author="golden-author")
    authority = EvidenceAuthority(b"test-only-independent-evidence-key-32bytes")
    return corpus, authority


def produce(corpus=None, authority=None, runner=None):
    default_corpus, default_authority = data()
    corpus = corpus or default_corpus
    authority = authority or default_authority
    if runner is None:
        runner = lambda case: {"wrong": case.scenario_id}
    baseline = authority.run_baseline(
        corpus, precode_subject_version=corpus.objective.subject_sha256,
        env_version="test-python-3.13", verifier_id="server-test-runner",
        runner=runner, implementation_author="coder",
    )
    return corpus, authority, baseline


def test_objective_ready_requires_signed_red_scenarios():
    corpus, authority, baseline = produce()
    ready = authority.ready(corpus, baseline, implementation_author="coder")
    assert ready.ready and ready.objective_version == corpus.objective.version
    assert ready.corpus_sha256 == corpus.corpus_sha256
    assert baseline.red_count == 2
    assert baseline.precode_subject_version == corpus.objective.subject_sha256
    assert len(baseline.observations) == 2
    assert baseline.run_digest and baseline.signature
    assert code_work_ready(
        permit=ready, corpus=corpus, evidence=baseline,
        authority=authority, implementation_author="coder",
    )
    assert not code_work_ready(
        permit=None, corpus=corpus, evidence=baseline,
        authority=authority, implementation_author="coder",
    )


def test_empty_oversized_and_nonjson_objective_rejected():
    corpus, authority = data()
    with pytest.raises(ObjectiveError, match="non-empty"):
        make_corpus(replace(corpus.objective, requirement=""), corpus.scenarios,
                    test_author=corpus.test_author)
    with pytest.raises(ObjectiveError, match="bounded"):
        make_corpus(replace(corpus.objective, requirement="x" * 16385), corpus.scenarios,
                    test_author=corpus.test_author)
    with pytest.raises(ObjectiveError, match="structured"):
        make_corpus(corpus.objective, (
            replace(corpus.scenarios[0], input="raw text"), corpus.scenarios[1]),
            test_author=corpus.test_author)
    with pytest.raises(ObjectiveError):
        make_corpus(corpus.objective, (
            replace(corpus.scenarios[0], input={"not-json": object()}), corpus.scenarios[1]),
            test_author=corpus.test_author)


def test_no_positive_no_negative_and_flood_rejected():
    corpus, authority = data()
    with pytest.raises(ObjectiveError, match="positive"):
        make_corpus(corpus.objective, (corpus.scenarios[1], replace(corpus.scenarios[1],
                        scenario_id="negative-2")), test_author="golden-author")
    with pytest.raises(ObjectiveError, match="negative"):
        make_corpus(corpus.objective, (corpus.scenarios[0], replace(corpus.scenarios[0],
                        scenario_id="positive-2")), test_author="golden-author")
    huge = [
        Scenario("p" + str(i), "positive" if i == 0 else "negative",
                 {"blob": "a" * 5000}, {"ok": i}, "golden-author")
        for i in range(129)
    ]
    with pytest.raises(ObjectiveError, match="128"):
        make_corpus(corpus.objective, huge, test_author="golden-author")


def test_mismatched_authors_rejected():
    corpus, authority = data()
    with pytest.raises(ObjectiveError, match="author mismatch"):
        make_corpus(corpus.objective, (replace(corpus.scenarios[0], author_id="coder"),
                                       corpus.scenarios[1]), test_author="golden-author")
    with pytest.raises(ObjectiveError, match="independent"):
        authority.run_baseline(corpus, precode_subject_version=corpus.objective.subject_sha256, env_version="env",
                               verifier_id="server-test-runner", runner=lambda _: {},
                               implementation_author="golden-author")
    with pytest.raises(ObjectiveError, match="independent"):
        authority.run_baseline(corpus, precode_subject_version=corpus.objective.subject_sha256, env_version="env",
                               verifier_id="coder", runner=lambda _: {},
                               implementation_author="coder")


def test_already_green_baseline_rejected():
    corpus, authority, baseline = produce(runner=lambda case: case.expected)
    assert baseline.red_count == 0
    with pytest.raises(ObjectiveError, match="already green"):
        authority.ready(corpus, baseline, implementation_author="coder")


def test_baseline_cannot_run_against_an_unrelated_source_version():
    corpus, authority = data()
    with pytest.raises(ObjectiveError, match="source snapshot"):
        authority.run_baseline(
            corpus, precode_subject_version=hashlib.sha256(b"other snapshot").hexdigest(),
            env_version="env", verifier_id="server-runner",
            runner=lambda _: {"wrong": True}, implementation_author="coder",
        )


def test_negative_only_red_is_not_enough():
    corpus, authority, baseline = produce(
        runner=lambda case: case.expected if case.kind == "positive" else {"wrong": 1})
    assert baseline.red_count == 1
    with pytest.raises(ObjectiveError, match="already green"):
        authority.ready(corpus, baseline, implementation_author="coder")


def test_tampered_baseline_and_changed_requirement_fail_closed():
    corpus, authority, baseline = produce()
    for changed in (
        replace(baseline, red_count=1),
        replace(baseline, env_version="different env"),
        replace(baseline, signature="f" * 64),
        replace(baseline, precode_subject_version="after-code"),
    ):
        with pytest.raises(ObjectiveError):
            authority.ready(corpus, changed, implementation_author="coder")
    different = make_corpus(replace(corpus.objective, requirement_version="req-v2"),
                            corpus.scenarios, test_author="golden-author")
    with pytest.raises(ObjectiveError, match="requirement changed"):
        authority.ready(different, baseline, implementation_author="coder")
    altered = make_corpus(corpus.objective,
                          (replace(corpus.scenarios[0], expected={"ok": "different"}),
                           corpus.scenarios[1]), test_author="golden-author")
    with pytest.raises(ObjectiveError):
        authority.ready(altered, baseline, implementation_author="coder")


def test_stale_signed_ready_permit_cannot_open_code_work():
    corpus, authority, baseline = produce()
    ready = authority.ready(corpus, baseline, implementation_author="coder")
    assert not code_work_ready(
        permit=replace(ready, objective_version="changed"), corpus=corpus,
        evidence=baseline, authority=authority, implementation_author="coder",
    )
    assert not code_work_ready(
        permit=ready, corpus=corpus, evidence=replace(baseline, signature="fake"),
        authority=authority, implementation_author="coder",
    )


def test_untrusted_or_short_signing_key_rejected():
    with pytest.raises(ObjectiveError, match="at least 32"):
        EvidenceAuthority(b"small")
    corpus, auth1, baseline = produce()
    auth2 = EvidenceAuthority(b"entirely-different-trusted-key-0123456789")
    with pytest.raises(ObjectiveError, match="signature"):
        auth2.ready(corpus, baseline, implementation_author="coder")


def test_runner_failure_is_not_accepted_as_red_baseline():
    def crash(case):
        raise RuntimeError("no pre-code implementation")
    with pytest.raises(ObjectiveError, match="baseline runner failed"):
        produce(runner=crash)


def test_immutable_golden_records_survive_restart(tmp_path):
    corpus, authority, baseline = produce()
    store = GoldenStore(tmp_path / "records")
    record_id = store.save(corpus, baseline, authority, implementation_author="coder")
    assert len(record_id) == 64
    assert store.save(corpus, baseline, authority, implementation_author="coder") == record_id
    restarted = GoldenStore(tmp_path / "records")
    loaded_corpus, loaded_baseline = restarted.load(
        record_id, authority, implementation_author="coder")
    assert loaded_corpus == corpus
    assert loaded_baseline == baseline
    assert authority.ready(loaded_corpus, loaded_baseline, implementation_author="coder").ready
    with pytest.raises(ObjectiveError, match="invalid golden record id"):
        restarted.load("../outside", authority, implementation_author="coder")


def test_immutable_golden_record_tampering_fails_closed(tmp_path):
    corpus, authority, baseline = produce()
    store = GoldenStore(tmp_path / "records")
    identifier = store.save(corpus, baseline, authority, implementation_author="coder")
    file = store.root / (identifier + ".json")
    original = file.read_bytes()
    file.write_bytes(original + b" ")
    with pytest.raises(ObjectiveError, match="digest mismatch"):
        store.load(identifier, authority, implementation_author="coder")
    with pytest.raises(ObjectiveError, match="corrupted"):
        store.save(corpus, baseline, authority, implementation_author="coder")


def test_green_baseline_cannot_be_persisted(tmp_path):
    corpus, authority, baseline = produce(runner=lambda case: case.expected)
    with pytest.raises(ObjectiveError, match="already green"):
        GoldenStore(tmp_path / "records").save(
            corpus, baseline, authority, implementation_author="coder")
