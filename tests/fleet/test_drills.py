from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dev_mcp.fleet.drill_fixtures import EXPECTED, FIXTURES, restore_fixture
from dev_mcp.fleet.drills import (
    DrillSafetyError, RepairSignal, SCHEDULER_CONTRACT, run_daily, run_scenario,
)


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def sandbox(tmp_path):
    base = tmp_path / "fleet-drill-sandbox"
    base.mkdir()
    return base


def test_all_five_scenarios_prove_recovery_on_versioned_evidence(tmp_path):
    root = sandbox(tmp_path)
    report = run_daily(root, now=NOW)
    assert report["status"] == "recovered"
    assert report["capability_gaps"] == []
    assert [c["scenario"] for c in report["scenarios"]] == [f.name for f in FIXTURES]
    for entry in report["scenarios"]:
        assert entry["status"] == "recovered"
        assert entry["synthetic"] and len(entry["route"]) >= 3
        name = entry["scenario"]
        assert entry["route"][0] == f"detect:{name}:{EXPECTED[name].cause_code}"
        assert entry["route"][1] == "controller:typed_obstacle"
        assert any(item.startswith("controller:routed_work:") for item in entry["route"])
        assert f"independent_domain_probe:{name}" in entry["route"]
        assert f"server_evidence:{EXPECTED[name].evidence_kind}" in entry["route"]
        if name == "resource_missing":
            assert "world:resource_down_to_up" in entry["route"]
        assert entry["route"][-1] == "controller:close_verified_outcome"
        assert entry["baseline_version"] != entry["fault_version"]
        assert entry["subject_version"] == entry["fault_version"]
        assert "/sha256:" in entry["env_version"]
        assert entry["evidence"][-1]["digest"] == entry["baseline_version"]
        assert {e["subject_version"] for e in entry["evidence"][1:]} == {entry["fault_version"]}
        assert {e["producer"] for e in entry["evidence"]} == {"independent_probe"}
        assert entry["elapsed_seconds"] >= 0
        assert entry["cleanup"] == "removed" and entry["report_digest"]
    assert not list(root.glob("case-*"))
    assert json.loads((root / "reports" / "2026-10-09.json").read_text()) == report
    assert SCHEDULER_CONTRACT["cadence"] == "daily"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_disabling_each_typed_supplier_blocks_recovery(tmp_path, fixture, monkeypatch):
    from dev_mcp.fleet import drills
    root = sandbox(tmp_path)
    monkeypatch.setattr(drills, "apply_recovery", lambda *args: False)
    result = run_scenario(root, fixture, now=NOW)
    assert result["status"] == "capability_gap"
    assert result["capability_gap"] == "agent_stuck"
    assert result["cleanup"] == "removed"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_disabling_each_domain_verifier_blocks_recovery(tmp_path, fixture, monkeypatch):
    from dev_mcp.fleet import drills
    root = sandbox(tmp_path)
    monkeypatch.setattr(drills, "recheck_domain", lambda *args: False)
    result = run_scenario(root, fixture, now=NOW)
    assert result["status"] == "capability_gap"
    assert result["capability_gap"] == "agent_stuck"


def test_red_gate_without_signed_attribution_never_passes(tmp_path, monkeypatch):
    from dev_mcp.fleet.gate_fault import GateFaultAttributor
    root = sandbox(tmp_path)
    class FakeUnknown:
        cause_code = "unknown"
    monkeypatch.setattr(GateFaultAttributor, "attribute", lambda *args, **kwargs: FakeUnknown())
    result = run_scenario(root, FIXTURES[0], now=NOW)
    assert result["status"] == "capability_gap"
    assert result["capability_gap"] == "agent_stuck"


def test_resource_up_event_must_be_processed_by_real_controller(tmp_path, monkeypatch):
    from dev_mcp.fleet.controller import Controller
    root = sandbox(tmp_path)
    monkeypatch.setattr(Controller, "_source_actions", lambda *args, **kwargs: [])
    result = run_scenario(root, FIXTURES[1], now=NOW)
    assert result["status"] == "capability_gap"
    assert result["capability_gap"] == "agent_stuck"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_missing_typed_detection_prevents_controller_dispatch(tmp_path, fixture, monkeypatch):
    from dev_mcp.fleet import drills
    root = sandbox(tmp_path)
    monkeypatch.setattr(drills, "diagnose", lambda *args: None)
    result = run_scenario(root, fixture, now=NOW)
    assert result["status"] == "capability_gap"
    assert result["capability_gap"] == "fault_injection_failed"


def test_double_run_returns_original_report_without_reexecuting(tmp_path):
    root = sandbox(tmp_path)
    first = run_daily(root, now=NOW)
    def unexpected(*args):
        raise AssertionError("duplicate must not run")
    second = run_daily(root, now=NOW + timedelta(hours=4), repair=unexpected)
    assert first == second
    assert len(list((root / "reports").iterdir())) == 1
    assert not list((root / "leases").iterdir())


