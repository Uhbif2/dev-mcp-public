"""TZ-29 synthetic device/cloud/CI/browser drivers. No real machines/accounts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest

from dev_mcp.operator_catalog import NAMES
from dev_mcp.operator_core import Operator
from dev_mcp.operators.adapters import (
    AdapterRuntime, BackendReceipt, BrowserPlan, ResourceSnapshot, make_adapters,
)

HASH = "a" * 64
ENV = "b" * 64
COMMIT = "1" * 40
SOURCE = "2" * 64
EVIDENCE = "proof:fixture"


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def args_for(op, root):
    out = {"workspace": str(root / "workspace"), "target": "staging",
           "execution_id": "f" * 32}
    if op == "android.build_install":
        out.update(artifact_sha256=HASH, build_profile="debug")
    elif op == "device.screens":
        out.update(scenario="smoke", count=2)
    elif op == "cloud.deploy":
        out.update(artifact_sha256=HASH, release_id="release_1")
    elif op == "eval.live":
        out.update(corpus_sha256=HASH, sample_count=3)
    elif op == "ci.run":
        out.update(commit_sha=COMMIT, workflow="clean_start")
    elif op == "second_pc.run":
        out.update(artifact_sha256=HASH, plan_id="fresh_install")
    elif op == "browser.task":
        out.update(task_id="oauth_account", url="https://accounts.example.test/oauth",
                   mode="configure")
    return out


class Backend:
    def __init__(self, name):
        self.name = name
        self.snapshot = ResourceSnapshot(resource_id="resource-" + name.replace(".", "-"),
                                         online=True, free_ram_mib=4096, free_disk_mib=8192)
        self.calls = []
        self.rollbacks = []
        self.fail = False
        self.secret_error = False
        self.bad_sha = False
        self.cloud_current = HASH
        self.pause = None
        self.proceed = None
        self.last_profile = None

    def probe(self, op):
        assert op == self.name
        return self.snapshot

    def run(self, op, params, profile):
        self.calls.append((op, dict(params)))
        self.last_profile = profile
        if self.pause is not None:
            self.pause.set()
            assert self.proceed.wait(5)
        if self.secret_error:
            raise RuntimeError("Authorization: Bearer should-not-escape")
        version = params.get("artifact_sha256", params.get("corpus_sha256",
                                                          SOURCE if op == "ci.run" else HASH))
        return BackendReceipt(
            operation=op, request_sha=_digest({"operation": op, "params": dict(params)}),
            resource_id=self.snapshot.resource_id, subject_version=("0" * 64 if self.bad_sha else version),
            env_version=ENV, ref=EVIDENCE, outcome="fail" if self.fail else "pass",
            issuer="trusted-remote", signature="s" * 128,
            artifact_sha256=params.get("artifact_sha256", ""),
            corpus_sha256=params.get("corpus_sha256", ""),
            commit_sha=params.get("commit_sha", ""),
            fresh=op in {"ci.run", "second_pc.run"},
            user_visible=op in {"ci.run", "second_pc.run"},
            metrics={"items": 1, **(
                {"screens": params["count"]} if op == "device.screens" else
                {"samples": params["sample_count"]} if op == "eval.live" else
                {"readback": True} if op == "browser.task" else {})},
        )

    def readback(self, params):
        return self.cloud_current

    def rollback(self, params):
        self.rollbacks.append(dict(params))
        return True


@pytest.fixture()
def built(tmp_path):
    (tmp_path / "profile_root").mkdir()
    drivers = {op: Backend(op) for op in NAMES}
    runtime = AdapterRuntime(
        tmp_path / "adapter.sqlite", drivers,
        verify_remote=lambda r: r.issuer == "trusted-remote" and r.signature == "s" * 128,
        profile_root=tmp_path / "profile_root",
        browser_plans={"oauth_account": BrowserPlan("oauth_account", "accounts.example.test",
                                                     "configure", "oauth-isolated",
                                                     "Create OAuth account")},
    )
    return tmp_path, drivers, runtime


@pytest.mark.parametrize("op", sorted(NAMES))
def test_all_named_operations_emit_versioned_proof(built, op):
    root, drivers, runtime = built
    params = args_for(op, root)
    adapters = make_adapters(runtime)
    proof = adapters[op](params)
    assert proof.verdict == "pass"
    assert proof.subject_version == (SOURCE if op == "ci.run" else
                                     HASH)
    assert proof.env_version == ENV
    assert proof.ref == EVIDENCE
    assert proof.kind in {"run", "screen", "delivery", "measure", "external"}
    assert proof.metrics["items"] == 1
    assert len(drivers[op].calls) == 1
    if op == "browser.task":
        assert drivers[op].last_profile == root / "profile_root" / "isolated-oauth-isolated"
        assert "agent-chrome" not in str(drivers[op].last_profile)
    else:
        assert drivers[op].last_profile is None


def test_hermetic_android_offline_and_human_occupied_pc(built):
    root, drivers, runtime = built
    for name, changed in [
        ("android.build_install", {"online": False}),
        ("device.screens", {"online": False}),
        ("second_pc.run", {"human_using": True}),
    ]:
        backend = drivers[name]
        backend.snapshot = replace(backend.snapshot, **changed)
        proof = runtime.execute(name, args_for(name, root))
        assert proof.verdict == "fail" and proof.metrics["needs_human"] == changed.get("human_using", False)
        assert not backend.calls


def test_browser_handoff_and_allowlisted_plan(built):
    root, drivers, runtime = built
    browser = drivers["browser.task"]
    browser.fail = True
    proof = runtime.execute("browser.task", args_for("browser.task", root))
    assert proof.verdict == "fail"
    assert len(browser.calls) == 1
    other = dict(args_for("browser.task", root), task_id="unknown")
    assert runtime.execute("browser.task", other).verdict == "fail"
    mismatched = dict(args_for("browser.task", root), url="https://evil.example.test/")
    assert runtime.execute("browser.task", mismatched).verdict == "fail"
    assert len(browser.calls) == 1


def test_sanitizes_api_secret_error_and_no_replay(built):
    root, drivers, runtime = built
    cloud = drivers["cloud.deploy"]
    cloud.secret_error = True
    args = args_for("cloud.deploy", root)
    first = runtime.execute("cloud.deploy", args)
    assert first.verdict == "fail"
    assert "should-not-escape" not in json.dumps(first.__dict__)
    assert len(cloud.rollbacks) == 1
    assert runtime.execute("cloud.deploy", args) == first
    assert len(cloud.calls) == 1


def test_cloud_readback_and_rollback_not_success(built):
    root, drivers, runtime = built
    cloud = drivers["cloud.deploy"]
    cloud.cloud_current = "0" * 64
    result = runtime.execute("cloud.deploy", args_for("cloud.deploy", root))
    assert result.verdict == "fail"
    assert len(cloud.rollbacks) == 1
    assert len(cloud.calls) == 1


@pytest.mark.parametrize("outcome", ["fail", "needs_human"])
def test_cloud_signed_nonpass_receipt_rolls_back(built, outcome):
    root, drivers, runtime = built
    cloud = drivers["cloud.deploy"]
    run = cloud.run

    def signed_nonpass(op, params, profile):
        # Keep the otherwise valid signed backend receipt; only its outcome
        # changes. An HTTP/transport exception would cover a different path.
        return replace(run(op, params, profile), outcome=outcome)

    cloud.run = signed_nonpass
    args = args_for("cloud.deploy", root)
    first = runtime.execute("cloud.deploy", args)
    assert first.verdict == "fail"
    assert len(cloud.calls) == 1
    assert cloud.rollbacks == [args]
    assert runtime.execute("cloud.deploy", args) == first
    assert len(cloud.calls) == 1  # cached failed outcome must not redeploy
    assert cloud.rollbacks == [args]  # or roll back a second time


def test_ci_timeout_and_wrong_artifact_hash_fail(built):
    root, drivers, runtime = built
    drivers["ci.run"].secret_error = True  # simulates remote timeout exception
    proof = runtime.execute("ci.run", args_for("ci.run", root))
    assert proof.verdict == "fail"
    drivers["second_pc.run"].bad_sha = True
    proof = runtime.execute("second_pc.run", args_for("second_pc.run", root))
    assert proof.verdict == "fail"


def test_unsigned_remote_result_fails_closed(built):
    root, drivers, runtime = built
    # Verifier configured in trusted Operator; rejected receipts cannot pass.
    runtime.verify_remote = lambda _: False
    proof = runtime.execute("eval.live", args_for("eval.live", root))
    assert proof.verdict == "fail"


def test_durable_replay_and_corrupt_journal(built):
    root, drivers, runtime = built
    params = args_for("ci.run", root)
    first = runtime.execute("ci.run", params)
    assert first.verdict == "pass"
    assert runtime.execute("ci.run", params) == first
    assert len(drivers["ci.run"].calls) == 1
    with sqlite3.connect(runtime.db) as db:
        db.execute("UPDATE adapter_receipts SET receipt_sha=?", ("0" * 64,))
    replay = runtime.execute("ci.run", params)
    assert replay.verdict == "fail" and len(drivers["ci.run"].calls) == 1


def test_atomic_lease_blocks_parallel_driver_calls(built):
    root, drivers, runtime = built
    ci = drivers["ci.run"]
    ci.pause, ci.proceed = threading.Event(), threading.Event()
    results = []
    thread = threading.Thread(target=lambda: results.append(runtime.execute(
        "ci.run", args_for("ci.run", root))))
    thread.start()
    try:
        assert ci.pause.wait(5)
        params = dict(args_for("ci.run", root), execution_id="e" * 32)
        second = runtime.execute("ci.run", params)
        assert second.verdict == "fail"
        assert len(ci.calls) == 1
    finally:
        ci.proceed.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert results[0].verdict == "pass"


def test_operator_budget_caps_and_structured_public_evidence(tmp_path, built):
    root, drivers, runtime = built
    budget = tmp_path / "budget.toml"
    grant = json.dumps(str(root))
    sections = []
    for op in sorted(NAMES):
        sections.append(f'''
[operations."{op}"]
approved = true
cost_usd = "0.01"
max_cost_usd = "0.01"
max_calls = 1
period_seconds = 86400
roots = [{grant}]
targets = ["staging"]
hosts = ["accounts.example.test"]
''')
    budget.write_text('version = 1\napproved = true\napproval_id = "fixture-approved"\n'
                      'expires_at = "2099-01-01T00:00:00Z"\n' + "\n".join(sections),
                      encoding="utf-8")
    operator = Operator(budget, tmp_path / "operator-ledger.sqlite",
                        make_adapters(runtime),
                        clock=lambda: datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp())
    params = args_for("cloud.deploy", root)
    first = operator.handle("cloud.deploy", params)
    assert first["ok"] is True and first["evidence"]["producer"] == "operator"
    assert first["evidence"]["subject_version"] == HASH
    assert first["evidence"]["ref"].startswith("opref:")
    assert operator.handle("cloud.deploy", params) == first
    assert len(drivers["cloud.deploy"].calls) == 1
    bigger = dict(params, execution_id="d" * 32)
    denied = operator.handle("cloud.deploy", bigger)
    assert denied["ok"] is False and denied["error"]["code"] == "budget_exhausted"
    assert len(drivers["cloud.deploy"].calls) == 1
