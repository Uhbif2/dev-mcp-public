"""TZ-304: extension-facing research tools return parseable JSON without envelopes."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dev_mcp.fleet.tools import FleetContext, register


class StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, **kwargs):
        def register_tool(callback):
            self.tools[name] = callback
            return callback
        return register_tool


def _bridge(pending, finish):
    store = object()
    ctx = FleetContext(store=store)
    ctx.modules["research"] = SimpleNamespace(pending=pending, finish=finish)
    mcp = StubMCP()
    register(mcp, ctx)
    return mcp.tools, store


def test_research_next_is_raw_json_with_only_expected_fields():
    calls = []
    entry = {
        "id": "research-123",
        "question": "Find implementations",
        "project": "P",
        "requested_at": "2026-10-13T00:00:00Z",
        "deadline_at": "2026-10-13T00:40:00Z",
    }
    tools, store = _bridge(
        pending=lambda received: calls.append(received) or [entry],
        finish=lambda *args, **kwargs: None,
    )
    raw = tools["fleet_research_next"]()
    assert isinstance(raw, str)
    assert json.loads(raw) == [entry]
    assert calls == [store]


@pytest.mark.parametrize("request_status,expected", [
    ("complete", "completed"),
    ("unavailable", "unavailable"),
])
def test_research_finish_forwards_backend_and_returns_raw_json(request_status, expected):
    calls = []

    def finish(store, *, request_id, status, report, reason):
        calls.append((store, request_id, status, report, reason))

    tools, store = _bridge(pending=lambda store: [], finish=finish)
    raw = tools["fleet_research_finish"](
        "research-123", request_status, "# Report", "reason"
    )
    assert json.loads(raw) == {"id": "research-123", "status": expected}
    assert calls == [(store, "research-123", request_status, "# Report", "reason")]


def test_research_finish_rejects_invalid_status_without_backend_call():
    calls = []
    tools, _ = _bridge(pending=lambda store: [], finish=lambda *a, **k: calls.append((a, k)))
    with pytest.raises(ValueError, match="status"):
        tools["fleet_research_finish"]("research-123", "unknown")
    assert calls == []
