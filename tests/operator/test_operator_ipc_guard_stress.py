"""Controlled real-IPC budget/idempotency regression for guard CPU contention.

All resources are synthetic and per-case: no launcher, provider keys or live fleet DB.
The original test_operator_ipc.py and production Operator code are not patched.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.operator_cli import OperatorIPCClient, serve_ipc
from dev_mcp.operator_core import AdapterEvidence, Operator


ITERATIONS = 20
CLIENT_TIMEOUT_S = 2.0


def _make_operator(tmp_path: Path, *, max_calls: int = 2, adapter_delay_s: float = 0.008):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    budget_path = tmp_path / "synthetic-budgets.toml"
    expires = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    budget_path.write_text(
        'version = 1\napproved = true\napproval_id = "operator-stress-fixture"\n'
        f'expires_at = "{expires}"\n'
        '[operations."device.screens"]\napproved = true\n'
        'cost_usd = "1.00"\nmax_cost_usd = "2.00"\n'
        f'max_calls = {max_calls}\nperiod_seconds = 86400\n'
        f'roots = [{json.dumps(str(workspace))}]\ntargets = ["node"]\n',
        encoding="utf-8",
    )
    calls: list[str] = []
    timings: dict[str, list[float]] = {"adapter_ms": [], "ledger_reserve_ms": [],
                                       "ledger_complete_ms": []}

    def adapter(params):
        started = time.perf_counter()
        calls.append(params["scenario"])
        try:
            time.sleep(adapter_delay_s)
            return AdapterEvidence(kind="screen", ref="synthetic-only",
                                   subject_version="a" * 64, env_version="b" * 64,
                                   verdict="pass")
        finally:
            timings["adapter_ms"].append((time.perf_counter() - started) * 1000)

    operator = Operator(budget_path, tmp_path / "ledger.sqlite",
                        adapters={"device.screens": adapter})

    for method, bucket in (("reserve", "ledger_reserve_ms"),
                           ("complete", "ledger_complete_ms")):
        original = getattr(operator.ledger, method)

        def timed(*args, _original=original, _bucket=bucket, **kwargs):
            started = time.perf_counter()
            try:
                return _original(*args, **kwargs)
            finally:
                timings[_bucket].append((time.perf_counter() - started) * 1000)

        setattr(operator.ledger, method, timed)
    return operator, workspace, calls, timings


def _pipe_address(socket_dir: Path) -> str:
    if os.name == "nt":
        return r"\\.\pipe\operator-stress-" + uuid.uuid4().hex
    return str(socket_dir / "operator.sock")


def _connect(address: str):
    if os.name == "nt":
        from multiprocessing.connection import Client
        return Client(address, family="AF_PIPE")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(1)
    try:
        conn.connect(address)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def _listener(tmp_path: Path, operator, timing):
    with tempfile.TemporaryDirectory(prefix="operator-ipc-stress-") as location:
        address = _pipe_address(Path(location))
        auth = tmp_path / "ipc-auth.bin"
        auth.write_bytes(b"synthetic-test-auth-only-" + b"x" * 32)
        stop = threading.Event()
        crashes = []

        def serve():
            try:
                serve_ipc(operator, address, auth, timeout_s=0.5, max_peers=8,
                          stop_event=stop)
            except Exception as exc:
                crashes.append(type(exc).__name__)

        thread = threading.Thread(target=serve, daemon=True,
                                  name="operator-stress-listener-" + uuid.uuid4().hex[:8])
        started = time.perf_counter()
        thread.start()
        try:
            for _ in range(80):
                if crashes:
                    pytest.fail(f"listener_crashed={crashes}")
                try:
                    with _connect(address):
                        break
                except (FileNotFoundError, ConnectionRefusedError, OSError):
                    time.sleep(0.02)
            else:
                pytest.fail("listener_not_ready")
            timing["listener_ready_ms"] = (time.perf_counter() - started) * 1000
            yield address, auth
        finally:
            stop.set()
            if os.name == "nt" and thread.is_alive():
                try:
                    with _connect(address):
                        pass
                except OSError:
                    pass
            thread.join(3)
            assert not thread.is_alive(), "listener_thread_leaked"
            assert not crashes, f"listener_crashed={crashes}"
            if os.name != "nt":
                assert not Path(address).exists(), "unix_socket_leaked"


@contextmanager
def _cpu_competition():
    """A paced CPU worker overlaps IPC without starving the test's own server.

    time.sleep(0) in a tight Python loop yielded no sustained idle interval;
    under guard load it saturated the GIL and made a 2s IPC deadline expire.
    A fixed wait after each CPU burst retains measurable contention while
    allowing the listener and SQLite ledger to make forward progress.
    """
    stop = threading.Event()
    progressed = threading.Event()
    turns = [0]

    def busy():
        seed = 1
        while not stop.is_set():
            for _ in range(3000):
                seed = ((seed * 1103515245) + 12345) & 0x7FFFFFFF
            turns[0] += 1
            progressed.set()
            if stop.wait(0.008):
                break

    thread = threading.Thread(target=busy, daemon=True, name="operator-stress-cpu")
    thread.start()
    try:
        assert progressed.wait(1), "cpu_competition_not_running"
        yield turns
    finally:
        stop.set()
        thread.join(2)
        assert not thread.is_alive(), "cpu_competition_thread_leaked"


def _assert_cycle(client, workspace: Path, calls: list[str], timing: dict):
    rpc_ms = []

    def request(scenario: str):
        started = time.perf_counter()
        result = client("device.screens", {"workspace": str(workspace),
                                          "target": "node", "scenario": scenario})
        rpc_ms.append((time.perf_counter() - started) * 1000)
        return result

    first = request("home")
    assert first.get("ok") is True, f"first_home_failed:{first.get('error')}"
    assert first["evidence"]["producer"] == "operator"
    assert first["evidence"]["verdict"] == "pass"
    assert calls == ["home"], f"first_home_reexecuted:{len(calls)}"

    repeated = request("home")
    assert repeated == first, "cached_evidence_changed"
    assert calls == ["home"], f"duplicate_adapter_execution:{len(calls)}"

    second = request("office")
    assert second.get("ok") is True, "second_unique_must_succeed"
    assert second["evidence"]["request_fingerprint"] != first["evidence"]["request_fingerprint"]
    assert calls == ["home", "office"], f"second_adapter_count:{len(calls)}"

    third = request("other")
    assert third == {"ok": False, "error": {"code": "budget_exhausted"}}, "third_not_budget_exhausted"
    assert calls == ["home", "office"], "exhausted_budget_executed_adapter"

    invalid = client("arbitrary.shell", {})
    assert invalid == {"ok": False, "error": {"code": "unknown_operation"}}, "invalid_operation_not_denied"
    assert calls == ["home", "office"], "invalid_operation_executed_adapter"
    timing["rpc_ms"] = rpc_ms


@pytest.mark.parametrize("iteration", range(ITERATIONS))
def test_operator_ipc_under_cpu_contention(tmp_path: Path, iteration: int):
    """20 genuinely isolated budgets, ledgers, auth files, listeners and CPU competitors."""
    case = tmp_path / f"iteration-{iteration:02d}"
    case.mkdir()
    operator, workspace, calls, timing = _make_operator(
        case, adapter_delay_s=0.006 + (iteration % 4) * 0.002,
    )
    try:
        with _listener(case, operator, timing) as (address, auth):
            client = OperatorIPCClient(address, auth, timeout_s=CLIENT_TIMEOUT_S)
            with _cpu_competition() as turns:
                before_calls = turns[0]
                _assert_cycle(client, workspace, calls, timing)
                assert turns[0] > before_calls, "cpu_competition_did_not_overlap_ipc"
        assert len(timing["adapter_ms"]) == 2
        assert len(timing["rpc_ms"]) == 4
        assert len(timing["ledger_reserve_ms"]) >= 3
        assert len(timing["ledger_complete_ms"]) == 2
        assert timing["listener_ready_ms"] >= 0
        # These are observations, NOT budget usage: the approved budget remains
        # exactly two *unique* calls irrespective of elapsed wall-clock time.
        print("ipc_stress_timing", {
            "iteration": iteration,
            "listener_readiness_ms": round(timing["listener_ready_ms"], 2),
            "rpc_wall_ms": [round(ms, 2) for ms in timing["rpc_ms"]],
            "adapter_ms": [round(ms, 2) for ms in timing["adapter_ms"]],
            "ledger_reserve_ms": [round(ms, 2) for ms in timing["ledger_reserve_ms"]],
            "ledger_complete_ms": [round(ms, 2) for ms in timing["ledger_complete_ms"]],
            "transport_and_validation_ms": "residual_not_individually_attributed",
        })
    except BaseException:
        # Safe timing diagnostics only: never log the auth file contents or full params.
        print("ipc_stress_failure", {"iteration": iteration, "timing": timing,
                                     "adapter_count": len(calls)})
        raise


def test_contract_detects_reduced_unique_call_limit(tmp_path: Path):
    """Mutation-sensitivity: a one-call budget must fail the same cycle assertion."""
    operator, workspace, calls, timing = _make_operator(tmp_path, max_calls=1)
    with _listener(tmp_path, operator, timing) as (address, auth):
        client = OperatorIPCClient(address, auth, timeout_s=CLIENT_TIMEOUT_S)
        with pytest.raises(AssertionError, match="second_unique_must_succeed"):
            _assert_cycle(client, workspace, calls, timing)
    assert calls == ["home"], "one_call_limit_was_bypassed"


def test_contract_detects_changed_replay_evidence(tmp_path: Path):
    """Mutation-sensitivity: even a validly signed, altered replay must be rejected."""
    operator, workspace, calls, timing = _make_operator(tmp_path)
    original_handle = operator.handle
    home_count = [0]

    def altered_cache(operation, params):
        result = original_handle(operation, params)
        if operation == "device.screens" and params.get("scenario") == "home" and result.get("ok"):
            home_count[0] += 1
            if home_count[0] == 2:
                altered = {**result["evidence"],
                           "observed_ts": result["evidence"]["observed_ts"] + 1}
                return {**result, "evidence": altered}
        return result

    operator.handle = altered_cache
    with _listener(tmp_path, operator, timing) as (address, auth):
        client = OperatorIPCClient(address, auth, timeout_s=CLIENT_TIMEOUT_S)
        with pytest.raises(AssertionError, match="cached_evidence_changed"):
            _assert_cycle(client, workspace, calls, timing)
    assert home_count[0] == 2 and calls == ["home"], "replay_mutation_not_exercised"
