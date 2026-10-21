import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.operator_budget import BudgetBook, BudgetError
from dev_mcp.operator_cli import serve
from dev_mcp.operator_core import AdapterEvidence, CredentialVault, Operator, verify_public_result


SHA = "b" * 64


def budget_file(tmp_path, *, approved=True, grant=True, maximum="10.00",
                cost="1.00", calls=10, expiry_days=10, targets=None):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    expiry = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()
    values = ", ".join(json.dumps(x) for x in (targets or ["device1"]))
    text = f'''version = 1
approved = {str(approved).lower()}
approval_id = "owner-test-2026"
expires_at = "{expiry}"
[operations."device.screens"]
approved = {str(grant).lower()}
cost_usd = "{cost}"
max_cost_usd = "{maximum}"
max_calls = {calls}
period_seconds = 86400
roots = [{json.dumps(str(root))}]
targets = [{values}]
'''
    path = tmp_path / "budgets.toml"
    path.write_text(text, encoding="utf-8")
    return path, root


def evidence(**overrides):
    data = {"kind": "screen", "ref": "screens/session1", "subject_version": SHA,
            "env_version": "c" * 64, "verdict": "pass", "metrics": {"count": 2}}
    data.update(overrides)
    return AdapterEvidence(**data)


def operator(tmp_path, handler=lambda p: evidence(), **kwargs):
    budget, root = budget_file(tmp_path, **kwargs)
    return Operator(budget, tmp_path / "ledger.sqlite",
                    adapters={"device.screens": handler}), root


def req(workspace, **other):
    return {"workspace": str(workspace), "target": "device1", "scenario": "home", **other}


def test_success_proof_is_bound_to_versions_and_operator(tmp_path):
    op, root = operator(tmp_path)
    result = op.handle("device.screens", req(root))
    assert result["ok"] is True
    proof = result["evidence"]
    assert proof["kind"] == "screen" and proof["producer"] == "operator"
    assert proof["subject_version"] == SHA and proof["env_version"] == "c" * 64
    assert proof["verdict"] == "pass"
    assert type(proof["observed_ts"]) is int
    assert len(proof["request_fingerprint"]) == 64
    assert proof["metrics"]["count"] == 2
    assert result == op.handle("device.screens", req(root))


def test_duplicate_executes_once_even_with_fresh_instance(tmp_path):
    calls = []
    def adapter(params):
        calls.append(1)
        return evidence()
    op, root = operator(tmp_path, handler=adapter)
    first = op.handle("device.screens", req(root))
    another = Operator(op.budgets_path if hasattr(op, "budgets_path") else tmp_path / "budgets.toml",
                       tmp_path / "ledger.sqlite", adapters={"device.screens": adapter})
    assert another.handle("device.screens", req(root)) == first
    assert len(calls) == 1


def test_explicit_execution_id_allows_new_observation(tmp_path):
    calls = []
    op, root = operator(tmp_path, handler=lambda p: (calls.append(1), evidence())[1])
    for key in ["a" * 32, "a" * 32, "b" * 32]:
        assert op.handle("device.screens", req(root, execution_id=key))["ok"]
    assert len(calls) == 2


def test_invalid_scope_and_budget_never_call_adapter(tmp_path):
    called = []
    op, root = operator(tmp_path, handler=lambda params: called.append(1))
    outside = tmp_path / "outside"
    outside.mkdir()
    for workspace in ("../outside", str(outside), str(root / ".." / "outside")):
        result = op.handle("device.screens", req(workspace))
        assert not result["ok"]
        assert result["error"]["code"] in ("invalid_workspace", "workspace_out_of_scope")
    assert op.handle("device.screens", req(root, target="unknown"))["error"]["code"] == "target_out_of_scope"
    assert not called


def test_storage_error_returns_safe_code_without_execution(tmp_path, monkeypatch):
    calls = []
    op, root = operator(tmp_path, handler=lambda p: (calls.append(1), evidence())[1])
    def broken(*args):
        raise sqlite3.OperationalError("open /private/API_KEY=secret failed")
    monkeypatch.setattr(op.ledger, "reserve", broken)
    result = op.handle("device.screens", req(root))
    assert result["error"]["code"] == "budget_store_failed"
    assert "API_KEY" not in json.dumps(result)
    assert calls == []


def test_complete_failure_does_not_release_budget_reservation(tmp_path, monkeypatch):
    op, root = operator(tmp_path)
    original = op.ledger.complete
    monkeypatch.setattr(op.ledger, "complete",
                        lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("token=secret")))
    response = op.handle("device.screens", req(root))
    assert response["error"]["code"] == "ledger_write_failed"
    monkeypatch.setattr(op.ledger, "complete", original)
    assert op.handle("device.screens", req(root))["error"]["code"] == "operation_in_progress"