def test_modified_cached_report_is_not_accepted_as_recovery(tmp_path):
    root = sandbox(tmp_path)
    run_daily(root, now=NOW)
    path = root / "reports" / "2026-10-09.json"
    data = json.loads(path.read_text())
    data["scenarios"][0]["evidence"][-1]["digest"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DrillSafetyError, match="cached drill report"):
        run_daily(root, now=NOW)


def test_concurrent_or_crashed_lease_fails_closed(tmp_path):
    root = sandbox(tmp_path)
    (root / "leases").mkdir()
    (root / "leases" / "2026-10-09.lock").mkdir()
    result = run_daily(root, now=NOW)
    assert result["status"] == "capability_gap"
    assert result["capability_gaps"] == ["daily_lease_busy"]
    assert not (root / "reports" / "2026-10-09.json").exists()


def test_false_positive_repair_claim_never_passes(tmp_path):
    root = sandbox(tmp_path)
    def liar(project, fixture, version):
        return RepairSignal("repaired", version)
    outcome = run_scenario(root, FIXTURES[0], repair=liar, now=NOW)
    assert outcome["status"] == "capability_gap"
    assert outcome["capability_gap"] == "repair_not_proven"
    assert outcome["evidence"][-1]["digest"] != outcome["baseline_version"]


def test_no_controller_reconciliation_means_no_recovery(tmp_path, monkeypatch):
    from dev_mcp.fleet.controller import Controller
    root = sandbox(tmp_path)
    monkeypatch.setattr(Controller, "tick", lambda self: 0)
    result = run_scenario(root, FIXTURES[0], now=NOW)
    assert result["status"] == "capability_gap"
    assert result["capability_gap"] == "agent_stuck"
    assert result["cleanup"] == "removed"


def test_stuck_agent_and_expired_wake_never_green(tmp_path):
    root = sandbox(tmp_path)
    stuck = run_scenario(root, FIXTURES[1], repair=lambda *args: None, now=NOW)
    assert stuck["capability_gap"] == "agent_stuck"
    def expired(project, fixture, version):
        restore_fixture(project, fixture)
        return RepairSignal("repaired", version, NOW - timedelta(seconds=1))
    expired_result = run_scenario(root, FIXTURES[1], repair=expired, now=NOW)
    assert expired_result["capability_gap"] == "expired_wake"
    assert expired_result["evidence"][-1]["digest"] == expired_result["baseline_version"]


def test_stale_proof_version_does_not_pass(tmp_path):
    root = sandbox(tmp_path)
    def stale(project, fixture, version):
        restore_fixture(project, fixture)
        return RepairSignal("repaired", "0" * 64)
    result = run_scenario(root, FIXTURES[2], repair=stale, now=NOW)
    assert result["capability_gap"] == "stale_subject_version"


def test_timeout_is_open_capability_gap(tmp_path):
    root = sandbox(tmp_path)
    ticks = iter([0.0, 30.0, 31.0])
    result = run_scenario(root, FIXTURES[0], now=NOW, clock=lambda: next(ticks), timeout_seconds=5)
    assert result["capability_gap"] == "agent_timed_out"


def test_bad_baseline_blocks_fault_and_repair(tmp_path):
    root = sandbox(tmp_path)
    def break_baseline(project):
        (project / "gate" / "status").write_text("invalid\n")
    result = run_scenario(root, FIXTURES[0], baseline_hook=break_baseline, now=NOW)
    assert result["capability_gap"] == "baseline_failed"
    assert result["fault_version"] is None and not result["evidence"]


def test_cleanup_failure_is_reported_not_hidden(tmp_path):
    root = sandbox(tmp_path)
    def broken_cleaner(project):
        raise OSError("unavailable")
    result = run_scenario(root, FIXTURES[0], now=NOW, cleaner=broken_cleaner)
    assert result["status"] == "capability_gap"
    assert result["capability_gap"] == "cleanup_failed"
    assert result["cleanup"] == "failed:OSError"
    assert len(list(root.glob("case-*"))) == 1


def test_production_root_and_symlink_rejected(tmp_path):
    with pytest.raises(DrillSafetyError):
        run_daily(tmp_path, now=NOW)
    with pytest.raises(DrillSafetyError):
        run_daily(Path("C:/hub/dev-mcp-work"), now=NOW)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "nested"
    alias.mkdir()
    link = alias / "fleet-drill-sandbox"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    with pytest.raises(DrillSafetyError):
        run_daily(link, now=NOW)


def test_separate_days_are_independent(tmp_path):
    root = sandbox(tmp_path)
    a = run_daily(root, now=NOW)
    b = run_daily(root, now=NOW + timedelta(days=1))
    assert a["date"] != b["date"]
    assert len(list((root / "reports").iterdir())) == 2
