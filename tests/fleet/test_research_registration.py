"""Public fleet_research MCP entrypoint must be registered and claim-scoped.

Regression: fleet_research_next/finish existed while no public request tool was
registered; a browser could consume requests, but fleet agents could not create
one through their own claim. No network or paid APIs are used here.
"""
from __future__ import annotations

from types import SimpleNamespace

from dev_mcp.fleet import tools as fleet_tools


class StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, **kwargs):
        def bind(callback):
            self.tools[name] = callback
            return callback
        return bind


def test_research_request_is_registered_and_uses_only_trusted_active_project(monkeypatch):
    observed = []
    mcp = StubMCP()
    store = object()
    ctx = fleet_tools.FleetContext(store=store)
    ctx.touch = lambda worker: observed.append(("touch", worker))
    ctx.modules["research"] = SimpleNamespace(
        request=lambda received, **kw: observed.append(("request", received, kw)) or "r-1234567890abcdef12345678"
    )
    monkeypatch.setattr(fleet_tools, "_active_task_info",
                        lambda current, agent: ("TrustedProject", "TZ-304", "work"))
    fleet_tools.register(mcp, ctx)
    assert "fleet_research" in mcp.tools, "public fleet_research entrypoint missing"
    raw = mcp.tools["fleet_research"]("W17", "Compare implementations")
    assert "r-1234567890abcdef12345678" in raw
    assert ("request", store, {"agent": "W17", "question": "Compare implementations",
                               "project": "TrustedProject"}) in observed
    assert ("touch", "W17") in observed


def test_research_request_rejects_missing_claim_or_blank_question(monkeypatch):
    calls = []
    mcp = StubMCP()
    ctx = fleet_tools.FleetContext(store=object())
    ctx.modules["research"] = SimpleNamespace(request=lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(fleet_tools, "_active_task_info",
                        lambda current, agent: (_ for _ in ()).throw(ValueError("no active claim")))
    fleet_tools.register(mcp, ctx)
    assert "fleet_research" in mcp.tools
    assert "Отказано" in mcp.tools["fleet_research"]("W17", "Find existing tools")
    assert "Отказано" in mcp.tools["fleet_research"]("W17", " ")
    assert "Отказано" in mcp.tools["fleet_research"]("", "request")
    assert not calls
