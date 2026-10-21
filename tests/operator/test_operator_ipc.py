"""Synthetic local IPC contract; no production key, socket or backend is used."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.operator_cli import (
    IPC_FRAME_MAX,
    OperatorIPCClient,
    _ipc_read_line,
    _ipc_recv_pipe,
    _ipc_send,
    _ipc_send_pipe,
    _ipc_sign,
    serve_ipc,
)
from dev_mcp.operator_core import AdapterEvidence, Operator


def _address(tmp_path: Path) -> str:
    if os.name == "nt":
        sep = chr(92)
        return sep * 2 + "." + sep + "pipe" + sep + "fleet-ipc-test-" + uuid.uuid4().hex
    return str(tmp_path / "operator.sock")


def _is_pipe(address: str) -> bool:
    return os.name == "nt"


def _connect(address: str):
    if _is_pipe(address):
        from multiprocessing.connection import Client
        return Client(address, family="AF_PIPE")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(2)
    conn.connect(address)
    return conn


def _send(conn, address: str, packet: dict) -> None:
    if _is_pipe(address):
        _ipc_send_pipe(conn, packet)
    else:
        _ipc_send(conn, packet, 2)


def _recv(conn, address: str) -> dict:
    if _is_pipe(address):
        return _ipc_recv_pipe(conn, 2)
    return _ipc_read_line(conn, 2)


def _wake(address: str) -> None:
    if _is_pipe(address):
        with _connect(address):
            pass


@contextmanager
def _server(tmp_path: Path, op, *, timeout_s: float = 0.5, max_peers: int = 8):
    address = _address(tmp_path)
    key_file = tmp_path / "ipc-auth.bin"
    key_file.write_bytes(b"synthetic-only-transport-key-" + b"x" * 32)
    stop = threading.Event()
    thread = threading.Thread(
        target=serve_ipc,
        args=(op, address, key_file),
        kwargs={"timeout_s": timeout_s, "max_peers": max_peers, "stop_event": stop},
        daemon=True,
    )
    thread.start()
    # AF_PIPE has no filesystem presence; the initial unauthenticated connection
    # is a readiness probe and must not call the Operator.
    for _ in range(60):
        try:
            with _connect(address):
                break
        except (FileNotFoundError, OSError):
            time.sleep(0.02)
    else:
        stop.set()
        pytest.fail("Operator IPC listener unavailable")
    try:
        yield address, key_file
    finally:
        stop.set()
        _wake(address)
        thread.join(3)
        assert not thread.is_alive(), "Operator IPC leaked a listener"


class FakeOperator:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def handle(self, operation: str, params: dict) -> dict:
        self.calls.append((operation, params))
        return {"ok": False, "error": {"code": "adapter_unavailable"}}


def test_authenticated_response_and_replay_denied(tmp_path):
    op = FakeOperator()
    with _server(tmp_path, op) as (address, key_file):
        client = OperatorIPCClient(address, key_file, timeout_s=1.5)
        assert client("device.screens", {"scenario": "synthetic"}) == {
            "ok": False, "error": {"code": "adapter_unavailable"},
        }
        assert len(op.calls) == 1
        # Authenticated, but timestamp-expired messages must also fail.
        expired = _ipc_sign(
            {"nonce": "a" * 32, "ts": int(time.time()) - 100,
             "operation": "device.screens", "params": {}},
            key_file.read_bytes(), b"operator-ipc-request-v1",
        )
        with _connect(address) as conn:
            _send(conn, address, expired)
            with pytest.raises((EOFError, ValueError, OSError)):
                _recv(conn, address)
        packet = _ipc_sign(
            {"nonce": "b" * 32, "ts": int(time.time()),
             "operation": "device.screens", "params": {}},
            key_file.read_bytes(), b"operator-ipc-request-v1",
        )
        with _connect(address) as conn:
            _send(conn, address, packet)
            assert _recv(conn, address)["nonce"] == "b" * 32
        with _connect(address) as conn:
            _send(conn, address, packet)
            with pytest.raises((EOFError, ValueError, OSError)):
                _recv(conn, address)
        # Bad MAC is never forwarded, even when frame and nonce look valid.
        with _connect(address) as conn:
            _send(conn, address, {**packet, "nonce": "c" * 32})
            with pytest.raises((EOFError, ValueError, OSError)):
                _recv(conn, address)
        assert len(op.calls) == 2


def test_oversized_frame_and_wrong_key_fail_closed(tmp_path):
    op = FakeOperator()
    with _server(tmp_path, op) as (address, key_file):
        wrong_key = tmp_path / "other-ipc-auth.bin"
        wrong_key.write_bytes(b"z" * 48)
        wrong = OperatorIPCClient(address, wrong_key, timeout_s=1)
        assert wrong("device.screens", {})["error"]["code"] == "operator_unavailable"
        with _connect(address) as conn:
            payload = b"x" * (IPC_FRAME_MAX + 1)
            try:
                if _is_pipe(address):
                    conn.send_bytes(payload)
                else:
                    conn.sendall(payload)
            except (BrokenPipeError, ConnectionResetError):
                # Windows may close a rejected oversized frame during write.
                pass
            else:
                with pytest.raises((EOFError, ValueError, OSError)):
                    _recv(conn, address)
        assert not op.calls


def _operator_with_synthetic_budget(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    budgets = tmp_path / "synthetic-budgets.toml"
    budgets.write_text(
        'version = 1\napproved = true\napproval_id = "synthetic-test"\n'
        f'expires_at = "{expiry}"\n'
        '[operations."device.screens"]\napproved = true\n'
        'cost_usd = "1.00"\nmax_cost_usd = "2.00"\n'
        'max_calls = 2\nperiod_seconds = 86400\n'
        f'roots = [{json.dumps(str(workspace))}]\ntargets = ["node"]\n',
        encoding="utf-8",
    )
    calls = []

    def adapter(params):
        calls.append(params["scenario"])
        return AdapterEvidence(
            kind="screen", ref="synthetic-only",
            subject_version="a" * 64, env_version="b" * 64,
            verdict="pass",
        )

    op = Operator(budgets, tmp_path / "ledger.sqlite",
                  adapters={"device.screens": adapter})
    return op, workspace, calls


def test_real_operator_budget_and_idempotency_over_ipc(tmp_path):
    op, workspace, calls = _operator_with_synthetic_budget(tmp_path)
    # Exercise budget/idempotency, not the tight-deadline contract tested
    # separately: loaded CI scheduling can exceed the 0.5 s fixture timeout.
    with _server(tmp_path, op, timeout_s=5.0) as (address, key_file):
        client = OperatorIPCClient(address, key_file, timeout_s=5.0)

        def invoke(scenario):
            return client("device.screens", {"workspace": str(workspace),
                                              "target": "node", "scenario": scenario})

        first = invoke("home")
        assert first["ok"] is True
        assert first["evidence"]["producer"] == "operator"
        assert invoke("home") == first
        assert invoke("office")["ok"] is True
        assert invoke("other")["error"]["code"] == "budget_exhausted"
        assert client("arbitrary.shell", {})["error"]["code"] == "unknown_operation"
        assert calls == ["home", "office"]


def test_client_timeout_is_bounded(tmp_path):
    address = _address(tmp_path)
    key = tmp_path / "ipc-auth.bin"
    key.write_bytes(b"k" * 48)
    ready = threading.Event()

    def hold():
        if _is_pipe(address):
            from multiprocessing.connection import Listener
            with Listener(address, family="AF_PIPE") as listener:
                ready.set()
                with listener.accept():
                    time.sleep(0.5)
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(address)
                listener.listen(1)
                ready.set()
                with listener.accept()[0]:
                    time.sleep(0.5)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert ready.wait(2)
    start = time.monotonic()
    result = OperatorIPCClient(address, key, timeout_s=0.1)("device.screens", {})
    elapsed = time.monotonic() - start
    assert result == {"ok": False, "error": {"code": "operator_unavailable"}}
    assert elapsed < 0.35
    thread.join(2)
    assert not thread.is_alive()


def test_standalone_cli_process_remains_fail_closed(tmp_path):
    """Prove the actual launcher CLI serves IPC without the delivery bridge."""
    _op, workspace, _calls = _operator_with_synthetic_budget(tmp_path)
    address = _address(tmp_path)
    key_file = tmp_path / "ipc-auth.bin"
    key_file.write_bytes(b"q" * 48)
    command = [
        sys.executable, "-m", "dev_mcp.operator_cli", "--serve",
        "--budgets", str(tmp_path / "synthetic-budgets.toml"),
        "--ledger", str(tmp_path / "child-ledger.sqlite"),
        "--ipc-socket", address, "--ipc-key-file", str(key_file),
    ]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        client = OperatorIPCClient(address, key_file, timeout_s=0.5)
        request = {"workspace": str(workspace), "target": "node", "scenario": "home"}
        for _ in range(80):
            assert child.poll() is None, "Operator launcher exited during startup"
            response = client("device.screens", request)
            if response["error"]["code"] == "adapter_unavailable":
                break
            time.sleep(0.04)
        else:
            pytest.fail("Operator process did not become available via IPC")
        assert response == {"ok": False, "error": {"code": "adapter_unavailable"}}
    finally:
        child.terminate()
        try:
            child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=3)
