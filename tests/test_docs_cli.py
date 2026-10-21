"""Context7 CLI: local network tests, no live service or credentials."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from tools import docs


@pytest.fixture
def server(monkeypatch, tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            calls.append((parsed.path, query))
            if parsed.path == "/api/v1/search":
                term = query.get("query", [""])[0]
                if term == "slow":
                    time.sleep(0.35)
                if term == "unauthorized":
                    return self.send_text(401, "not authorized", "text/plain")
                if term == "missing":
                    return self.send_text(404, "not found", "text/plain")
                if term == "badjson":
                    return self.send_text(200, "{oops", "application/json")
                if term == "empty":
                    return self.send_text(200, "", "application/json")
                if term == "noresults":
                    return self.send_text(200, '{"results":[]}', "application/json")
                payload = {"results": [{"id": "/websites/fastapi_tiangolo", "title": "FastAPI",
                                        "versions": ["v1", "v2"], "trustScore": 9,
                                        "benchmarkScore": 83.11}]}
                return self.send_text(200, json.dumps(payload), "application/json")
            if parsed.path == "/api/v1/websites/fastapi_tiangolo":
                return self.send_text(200, "FastAPI APIRouter " + "hello " * 9000, "text/plain")
            return self.send_text(404, "not found", "text/plain")

        def send_text(self, status, text, content_type):
            body = text.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CONTEXT7_BASE_URL", f"http://127.0.0.1:{httpd.server_port}")
    monkeypatch.setenv("CONTEXT7_CACHE_DIR", str(tmp_path / "cache"))
    try:
        yield calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_search_renders_id_name_versions_and_ratings(server, capsys):
    assert docs.main(["search", "fastapi"]) == 0
    out = capsys.readouterr().out
    assert "websites/fastapi_tiangolo |" in out
    assert "FastAPI" in out and "v1, v2" in out and "trust=9" in out
    assert server[0][1]["query"] == ["fastapi"]


def test_get_topic_tokens_and_local_output_limit(server, capsys):
    assert docs.main(["get", "/websites/fastapi_tiangolo",
                      "--topic", "routing and deps", "--tokens", "9000"]) == 0
    out = capsys.readouterr().out
    assert "FastAPI APIRouter" in out
    assert "truncated to 8000 estimated tokens" in out
    assert len(out) <= 8001 * docs.CHARS_PER_TOKEN
    path, params = server[-1]
    assert path == "/api/v1/websites/fastapi_tiangolo"
    assert params["topic"] == ["routing and deps"] and params["tokens"] == ["9000"]


def test_get_small_token_budget_fetches_minimum_and_truncates_locally(server, capsys):
    assert docs.main(["get", "websites/fastapi_tiangolo",
                      "--topic", "routing", "--tokens", "200", "--no-cache"]) == 0
    output = capsys.readouterr().out
    assert "truncated to 200 estimated tokens" in output
    assert len(output) <= 200 * docs.CHARS_PER_TOKEN + 2
    assert server[-1][1]["tokens"] == ["1000"]


def test_cache_hit_skips_second_request(server, capsys):
    assert docs.main(["search", "fastapi"]) == 0
    assert len(server) == 1
    assert docs.main(["search", "fastapi"]) == 0
    assert len(server) == 1
    assert "FastAPI" in capsys.readouterr().out


def test_cache_disabled_uses_network_each_time(server):
    assert docs.main(["search", "fastapi", "--no-cache"]) == 0
    assert docs.main(["search", "fastapi", "--no-cache"]) == 0
    assert len(server) == 2


def test_cache_expires_after_one_day(server, tmp_path):
    assert docs.main(["search", "fastapi"]) == 0
    cache_files = list((tmp_path / "cache").glob("*.json"))
    assert len(cache_files) == 1
    payload = json.loads(cache_files[0].read_text(encoding="utf-8"))
    payload["created"] -= docs.CACHE_TTL_SECONDS + 2
    cache_files[0].write_text(json.dumps(payload), encoding="utf-8")
    assert docs.main(["search", "fastapi"]) == 0
    assert len(server) == 2


@pytest.mark.parametrize(("term", "error"), [
    ("missing", "HTTP 404"),
    ("unauthorized", "Context7 authorization failed"),
    ("badjson", "invalid JSON"),
    ("empty", "empty response"),
    ("noresults", "no libraries"),
])
def test_friendly_failures(server, capsys, term, error):
    assert docs.main(["search", term, "--no-cache"]) == 1
    assert error in capsys.readouterr().err


def test_timeout_has_nonzero_rc(server, capsys):
    assert docs.main(["search", "slow", "--timeout", "0.05", "--no-cache"]) == 1
    assert "timed out" in capsys.readouterr().err.lower()


def test_get_missing_is_nonzero(server, capsys):
    assert docs.main(["get", "/unknown/library", "--no-cache"]) == 1
    assert "HTTP 404" in capsys.readouterr().err


def test_rejects_external_test_server_and_path_traversal(monkeypatch, capsys, server):
    assert docs.main(["get", "../bad", "--no-cache"]) == 1
    assert "library id" in capsys.readouterr().err
    monkeypatch.setenv("CONTEXT7_BASE_URL", "http://example.invalid")
    assert docs.main(["search", "fastapi", "--no-cache"]) == 1
    error = capsys.readouterr().err
    assert "base URL" in error or "loopback" in error
