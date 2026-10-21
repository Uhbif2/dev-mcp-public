"""Where this installation finds things outside its own folder — the one machine-specific place.

Defaults are relative to the installation: the folder that holds dev-mcp is the *hub* (shared
inbox of intents, owner inbox, shared check scripts), and the hub is the only workspace.
A machine overrides them in `machine.toml` next to `pyproject.toml` (not versioned) or in the file
named by `DEV_MCP_MACHINE`:

    workspaces = ['D:/work', 'D:/projects']    # where intent projects may live
    live_home = 'D:/work/dev-mcp'              # the production install: enables process guards
    worktree_base = 'D:/projects/.fleet-wt'
    lens_docs = ['D:/skills/art-director/SKILL.md']
    gateway_health_url = 'https://gateway.example.net/healthz'
    github_owner = 'your-github-name'           # private repos for intent projects

Features that touch the host (listener guard, deployment guard) stay off unless `live_home`
names this very installation.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

HOME = Path(__file__).resolve().parents[2]


def _load() -> dict[str, Any]:
    path = Path(os.environ.get("DEV_MCP_MACHINE") or HOME / "machine.toml")
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


SETTINGS = _load()


def _path(key: str, default: Path | None) -> Path | None:
    value = SETTINGS.get(key)
    return Path(value) if value else default


def _paths(key: str, default: list[Path]) -> tuple[Path, ...]:
    values = SETTINGS.get(key)
    return tuple(Path(v) for v in values) if isinstance(values, list) else tuple(default)


HUB: Path = _path("hub", HOME.parent)  # type: ignore[assignment]
WORKSPACES = _paths("workspaces", [HUB])
LIVE_HOME = _path("live_home", None)
WORKTREE_BASE: Path = _path("worktree_base", HUB / ".fleet-wt")  # type: ignore[assignment]
LENS_DOCS = _paths("lens_docs", [])
GATEWAY_HEALTH_URL = str(SETTINGS.get("gateway_health_url") or "")
GITHUB_OWNER = str(SETTINGS.get("github_owner") or "")
OWNER_PROJECT = str(SETTINGS.get("owner_project") or "Owner Inbox")

FLEET_DB = HOME / "data" / "fleet" / "fleet.sqlite"
INTENT_INBOX = HUB / "_todo" / "intents"
OWNER_INBOX = HUB / "_todo" / "owner-inbox.md"
HUB_FLEET = HUB / "_todo" / "hub-fleet"
SHARED_LIB = HUB / "_lib"
STACK_JSON = HUB / "stack.json"
PROXY_STATE: Path = _path("proxy_state", HUB / "gateway-host" / "mcp-proxy" / "state.json")  # type: ignore[assignment]
GEMINI_RELAY: Path = _path("gemini_relay", HUB / "ai-relay" / "relay.mjs")  # type: ignore[assignment]


def is_live(home: Path) -> bool:
    """This process runs the production installation named in machine.toml."""
    return LIVE_HOME is not None and home.resolve() == LIVE_HOME.resolve()
