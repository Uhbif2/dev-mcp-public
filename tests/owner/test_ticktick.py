from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.owner.ticktick import (
    PublishedTask, StreamableHTTP, TickTickAdapter, TickTickMCPClient,
    TickTickUnavailable, VerifiedTask, check_task_url,
)


class FakeAPI:
    def __init__(self):
        self.creates = []
        self.fetches = []
        self.fail = False

    def create(self, payload, *, credential, idempotency_key):
        self.creates.append((dict(payload), credential, idempotency_key))
        if self.fail:
            raise TimeoutError("synthetic network down")
        return PublishedTask("fake-33", "https://ticktick.com/webapp/#p/fake-33")

    def fetch(self, task_id, *, credential):
        self.fetches.append((task_id, credential))
        return VerifiedTask(task_id, "owner-action:local-req", "completed",
                            "2099-01-01T00:00:00+00:00")


def test_adapter_is_disabled_even_with_injected_client_and_fake_credentials():
    api = FakeAPI()
    disabled = TickTickAdapter(api, lambda: "fake-secret")
    with pytest.raises(TickTickUnavailable, match="authorization"):
        disabled.create({"title": "No network"}, idempotency_key="req-1")
    with pytest.raises(TickTickUnavailable):
        disabled.get("fake-33")
    assert api.creates == [] and api.fetches == []


def test_operator_authorization_alone_without_credential_is_not_enough():
    api = FakeAPI()
    no_credentials = TickTickAdapter(api, operator_authorized=True)
    with pytest.raises(TickTickUnavailable):
        no_credentials.create({}, idempotency_key="req-1")
    empty = TickTickAdapter(api, lambda: "", operator_authorized=True)
    with pytest.raises(TickTickUnavailable, match="absent"):
        empty.get("fake-33")
    assert api.creates == [] and api.fetches == []


def test_fake_operator_adapter_receipts_and_transient_failure():
    api = FakeAPI()
    adapter = TickTickAdapter(api, lambda: "fake-credential",
                              operator_authorized=True)
    task = adapter.create({"title": "Fake-only"}, idempotency_key="req-1")
    assert task.task_id == "fake-33"
    assert len(api.creates) == 1
    assert adapter.get(task.task_id).status == "completed"
    assert api.fetches == [("fake-33", "fake-credential")]
    api.fail = True
    with pytest.raises(TimeoutError, match="network down"):
        adapter.create({"title": "Other"}, idempotency_key="req-2")


@pytest.mark.parametrize("url", (
    "http://ticktick.com/webapp/#p/1",
    "https://ticktick.com.evil.example/webapp/#p/1",
    "https://evil.example/webapp/#p/1",
    "https://user:pass@ticktick.com/webapp/#p/1",
    "https://ticktick.com:444/webapp/#p/1",
    "https://ticktick.com",
    "javascript:alert(1)",
    "https://ticktick.com\\@evil.example/webapp/",
))
def test_malformed_urls_rejected(url):
    with pytest.raises(ValueError, match="invalid"):
        check_task_url(url)


def test_exact_https_ticktick_hosts_only():
    assert check_task_url("https://ticktick.com/webapp/#p/t-1") == (
        "https://ticktick.com/webapp/#p/t-1"
    )
    assert check_task_url("https://www.ticktick.com/webapp/#q/t-2") == (
        "https://www.ticktick.com/webapp/#q/t-2"
    )


def test_api_bad_task_url_rejected():
    class BadFake(FakeAPI):
        def create(self, payload, *, credential, idempotency_key):
            return PublishedTask("fake-33", "https://evil.example/steal")
    adapter = TickTickAdapter(BadFake(), lambda: "only-fake", operator_authorized=True)
    with pytest.raises(ValueError, match="invalid"):
        adapter.create({"title": "Fake"}, idempotency_key="request")


class FakeMCPResponse:
    def __init__(self, payload, *, session=None, sse=False):
        message = json.dumps(payload, ensure_ascii=False)
        self.content = (f"event: message\ndata: {message}\n\n" if sse else message).encode("utf-8")
        self.headers = {
            "Content-Type": "text/event-stream" if sse else "application/json",
            "Mcp-Session-Id": session or "",
        }

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, _limit):
        return self.content


