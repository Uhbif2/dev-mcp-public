"""No48 B3/H (owner 09.11): building the project's own code is a class, not an owner question."""
from __future__ import annotations

from types import SimpleNamespace

from dev_mcp import request_triage

POLICY = SimpleNamespace(exec_allowed_bin=["python", "git", "cargo", "rustc", "gradle", "go", "dotnet"],
                         tasks={})


def test_cargo_manifest_path_is_approved_by_class():
    # Live TSPU sub:LEAD-8 request: path = the Cargo.toml, not the binary.
    decision, note = request_triage.decide(
        {"kind": "bin", "path": "D:\\Craft Anything\\app\\Cargo.toml", "summary": "cargo test"}, POLICY)
    assert decision == "approved" and "cargo" in note


def test_bare_cargo_and_gradle_wrapper_are_approved():
    assert request_triage.decide({"kind": "bin", "path": "cargo"}, POLICY)[0] == "approved"
    assert request_triage.decide({"kind": "bin", "summary": "нужен gradlew для сборки APK"},
                                 POLICY)[0] == "approved"


def test_unknown_binary_still_denied():
    assert request_triage.decide({"kind": "bin", "path": "docker", "summary": "docker"}, POLICY)[0] == "denied"


def test_toolchain_not_in_policy_is_not_approved():
    narrow = SimpleNamespace(exec_allowed_bin=["python"], tasks={})
    assert request_triage.decide({"kind": "bin", "path": "cargo"}, narrow)[0] == "denied"
