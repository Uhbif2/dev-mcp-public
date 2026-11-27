"""W10-5: MCP handoff refusals remain JSON; neighboring tools keep working."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from dev_mcp.audit import Audit
from dev_mcp.policy import Policy
from dev_mcp.server import build_server


def _server(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{root.as_posix()}']\n"
        f"write_roots = ['{root.as_posix()}']\n"
        f"exec_roots = ['{root.as_posix()}']\n"
        "exec_allowed_bin = ['python']\n"
        "allow_write = false\n"
        "allow_network_fetch = false\n"
        "exec_timeout_cap_s = 30\n",
        encoding="utf-8",
    )
    return build_server(Policy.load(config), Audit(tmp_path / "logs" / "audit.jsonl"))


def test_inactive_quick_handoff_returns_json_refusal(tmp_path: Path):
    server = _server(tmp_path)
    tool = asyncio.run(server.get_tool("fleet_chat_handoff"))
    result = json.loads(tool.fn(
        agent="Q6-7278A5", command="observe",
        url="https://chatgpt.com/c/abcdefab-0123-4567", label="pult-q6",
        fill=1.0, full=True,
    ))
    assert result["action"] == "refused"
    assert "quick" in result["reason"]
    assert result["retry_after_s"] >= 600


def test_invalid_handoff_url_is_json_and_other_tools_unchanged(tmp_path: Path):
    server = _server(tmp_path)
    handoff = asyncio.run(server.get_tool("fleet_chat_handoff"))
    result = json.loads(handoff.fn(
        agent="Q6-7278A5", command="observe", url="https://chatgpt.com/g/project",
    ))
    assert result["action"] == "refused"
    assert "conversation URL" in result["reason"]
    listing = asyncio.run(server.get_tool("fleet_list"))
    assert callable(listing.fn)
