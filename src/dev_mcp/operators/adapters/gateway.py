"""Fixed-endpoint HTTPS bridge for trusted remote runner services.

Network endpoint, bearer credential, receipt-signing key and browser plans
come ONLY from operator-private launcher config, never fleet_op input.
The backend calls bounded NAMED actions; no command, script or shell API.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .runtime import (AdapterRuntime, BackendReceipt, BrowserPlan,
                      ResourceSnapshot, _digest)
from dev_mcp.operator_catalog import NAMES

_MAX_RESPONSE = 32768
# 10 x 4 MiB images after base64 and signed JSON envelope. Capture is private,
# fetched only by the Operator identity, and never exposed over fleet_op.
_MAX_CAPTURE_RESPONSE = 60 * 1024 * 1024


class GatewayError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise GatewayError("redirect_forbidden")


class RemoteGateway:
    """Uniform transport; operations remain finite and individually named."""

    def __init__(self, endpoint: str, token: bytes, *, timeout_s: int = 30):
        url = urllib.parse.urlsplit(endpoint)
        if (url.username or url.password or url.query or url.fragment or url.path not in ("", "/")
                or not url.hostname or url.scheme not in ("http", "https")
                or (url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost"))
                or type(timeout_s) is not int or not 1 <= timeout_s <= 120):
            raise GatewayError("invalid_gateway_endpoint")
        if not token or len(token) > 8192:
            raise GatewayError("invalid_gateway_token")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout_s
        # Do NOT pick up Windows HTTP_PROXY and never follow redirects. TLS
        # verification is left on; certificate identity binds the hostname.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  _NoRedirect())

    def request(self, action: str, operation: str, data: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if (action not in {"probe", "run", "readback", "rollback", "capture"}
                or operation not in NAMES
                or (action == "capture" and operation != "device.screens")):
            raise GatewayError("invalid_named_action")
        route = self.endpoint + "/v1/" + action + "/" + operation
        body = None if data is None else json.dumps(data, sort_keys=True, ensure_ascii=True,
                                                     allow_nan=False).encode("ascii")
        if body is not None and len(body) > 8192:
            raise GatewayError("request_too_large")
        req = urllib.request.Request(
            route, data=body, method="GET" if body is None else "POST",
            headers={"Authorization": "Bearer " + self.token.decode("ascii"),
                     "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                # Strict size cap even when Content-Length absent or dishonest.
                maximum = _MAX_CAPTURE_RESPONSE if action == "capture" else _MAX_RESPONSE
                raw = response.read(maximum + 1)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise GatewayError("remote_unavailable") from None
        if len(raw) > (_MAX_CAPTURE_RESPONSE if action == "capture" else _MAX_RESPONSE):
            raise GatewayError("response_too_large")
        try:
            result = json.loads(raw)
        except (TypeError, UnicodeDecodeError, ValueError) as exc:
            raise GatewayError("invalid_remote_json") from None
        if type(result) is not dict:
            raise GatewayError("invalid_remote_payload")
        return result


class GatewayBackend:
    def __init__(self, operation: str, gateway: RemoteGateway,
                 browser_plans: Mapping[str, BrowserPlan] | None = None):
        if operation not in NAMES:
            raise GatewayError("unknown_operation")
        self.operation = operation
        self.gateway = gateway
        self.browser_plans = dict(browser_plans or {})

    def probe(self, action: str) -> ResourceSnapshot:
        if action != self.operation:
            raise GatewayError("wrong_operation")
        data = self.gateway.request("probe", self.operation, None)
        return ResourceSnapshot(**data)

    def run(self, action: str, params: Mapping[str, Any],
            browser_profile: Path | None) -> BackendReceipt:
        if action != self.operation:
            raise GatewayError("wrong_operation")
        # Profile is a trusted path, not a caller-supplied filesystem path.
        body = {"params": dict(params)}
        if browser_profile is not None:
            body["isolated_browser_profile"] = str(browser_profile)
            plan = self.browser_plans.get(params["task_id"])
            if plan is None:
                raise GatewayError("browser_plan_not_approved")
            # Only trusted launcher config supplies instructions/vault slots.
            # The agent's fleet_op params can never inject either.
            body["trusted_goal"] = plan.instruction
            body["vault_slot"] = plan.vault_slot
        receipt = self.gateway.request("run", self.operation, body)
        return BackendReceipt(**receipt)

    def readback(self, params: Mapping[str, Any]) -> str:
        if self.operation != "cloud.deploy":
            raise GatewayError("readback_only_cloud")
        data = self.gateway.request("readback", self.operation, {"params": dict(params)})
        return data.get("artifact_sha256", "")

    def rollback(self, params: Mapping[str, Any]) -> bool:
        if self.operation != "cloud.deploy":
            raise GatewayError("rollback_only_cloud")
        data = self.gateway.request("rollback", self.operation, {"params": dict(params)})
        return data.get("restored") is True


class PinnedHmacVerifier:
    """Concrete pinned-key authentication of every BackendReceipt field."""

    def __init__(self, issuer: str, key: bytes):
        if (not issuer or not re.fullmatch(r"[A-Za-z0-9_.:-]{4,100}", issuer)
                or len(key) < 32):
            raise GatewayError("invalid_attestation_key")
        self.issuer = issuer
        self.key = key

    def __call__(self, receipt: BackendReceipt) -> bool:
        if receipt.issuer != self.issuer or not re.fullmatch(r"[a-f0-9]{64}", receipt.signature):
            return False
        payload = asdict(receipt)
        signature = payload.pop("signature")
        expected = hmac.new(self.key, _digest(payload).encode("ascii"),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)


def configure_operator_adapters(config_path: str | Path) -> AdapterRuntime:
    """Trusted launcher-only TOML. No path or secret is sourced from fleet_op."""
    try:
        conf = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
        if conf.get("version") != 1:
            raise GatewayError("invalid_config_version")
        spec = conf["remote"]
        gateway_token = Path(spec["token_file"]).read_bytes().strip()
        verify_key = Path(spec["attestation_key_file"]).read_bytes().strip()
        # Require private file contents; never return/print any of them.
        gateway = RemoteGateway(spec["endpoint"], gateway_token,
                                timeout_s=spec.get("timeout_seconds", 30))
        verifier = PinnedHmacVerifier(spec["issuer"], verify_key)
        plans = {name: BrowserPlan(name, data["host"], data["mode"], data["profile_id"],
                                   data.get("instruction", ""), data.get("vault_slot", ""))
                 for name, data in conf.get("browser_plans", {}).items()}
        return AdapterRuntime(
            spec["lease_db"], {name: GatewayBackend(name, gateway, plans) for name in NAMES},
            verifier, profile_root=spec["profile_root"], browser_plans=plans,
            minimum_ram_mib=spec.get("minimum_ram_mib", 512),
            minimum_disk_mib=spec.get("minimum_disk_mib", 512),
        )
    except (KeyError, TypeError, ValueError, OSError, UnicodeError,
            tomllib.TOMLDecodeError) as exc:
        # Never disclose operator-private paths, credentials or provider errors.
        raise GatewayError("adapter_configuration_unavailable") from None
