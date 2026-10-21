"""JSON-lines or authenticated local IPC for a dedicated Operator OS process.

Launcher must run as a restricted operator identity with protected budget, ledger,
and vault files. No shell and no arbitrary Python module selected by fleet input.
TZ-29 supplies trusted adapters; until then the CLI is fail-closed.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import socket
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import TextIO

from .operator_catalog import public_catalog
from .operator_core import Operator, verify_public_result


# AF_UNIX (POSIX) or AF_PIPE (Windows) is not the authorization boundary.
# Launcher must ACL-protect the endpoint and the separate transport key file;
# only Operator holds budget, ledger and vault privileges.
IPC_FRAME_MAX = 16384
IPC_CLOCK_SKEW_S = 30
IPC_MAX_PEERS = 8
IPC_MAX_TIMEOUT_S = 60.0


def _ipc_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or not 32 <= path.stat().st_size <= 128:
        raise ValueError("invalid_ipc_key_file")
    key = path.read_bytes()
    if not 32 <= len(key) <= 128:
        raise ValueError("invalid_ipc_key_file")
    return key


def _ipc_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _ipc_sign(payload: dict, key: bytes, domain: bytes) -> dict:
    body = _ipc_bytes(payload)
    mac = hmac.new(key, domain + b"\x00" + body, hashlib.sha256).hexdigest()
    return {**payload, "mac": mac}


def _ipc_verify(packet: object, key: bytes, domain: bytes, fields: set[str]) -> bool:
    if type(packet) is not dict or set(packet) != fields | {"mac"}:
        return False
    mac = packet["mac"]
    if type(mac) is not str or len(mac) != 64:
        return False
    try:
        body = _ipc_bytes({k: v for k, v in packet.items() if k != "mac"})
    except (TypeError, ValueError, RecursionError):
        return False
    expected = hmac.new(key, domain + b"\x00" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)


def _ipc_read_line(conn: socket.socket, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    data = bytearray()
    while len(data) <= IPC_FRAME_MAX:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ipc_deadline")
        conn.settimeout(remaining)
        part = conn.recv(min(4096, IPC_FRAME_MAX + 1 - len(data)))
        if not part:
            raise ValueError("ipc_eof")
        data.extend(part)
        if len(data) > IPC_FRAME_MAX:
            raise ValueError("ipc_request_too_large")
        if b"\n" in part:
            if not data.endswith(b"\n") or data.count(b"\n") != 1:
                raise ValueError("ipc_invalid_frame")
            value = json.loads(data[:-1])
            if type(value) is not dict:
                raise ValueError("ipc_invalid_frame")
            return value
    raise ValueError("ipc_request_too_large")


def _ipc_send(conn: socket.socket, packet: dict, timeout_s: float) -> None:
    payload = _ipc_bytes(packet) + b"\n"
    if len(payload) > IPC_FRAME_MAX:
        raise ValueError("ipc_response_too_large")
    deadline = time.monotonic() + timeout_s
    sent = 0
    while sent < len(payload):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ipc_deadline")
        conn.settimeout(remaining)
        count = conn.send(payload[sent:])
        if count <= 0:
            raise OSError("ipc_socket_closed")
        sent += count


def _ipc_pipe_address(address: str) -> bool:
    """Windows AF_PIPE only; never interpret an arbitrary network/pipe address."""
    prefix = chr(92) * 2 + "." + chr(92) + "pipe" + chr(92)
    return address.startswith(prefix) and re.fullmatch(
        r"[A-Za-z0-9_-]{1,80}", address[len(prefix):]) is not None


def _ipc_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("ipc_deadline")
    return remaining


def _ipc_pipe_millis(deadline: float) -> int:
    # Windows kernel waits are bounded by a *single* monotonic deadline,
    # including named-pipe connect, write, and response read.
    return max(1, int(_ipc_remaining(deadline) * 1000))


def _ipc_pipe_overlapped(ov, error: int, deadline: float) -> int:
    """Finish or cancel kernel-overlapped I/O; never strand a worker thread."""
    import _winapi

    if error == _winapi.ERROR_IO_PENDING:
        try:
            state = _winapi.WaitForMultipleObjects(
                [ov.event], False, _ipc_pipe_millis(deadline),
            )
            if state != _winapi.WAIT_OBJECT_0:
                raise TimeoutError("ipc_deadline")
        except BaseException:
            ov.cancel()
            raise
    elif error != 0:
        ov.cancel()
        raise OSError("ipc_pipe_io_failed")
    transferred, final_error = ov.GetOverlappedResult(True)
    if final_error != 0:
        raise OSError("ipc_pipe_io_failed")
    return transferred


def _ipc_pipe_write_handle(handle: int, payload: bytes, deadline: float) -> None:
    import _winapi

    _ipc_remaining(deadline)
    ov, error = _winapi.WriteFile(handle, payload, overlapped=True)
    if _ipc_pipe_overlapped(ov, error, deadline) != len(payload):
        raise OSError("ipc_pipe_short_write")


def _ipc_pipe_read_handle(handle: int, deadline: float) -> dict:
    import _winapi

    _ipc_remaining(deadline)
    # Message-mode pipes yield a complete JSON frame. Anything longer than
    # IPC_FRAME_MAX is rejected before JSON parsing.
    ov, error = _winapi.ReadFile(handle, IPC_FRAME_MAX + 1, overlapped=True)
    length = _ipc_pipe_overlapped(ov, error, deadline)
    if length > IPC_FRAME_MAX:
        raise ValueError("ipc_response_too_large")
    value = json.loads(bytes(ov.getbuffer()[:length]))
    if type(value) is not dict:
        raise ValueError("ipc_invalid_frame")
    return value


def _ipc_recv_pipe(conn, timeout_s: float) -> dict:
    return _ipc_pipe_read_handle(conn.fileno(), time.monotonic() + timeout_s)


def _ipc_send_pipe(conn, packet: dict, timeout_s: float = 5.0) -> None:
    payload = _ipc_bytes(packet)
    if len(payload) > IPC_FRAME_MAX:
        raise ValueError("ipc_response_too_large")
    _ipc_pipe_write_handle(conn.fileno(), payload, time.monotonic() + timeout_s)


def _ipc_pipe_exchange(address: str, packet: dict, timeout_s: float) -> dict:
    """Hard kernel deadline: AF_PIPE client never uses a soft worker.join()."""
    import _winapi

    deadline = time.monotonic() + timeout_s
    payload = _ipc_bytes(packet)
    if len(payload) > IPC_FRAME_MAX:
        raise ValueError("ipc_request_too_large")
    # PipeClient() from multiprocessing retries for ~20s without a caller
    # timeout. Connect via WaitNamedPipe and overlapped CreateFile instead.
    while True:
        _winapi.WaitNamedPipe(address, _ipc_pipe_millis(deadline))
        try:
            handle = _winapi.CreateFile(
                address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                0, _winapi.NULL, _winapi.OPEN_EXISTING,
                _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL,
            )
            break
        except OSError as exc:
            if exc.winerror != _winapi.ERROR_PIPE_BUSY:
                raise
            _ipc_remaining(deadline)
    try:
        _winapi.SetNamedPipeHandleState(
            handle, _winapi.PIPE_READMODE_MESSAGE, None, None,
        )
        _ipc_pipe_write_handle(handle, payload, deadline)
        return _ipc_pipe_read_handle(handle, deadline)
    finally:
        _winapi.CloseHandle(handle)


class OperatorIPCClient:
    """Trusted launcher injection for build_server(operator_client=...).

    The fleet only supplies operation and params. A protected, separate IPC
    credential authenticates both endpoints and binds responses to a nonce.
    """

    def __init__(self, address: Path | str, key_file: Path | str, *,
                 timeout_s: float = 30.0):
        if not 0 < timeout_s <= IPC_MAX_TIMEOUT_S:
            raise ValueError("invalid_ipc_timeout")
        self.address = str(address)
        if self.address.startswith(chr(92) * 2 + "." + chr(92) + "pipe") and not _ipc_pipe_address(self.address):
            raise ValueError("invalid_ipc_pipe_name")
        self.key = _ipc_key(Path(key_file))
        self.timeout_s = timeout_s

    def __call__(self, operation: str, params: dict) -> dict:
        nonce = secrets.token_hex(16)
        try:
            deadline = time.monotonic() + self.timeout_s
            request = _ipc_sign({"nonce": nonce, "ts": int(time.time()),
                                 "operation": operation, "params": params},
                                self.key, b"operator-ipc-request-v1")
            if _ipc_pipe_address(self.address):
                response = _ipc_pipe_exchange(
                    self.address, request, _ipc_remaining(deadline),
                )
            else:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                    conn.settimeout(_ipc_remaining(deadline))
                    conn.connect(self.address)
                    _ipc_send(conn, request, _ipc_remaining(deadline))
                    response = _ipc_read_line(conn, _ipc_remaining(deadline))
            if not _ipc_verify(response, self.key, b"operator-ipc-response-v1",
                               {"nonce", "result"}):
                raise ValueError("ipc_response_auth_failed")
            if response["nonce"] != nonce:
                raise ValueError("ipc_response_nonce_mismatch")
            return verify_public_result(response["result"], operation)
        except (OSError, TimeoutError, AttributeError, TypeError, ValueError,
                RecursionError, EOFError, ImportError):
            # No raw transport error, address, exception or credential crosses
            # the public fleet_op boundary.
            return {"ok": False, "error": {"code": "operator_unavailable"}}


def load_trusted_operator_ipc(config_path: Path | str) -> OperatorIPCClient:
    """Construct the callable used by DeliveryRuntime.fleet_op.

    The controller/bootstrap passes a fixed OS-protected TOML *file path*;
    NEVER a path, key, timeout or address from an intent, task or fleet_op
    payload. The only accepted section is:

      [operator_ipc]
      address = "/protected/run/operator.sock"  # or a local Windows named pipe
      key_file = "C:/protected/operator-ipc.key"
      timeout_s = 15.0

    The Operator service itself must be started separately under a restricted
    OS identity. This factory does not start a subprocess or grant privileges.
    The launcher must ACL-protect the config, transport key and endpoint.
    Missing or malformed config fails closed; it is NOT a reason to construct
    an in-process Operator with fleet credentials.
    """
    path = Path(config_path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_size > 4096):
        raise ValueError("trusted_operator_ipc_config_unavailable")
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    if type(config) is not dict or set(config) != {"operator_ipc"}:
        raise ValueError("invalid_operator_ipc_config")
    settings = config["operator_ipc"]
    if type(settings) is not dict or set(settings) != {
            "address", "key_file", "timeout_s",
    }:
        raise ValueError("invalid_operator_ipc_config")
    address, key_file, timeout_s = (
        settings["address"], settings["key_file"], settings["timeout_s"],
    )
    if (type(address) is not str or not address
            or type(key_file) is not str or not Path(key_file).is_absolute()
            or type(timeout_s) not in (int, float)
            or not 0 < timeout_s <= IPC_MAX_TIMEOUT_S):
        raise ValueError("invalid_operator_ipc_config")
    if not _ipc_pipe_address(address) and not Path(address).is_absolute():
        raise ValueError("invalid_operator_ipc_address")
    return OperatorIPCClient(address, key_file, timeout_s=timeout_s)


def serve_ipc(operator: Operator, address: Path | str, key_file: Path | str, *,
              timeout_s: float = 5.0, max_peers: int = IPC_MAX_PEERS,
              stop_event: threading.Event | None = None) -> None:
    """Bounded authenticated local server; no production bridge dependency.

    A valid request may spend only Operator-approved budgets; peer concurrency,
    input/output bytes, replay lifetime and client I/O all have hard limits.
    Operator adapters manage their own execution deadline and durable ledger.
    """
    if not 0 < timeout_s <= IPC_MAX_TIMEOUT_S or not 1 <= max_peers <= 32:
        raise ValueError("invalid_ipc_limits")
    key = _ipc_key(Path(key_file))
    address = str(address)
    is_pipe = _ipc_pipe_address(address)
    if address.startswith(chr(92) * 2 + "." + chr(92) + "pipe") and not is_pipe:
        raise ValueError("invalid_ipc_pipe_name")
    path = Path(address)
    if not is_pipe and (path.exists() or path.is_symlink() or not path.parent.is_dir()):
        raise ValueError("ipc_socket_path_unavailable")
    nonce_seen: dict[str, int] = {}
    nonce_lock = threading.Lock()
    peers = threading.BoundedSemaphore(max_peers)

    def handle_peer(conn) -> None:
        try:
            with conn:
                packet = (_ipc_recv_pipe(conn, timeout_s) if is_pipe
                          else _ipc_read_line(conn, timeout_s))
                if not _ipc_verify(packet, key, b"operator-ipc-request-v1",
                                   {"nonce", "ts", "operation", "params"}):
                    return
                nonce, ts = packet["nonce"], packet["ts"]
                now = int(time.time())
                if (type(nonce) is not str or len(nonce) != 32
                        or any(c not in "0123456789abcdef" for c in nonce)
                        or type(ts) is not int or abs(now - ts) > IPC_CLOCK_SKEW_S
                        or type(packet["operation"]) is not str
                        or type(packet["params"]) is not dict):
                    return
                with nonce_lock:
                    for old, when in list(nonce_seen.items()):
                        if now - when > IPC_CLOCK_SKEW_S * 2:
                            del nonce_seen[old]
                    if nonce in nonce_seen or len(nonce_seen) >= 4096:
                        return
                    nonce_seen[nonce] = now
                try:
                    result = operator.handle(packet["operation"], packet["params"])
                    result = verify_public_result(result, packet["operation"])
                except Exception:
                    result = {"ok": False, "error": {"code": "operator_unavailable"}}
                response = _ipc_sign({"nonce": nonce, "result": result},
                                     key, b"operator-ipc-response-v1")
                if is_pipe:
                    _ipc_send_pipe(conn, response, timeout_s)
                else:
                    _ipc_send(conn, response, timeout_s)
        except (OSError, TimeoutError, ValueError, TypeError, RecursionError,
                EOFError):
            pass
        finally:
            peers.release()

    if is_pipe:
        from multiprocessing.connection import Listener
        with Listener(address, family="AF_PIPE") as listener:
            while stop_event is None or not stop_event.is_set():
                conn = listener.accept()
                if stop_event is not None and stop_event.is_set():
                    conn.close()
                    break
                if not peers.acquire(blocking=False):
                    conn.close()
                    continue
                threading.Thread(target=handle_peer, args=(conn,), daemon=True).start()
        return

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        owner_stat = path.stat() if path.exists() else None
        try:
            listener.listen(max_peers)
            listener.settimeout(0.2)
            while stop_event is None or not stop_event.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                if not peers.acquire(blocking=False):
                    conn.close()
                    continue
                threading.Thread(target=handle_peer, args=(conn,), daemon=True).start()
        finally:
            # Never unlink a pre-existing path or another process's replacement.
            if owner_stat is not None and path.exists():
                current = path.stat()
                if (current.st_dev, current.st_ino) == (owner_stat.st_dev, owner_stat.st_ino):
                    path.unlink()


def _configure_private_screens(runtime, config_path: Path):
    """Only the OS-launched Operator can bind private capture and frame loading.

    Without an independently authenticated pixel source, screen operations are
    denied even if the gateway reports a signed metadata-only success.
    """
    from .operators.adapters.device_screens import (
        PinnedCaptureDriver, PrivateScreenStore, UnavailableScreenBackend,
        install_capture_bridge,
    )
    from .operators.adapters.gateway import GatewayBackend

    backend = runtime.backends["device.screens"]
    runtime.backends["device.screens"] = UnavailableScreenBackend()
    spec = tomllib.loads(config_path.read_text(encoding="utf-8")).get("screens")
    if spec is None:
        return None, None
    if not isinstance(backend, GatewayBackend):
        raise ValueError("trusted_gateway_required")
    # All file names and keys are Operator-only launch configuration, never
    # fleet_op parameters, public receipts, stdout or process environment.
    store = PrivateScreenStore(spec["store_dir"], Path(spec["store_key_file"]).read_bytes())
    driver = PinnedCaptureDriver(
        backend.gateway, Path(spec["capture_key_file"]).read_bytes(),
        spec["subject_version_file"], spec["env_version_file"],
    )
    runtime.backends["device.screens"] = backend
    loader = install_capture_bridge(
        runtime, store, fetch_frames=driver.fetch_frames,
        trusted_versions=driver.trusted_versions,
    )
    def current_version_loader(proof, scenario):
        # Even a correctly signed historic bundle stops being acceptable once
        # the trusted device/build/environment version changes on disk.
        from .operators.adapters.device_screens import ScreenStoreError
        subject, env = driver.trusted_versions({})
        if proof.get("subject_version") != subject or proof.get("env_version") != env:
            raise ScreenStoreError("capture_version_stale")
        return loader(proof, scenario)
    return current_version_loader, driver


def _privileged_operator(budgets: Path, ledger: Path, config: Path):
    from .operators.adapters import make_adapters
    from .operators.adapters.gateway import configure_operator_adapters

    runtime = configure_operator_adapters(config)
    loader, driver = _configure_private_screens(runtime, config)
    return Operator(budgets, ledger, adapters=make_adapters(runtime)), loader, driver


def _trusted_visual_runner(operator: Operator, loader, driver, *,
                           intent: str, workspace: str, target: str,
                           rubric_paths=None, look=None):
    """Create LensRunner INSIDE the protected process, never over fleet IPC."""
    from .fleet.lenses.engine import LensRunner
    if loader is None or driver is None:
        raise ValueError("private_screens_not_configured")
    subject, _ = driver.trusted_versions({})
    kw = {"intent": intent, "subject_version": subject, "workspace": workspace,
          "target": target, "operator_call": operator.handle, "frame_loader": loader}
    if rubric_paths is not None:
        kw["rubric_paths"] = rubric_paths
    if look is not None:
        kw["look"] = look
    return LensRunner(**kw)


def serve(operator: Operator, source: TextIO, destination: TextIO) -> None:
    for raw in source:
        try:
            if len(raw) > 16384:
                response = {"ok": False, "error": {"code": "request_too_large"}}
            else:
                req = json.loads(raw)
                if not isinstance(req, dict) or set(req) != {"operation", "params"}:
                    response = {"ok": False, "error": {"code": "invalid_request"}}
                else:
                    response = operator.handle(req["operation"], req["params"])
        except (TypeError, ValueError, RecursionError):
            response = {"ok": False, "error": {"code": "invalid_json"}}
        destination.write(json.dumps(response, sort_keys=True, ensure_ascii=True) + "\n")
        destination.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dedicated fail-closed Operator process")
    parser.add_argument("--catalog", action="store_true", help="print public contract only")
    parser.add_argument("--serve", action="store_true", help="JSON-lines stdin/stdout service")
    parser.add_argument("--ipc-socket", type=Path,
                        help="trusted AF_UNIX path or Windows AF_PIPE name (requires --serve)")
    parser.add_argument("--ipc-key-file", type=Path,
                        help="ACL-protected transport key, separate from Operator vault")
    parser.add_argument("--budgets", type=Path, help="trusted launcher private budget path")
    parser.add_argument("--ledger", type=Path, help="trusted launcher private ledger path")
    parser.add_argument("--adapters-config", type=Path,
                        help="trusted operator-only gateway, profile and attestation TOML")
    parser.add_argument("--visual-lens", choices=["visual_motion", "error_empty", "first_run"],
                        help="execute visual lens inside the private Operator process")
    parser.add_argument("--visual-intent-file", type=Path,
                        help="trusted launcher-owned intent document")
    parser.add_argument("--visual-workspace", help="trusted launcher workspace")
    parser.add_argument("--visual-target", help="trusted launcher target")
    args = parser.parse_args(argv)
    if args.catalog:
        print(json.dumps(public_catalog(), sort_keys=True))
        return 0
    if (args.serve == bool(args.visual_lens) or not args.budgets or not args.ledger):
        parser.error("exactly one of --serve/--visual-lens requires private budgets and ledger")
    if bool(args.ipc_socket) != bool(args.ipc_key_file) or (
            args.ipc_socket is not None and not args.serve):
        parser.error("AF_UNIX requires --serve, --ipc-socket and --ipc-key-file")
    try:
        loader = driver = None
        if args.adapters_config is not None:
            operator, loader, driver = _privileged_operator(
                args.budgets, args.ledger, args.adapters_config,
            )
        else:
            operator = Operator(args.budgets, args.ledger)
        if args.visual_lens:
            intent_file = args.visual_intent_file
            if (intent_file is None or intent_file.is_symlink()
                    or not intent_file.is_file()
                    or intent_file.stat().st_size > 32000
                    or not args.visual_workspace or not args.visual_target):
                raise ValueError("trusted_visual_context_required")
            runner = _trusted_visual_runner(
                operator, loader, driver, intent=intent_file.read_text(encoding="utf-8"),
                workspace=args.visual_workspace, target=args.visual_target,
            )
            # Two fresh passes are needed to prove stability; each invocation
            # generates a distinct capture execution_id and cannot replay old pixels.
            first = runner.run_once(args.visual_lens)
            report = runner.run_once(args.visual_lens) if first.status == "open" else first
            print(json.dumps({"lens": args.visual_lens, "status": report.status,
                              "subject_version": report.subject_version,
                              "clean_streak": report.clean_streak,
                              "obstacles": list(report.obstacles)}))
            return 0 if report.done else 3
    except Exception:
        # Avoid printing exceptions or config data from operator-private directories.
        print("operator_unavailable", file=sys.stderr)
        return 2
    if args.ipc_socket is not None:
        try:
            serve_ipc(operator, args.ipc_socket, args.ipc_key_file)
        except Exception:
            print("operator_unavailable", file=sys.stderr)
            return 2
    else:
        serve(operator, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
