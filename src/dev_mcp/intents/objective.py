"""Independent, versioned golden scenarios and executable red baseline.

This is a pure server-side contract. A report from a coding agent cannot claim
a green/red baseline: only EvidenceAuthority.run_baseline invokes the runner and
signs its observations. Live runners and controller integration belong elsewhere.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

MAX_CASES = 128
MAX_CORPUS_BYTES = 256 * 1024
MAX_CASE_BYTES = 16 * 1024
MAX_REQUIREMENT_BYTES = 16 * 1024
MIN_TOKEN_BYTES = 32

Kind = Literal["positive", "negative"]


class ObjectiveError(ValueError):
    """An incomplete or unverifiable scenario objective."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, OverflowError, ValueError, RecursionError) as exc:
        raise ObjectiveError("not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _nonempty(value: str, name: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ObjectiveError(f"{name} must be non-empty and bounded")
    return value.strip()


@dataclass(frozen=True)
class ObjectiveSpec:
    outcome_id: str
    requirement_version: str
    requirement: str
    author_id: str
    subject_sha256: str

    def validate(self) -> None:
        _nonempty(self.outcome_id, "outcome_id")
        _nonempty(self.requirement_version, "requirement_version")
        _nonempty(self.author_id, "author_id")
        _nonempty(self.requirement, "requirement", limit=MAX_REQUIREMENT_BYTES)
        if (not isinstance(self.subject_sha256, str) or
                len(self.subject_sha256) != 64 or
                any(c not in "0123456789abcdef" for c in self.subject_sha256)):
            raise ObjectiveError("subject_sha256 must be a SHA256 digest")

    @property
    def version(self) -> str:
        self.validate()
        return _digest({
            "outcome_id": self.outcome_id, "requirement_version": self.requirement_version,
            "requirement": self.requirement, "subject_sha256": self.subject_sha256,
        })


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    kind: Kind
    input: object
    expected: object
    author_id: str

    def validate(self) -> None:
        _nonempty(self.scenario_id, "scenario_id")
        _nonempty(self.author_id, "scenario author")
        if self.kind not in ("positive", "negative"):
            raise ObjectiveError("scenario kind must be positive or negative")
        if not isinstance(self.input, (dict, list)) or not isinstance(self.expected, (dict, list)):
            raise ObjectiveError("scenario input/expected must be structured JSON")
        if len(_canonical({"input": self.input, "expected": self.expected})) > MAX_CASE_BYTES:
            raise ObjectiveError("scenario exceeds size budget")


@dataclass(frozen=True)
class GoldenCorpus:
    objective: ObjectiveSpec
    scenarios: tuple[Scenario, ...]
    test_author: str
    corpus_sha256: str

    def validate(self) -> None:
        self.objective.validate()
        _nonempty(self.test_author, "test_author")
        if not 2 <= len(self.scenarios) <= MAX_CASES:
            raise ObjectiveError("golden corpus must contain 2..128 scenarios")
        if len({s.scenario_id for s in self.scenarios}) != len(self.scenarios):
            raise ObjectiveError("duplicate scenario id")
        if not any(s.kind == "positive" for s in self.scenarios):
            raise ObjectiveError("missing positive scenario")
        if not any(s.kind == "negative" for s in self.scenarios):
            raise ObjectiveError("missing negative scenario")
        for scenario in self.scenarios:
            scenario.validate()
            if scenario.author_id != self.test_author:
                raise ObjectiveError("scenario author mismatch")
        serialized = self._content()
        if len(serialized) > MAX_CORPUS_BYTES:
            raise ObjectiveError("golden corpus exceeds size budget")
        if hashlib.sha256(serialized).hexdigest() != self.corpus_sha256:
            raise ObjectiveError("golden corpus digest mismatch")

    def _content(self) -> bytes:
        return _canonical({
            "objective": self.objective.version,
            "test_author": self.test_author,
            "scenarios": [
                {"id": s.scenario_id, "kind": s.kind, "input": s.input,
                 "expected": s.expected, "author": s.author_id}
                for s in self.scenarios
            ],
        })


def make_corpus(objective: ObjectiveSpec, scenarios: Sequence[Scenario], *,
                test_author: str) -> GoldenCorpus:
    objective.validate()
    cases = tuple(scenarios)
    corpus = GoldenCorpus(objective, cases, test_author, "")
    result = GoldenCorpus(objective, cases, test_author,
                          hashlib.sha256(corpus._content()).hexdigest())
    result.validate()
    return result


@dataclass(frozen=True)
class ScenarioObservation:
    scenario_id: str
    kind: Kind
    expected_sha256: str
    observed_sha256: str
    passed: bool


@dataclass(frozen=True)
class BaselineEvidence:
    objective_version: str
    corpus_sha256: str
    requirement_version: str
    precode_subject_version: str
    env_version: str
    verifier_id: str
    test_author: str
    observations: tuple[ScenarioObservation, ...]
    red_count: int
    run_digest: str
    signature: str


@dataclass(frozen=True)
class ObjectiveReady:
    objective_version: str
    corpus_sha256: str
    precode_subject_version: str
    env_version: str
    baseline_digest: str
    ready: bool = True


class EvidenceAuthority:
    """Trusted server capability. Never expose the key or this issuer as MCP.

    The runner callable must be provided by trusted orchestration; results are
    captured as hashes, not user text. Verifiers without this key cannot forge
    a baseline merely by instantiating a dataclass.
    """

    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key) < MIN_TOKEN_BYTES:
            raise ObjectiveError("server evidence key must be at least 32 bytes")
        self.__key = key

    def sign(self, data: Mapping[str, Any]) -> tuple[str, str]:
        digest = _digest(data)
        return digest, hmac.new(self.__key, digest.encode("ascii"),
                                hashlib.sha256).hexdigest()

    def verify(self, data: Mapping[str, Any], digest: str, signature: str) -> None:
        actual_digest, expected_signature = self.sign(data)
        if (not isinstance(digest, str) or not isinstance(signature, str)
                or not hmac.compare_digest(actual_digest, digest)
                or not hmac.compare_digest(expected_signature, signature)):
            raise ObjectiveError("server evidence signature mismatch")

    def run_baseline(
        self, corpus: GoldenCorpus, *, precode_subject_version: str,
        env_version: str, verifier_id: str,
        runner: Callable[[Scenario], object], implementation_author: str,
    ) -> BaselineEvidence:
        corpus.validate()
        for name, value in (("precode_subject_version", precode_subject_version),
                            ("env_version", env_version), ("verifier_id", verifier_id),
                            ("implementation_author", implementation_author)):
            _nonempty(value, name)
        if precode_subject_version != corpus.objective.subject_sha256:
            raise ObjectiveError("red baseline is not bound to objective source snapshot")
        if (verifier_id in {corpus.test_author, implementation_author}
                or corpus.test_author == implementation_author):
            raise ObjectiveError("golden author, code author and verifier must be independent")
        if not callable(runner):
            raise ObjectiveError("trusted baseline runner required")
        observations: list[ScenarioObservation] = []
        for case in corpus.scenarios:
            try:
                observed = runner(case)
                result_digest = _digest(observed)
                matched = result_digest == _digest(case.expected)
            except Exception as exc:
                # A crashed *runner* is not proof that the product failed the
                # scenario. Trusted adapters must return an observed error
                # object for expected application failures; infra errors fail
                # closed rather than fabricating a valid red baseline.
                raise ObjectiveError("baseline runner failed: " + type(exc).__name__) from exc
            observations.append(ScenarioObservation(
                case.scenario_id, case.kind, _digest(case.expected),
                result_digest, matched,
            ))
        red = sum(not item.passed for item in observations)
        payload = self._baseline_payload(
            corpus.objective.version, corpus.corpus_sha256,
            corpus.objective.requirement_version, precode_subject_version,
            env_version, verifier_id, corpus.test_author, observations, red,
        )
        digest, signature = self.sign(payload)
        return BaselineEvidence(
            corpus.objective.version, corpus.corpus_sha256,
            corpus.objective.requirement_version, precode_subject_version,
            env_version, verifier_id, corpus.test_author, tuple(observations),
            red, digest, signature,
        )

    @staticmethod
    def _baseline_payload(
        objective_version: str, corpus_sha256: str, requirement_version: str,
        precode_subject_version: str, env_version: str, verifier_id: str,
        test_author: str, observations: Sequence[ScenarioObservation], red: int,
    ) -> dict[str, object]:
        return {
            "objective_version": objective_version, "corpus_sha256": corpus_sha256,
            "requirement_version": requirement_version,
            "precode_subject_version": precode_subject_version,
            "env_version": env_version, "verifier_id": verifier_id,
            "test_author": test_author,
            "observations": [vars(x) for x in observations], "red_count": red,
        }

    def ready(self, corpus: GoldenCorpus, evidence: BaselineEvidence, *,
              implementation_author: str) -> ObjectiveReady:
        corpus.validate()
        _nonempty(implementation_author, "implementation_author")
        if evidence.objective_version != corpus.objective.version:
            raise ObjectiveError("baseline requirement changed")
        if (evidence.corpus_sha256 != corpus.corpus_sha256 or
                evidence.requirement_version != corpus.objective.requirement_version or
                evidence.test_author != corpus.test_author):
            raise ObjectiveError("baseline corpus or requirement changed")
        if (implementation_author in {evidence.test_author, evidence.verifier_id}
                or evidence.verifier_id == evidence.test_author):
            raise ObjectiveError("baseline is not independent of implementation")
        for name in ("precode_subject_version", "env_version", "verifier_id"):
            _nonempty(getattr(evidence, name), name)
        if evidence.precode_subject_version != corpus.objective.subject_sha256:
            raise ObjectiveError("baseline subject version changed")
        if len(evidence.observations) != len(corpus.scenarios):
            raise ObjectiveError("baseline skipped scenarios")
        for obs, case in zip(evidence.observations, corpus.scenarios):
            if (obs.scenario_id != case.scenario_id or obs.kind != case.kind
                    or obs.expected_sha256 != _digest(case.expected)):
                raise ObjectiveError("baseline scenario not from this corpus")
            if (not isinstance(obs.passed, bool) or
                    not isinstance(obs.observed_sha256, str) or
                    len(obs.observed_sha256) != 64):
                raise ObjectiveError("malformed observation")
        actual_red = sum(not obs.passed for obs in evidence.observations)
        if actual_red != evidence.red_count:
            raise ObjectiveError("baseline red_count is inconsistent")
        payload = self._baseline_payload(
            evidence.objective_version, evidence.corpus_sha256,
            evidence.requirement_version, evidence.precode_subject_version,
            evidence.env_version, evidence.verifier_id, evidence.test_author,
            evidence.observations, evidence.red_count,
        )
        self.verify(payload, evidence.run_digest, evidence.signature)
        if not actual_red or not any(
                obs.kind == "positive" and not obs.passed for obs in evidence.observations):
            raise ObjectiveError("baseline is already green before code")
        return ObjectiveReady(
            corpus.objective.version, corpus.corpus_sha256,
            evidence.precode_subject_version, evidence.env_version,
            evidence.run_digest,
        )