@pytest.mark.parametrize("settings,code", [
    ({"approved": False}, "owner_approval_required"),
    ({"grant": False}, "operation_not_approved"),
    ({"expiry_days": -1}, "approval_expired"),
])
def test_missing_authorization_fails_closed(tmp_path, settings, code):
    if code == "owner_approval_required":
        with pytest.raises(BudgetError, match=code):
            operator(tmp_path, **settings)
    else:
        op, root = operator(tmp_path, **settings)
        assert op.handle("device.screens", req(root))["error"]["code"] == code


def test_ledger_exhaustion_and_concurrent_budget(tmp_path):
    calls = []
    op, root = operator(tmp_path, handler=lambda params: (calls.append(1), evidence())[1],
                        maximum="2.00", calls=2)
    # Distinct parameters are distinct operations and each reserves 1.00 atomically.
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: op.handle("device.screens", req(root, count=i)),
                                range(1, 11)))
    assert sum(r["ok"] for r in results) == 2
    assert sum(r.get("error", {}).get("code") == "budget_exhausted" for r in results) == 8
    assert len(calls) == 2


def test_duplicate_parallel_race_one_execution(tmp_path):
    import threading
    calls = []
    ready = threading.Barrier(5)
    op, root = operator(tmp_path, handler=lambda params: (calls.append(1), evidence())[1])
    def invoke(_):
        ready.wait()
        return op.handle("device.screens", req(root))
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(invoke, range(5)))
    assert len(calls) == 1
    assert all(r["ok"] or r["error"]["code"] == "operation_in_progress" for r in results)


def test_adapter_secret_exception_not_leaked_and_charged_once(tmp_path, monkeypatch):
    def fail(params):
        raise RuntimeError("api_key=DO-NOT-REVEAL token=secret")
    op, root = operator(tmp_path, handler=fail, maximum="1.00", calls=1)
    response = op.handle("device.screens", req(root))
    assert response["error"]["code"] == "adapter_failed"
    assert "DO-NOT-REVEAL" not in json.dumps(response)
    assert response["evidence"]["verdict"] == "fail"
    assert op.handle("device.screens", req(root)) == response
    assert op.handle("device.screens", req(root, count=2))["error"]["code"] == "budget_exhausted"


def test_adapter_ref_is_sanitized(tmp_path):
    op, root = operator(tmp_path, handler=lambda p: evidence(ref="https://example.test/?token=secretvalue"))
    serialized = json.dumps(op.handle("device.screens", req(root)))
    assert "secretvalue" not in serialized
    assert "opref:" in serialized


def test_browser_host_scope_must_be_explicit(tmp_path):
    budget, root = budget_file(tmp_path)
    expiry = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    budget.write_text(f'''version = 1
approved = true
approval_id = "owner-test-2026"
expires_at = "{expiry}"
[operations."browser.task"]
approved = true
cost_usd = "0.01"
max_cost_usd = "1.00"
max_calls = 10
period_seconds = 86400
roots = [{json.dumps(str(root))}]
targets = ["browser-profile"]
hosts = ["example.com"]
''', encoding="utf-8")
    op = Operator(budget, tmp_path / "ledger.sqlite", adapters={"browser.task": lambda p: evidence(kind="external")})
    base = {"workspace": str(root), "target": "browser-profile", "task_id": "login", "mode": "inspect"}
    assert op.handle("browser.task", {**base, "url": "https://internal.example/path"})["error"]["code"] == "host_out_of_scope"
    assert op.handle("browser.task", {**base, "url": "https://example.com/path"})["ok"]


def test_artifact_version_mismatch_is_not_success(tmp_path):
    budget, root = budget_file(tmp_path)
    expiry = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    budget.write_text(f'''version = 1
approved = true
approval_id = "owner-test-2026"
expires_at = "{expiry}"
[operations."cloud.deploy"]
approved = true
cost_usd = "1.00"
max_cost_usd = "10.00"
max_calls = 10
period_seconds = 86400
roots = [{json.dumps(str(root))}]
targets = ["staging"]
''', encoding="utf-8")
    op = Operator(budget, tmp_path / "ledger.sqlite", adapters={"cloud.deploy": lambda p: evidence(kind="delivery")})
    result = op.handle("cloud.deploy", {"workspace": str(root), "target": "staging",
                                        "release_id": "r-1", "artifact_sha256": "a" * 64})
    assert result["error"]["code"] == "adapter_failed"
    assert result["evidence"]["verdict"] == "fail"


def test_invalid_evidence_fails_closed(tmp_path):
    op, root = operator(tmp_path, handler=lambda p: evidence(subject_version="missing"))
    result = op.handle("device.screens", req(root))
    assert result["error"]["code"] == "adapter_failed"
    assert result["evidence"]["producer"] == "operator"


