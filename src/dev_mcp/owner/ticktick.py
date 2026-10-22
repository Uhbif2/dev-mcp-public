"""TickTick actions through the official Streamable HTTP MCP, disabled by default.

Owner 19.10.2026: TickTick only through the official MCP with OAuth (a client connector).
No API token, no token file: the adapter never opens credential files, reads env vars or
logs a credential; a caller with an OAuth session may inject one by callback.
Tool names and argument schemas are discovered via tools/list, never presumed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MCP_ENDPOINT = "https://mcp.ticktick.com"
MCP_VERSION = "2025-06-18"
OWNER_ACTION = (
    "TickTick — только официальный MCP https://mcp.ticktick.com через OAuth: "
    "Claude → Settings → Connectors → TickTick → Connect. Токенов и файлов с ними нет."
)


class TickTickUnavailable(RuntimeError):
    pass


class TickTickRateLimited(TickTickUnavailable):
    def __init__(self, retry_after_s: float = 120.0):
        import math
        super().__init__("TickTick MCP rate limited")
        self.retry_after_s = max(0.0, retry_after_s) if math.isfinite(retry_after_s) else 120.0


@dataclass(frozen=True)
class PublishedTask:
    task_id: str
    url: str


@dataclass(frozen=True)
class VerifiedTask:
    task_id: str
    marker: str
    status: str
    completed_at: str


class TickTickClient(Protocol):
    def create(self, payload: dict, *, credential: str, idempotency_key: str) -> PublishedTask: ...
    def fetch(self, task_id: str, *, credential: str) -> VerifiedTask: ...


def check_task_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid TickTick task URL")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in {"ticktick.com", "www.ticktick.com"}
            or parts.username or parts.password or parts.port or not (parts.path or parts.fragment)
            or "\\" in url or any(ord(ch) < 32 for ch in url)):
        raise ValueError("invalid TickTick task URL")
    return url


class _NoRedirect(HTTPRedirectHandler):
    """Forbid redirects carrying the Bearer header to an unexpected host."""

    def http_error_302(self, req, fp, code, msg, headers):
        raise TickTickUnavailable("TickTick MCP redirect refused")

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


class StreamableHTTP:
    """Stdlib MCP transport with JSON and server-sent-event responses."""

    def __init__(self, endpoint: str = MCP_ENDPOINT, *, timeout: float = 15.0,
                 opener: Any | None = None):
        parts = urlsplit(endpoint)
        if (parts.scheme != "https" or parts.hostname != "mcp.ticktick.com"
                or parts.port or parts.username or parts.password
                or parts.path not in ("", "/") or parts.query or parts.fragment
                or "\\" in endpoint):
            raise ValueError("untrusted TickTick MCP endpoint")
        self.endpoint = endpoint
        self.timeout = timeout
        self._opener = opener or build_opener(_NoRedirect())
        self._session: str | None = None
        self._next_id = 0

    def request(self, method: str, params: dict, credential: str, *, notification: bool = False) -> dict:
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            message["id"] = self._next_id
        if params:
            message["params"] = params
        headers = {
            "Authorization": "Bearer " + credential,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_VERSION,
        }
        if self._session is not None:
            headers["Mcp-Session-Id"] = self._session
        req = Request(
            self.endpoint, data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self._session = session
                raw = response.read(1024 * 1024 + 1)
                ctype = response.headers.get("Content-Type", "").lower().split(";", 1)[0].strip()
        except HTTPError as exc:
            if exc.code == 429:
                from datetime import datetime, timezone
                from email.utils import parsedate_to_datetime

                value = exc.headers.get("Retry-After", "120") if exc.headers else "120"
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        delay = 120.0
                raise TickTickRateLimited(delay) from None
            raise TickTickUnavailable("TickTick MCP unavailable") from None
        except (URLError, TimeoutError, OSError):
            raise TickTickUnavailable("TickTick MCP unavailable") from None
        if len(raw) > 1024 * 1024:
            raise TickTickUnavailable("TickTick MCP response too large")
        if notification:
            return {}
        try:
            if ctype == "text/event-stream":
                events = []
                lines: list[str] = []
                for line in raw.decode("utf-8", "strict").splitlines():
                    if not line:
                        if lines:
                            events.append("\n".join(lines))
                            lines = []
                    elif line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                if lines:
                    events.append("\n".join(lines))
                objects = [json.loads(item) for item in events]
                result = next((obj for obj in objects if isinstance(obj, dict)
                               and obj.get("id") == message["id"]), None)
            elif ctype == "application/json":
                result = json.loads(raw.decode("utf-8", "strict"))
            else:
                raise TickTickUnavailable("unexpected TickTick MCP content type")
        except (UnicodeError, ValueError):
            # Never surface untrusted server text or private tool arguments.
            raise TickTickUnavailable("malformed TickTick MCP response") from None
        if not isinstance(result, dict) or result.get("id") != message["id"]:
            raise TickTickUnavailable("TickTick MCP response ID mismatch")
        if "error" in result:
            # Upstream error details may echo sensitive input. Never log/return them.
            raise TickTickUnavailable("TickTick MCP returned an error")
        if not isinstance(result.get("result"), dict):
            raise TickTickUnavailable("TickTick MCP result missing")
        return result["result"]


class TickTickMCPClient:
    """Discover task operations from official MCP metadata, fail closed on ambiguity."""

    def __init__(self, transport: StreamableHTTP | Any | None = None):
        self.transport = transport or StreamableHTTP()

    def _tools(self, credential: str) -> list[dict]:
        self.transport.request("initialize", {
            "protocolVersion": MCP_VERSION, "capabilities": {},
            "clientInfo": {"name": "dev-mcp-owner-actions", "version": "1"},
        }, credential)
        self.transport.request("notifications/initialized", {}, credential, notification=True)
        tools: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(8):
            params = {"cursor": cursor} if cursor else {}
            page = self.transport.request("tools/list", params, credential)
            chunk = page.get("tools")
            if not isinstance(chunk, list) or any(not isinstance(t, dict) for t in chunk):
                raise TickTickUnavailable("invalid MCP tools list")
            tools.extend(chunk)
            cursor = page.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen:
                raise TickTickUnavailable("invalid MCP pagination")
            seen.add(cursor)
        raise TickTickUnavailable("MCP tool discovery exceeded page limit")

    @staticmethod
    def _choose(tools: list[dict], action: str) -> dict:
        verbs = ("create", "add") if action == "create" else ("get", "retrieve", "fetch")
        matches = []
        for tool in tools:
            name = tool.get("name")
            description = tool.get("description", "")
            schema = tool.get("inputSchema")
            if not isinstance(name, str) or not isinstance(schema, dict):
                continue
            words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
            text = (words + " " + str(description)).lower().replace("_", " ")
            props = schema.get("properties", {})
            if (not any(re.search(r"\b" + verb + r"\b", text) for verb in verbs)
                    or not re.search(r"\btask\b|\btasks\b", text)
                    or not isinstance(props, dict)):
                continue
            if action == "create" and ("title" not in props or
                                       not ({"content", "description", "notes"} & props.keys())):
                continue
            if action == "fetch" and not ({"taskId", "task_id", "id"} & props.keys()):
                continue
            matches.append(tool)
        if len(matches) != 1:
            raise TickTickUnavailable("TickTick MCP task operation unavailable or ambiguous")
        return matches[0]

    @staticmethod
    def _arguments(tool: dict, values: dict) -> dict:
        schema = tool["inputSchema"]
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            raise TickTickUnavailable("invalid MCP task schema")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(s, str) for s in required):
            raise TickTickUnavailable("invalid MCP required arguments")
        args = {k: v for k, v in values.items() if k in props and v is not None}
        if any(k not in args for k in required):
            raise TickTickUnavailable("required MCP arguments cannot be supplied")
        return args

    @staticmethod
    def _task(result: dict) -> dict:
        if result.get("isError") is True:
            raise TickTickUnavailable("TickTick MCP tool returned an error")
        data = result.get("structuredContent")
        if isinstance(data, dict):
            return data.get("task", data) if isinstance(data.get("task", data), dict) else data
        blocks = result.get("content")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        parsed = json.loads(str(block.get("text", "")))
                    except ValueError:
                        continue
                    if isinstance(parsed, dict):
                        return parsed.get("task", parsed) if isinstance(parsed.get("task", parsed), dict) else parsed
        raise TickTickUnavailable("TickTick MCP task receipt not machine-readable")

    def create(self, payload: dict, *, credential: str, idempotency_key: str) -> PublishedTask:
        tool = self._choose(self._tools(credential), "create")
        props = tool["inputSchema"]["properties"]
        body = next((name for name in ("content", "description", "notes") if name in props), None)
        if body is None:
            raise TickTickUnavailable("TickTick MCP cannot preserve owner-action marker")
        values = {"title": payload["title"], body: payload["content"]}
        if "dueDate" in props:
            values["dueDate"] = payload.get("due")
        if "due" in props:
            values["due"] = payload.get("due")
        args = self._arguments(tool, values)
        answer = self.transport.request("tools/call", {
            "name": tool["name"], "arguments": args,
        }, credential)
        task = self._task(answer)
        task_id = task.get("id") or task.get("taskId")
        url = task.get("url") or task.get("taskUrl")
        if not isinstance(task_id, str) or not isinstance(url, str):
            raise TickTickUnavailable("TickTick MCP task ID or URL missing")
        return PublishedTask(task_id, check_task_url(url))

    def fetch(self, task_id: str, *, credential: str) -> VerifiedTask:
        tool = self._choose(self._tools(credential), "fetch")
        props = tool["inputSchema"]["properties"]
        key = next((k for k in ("taskId", "task_id", "id") if k in props), None)
        if key is None:
            raise TickTickUnavailable("TickTick MCP read task argument missing")
        args = self._arguments(tool, {key: task_id})
        task = self._task(self.transport.request("tools/call", {
            "name": tool["name"], "arguments": args,
        }, credential))
        got_id = task.get("id") or task.get("taskId")
        if got_id != task_id:
            raise TickTickUnavailable("TickTick MCP returned another task")
        content = task.get("content") or task.get("description") or task.get("notes") or ""
        if not isinstance(content, str):
            raise TickTickUnavailable("TickTick MCP task marker missing")
        markers = re.findall(r"owner-action:[a-f0-9]{64}", content)
        if len(markers) != 1:
            raise TickTickUnavailable("TickTick MCP owner-action marker missing or ambiguous")
        status = str(task.get("status") or "").lower()
        if status in {"done", "completed", "2"}:
            state = "completed"
        elif status in {"refused", "rejected", "cancelled", "canceled"}:
            state = "refused"
        else:
            state = "active"
        completed_at = task.get("completedAt") or task.get("completed_at") or ""
        if state != "active" and not isinstance(completed_at, str):
            raise TickTickUnavailable("TickTick MCP completion timestamp missing")
        # Completion evidence must have an explicit event time. Refusal is
        # separately verifiable from status+marker and may lack completedAt.
        if state == "completed" and not completed_at:
            raise TickTickUnavailable("TickTick MCP completion timestamp missing")
        return VerifiedTask(task_id, markers[0], state, completed_at)


class TickTickAdapter:
    def __init__(self, client: TickTickClient | None = None,
                 credentials: Callable[[], str] | None = None, *,
                 operator_authorized: bool = False):
        self._client = client
        self._credentials = credentials
        self._enabled = operator_authorized

    def _access(self) -> tuple[TickTickClient, str]:
        if not self._enabled or self._client is None or self._credentials is None:
            raise TickTickUnavailable('operator authorization required; ' + OWNER_ACTION)
        try:
            credential = self._credentials()
        except (OSError, ValueError):
            raise TickTickUnavailable(OWNER_ACTION) from None
        if not isinstance(credential, str) or not credential.strip():
            raise TickTickUnavailable('operator credentials absent; ' + OWNER_ACTION)
        return self._client, credential

    def require_authorization(self) -> None:
        self._access()

    def create(self, payload: dict, *, idempotency_key: str) -> PublishedTask:
        client, credential = self._access()
        result = client.create(payload, credential=credential, idempotency_key=idempotency_key)
        if not isinstance(result, PublishedTask) or not result.task_id or len(result.task_id) > 256:
            raise ValueError("invalid TickTick response")
        check_task_url(result.url)
        return result

    def get(self, task_id: str) -> VerifiedTask:
        client, credential = self._access()
        result = client.fetch(task_id, credential=credential)
        if not isinstance(result, VerifiedTask):
            raise ValueError("invalid TickTick verification response")
        return result
