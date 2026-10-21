"""Trusted Operator startup -> signed remote capture -> private pixels -> LensRunner.

All backend responses/images are synthetic. No adb, network, Gemini or real vault.
This is intentionally an integration test of the ACTUAL Operator.handle path,
not an isolated PrivateScreenStore roundtrip.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import zlib
from dataclasses import asdict, replace

import pytest

from dev_mcp import operator_cli
from dev_mcp.operator_catalog import NAMES
from dev_mcp.operators.adapters import AdapterRuntime, BackendReceipt
from dev_mcp.operators.adapters.device_screens import ScreenStoreError
from dev_mcp.operators.adapters.gateway import (
    GatewayBackend, PinnedHmacVerifier,
)
from dev_mcp.operators.adapters.runtime import _digest

V = "a" * 64
ENV = "b" * 64
ATTESTATION_KEY = b"backend-receipt-test-key-32-bytes-plus"
CAPTURE_KEY = b"independent-capture-test-key-32-bytes"
STORE_KEY = b"operator-private-store-test-key-32-bytes"


def png(index):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    raw = b"".join(b"\0" + bytes((22 + index, 39, 51)) * 2 for _ in range(2))
    return (bytes.fromhex("89504e470d0a1a0a") + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class SignedDriver:
    """Synthetic separately signed receipt and pixel bundle; no network."""

    def __init__(self):
        self.runs = []
        self.captures = {}
        self.tamper = False

    def request(self, action, operation, data):
        if action == "probe":
            return {"resource_id": "device-1", "online": True, "human_using": False,
                    "free_ram_mib": 4096, "free_disk_mib": 4096}
        if action == "run":
            params = data["params"]
            ref = "private:device:" + params["execution_id"]
            receipt = BackendReceipt(
                operation=operation,
                request_sha=_digest({"operation": operation, "params": params}),
                resource_id="device-1", subject_version=V, env_version=ENV,
                ref=ref, outcome="pass", issuer="trusted-remote", fresh=True,
                metrics={"screens": params["count"]},
            )
            payload = asdict(receipt)
            payload["signature"] = hmac.new(
                ATTESTATION_KEY, _digest({k: v for k, v in payload.items()
                                         if k != "signature"}).encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            receipt = BackendReceipt(**payload)
            self.captures[ref] = (receipt, params)
            self.runs.append(dict(params))
            return asdict(receipt)
        if action == "capture":
            receipt, params = self.captures[data["receipt_ref"]]
            assert data["receipt_request_sha"] == receipt.request_sha
            assert data["execution_id"] == params["execution_id"]
            payload = {
                "receipt_ref": receipt.ref,
                "receipt_request_sha": receipt.request_sha,
                "subject_version": receipt.subject_version,
                "env_version": receipt.env_version,
                "frames": [base64.b64encode(png(i + len(self.runs))).decode("ascii")
                           for i in range(params["count"])],
            }
            signature = hmac.new(
                CAPTURE_KEY,
                json.dumps(payload, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":")).encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if self.tamper:
                payload["frames"][0] = base64.b64encode(png(98)).decode("ascii")
            return {"payload": payload, "hmac": signature}
        raise AssertionError("Unexpected named action")


def _protected_file(path, value):
    path.write_bytes(value)
    path.chmod(0o600)
    return path


@pytest.fixture
def wired(tmp_path, monkeypatch):
    from dev_mcp.operators.adapters import gateway as module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    private.chmod(0o700)
    (tmp_path / "device.version").write_text(V, encoding="ascii")
    (tmp_path / "environment.version").write_text(ENV, encoding="ascii")
    for path in (tmp_path / "device.version", tmp_path / "environment.version"):
        path.chmod(0o600)
    _protected_file(tmp_path / "store.key", STORE_KEY)
    _protected_file(tmp_path / "capture.key", CAPTURE_KEY)
    config = tmp_path / "operator.toml"
    config.write_text(
        "[screens]\n"
        f'store_dir = "{private.as_posix()}"\n'
        f'store_key_file = "{(tmp_path / "store.key").as_posix()}"\n'
        f'capture_key_file = "{(tmp_path / "capture.key").as_posix()}"\n'
        f'subject_version_file = "{(tmp_path / "device.version").as_posix()}"\n'
        f'env_version_file = "{(tmp_path / "environment.version").as_posix()}"\n',
        encoding="utf-8",
    )
    gateway = SignedDriver()
    runtime = AdapterRuntime(
        tmp_path / "adapter.sqlite",
        {name: GatewayBackend(name, gateway) for name in NAMES},
        PinnedHmacVerifier("trusted-remote", ATTESTATION_KEY),
        profile_root=profile,
    )
    monkeypatch.setattr(module, "configure_operator_adapters", lambda path: runtime)
    budgets = tmp_path / "budgets.toml"
    budgets.write_text(
        'version = 1\napproved = true\napproval_id = "test-screens"\n'
        'expires_at = "2099-01-01T00:00:00+00:00"\n'
        '[operations."device.screens"]\napproved = true\n'
        'cost_usd = "0.01"\nmax_cost_usd = "100.00"\nmax_calls = 100\n'
        'period_seconds = 86400\n'
        f'roots = ["{workspace.as_posix()}"]\ntargets = ["device1"]\n',
        encoding="utf-8",
    )
    op, loader, driver = operator_cli._privileged_operator(
        budgets, tmp_path / "operator-ledger.sqlite", config,
    )
    return tmp_path, workspace, op, loader, driver, gateway, config, budgets


def test_real_operator_handle_stores_pixels_and_trusted_lens_reads_them(wired):
    root, workspace, op, loader, driver, gateway, _, _ = wired
    rubric = root / "rubric.md"
    rubric.write_text("Do not clip images or overlap buttons.", encoding="utf-8")
    images = []
    runner = operator_cli._trusted_visual_runner(
        op, loader, driver, intent="Legible mobile design",
        workspace=str(workspace), target="device1",
        rubric_paths=(rubric,),
        look=lambda frames, prompt: (images.append(tuple(frames))
                                     or {"ok": True, "defects": []}),
    )
    assert runner.run_once("visual_motion").status == "open"
    final = runner.run_once("visual_motion")
    assert final.done and final.clean_streak == 2
    assert len(gateway.runs) == 4 and len(images) == 4
    assert len({r["execution_id"] for r in gateway.runs}) == 4
    assert len(list((root / "private").glob("*/manifest.json"))) == 4
    assert all(x.startswith(bytes.fromhex("89504e470d0a1a0a"))
               for group in images for x in group)
    assert all("png" not in json.dumps(o.evidence) or "data" not in o.evidence
               for run in final.runs for o in run)


def test_tampered_stale_and_unbacked_proofs_rejected(wired):
    root, workspace, op, loader, driver, gateway, _, _ = wired
    params = {"workspace": str(workspace), "target": "device1",
              "scenario": "home", "count": 1, "execution_id": "c" * 32}
    response = op.handle("device.screens", params)
    assert response["ok"] and response["evidence"]["kind"] == "screen"
    proof = response["evidence"]
    assert len(loader(proof, "home")) == 1
    for changed in (
        dict(proof, ref="opref:" + "0" * 64),
        dict(proof, env_version="0" * 64),
        dict(proof, request_fingerprint="0" * 64),
    ):
        with pytest.raises(ScreenStoreError):
            loader(changed, "home")
    with pytest.raises(ScreenStoreError):
        loader(proof, "other-scenario")
    (root / "device.version").write_text("d" * 64, encoding="ascii")
    with pytest.raises(ScreenStoreError, match="capture_version_stale"):
        loader(proof, "home")
    newer = dict(params, execution_id="d" * 32)
    assert op.handle("device.screens", newer)["ok"] is False


def test_unsigned_pixel_data_and_missing_capture_config_fail_closed(wired, monkeypatch):
    root, workspace, op, loader, driver, gateway, config, budgets = wired
    gateway.tamper = True
    response = op.handle("device.screens", {
        "workspace": str(workspace), "target": "device1",
        "scenario": "home", "count": 1, "execution_id": "e" * 32,
    })
    assert response["ok"] is False
    assert response.get("evidence", {}).get("verdict") == "fail"
    assert not list((root / "private").glob("*/manifest.json"))

    config.write_text("version = 1\n", encoding="utf-8")
    from dev_mcp.operators.adapters import gateway as module
    new_runtime = AdapterRuntime(
        root / "second-adapter.sqlite",
        {name: GatewayBackend(name, gateway) for name in NAMES},
        PinnedHmacVerifier("trusted-remote", ATTESTATION_KEY),
        profile_root=root / "profile",
    )
    monkeypatch.setattr(module, "configure_operator_adapters", lambda _: new_runtime)
    unconfigured, no_loader, no_driver = operator_cli._privileged_operator(
        budgets, root / "second-operator-ledger.sqlite", config,
    )
    assert no_loader is no_driver is None
    failure = unconfigured.handle("device.screens", {
        "workspace": str(workspace), "target": "device1",
        "scenario": "home", "count": 1, "execution_id": "f" * 32,
    })
    assert failure["ok"] is False
