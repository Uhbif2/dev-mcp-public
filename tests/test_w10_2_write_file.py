"""W10-2: write_file failures must be safe, explicit MCP results."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dev_mcp import server as server_mod  # noqa: E402
from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.fleet import quick  # noqa: E402
from dev_mcp.policy import Policy  # noqa: E402


def _server(tmp_path: Path, *, protected: bool = False):
    root = tmp_path / "workspace"
    target_dir = root / "data" / "fleet" / "quick" / "tasks" / "wave9" / "out"
    target_dir.mkdir(parents=True)
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{tmp_path.as_posix()}']\n"
        f"tree_roots = ['{tmp_path.as_posix()}']\n"
        + (f"tree_deny = ['{root.as_posix()}']\n" if protected else "")
        + "allow_write = false\n",
        encoding="utf-8",
    )
    mcp = server_mod.build_server(
        Policy.load(config),
        Audit(tmp_path / "audit.jsonl"),
        fleet_db=tmp_path / "registry" / "fleet.sqlite",
    )
    return mcp, target_dir / "W9-5-integration-backlog.md"


async def _call(mcp, path: Path, *, agent: str = "") -> str:
    async with Client(mcp) as client:
        response = await client.call_tool("write_file", {
            "path": str(path), "content": "report", "agent": agent,
        })
        assert not response.is_error
        return response.content[0].text


@pytest.mark.asyncio
async def test_protected_fleet_report_requires_verified_operator_scope(tmp_path, monkeypatch):
    mcp, target = _server(tmp_path, protected=True)
    agent = "Q14-7278A5"
    # A damaged/unreadable quick-agent book yields no verifiable job.
    monkeypatch.setattr(quick, "lookup", lambda book, name: None)
    denied = await _call(mcp, target, agent=agent)
    assert denied.startswith("Отказано:")
    assert "папка данных флота закрыта" in denied
    assert 'fleet_quick(command="report")' in denied
    assert not target.exists()

    # The same protected path is permitted for an authenticated, open Claude scope.
    monkeypatch.setattr(quick, "lookup", lambda book, name: {
        "owner": "claude", "state": "working",
        "scope": [str(tmp_path / "workspace")],
    })
    monkeypatch.setattr(quick, "mark_acting", lambda book, job: None)
    allowed = await _call(mcp, target, agent=agent)
    assert allowed.startswith("создан:")
    assert target.read_text(encoding="utf-8") == "report"


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", [OSError("access denied"), RuntimeError("unexpected write error")])
async def test_write_file_internal_failure_becomes_audited_mcp_denial(tmp_path, monkeypatch, problem):
    mcp, target = _server(tmp_path)
    def break_write(path, content, eol):
        raise problem
    monkeypatch.setattr(server_mod, "_write_keep_eol", break_write)
    message = await _call(mcp, target)
    assert message.startswith("Отказано:")
    assert ("ошибка файловой системы" in message or "внутренняя ошибка записи" in message)
    assert not target.exists()
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"tool": "write_file"' in audit
    assert '"decision": "deny"' in audit
