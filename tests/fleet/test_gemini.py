"""TZ-06: fake loopback relay, no real model or secret use."""
from __future__ import annotations
import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from dev_mcp.fleet import gemini

PNG = bytes.fromhex("89504e470d0a1a0a") + b"fake"


def answer(text):
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode()


@pytest.fixture
def relay(monkeypatch):
    seen = []
    replies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers.get("content-length", 0)))))
            code, body = replies.pop(0) if replies else (200, answer("ok"))
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(gemini, "BASE_URL", f"http://127.0.0.1:{server.server_port}/v1beta")
    yield seen, replies
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def test_text(relay):
    seen, replies = relay
    replies.append((200, answer("yes")))
    assert gemini.ask("question") == "yes"
    assert seen[0]["contents"][0]["parts"] == [{"text": "question"}]
    assert gemini.last_error() is None


def test_images_and_mime(relay):
    seen, _ = relay
    jpeg = bytes.fromhex("ffd8ff") + b"hello"
    assert gemini.ask("inspect", images=[PNG, ("image/jpeg", jpeg)]) == "ok"
    parts = seen[0]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert parts[2]["inline_data"]["mime_type"] == "image/jpeg"
    assert parts[1]["inline_data"]["data"].startswith("iVBOR")


def test_json_schema_response(relay):
    seen, replies = relay
    replies.append((200, answer('{"answer":"ok","p":0.8,"why":"valid"}')))
    val = gemini.ask("judge", schema=gemini.JUDGE_SCHEMA)
    assert val["p"] == 0.8
    assert seen[0]["generationConfig"]["responseMimeType"] == "application/json"
    assert seen[0]["generationConfig"]["responseSchema"] == gemini.JUDGE_SCHEMA


def test_invalid_json_fails_closed(relay):
    _, replies = relay
    replies.append((200, answer("broken json")))
    assert gemini.ask("x", schema=gemini.JUDGE_SCHEMA) is None
    assert gemini.last_error()["code"] == "invalid_response"


@pytest.mark.parametrize("parts", [
    [13],
    ["malformed"],
    [None],
    {"text": "unexpected mapping"},
    [{"text": 13}],
    [{"text": ["nested"]}],
])
def test_malformed_gemini_parts_return_invalid_response(relay, parts):
    seen, replies = relay
    replies.append((200, json.dumps({
        "candidates": [{"content": {"parts": parts}}]
    }).encode("utf-8")))
    assert gemini.ask("question") is None
    assert gemini.last_error()["code"] == "invalid_response"
    assert len(seen) == 1  # Malformed response must not cause network retry.


def test_429_retried_once_then_none(relay):
    seen, replies = relay
    replies.extend([(429, b"{}"), (429, b"{}")])
    assert gemini.ask("x") is None
    assert len(seen) == 2
    assert gemini.last_error()["code"] == "rate_limited"


def test_403_is_not_retried(relay):
    seen, replies = relay
    replies.append((403, b"{}"))
    assert gemini.ask("x") is None
    assert len(seen) == 1
    assert gemini.last_error()["code"] == "http_403"


def test_network_retry_once(relay, monkeypatch):
    seen, _ = relay
    original = gemini._opener.open
    counter = [0]
    def open_once(*args, **kwargs):
        counter[0] += 1
        if counter[0] == 1:
            raise urllib.request.URLError("temporary")
        return original(*args, **kwargs)
    monkeypatch.setattr(gemini._opener, "open", open_once)
    assert gemini.ask("x") == "ok"
    assert counter[0] == 2 and len(seen) == 1


def test_relay_not_listening_returns_none(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(gemini, "BASE_URL", f"http://127.0.0.1:{port}/v1beta")
    assert gemini.ask("x") is None
    assert gemini.last_error()["code"] == "relay_unavailable"


def test_image_too_large_before_connect(monkeypatch):
    monkeypatch.setattr(gemini, "ensure_relay", lambda: pytest.fail("unexpected relay connect"))
    assert gemini.ask("x", images=[bytes.fromhex("89504e470d0a1a0a") + b"1" * (gemini.MAX_IMAGE_BYTES + 1)]) is None
    assert gemini.last_error()["code"] == "image_too_large"


def test_proxy_env_never_used(relay, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    assert gemini.ask("x") == "ok"
    # Positive end-to-end success under a poisoned proxy environment is the contract.
    # urllib omits a ProxyHandler({}) from opener.handlers because it has no methods.
    assert gemini.last_error() is None


def test_fixed_roles_with_structured_answers(relay):
    _, replies = relay
    replies.append((200, answer('{"answer":"yes","p":0.6,"why":"evidence","cause_code":"TECH"}')))
    assert gemini.judge("q", "c")["cause_code"] == "TECH"
    replies.append((200, answer('{"ok":false,"defects":[{"severity":"P1","where":"label","what":"unreadable"}]}')))
    assert gemini.look([PNG], "readable text")["defects"][0]["severity"] == "P1"
    replies.append((200, answer('{"verdict":"reject","defects":["missing test"]}')))
    assert gemini.cross_review("diff", "spec", "rc=0")["verdict"] == "reject"


def test_role_rejects_malformed_structure(relay):
    _, replies = relay
    replies.append((200, answer('{"ok":true,"defects":[{"severity":"P9","where":"x","what":"bad"}]}')))
    assert gemini.look([PNG], "rubric") is None
    assert gemini.last_error()["code"] == "invalid_role_response"