class FakeLocalMCP:
    """In-process fake official MCP server: no real network or credentials."""

    def __init__(self, *, create_tool=True, wrong_id=False):
        self.requests = []
        self.marker = "owner-action:" + "a" * 64
        self.create_tool = create_tool
        self.wrong_id = wrong_id
        self.use_sse = False
        self.fetch_status = "completed"
        self.fetch_timestamp = "2099-01-01T00:00:00+00:00"

    def open(self, request, *, timeout):
        message = json.loads(request.data)
        self.requests.append((message, dict(request.header_items())))
        assert request.full_url == "https://mcp.ticktick.com"
        assert timeout > 0
        assert request.get_header("Authorization") == "Bearer fake-local-test"
        assert "text/event-stream" in request.get_header("Accept")
        method = message["method"]
        if method == "notifications/initialized":
            return FakeMCPResponse({})
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": []}
            if self.create_tool:
                result["tools"].append({
                    "name": "generated_add_workspace_task",
                    "description": "Add a task to the workspace",
                    "inputSchema": {"type": "object", "properties": {
                        "title": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["title", "content"]},
                })
            result["tools"].append({
                "name": "generated_get_workspace_task",
                "description": "Retrieve a task by ID",
                "inputSchema": {"type": "object",
                                "properties": {"taskId": {"type": "string"}},
                                "required": ["taskId"]},
            })
        elif method == "tools/call":
            if message["params"]["name"] == "generated_add_workspace_task":
                assert self.marker in message["params"]["arguments"]["content"]
                result = {"structuredContent": {"id": "fake-33",
                    "url": "https://ticktick.com/webapp/#p/fake-33"}}
            else:
                assert message["params"]["name"] == "generated_get_workspace_task"
                assert message["params"]["arguments"] == {"taskId": "fake-33"}
                task = {"id": "fake-33", "content": "proof " + self.marker,
                        "status": self.fetch_status}
                if self.fetch_timestamp is not None:
                    task["completedAt"] = self.fetch_timestamp
                result = {"structuredContent": task}
        else:
            raise AssertionError("unknown MCP operation: " + method)
        response = {"jsonrpc": "2.0",
                    "id": message["id"] + int(self.wrong_id), "result": result}
        return FakeMCPResponse(response, session="session-fake", sse=self.use_sse)


def test_official_ticktick_mcp_fake_local_handshake_and_dynamic_tools():
    fake = FakeLocalMCP()
    adapter = TickTickAdapter(
        TickTickMCPClient(StreamableHTTP(opener=fake)),
        lambda: "fake-local-test", operator_authorized=True,
    )
    marker_id = "a" * 64
    created = adapter.create(
        {"title": "Review owner choice", "content": "proposal owner-action:" + marker_id},
        idempotency_key=marker_id,
    )
    assert created.task_id == "fake-33"
    assert adapter.get(created.task_id) == VerifiedTask(
        "fake-33", "owner-action:" + marker_id, "completed",
        "2099-01-01T00:00:00+00:00",
    )
    names = [request["method"] for request, _ in fake.requests]
    assert names == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]
    assert "Mcp-session-id" not in fake.requests[0][1]
    assert all(headers.get("Mcp-session-id") == "session-fake"
               for _, headers in fake.requests[1:])
    assert any(request["params"]["name"] == "generated_add_workspace_task"
               for request, _ in fake.requests if request["method"] == "tools/call")


@pytest.mark.parametrize("remote_status", ("refused", "cancelled"))
def test_ticktick_mcp_refusal_without_completed_at_is_verifiable(remote_status):
    fake = FakeLocalMCP()
    fake.fetch_status = remote_status
    fake.fetch_timestamp = None
    task = TickTickMCPClient(StreamableHTTP(opener=fake)).fetch(
        "fake-33", credential="fake-local-test")
    assert task == VerifiedTask("fake-33", fake.marker, "refused", "")


def test_ticktick_mcp_completion_without_completed_at_fails_closed():
    fake = FakeLocalMCP()
    fake.fetch_timestamp = None
    with pytest.raises(TickTickUnavailable, match="completion timestamp missing"):
        TickTickMCPClient(StreamableHTTP(opener=fake)).fetch(
            "fake-33", credential="fake-local-test")


def test_ticktick_mcp_accepts_sse_response_and_rejects_bad_response_id():
    fake = FakeLocalMCP()
    fake.use_sse = True
    client = TickTickMCPClient(StreamableHTTP(opener=fake))
    result = client.create(
        {"title": "check", "content": "owner-action:" + "a" * 64},
        credential="fake-local-test", idempotency_key="a" * 64,
    )
    assert result.task_id == "fake-33"
    bad = FakeLocalMCP(wrong_id=True)
    with pytest.raises(TickTickUnavailable, match="mismatch"):
        TickTickMCPClient(StreamableHTTP(opener=bad)).create(
            {"title": "check", "content": "owner-action:" + "a" * 64},
            credential="fake-local-test", idempotency_key="a" * 64,
        )


def test_ticktick_mcp_discovery_fail_closed_and_endpoint_not_redirectable():
    fake = FakeLocalMCP(create_tool=False)
    with pytest.raises(TickTickUnavailable, match="unavailable"):
        TickTickMCPClient(StreamableHTTP(opener=fake)).create(
            {"title": "check", "content": "proof"},
            credential="fake-local-test", idempotency_key="id",
        )
    assert all(m["method"] != "tools/call" for m, _ in fake.requests)
    for endpoint in ("http://mcp.ticktick.com", "https://mcp.ticktick.com.evil.example",
                     "https://evil.example", "https://mcp.ticktick.com:443"):
        with pytest.raises(ValueError, match="untrusted"):
            StreamableHTTP(endpoint)


def test_missing_credential_points_to_the_official_oauth_connector_without_network():
    fake = FakeLocalMCP()

    def missing():
        raise FileNotFoundError("credential file absent")

    adapter = TickTickAdapter(
        TickTickMCPClient(StreamableHTTP(opener=fake)),
        missing, operator_authorized=True,
    )
    with pytest.raises(TickTickUnavailable, match="OAuth") as caught:
        adapter.create({"title": "no token"}, idempotency_key="id")
    assert "API Token" not in str(caught.value) and "ticktick.json" not in str(caught.value)
    assert fake.requests == []


def test_tools_list_never_guesses_missing_write_capability():
    fake = FakeLocalMCP(create_tool=False)
    client = TickTickMCPClient(StreamableHTTP(opener=fake))
    with pytest.raises(TickTickUnavailable, match="task operation unavailable"):
        client.create(
            {"title": "Owner action", "content": "owner-action:" + "a" * 64},
            credential="fake-local-test", idempotency_key="a" * 64,
        )
    assert [m["method"] for m, _ in fake.requests].count("tools/call") == 0


def test_mcp_fake_corrupted_json_response_fails_closed():
    class Corrupt:
        def open(self, req, **_):
            class Invalid(FakeMCPResponse):
                def __init__(self):
                    self.content = b"{not-json"
                    self.headers = {"Content-Type": "application/json"}
            return Invalid()

    transport = StreamableHTTP(opener=Corrupt())
    with pytest.raises(TickTickUnavailable, match="malformed"):
        transport.request("initialize", {"protocolVersion": "2025-06-18"},
                          "fake-local-test")


