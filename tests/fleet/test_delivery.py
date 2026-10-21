"""TZ-13: all side effects replaced with deterministic trusted operator fakes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_mcp.fleet.delivery import AcceptedEvent, DefectRequest, on_accept
from dev_mcp.fleet.delivery_rules import DeliveryPolicyError, load_delivery_policy

OLD = "a" * 64
NEW_ENV = "b" * 64
GOOD = "c" * 64
SUBJECT = "d" * 64
FP = "f" * 64


def policy_text(workspace: Path) -> str:
    folder = json.dumps(str(workspace))
    return f"""
[delivery]
version = 1
target = "staging"
workspace = {folder}
timeout_seconds = 30
known_good_revision = "{GOOD}"

[apply]
operation = "cloud.deploy"
[apply.params]
workspace = {folder}
target = "staging"
artifact_sha256 = "{SUBJECT}"
release_id = "release_1"

[verify]
operation = "ci.run"
[verify.params]
workspace = {folder}
target = "staging"
commit_sha = "{SUBJECT}"
workflow = "postdeploy"

[rollback]
operation = "cloud.deploy"
[rollback.params]
workspace = {folder}
target = "staging"
artifact_sha256 = "{GOOD}"
release_id = "known_good"
"""


def accepted(event_id: str = "accepted_1") -> AcceptedEvent:
    return AcceptedEvent(
        event_id=event_id, project="Demo", subject_version=SUBJECT,
        env_version=OLD,
        evidence={
            "kind": "review", "verdict": "pass", "producer": "server",
            "subject_version": SUBJECT, "env_version": OLD, "ref": "server-review-1",
        },
    )


def proof(operation: str, *, sha: str, env: str, ok: bool = True) -> dict:
    return {"ok": ok, "evidence": {
        "kind": "delivery" if operation == "cloud.deploy" else "run",
        "ref": "opref:" + "a" * 64, "subject_version": sha, "env_version": env,
        "verdict": "pass" if ok else "fail", "producer": "operator",
        "operation": operation, "request_fingerprint": FP, "metrics": {"steps": 1},
        "observed_ts": 42,
    }}


class Harness:
    def __init__(self, root: Path):
        self.root = root
        self.db = root / "runtime" / "delivery.sqlite"
        self.calls: list[tuple[str, dict]] = []
        self.repairs: list[DefectRequest] = []
        self.environment = OLD
        self.verify_ok = True
        self.rollback_ok = True
        self.change_environment_after_apply = False
        self.bad_operator_evidence = False
        self.raise_after_apply = False
        self.clock = 0.0

    def op(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        if name == "cloud.deploy" and args["artifact_sha256"] == SUBJECT:
            self.environment = "e" * 64 if self.change_environment_after_apply else NEW_ENV
            if self.raise_after_apply:
                raise RuntimeError("private upstream failure: do not log")
            response = proof(name, sha=SUBJECT, env=NEW_ENV)
            if self.bad_operator_evidence:
                response["evidence"]["producer"] = "agent"
            return response
        if name == "ci.run":
            return proof(name, sha=SUBJECT, env=self.environment, ok=self.verify_ok)
        if name == "cloud.deploy" and args["artifact_sha256"] == GOOD:
            if self.rollback_ok:
                self.environment = GOOD
            return proof(name, sha=GOOD, env=self.environment, ok=self.rollback_ok)
        raise AssertionError("unregistered operation was invoked")

    def probe(self, root: Path, target: str) -> str:
        assert root == self.root.resolve() and target == "staging"
        return self.environment

    def repair(self, task: DefectRequest) -> str:
        self.repairs.append(task)
        return "fix_delivery_1"

    def run(self, event: AcceptedEvent | None = None, *, project_root=None, **kw) -> dict:
        return on_accept(
            event or accepted(), project_root=project_root or self.root,
            allowed_roots=(self.root,), state_db=self.db,
            fleet_op=self.op, probe_environment=self.probe,
            create_defect=self.repair, monotonic=lambda: self.clock,
            **kw,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    (tmp_path / "deliver.toml").write_text(policy_text(tmp_path), encoding="utf-8")
    return Harness(tmp_path)


def test_delivers_only_after_server_review_and_operator_verification(harness):
    result = harness.run()
    assert result["status"] == "delivered"
    assert result["subject_version"] == SUBJECT
    assert result["expected_env_version"] == OLD
    assert result["env_version"] == NEW_ENV
    assert result["repair_work"] is None and result["error_code"] is None
    assert [step["stage"] for step in result["evidence"]] == ["apply", "verify"]
    assert all(step["evidence"]["producer"] == "operator" for step in result["evidence"])
    assert all(step["evidence"]["subject_version"] == SUBJECT for step in result["evidence"])
    assert [op for op, _ in harness.calls] == ["cloud.deploy", "ci.run"]
    assert not harness.repairs


def test_duplicate_accept_does_not_redeploy(harness):
    first = harness.run()
    second = harness.run()
    assert second == first
    assert len(harness.calls) == 2
    assert not harness.repairs
    changed = harness.run(accepted("accepted_1").__class__(
        event_id="accepted_1", project="Demo", subject_version="e" * 64,
        env_version=OLD,
        evidence={"kind": "review", "verdict": "pass", "producer": "server",
                  "subject_version": "e" * 64, "env_version": OLD, "ref": "new_review"},
    ))
    assert changed["status"] == "invalid_policy"
    assert len(harness.calls) == 2


def test_no_policy_never_executes(tmp_path):
    h = Harness(tmp_path)
    result = h.run()
    assert result["status"] == "not_configured"
    assert result["error_code"] == "deliver_toml_missing"
    assert not h.calls


@pytest.mark.parametrize("text,code", [
    ("delivery = [broken", "invalid_toml"),
    ("[delivery]\nversion = 999\n", "policy_schema"),
])
def test_bad_toml_fails_before_effects(harness, text, code):
    (harness.root / "deliver.toml").write_text(text, encoding="utf-8")
    result = harness.run()
    assert result["status"] == "invalid_policy"
    assert result["error_code"] == code
    assert not harness.calls


def test_invalid_operation_rejected_before_effect(harness):
    path = harness.root / "deliver.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        'operation = "cloud.deploy"', 'operation = "shell.run"', 1), encoding="utf-8")
    assert harness.run()["error_code"] == "apply_operation_not_allowed"
    assert not harness.calls


def test_path_outside_server_roots_refused(harness, tmp_path):
    other = tmp_path.parent / "unauthorized-target"
    other.mkdir(exist_ok=True)
    path = harness.root / "deliver.toml"
    path.write_text(policy_text(other), encoding="utf-8")
    result = harness.run()
    assert result["error_code"] == "path_outside_roots"
    assert not harness.calls


def test_policy_symlink_refused(harness, tmp_path):
    root = tmp_path / "sub"
    root.mkdir()
    outside = tmp_path.parent
    link = root / "deliver.toml"
    try:
        link.symlink_to(harness.root / "deliver.toml")
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege")
    with pytest.raises(DeliveryPolicyError, match="symlink_policy"):
        load_delivery_policy(root, allowed_roots=(tmp_path,))


def test_failed_verify_rolls_back_and_creates_defect_with_prior_proof(harness):
    harness.verify_ok = False
    result = harness.run()
    assert result["status"] == "failed"
    assert result["error_code"] == "operator_operation_failed"
    assert [x[0] for x in harness.calls] == ["cloud.deploy", "ci.run", "cloud.deploy"]
    assert harness.calls[-1][1]["artifact_sha256"] == GOOD
    assert [x["stage"] for x in result["evidence"]] == ["apply", "verify", "rollback"]
    assert result["repair_work"] == "fix_delivery_1"
    assert result["env_version"] is None
    assert harness.repairs[0].old_evidence["producer"] == "server"
    assert harness.repairs[0].known_good_revision == GOOD
    assert harness.repairs[0].failure_code == "operator_operation_failed"


def test_rollback_failure_never_marks_delivered(harness):
    harness.verify_ok = False
    harness.rollback_ok = False
    result = harness.run()
    assert result["status"] == "rollback_failed"
    assert result["repair_work"] == "fix_delivery_1"
    assert len(harness.calls) == 3
    assert harness.environment == NEW_ENV


def test_env_version_changed_before_deployment(harness):
    harness.environment = "e" * 64
    result = harness.run()
    assert result["status"] == "failed"
    assert result["error_code"] == "environment_changed"
    assert harness.calls == []
    assert result["repair_work"] == "fix_delivery_1"


def test_env_version_changed_mid_deployment_rolls_back(harness):
    harness.change_environment_after_apply = True
    result = harness.run()
    assert result["status"] == "failed"
    assert result["error_code"] == "environment_changed"
    assert [x[0] for x in harness.calls] == ["cloud.deploy", "cloud.deploy"]
    assert result["repair_work"] == "fix_delivery_1"


def test_invalid_operator_evidence_forces_rollback(harness):
    harness.bad_operator_evidence = True
    result = harness.run()
    assert result["status"] == "failed"
    assert result["error_code"] == "operator_evidence_mismatch"
    assert [x[0] for x in harness.calls] == ["cloud.deploy", "cloud.deploy"]


def test_unsanitized_operator_reference_cannot_enter_receipt(harness):
    original = harness.op
    def bad_reference(name, args):
        response = original(name, args)
        if name == "cloud.deploy" and args["artifact_sha256"] == SUBJECT:
            response["evidence"]["ref"] = "Authorization: private-secret-should-not-leak"
        return response
    harness.op = bad_reference
    report = harness.run()
    assert report["status"] == "failed"
    assert "private-secret" not in json.dumps(report)
    assert report["error_code"] == "operator_evidence_mismatch"


def test_wrong_verified_commit_never_publishes(harness):
    policy_file = harness.root / "deliver.toml"
    policy_file.write_text(policy_file.read_text(encoding="utf-8").replace(
        f'commit_sha = "{SUBJECT}"', f'commit_sha = "{"e" * 64}"'), encoding="utf-8")
    report = harness.run()
    assert report["status"] == "invalid_policy"
    assert report["error_code"] == "verify_subject_mismatch"
    assert not harness.calls


def test_untrusted_acceptance_report_refused(harness):
    x = accepted()
    forged = AcceptedEvent(x.event_id, x.project, x.subject_version, x.env_version,
                           {**x.evidence, "producer": "agent"})
    with pytest.raises(ValueError, match="accepted_evidence_not_server_verified"):
        harness.run(forged)
    assert not harness.calls


def test_timeout_after_mutation_rolls_back(harness):
    original = harness.op
    def slow(name, args):
        r = original(name, args)
        if name == "cloud.deploy" and args["artifact_sha256"] == SUBJECT:
            harness.clock = 31
        return r
    harness.op = slow
    result = harness.run()
    assert result["status"] == "failed" and result["error_code"] == "deadline_exceeded"
    assert [x[0] for x in harness.calls] == ["cloud.deploy", "cloud.deploy"]
    assert result["repair_work"] == "fix_delivery_1"


def test_operator_exception_does_not_leak_secrets_and_rolls_back(harness):
    harness.raise_after_apply = True
    result = harness.run()
    assert result["status"] == "failed"
    assert result["error_code"] == "delivery_operation_failed"
    assert "private" not in json.dumps(result)
    assert harness.calls[-1][1]["artifact_sha256"] == GOOD


def test_repair_scheduler_failure_preserves_non_delivered_state(harness):
    harness.verify_ok = False
    def broken(_):
        raise RuntimeError("task queue unavailable")
    harness.repair = broken
    result = harness.run()
    assert result["status"] == "repair_pending"
    assert result["repair_work"] is None
    assert harness.run() == result
    assert len(harness.calls) == 3
