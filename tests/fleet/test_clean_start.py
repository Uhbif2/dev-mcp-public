"""TZ-19: synthetic end-to-end attestation; never call a live CI/second PC."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from dev_mcp.fleet.clean_start import CleanStartCoordinator, CleanStartSpec, ResourceProfile
from dev_mcp.operator_catalog import CATALOG_VERSION


COMMIT = "1" * 40
SOURCE = "2" * 64
LOCK = "3" * 64
ENV = "4" * 64
ARTIFACT = "5" * 64
OUTCOME = "6" * 64


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


@pytest.fixture()
def spec():
    return CleanStartSpec(
        task_id="tz19-demo", commit_sha=COMMIT, source_sha=SOURCE,
        lock_sha=LOCK, env_sha=ENV, artifact_sha=ARTIFACT,
        outcome_sha=OUTCOME, workflow="fresh_e2e", second_pc_plan_id="fresh_e2e",
        target="staging", min_ram_mib=1024, min_disk_mib=2048,
    )


@pytest.fixture()
def profiles(tmp_path):
    ci = ResourceProfile(
        resource_id="runner-ci", operation="ci.run", workspace_base=str(tmp_path / "ci"),
        online=True, human_using=False, free_ram_mib=4096, free_disk_mib=8192,
        ready_secret_names=frozenset(),
    )
    pc = replace(ci, resource_id="runner-pc", operation="second_pc.run",
                 workspace_base=str(tmp_path / "pc"))
    return ci, pc


class Harness:
    def __init__(self, tmp_path, spec):
        self.db = tmp_path / "journal.sqlite"
        self.spec = spec
        self.calls = []
        self.fetches = []
        self.last_observation = None
        self.bad_stage = None
        self.bad_field = None
        self.operator_ok = True
        self.raise_secret = False
        self.no_witness = False
        self.pause = None
        self.proceed = None

    def op(self, operation, params):
        self.calls.append((operation, dict(params)))
        if self.pause is not None:
            self.pause.set()
            assert self.proceed.wait(5)
        if self.raise_secret:
            raise RuntimeError("upstream Authorization: private-token")
        version = SOURCE if operation == "ci.run" else ARTIFACT
        return {"ok": self.operator_ok, "evidence": {
            "kind": "run", "ref": "opref:" + "a" * 64,
            "subject_version": version, "env_version": ENV,
            "verdict": "pass" if self.operator_ok else "fail",
            "producer": "operator", "operation": operation,
            "request_fingerprint": digest({
                "version": CATALOG_VERSION, "operation": operation, "params": params,
            }), "metrics": {"clone": True}, "observed_ts": 1234,
        }}

    def fetch(self, resource_id, nonce, operator_ref):
        self.fetches.append((resource_id, nonce, operator_ref))
        operation, params = self.calls[-1]
        stages = [
            {"stage": "clone", "version": COMMIT, "verdict": "pass", "duration_ms": 12},
            {"stage": "install", "version": LOCK, "verdict": "pass", "duration_ms": 20},
            {"stage": "run", "version": OUTCOME, "verdict": "pass",
             "duration_ms": 30, "user_visible": True},
        ]
        observation = {
            "nonce": nonce, "workspace": params["workspace"],
            "resource_id": resource_id, "operation": operation,
            "operator_ref": operator_ref, "commit_sha": COMMIT,
            "source_sha": SOURCE, "lock_sha": LOCK, "env_sha": ENV,
            "artifact_sha": ARTIFACT, "outcome_sha": OUTCOME,
            "fresh_clone": True, "empty_workspace_before_clone": True,
            "no_previous_artifacts": True, "real_e2e": True,
            "stages": stages, "observed_ts": 4567,
        }
        if self.bad_stage:
            i, key, value = self.bad_stage
            stages[i][key] = value
        if self.bad_field:
            observation[self.bad_field[0]] = self.bad_field[1]
        self.last_observation = observation
        return observation

    def verify(self, observation):
        # Fake for a PINNED trusted-key verifier. No live external execution.
        return observation is self.last_observation and not self.no_witness

    def coordinator(self, *, verifier=True, fetcher=True):
        return CleanStartCoordinator(
            self.db, fleet_op=self.op,
            fetch_attestation=self.fetch if fetcher else None,
            verify_attestation=self.verify if verifier else None,
        )


def assert_verified(result, spec, operation):
    assert result["status"] == "verified" and result["error_code"] is None
    assert result["subject_version"] == spec.source_sha
    assert result["source_commit"] == spec.commit_sha
    assert result["dependency_lock_sha"] == spec.lock_sha
    assert result["env_version"] == spec.env_sha
    assert result["artifact_sha"] == spec.artifact_sha
    assert result["outcome_sha"] == spec.outcome_sha
    assert result["operation"] == operation
    assert len(result["evidence"]) == 2
    op, witness = result["evidence"]
    assert op["verdict"] == witness["verdict"] == "pass"
    assert op["operation"] == operation and op["producer"] == "operator"
    assert op["subject_version"] == (SOURCE if operation == "ci.run" else ARTIFACT)
    assert witness["operator_ref"] == op["ref"]
    assert witness["ref"].startswith("attest:")
    assert [step["stage"] for step in witness["stages"]] == ["clone", "install", "run"]
    assert [step["version"] for step in witness["stages"]] == [COMMIT, LOCK, OUTCOME]


@pytest.mark.parametrize("use_pc", [False, True])
def test_full_witness_and_versioned_outcome(tmp_path, spec, profiles, use_pc):
    h = Harness(tmp_path, spec)
    resources = (replace(profiles[0], online=False), profiles[1]) if use_pc else profiles
    result = h.coordinator().run_once(spec, resources)
    operation = "second_pc.run" if use_pc else "ci.run"
    assert_verified(result, spec, operation)
    assert len(h.calls) == len(h.fetches) == 1
    assert h.calls[0][1]["execution_id"] == h.fetches[0][1]
    assert h.calls[0][1]["workspace"].endswith("clean-" + h.fetches[0][1])
    assert h.calls[0][1]["workspace"].startswith(profiles[use_pc].workspace_base)
    if use_pc:
        assert h.calls[0][1]["artifact_sha256"] == ARTIFACT
    else:
        assert h.calls[0][1]["commit_sha"] == COMMIT


def test_ci_down_second_pc_busy_or_underpowered_waits(tmp_path, spec, profiles):
    h = Harness(tmp_path, spec)
    result = h.coordinator().run_once(
        spec, (replace(profiles[0], online=False),
               replace(profiles[1], human_using=True)),
    )
    assert result["status"] == "waiting"
    assert result["error_code"] == "clean_resource_unavailable"
    assert result["predicate"]["second_pc"] == "online_idle_and_capacity"
    assert result["evidence"] == [] and not h.calls
    result = h.coordinator().run_once(spec, (
        replace(profiles[0], free_ram_mib=0),
        replace(profiles[1], free_disk_mib=0),
    ))
    assert result["status"] == "waiting" and not h.calls


def test_missing_secret_defect_and_fallback(tmp_path, spec, profiles):
    spec = replace(spec, required_secrets=("CI_SIGNING",))
    h = Harness(tmp_path, spec)
    result = h.coordinator().run_once(spec, profiles)
    assert result["status"] == "defect" and result["error_code"] == "required_secret_missing"
    assert result["evidence"] == [] and not h.calls
    pc = replace(profiles[1], ready_secret_names=frozenset({"CI_SIGNING"}))
    assert_verified(h.coordinator().run_once(spec, (profiles[0], pc)), spec, "second_pc.run")
    assert "CI_SIGNING" not in json.dumps(h.calls)


@pytest.mark.parametrize("field,value,code", [
    ("empty_workspace_before_clone", False, "dirty_or_incomplete_workspace"),
    ("no_previous_artifacts", False, "dirty_or_incomplete_workspace"),
    ("fresh_clone", False, "dirty_or_incomplete_workspace"),
    ("real_e2e", False, "dirty_or_incomplete_workspace"),
    ("env_sha", "9" * 64, "attested_versions_mismatch"),
    ("source_sha", "9" * 64, "attested_versions_mismatch"),
    ("outcome_sha", "9" * 64, "attested_versions_mismatch"),
    ("nonce", "9" * 32, "attested_versions_mismatch"),
    ("workspace", "/stale/workspace", "attested_versions_mismatch"),
    ("operator_ref", "opref:" + "9" * 64, "attested_versions_mismatch"),
])
def test_dirty_workspace_and_changed_sha_fail(tmp_path, spec, profiles, field, value, code):
    h = Harness(tmp_path, spec)
    h.bad_field = (field, value)
    result = h.coordinator().run_once(spec, profiles)
    assert result["status"] == "defect" and result["error_code"] == code
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["producer"] == "operator"


@pytest.mark.parametrize("bad_stage", [
    (0, "verdict", "fail"), (1, "version", "0" * 64),
    (2, "user_visible", False), (2, "stage", "install"),
])
def test_clone_install_user_outcome_must_pass(tmp_path, spec, profiles, bad_stage):
    h = Harness(tmp_path, spec)
    h.bad_stage = bad_stage
    result = h.coordinator().run_once(spec, profiles)
    assert result["status"] == "defect"
    assert result["error_code"] in {"fresh_stage_failed", "user_outcome_not_observed"}


def test_broken_environment_or_operator_error_fails_closed(tmp_path, spec, profiles):
    h = Harness(tmp_path, spec)
    h.operator_ok = False
    result = h.coordinator().run_once(spec, profiles)
    assert result["status"] == "defect"
    assert result["error_code"] == "operator_failed" and result["evidence"] == []
    h2 = Harness(tmp_path, replace(spec, task_id="different"))
    h2.db = tmp_path / "broken.sqlite"
    h2.raise_secret = True
    result = h2.coordinator().run_once(h2.spec, profiles)
    assert result["status"] == "defect"
    assert "private-token" not in json.dumps(result)
    assert result["error_code"] == "clean_start_operation_failed"


@pytest.mark.parametrize("verifier,fetcher,invalid", [
    (False, True, False), (True, False, False), (True, True, True),
])
def test_missing_or_untrusted_attestor_never_verifies(tmp_path, spec, profiles,
                                                       verifier, fetcher, invalid):
    h = Harness(tmp_path, spec)
    h.no_witness = invalid
    result = h.coordinator(verifier=verifier, fetcher=fetcher).run_once(spec, profiles)
    if not verifier or not fetcher:
        assert result["status"] == "waiting" and not h.calls
    else:
        assert result["status"] == "defect" and result["error_code"] == "attestation_invalid"
    assert result["status"] != "verified"


def test_replay_offline_no_duplicate_effect_and_spec_conflict(tmp_path, spec, profiles):
    h = Harness(tmp_path, spec)
    first = h.coordinator().run_once(spec, profiles)
    assert_verified(first, spec, "ci.run")
    offline = (replace(profiles[0], online=False),)
    assert h.coordinator().run_once(spec, offline) == first
    changed = replace(spec, outcome_sha="7" * 64)
    assert h.coordinator().run_once(changed, offline)["error_code"] == "spec_changed"
    assert len(h.calls) == 1
    with sqlite3.connect(h.db) as con:
        assert con.execute("select count(*) from clean_start_leases").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [
    ("receipt_sha", "0" * 64), ("receipt_json", "{bad-json"),
])
def test_journal_sha_or_json_tamper_defect_not_reexecution(tmp_path, spec, profiles,
                                                            field, value):
    h = Harness(tmp_path, spec)
    h.coordinator().run_once(spec, profiles)
    with sqlite3.connect(h.db) as con:
        con.execute(f"UPDATE clean_start_attempts SET {field}=? WHERE task_id=?",
                    (value, spec.task_id))
    result = h.coordinator().run_once(spec, ())
    assert result["status"] == "defect" and result["error_code"] == "journal_digest_mismatch"
    assert not result["evidence"] and len(h.calls) == 1


def test_atomic_lease_blocks_concurrent_task(tmp_path, spec, profiles):
    h = Harness(tmp_path, spec)
    h.pause, h.proceed = threading.Event(), threading.Event()
    result = []
    thread = threading.Thread(target=lambda: result.append(
        h.coordinator().run_once(spec, (profiles[0],))))
    thread.start()
    try:
        assert h.pause.wait(5)
        other = replace(spec, task_id="tz19-other")
        second = h.coordinator().run_once(other, (profiles[0],))
        assert second["status"] == "waiting"
        assert second["error_code"] == "resource_reserved"
        same = h.coordinator().run_once(spec, (profiles[0],))
        assert same["status"] == "waiting" and same["error_code"] == "in_progress"
        assert len(h.calls) == 1
    finally:
        h.proceed.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert_verified(result[0], spec, "ci.run")


def test_busy_ci_falls_back_to_idle_second_pc(tmp_path, spec, profiles):
    primary = Harness(tmp_path, spec)
    primary.pause, primary.proceed = threading.Event(), threading.Event()
    first_result = []
    worker = threading.Thread(target=lambda: first_result.append(
        primary.coordinator().run_once(spec, (profiles[0],))))
    worker.start()
    try:
        assert primary.pause.wait(5)
        other_spec = replace(spec, task_id="tz19-fallback")
        secondary = Harness(tmp_path, other_spec)
        secondary.db = primary.db
        assert profiles[1].available(other_spec)
        second_result = secondary.coordinator().run_once(other_spec, profiles)
        assert_verified(second_result, other_spec, "second_pc.run")
        assert len(secondary.calls) == 1
    finally:
        primary.proceed.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert_verified(first_result[0], spec, "ci.run")


def test_all_sqlite_connections_closed_after_run(tmp_path, spec, profiles, monkeypatch):
    real_connect = sqlite3.connect
    opened = []

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    h = Harness(tmp_path, spec)
    assert_verified(h.coordinator().run_once(spec, profiles), spec, "ci.run")
    assert len(opened) >= 3
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_invalid_spec_and_resource_rejected_before_execution(tmp_path, spec, profiles):
    h = Harness(tmp_path, spec)
    coordinator = h.coordinator()
    with pytest.raises(ValueError, match="invalid_commit"):
        coordinator.run_once(replace(spec, commit_sha=123), profiles)
    with pytest.raises(ValueError, match="invalid_workspace_base"):
        coordinator.run_once(spec, (replace(profiles[0], workspace_base=123),))
    with pytest.raises(ValueError, match="invalid_resource_profile"):
        coordinator.run_once(spec, (replace(profiles[0], operation="shell.run"),))
    assert h.calls == []