def test_transport_result_blocks_raw_secrets_and_unexpected_metadata(tmp_path):
    op, root = operator(tmp_path)
    valid = op.handle("device.screens", req(root))
    poisoned = dict(valid)
    poisoned["stdout"] = "Authorization: Basic very-secret-value"
    poisoned["evidence"] = {**valid["evidence"], "trace": "sk-very-secret-value"}
    safe = verify_public_result(poisoned, "device.screens")
    assert "trace" not in json.dumps(safe)
    assert "stdout" not in json.dumps(safe)
    assert "sk-very-secret" not in json.dumps(safe)
    with pytest.raises(ValueError):
        verify_public_result({"ok": False, "error": {"code": "token=abc123"}}, "device.screens")
    with pytest.raises(ValueError):
        verify_public_result({"ok": True, "evidence": {**valid["evidence"], "ref": "secret=raw"}}, "device.screens")


def test_fleet_wire_contract_only_operation_and_params(tmp_path):
    op, root = operator(tmp_path)
    lines = "\n".join([
        json.dumps({"operation": "device.screens", "params": req(root)}),
        json.dumps({"operation": "device.screens", "params": req(root), "budget": 999}),
        '{"operation":',
    ]) + "\n"
    dest = io.StringIO()
    serve(op, io.StringIO(lines), dest)
    response = [json.loads(x) for x in dest.getvalue().splitlines()]
    assert response[0]["ok"]
    assert response[1]["error"]["code"] == "invalid_request"
    assert response[2]["error"]["code"] == "invalid_json"


def test_process_cli_exposes_catalog_without_secret_or_adapter(tmp_path):
    run = subprocess.run([sys.executable, "-m", "dev_mcp.operator_cli", "--catalog"],
                         capture_output=True, text=True, timeout=10)
    assert run.returncode == 0
    assert "device.screens" in json.loads(run.stdout)["operations"]
    assert "secret" not in run.stderr.lower()


def test_separate_process_fails_closed_without_installed_adapter(tmp_path):
    budget, root = budget_file(tmp_path)
    request = json.dumps({"operation": "device.screens", "params": req(root)}) + "\n"
    result = subprocess.run([sys.executable, "-m", "dev_mcp.operator_cli", "--serve",
                             "--budgets", str(budget), "--ledger", str(tmp_path / "ledger.sqlite")],
                            input=request, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout)["error"]["code"] == "adapter_unavailable"


def test_credential_vault_rejects_escape_and_does_not_use_environment(tmp_path, monkeypatch):
    location = tmp_path / "private"
    location.mkdir()
    (location / "api").write_bytes(b"fixture-key")
    monkeypatch.setenv("OPERATOR_API", "environment-token-not-a-credential")
    vault = CredentialVault(location)
    assert vault.read("api") == b"fixture-key"
    with pytest.raises(ValueError):
        vault.read("../api")
    with pytest.raises(ValueError):
        vault.read("OPERATOR_API")


@pytest.mark.asyncio
async def test_mcp_fleet_op_only_accepts_named_operation_and_params(tmp_path):
    from fastmcp import Client
    from dev_mcp.audit import Audit
    from dev_mcp.policy import Policy
    from dev_mcp.server import build_server

    budgets, root = budget_file(tmp_path)
    (tmp_path / "policy.toml").write_text(
        f"roots = [{json.dumps(str(root))}]\nallow_write = false\n", encoding="utf-8")
    policy = Policy.load(tmp_path / "policy.toml")
    audit = Audit(tmp_path / "audit.jsonl")
    valid = {"operation": "device.screens", "params": req(root)}
    server = build_server(policy, audit, fleet_db=tmp_path / "fleet-no-operator.sqlite")
    async with Client(server) as client:
        tools = await client.list_tools()
        schema = next(x.input_schema for x in tools if x.name == "fleet_op")
        assert set(schema["properties"]) == {"operation", "params"}
        denied = await client.call_tool("fleet_op", valid)
        assert json.loads(denied.content[0].text)["error"]["code"] == "operator_unavailable"
        invalid = await client.call_tool("fleet_op", {"operation": "arbitrary.shell", "params": {}})
        assert json.loads(invalid.content[0].text)["error"]["code"] == "unknown_operation"
    # Injection is trusted launcher-only; the agent still sends only operation/params.
    op = Operator(budgets, tmp_path / "ledger.sqlite", adapters={"device.screens": lambda p: evidence()})
    server2 = build_server(policy, Audit(tmp_path / "audit2.jsonl"),
                           fleet_db=tmp_path / "fleet-with-operator.sqlite",
                           operator_client=lambda name, params: op.handle(name, params))
    async with Client(server2) as client:
        success = await client.call_tool("fleet_op", valid)
        payload = json.loads(success.content[0].text)
        assert payload["ok"] and payload["evidence"]["subject_version"] == SHA
