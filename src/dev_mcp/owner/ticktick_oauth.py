"""The dev-mcp server as an OAuth client of the official TickTick MCP (owner, 19.10.2026).

Only https://mcp.ticktick.com, only standard MCP authorization: dynamic client registration,
PKCE S256, authorization code. No API Token, no Open API. The access token lives in
``data/secrets/ticktick-oauth.json`` (covered by the ``*secret*`` deny mask, so fleet agents
cannot read it) and never enters logs, events or exceptions.

TickTick advertises no refresh grant, so the token's own lifetime decides when the owner must
reconnect; ``status()`` reports days left for the system card on the board.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as _secrets
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import ProxyHandler, Request, build_opener

RESOURCE = "https://mcp.ticktick.com/"
PROTECTED_METADATA = "https://mcp.ticktick.com/.well-known/oauth-protected-resource"
SECRET_PATH = Path(__file__).resolve().parents[3] / "data" / "secrets" / "ticktick-oauth.json"
SCOPE = "tasks:read tasks:write"
_ALLOWED_HOSTS = {"ticktick.com", "api.ticktick.com", "mcp.ticktick.com"}


class TickTickAuthError(RuntimeError):
    """Connection is impossible or the stored grant is unusable; message never holds a secret."""


def _opener():
    # The machine-wide HTTP proxy silently hangs some domains (dev-mcp CLAUDE.md, «грабли»).
    return build_opener(ProxyHandler({}))


def _trusted(url: str) -> str:
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS or parts.username:
        raise TickTickAuthError(f"untrusted OAuth endpoint host: {parts.hostname!r}")
    return url


def _json(url: str, *, data: dict | None = None, form: dict | None = None,
          opener: Any = None, timeout: float = 20.0) -> dict:
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        body = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(_trusted(url), data=body, headers=headers, method="POST" if body else "GET")
    try:
        with (opener or _opener()).open(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise TickTickAuthError(f"{urlparse(url).path}: HTTP {exc.code}") from None
    except (URLError, OSError, ValueError) as exc:
        raise TickTickAuthError(f"{urlparse(url).path}: {type(exc).__name__}") from None


def discover(opener: Any = None) -> dict:
    resource = _json(PROTECTED_METADATA, opener=opener)
    servers = resource.get("authorization_servers") or []
    if not servers:
        raise TickTickAuthError("no authorization server advertised")
    base = _trusted(str(servers[0])).rstrip("/")
    meta = _json(base + "/.well-known/oauth-authorization-server", opener=opener)
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        _trusted(str(meta.get(key) or ""))
    if "S256" not in (meta.get("code_challenge_methods_supported") or []):
        raise TickTickAuthError("PKCE S256 unsupported")
    return meta


def register(meta: dict, redirect_uri: str, opener: Any = None) -> str:
    client = _json(meta["registration_endpoint"], data={
        "client_name": "dev-mcp fleet (owner workstation)",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }, opener=opener)
    client_id = client.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise TickTickAuthError("registration returned no client_id")
    return client_id


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(meta: dict, client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return meta["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": SCOPE, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": RESOURCE,
    })


def exchange(meta: dict, client_id: str, redirect_uri: str, code: str, verifier: str,
             opener: Any = None, now: Callable[[], float] = time.time) -> dict:
    token = _json(meta["token_endpoint"], form={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier, "resource": RESOURCE,
    }, opener=opener)
    access = token.get("access_token")
    if not isinstance(access, str) or not access:
        raise TickTickAuthError("token endpoint returned no access_token")
    lifetime = token.get("expires_in")
    return {
        "access_token": access,
        "token_type": token.get("token_type", "Bearer"),
        "scope": token.get("scope", SCOPE),
        "client_id": client_id,
        "obtained_at": now(),
        "expires_at": (now() + float(lifetime)) if isinstance(lifetime, (int, float)) else None,
    }


def save(grant: dict, path: Path | None = None) -> None:
    path = path or SECRET_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(grant), encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path | None = None) -> dict | None:
    path = path or SECRET_PATH
    try:
        grant = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return grant if isinstance(grant, dict) and isinstance(grant.get("access_token"), str) else None


def credential(path: Path | None = None, now: Callable[[], float] = time.time) -> str:
    """The bearer for StreamableHTTP; raises when absent or expired (callers fail closed)."""
    grant = load(path)
    if grant is None:
        raise TickTickAuthError("TickTick не подключён: запустите dev-mcp\\ticktick-connect.cmd")
    expires = grant.get("expires_at")
    if isinstance(expires, (int, float)) and expires <= now() + 60:
        raise TickTickAuthError("ключ TickTick истёк: запустите dev-mcp\\ticktick-connect.cmd")
    return grant["access_token"]


def status(path: Path | None = None, now: Callable[[], float] = time.time) -> dict:
    grant = load(path)
    if grant is None:
        return {"connected": False}
    expires = grant.get("expires_at")
    days = None if not isinstance(expires, (int, float)) else round((expires - now()) / 86400, 1)
    return {"connected": days is None or days > 0, "days_left": days, "scope": grant.get("scope")}


def parse_callback(path_and_query: str, expected_state: str) -> str:
    query = parse_qs(urlparse(path_and_query).query)
    if query.get("error"):
        raise TickTickAuthError("TickTick отказал: " + query["error"][0][:80])
    if query.get("state", [""])[0] != expected_state:
        raise TickTickAuthError("state mismatch")
    code = query.get("code", [""])[0]
    if not code:
        raise TickTickAuthError("no authorization code")
    return code


def new_state() -> str:
    return _secrets.token_urlsafe(24)
