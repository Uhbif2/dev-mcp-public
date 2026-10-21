"""No live HTTP: gateway parser, signer, finite routing and CLI config."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, replace

import pytest

from dev_mcp.operators.adapters import BackendReceipt
from dev_mcp.operators.adapters.gateway import (
    GatewayBackend, GatewayError, PinnedHmacVerifier, RemoteGateway,
    configure_operator_adapters,
)
from dev_mcp.operators.adapters.runtime import _digest


def test_pinned_attestation_checks_payload_not_just_signature():
    key = b"some-trusted-remote-secret-bits-32+" * 2
    signer = PinnedHmacVerifier("trusted-remote", key)
    receipt = BackendReceipt(
        operation="ci.run", request_sha="a" * 64, resource_id="ci-runner",
        subject_version="b" * 64, env_version="c" * 64,
        ref="operator-internal:123", outcome="pass", issuer="trusted-remote",
        commit_sha="1" * 40, fresh=True, user_visible=True,
    )
    payload = asdict(receipt)
    payload.pop("signature")
    signed = replace(receipt, signature=hmac.new(
        key, _digest(payload).encode("ascii"), hashlib.sha256).hexdigest())
    assert signer(signed)
    assert not signer(replace(signed, subject_version="d" * 64))
    assert not signer(replace(signed, issuer="forged"))
    assert not signer(replace(signed, signature="0" * 64))


@pytest.mark.parametrize("url", [
    "http://example.com", "file:///tmp/", "https://user:pass@example.com",
    "https://example.com/extra", "https://example.com/?key=secret",
    "https://example.com/#fragment", "http://192.168.1.4:9999",
])
def test_gateway_rejects_ssrf_credentials_and_url_paths(url):
    with pytest.raises(GatewayError, match="invalid_gateway_endpoint"):
        RemoteGateway(url, b"private-token")


def test_remote_gateway_uses_named_actions_only():
    gateway = RemoteGateway("http://127.0.0.1:8767", b"secret")
    with pytest.raises(GatewayError, match="invalid_named_action"):
        gateway.request("shell", "ci.run", None)
    with pytest.raises(GatewayError, match="invalid_named_action"):
        gateway.request("run", "bash", {})


def test_browser_bridge_preserves_isolated_profile_without_shell(tmp_path):
    class GatewayFake:
        def __init__(self):
            self.calls = []

        def request(self, action, operation, body):
            self.calls.append((action, operation, body))
            return {
                "operation": "browser.task", "request_sha": "a" * 64,
                "resource_id": "browser-1", "subject_version": "b" * 64,
                "env_version": "c" * 64, "ref": "trusted-ref", "outcome": "pass",
                "issuer": "trusted-remote", "signature": "f" * 64,
            }

    fake = GatewayFake()
    from dev_mcp.operators.adapters import BrowserPlan
    backend = GatewayBackend("browser.task", fake, {
        "oauth": BrowserPlan("oauth", "example.com", "configure",
                             "isolated", "Create OAuth app", "oauth-private-slot"),
    })
    params = {"task_id": "oauth", "url": "https://example.com", "mode": "configure",
              "workspace": str(tmp_path), "target": "staging"}
    backend.run("browser.task", params, tmp_path / "operator-isolated-profile")
    assert fake.calls[0][0:2] == ("run", "browser.task")
    assert fake.calls[0][2]["isolated_browser_profile"].endswith(
        "operator-isolated-profile")
    assert fake.calls[0][2]["trusted_goal"] == "Create OAuth app"
    assert fake.calls[0][2]["vault_slot"] == "oauth-private-slot"
    assert "cmd" not in fake.calls[0][2] and "shell" not in fake.calls[0][2]


def test_missing_private_config_fails_closed(tmp_path):
    with pytest.raises(GatewayError, match="adapter_configuration_unavailable"):
        configure_operator_adapters(tmp_path / "missing.toml")
