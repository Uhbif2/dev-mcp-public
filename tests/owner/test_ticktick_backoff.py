from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from dev_mcp.owner import overview, ticktick_board as tb, ticktick_oauth
from dev_mcp.owner.ticktick import StreamableHTTP, TickTickRateLimited, TickTickUnavailable


@pytest.mark.parametrize("retry", ["2400", "garbage"])
def test_http_429_preserves_safe_retry_after_without_echoing_response(retry):
    headers = Message()
    headers["Retry-After"] = retry

    class Limited:
        def open(self, *args, **kwargs):
            raise HTTPError("https://mcp.ticktick.com", 429, "private upstream text", headers, None)

    with pytest.raises(TickTickRateLimited) as exc:
        StreamableHTTP(opener=Limited()).request("tools/list", {}, "fake-local-test")
    assert exc.value.retry_after_s == (2400 if retry == "2400" else 120)
    assert "private" not in str(exc.value)


def test_sync_backoff_resets_on_success_and_exposes_request_counts(monkeypatch):
    states, waits = [], []
    answers = iter([TickTickUnavailable("offline"), TickTickRateLimited(600), None,
                    TickTickUnavailable("offline again")])

    class Stop:
        def is_set(self):
            return len(waits) == 4

        def wait(self, seconds):
            waits.append(seconds)

    class Board:
        def __init__(self, store, api):
            self.api = api

        def sync(self, *args, **kwargs):
            self.api.requests += 3
            result = next(answers)
            if result:
                raise result
            return {"sections": 1}

    monkeypatch.setattr(ticktick_oauth, "status", lambda: {"connected": True})
    monkeypatch.setattr(overview, "snapshot", lambda store: {"status": "ready"})
    monkeypatch.setattr(tb, "Board", Board)
    monkeypatch.setattr(tb, "system_lines", lambda *args: [])
    monkeypatch.setattr(tb, "_status", states.append)
    tb.run_worker(SimpleNamespace(), Stop())
    assert waits == [120, 600, 120, 120]
    assert [x["ok"] for x in states] == [False, False, True, False]
    assert [x["requests"] for x in states] == [3, 3, 3, 3]
    assert states[-1]["consecutive_failures"] == 1