def code_work_ready(*, permit: ObjectiveReady | None,
                    corpus: GoldenCorpus, evidence: BaselineEvidence,
                    authority: EvidenceAuthority, implementation_author: str) -> bool:
    """Fail closed; trusted scheduler calls this before issuing code-work."""
    if permit is None or permit.ready is not True:
        return False
    try:
        expected = authority.ready(corpus, evidence,
                                   implementation_author=implementation_author)
        return permit == expected
    except ObjectiveError:
        return False


class GoldenStore:
    """Immutable local JSON records for a versioned corpus and signed red run.

    The server owns root, evidence key and writer capability. References name
    the hash of the entire byte-for-byte record, not an agent-provided path.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ObjectiveError("golden store root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _object(corpus: GoldenCorpus, baseline: BaselineEvidence) -> dict[str, object]:
        return {
            "schema": 1,
            "corpus": {
                "objective": asdict(corpus.objective),
                "scenarios": [asdict(s) for s in corpus.scenarios],
                "test_author": corpus.test_author,
                "corpus_sha256": corpus.corpus_sha256,
            },
            "baseline": asdict(baseline),
        }

    def save(self, corpus: GoldenCorpus, baseline: BaselineEvidence,
             authority: EvidenceAuthority, *, implementation_author: str) -> str:
        authority.ready(corpus, baseline, implementation_author=implementation_author)
        content = _canonical(self._object(corpus, baseline))
        record_id = hashlib.sha256(content).hexdigest()
        target = self.root / (record_id + ".json")
        if target.exists():
            if target.is_symlink() or target.read_bytes() != content:
                raise ObjectiveError("existing golden record is corrupted")
            return record_id
        stage: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.root, prefix=".golden-", suffix=".tmp", delete=False
            ) as handle:
                stage = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Hardlink is an atomic create-if-absent on the same filesystem:
                # a second writer cannot overwrite an immutable record.
                os.link(stage, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != content:
                    raise ObjectiveError("conflicting golden record")
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)
        return record_id

    def load(self, record_id: str, authority: EvidenceAuthority, *,
             implementation_author: str) -> tuple[GoldenCorpus, BaselineEvidence]:
        if not isinstance(record_id, str) or re.fullmatch(r"[0-9a-f]{64}", record_id) is None:
            raise ObjectiveError("invalid golden record id")
        target = self.root / (record_id + ".json")
        if target.is_symlink():
            raise ObjectiveError("golden record symlink")
        try:
            raw = target.read_bytes()
            if hashlib.sha256(raw).hexdigest() != record_id:
                raise ObjectiveError("golden record digest mismatch")
            record = json.loads(raw)
            if set(record) != {"schema", "corpus", "baseline"} or record["schema"] != 1:
                raise ObjectiveError("invalid golden record schema")
            if _canonical(record) != raw:
                raise ObjectiveError("noncanonical golden record")
            saved = record["corpus"]
            objective = ObjectiveSpec(**saved["objective"])
            scenarios = tuple(Scenario(**scenario) for scenario in saved["scenarios"])
            corpus = GoldenCorpus(objective, scenarios,
                                  saved["test_author"], saved["corpus_sha256"])
            data = record["baseline"]
            observations = tuple(ScenarioObservation(**item) for item in data["observations"])
            evidence = BaselineEvidence(
                **{**data, "observations": observations},
            )
            authority.ready(corpus, evidence, implementation_author=implementation_author)
            return corpus, evidence
        except (OSError, TypeError, KeyError, ValueError, RecursionError) as exc:
            if isinstance(exc, ObjectiveError):
                raise
            raise ObjectiveError("golden record missing, malformed, or unverified") from exc
