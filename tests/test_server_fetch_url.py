"""fetch_url SSRF, redirects, and atomic destination writes."""
from __future__ import annotations

import http.server
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fastmcp import Client
from dev_mcp.audit import Audit
from dev_mcp.policy import Policy
from dev_mcp import server as server_mod
from dev_mcp.server import build_server


def make_server(tmp_path, extra=''):
    root = tmp_path / 'workspace'
    root.mkdir(exist_ok=True)
    conf = tmp_path / 'policy.toml'
    conf.write_text(
        f"roots = ['{root}']\nallow_write = true\nallow_network_fetch = true\n" + extra,
        encoding='utf-8')
    return build_server(Policy.load(conf), Audit(tmp_path / 'logs' / 'audit.jsonl')), root


async def call(server, **kwargs):
    async with Client(server) as client:
        result = await client.call_tool('fetch_url', kwargs)
        return result.content[0].text


@pytest.fixture
def local_http():
    class Handler(http.server.BaseHTTPRequestHandler):
        mode = 'ok'

        def do_GET(self):
            if Handler.mode == 'redirect':
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.2:12345/secret')
                self.end_headers()
                return
            self.send_response(200)
            if Handler.mode == 'partial':
                self.send_header('Content-Length', '100')
                self.end_headers()
                self.wfile.write(b'partial')
                return
            self.end_headers()
            self.wfile.write(b'new-bytes')

        def log_message(self, *_):
            pass

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd, Handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=3)


@pytest.mark.asyncio
async def test_redirect_to_private_host_is_rejected_before_follow(tmp_path, monkeypatch, local_http):
    httpd, handler = local_http
    handler.mode = 'redirect'
    initial = str(httpd.server_address[0])
    # Allow the controlled first local server but reject the distinct redirect target.
    checks = []
    def guarded(host):
        checks.append(host)
        return (True, '') if host == initial else (False, 'blocked redirect')
    monkeypatch.setattr(server_mod, '_is_public_host', guarded)
    srv, root = make_server(tmp_path)
    dst = root / 'out.bin'
    text = await call(srv, url=f'http://{initial}:{httpd.server_port}/', dest=str(dst))
    assert text.startswith('Отказано:'), text
    assert 'blocked redirect' in text, text
    assert '127.0.0.2' in checks
    assert not dst.exists()


@pytest.mark.asyncio
async def test_dns_rebinding_private_second_answer_blocked(tmp_path, monkeypatch):
    srv, root = make_server(tmp_path)
    original = socket.getaddrinfo
    count = []
    def swapped(host, port, *args, **kwargs):
        if host == 'public.test':
            count.append(1)
            ip = '93.184.216.34' if len(count) == 1 else '127.0.0.1'
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port or 0))]
        return original(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', swapped)
    text = await call(srv, url='http://public.test/resource', dest=str(root / 'out'))
    assert 'Отказано:' in text and ('приватн' in text or 'адрес отклонён' in text), text
    assert not (root / 'out').exists()


@pytest.mark.asyncio
async def test_overwrite_preserved_on_size_limit_and_read_error(tmp_path, monkeypatch, local_http):
    httpd, handler = local_http
    monkeypatch.setattr(server_mod, '_is_public_host', lambda h: (True, ''))
    srv, root = make_server(tmp_path, extra='fetch_max_bytes = 4\n')
    dst = root / 'saved.bin'
    dst.write_bytes(b'original bytes')
    text = await call(srv, url=f'http://127.0.0.1:{httpd.server_port}/', dest=str(dst), overwrite=True)
    assert 'лимит' in text
    assert dst.read_bytes() == b'original bytes'
    assert sorted(p.name for p in root.iterdir()) == ['saved.bin']


@pytest.mark.asyncio
async def test_overwrite_preserved_on_truncated_transfer(tmp_path, monkeypatch, local_http):
    httpd, handler = local_http
    handler.mode = 'partial'
    monkeypatch.setattr(server_mod, '_is_public_host', lambda h: (True, ''))
    srv, root = make_server(tmp_path)
    dst = root / 'saved.bin'
    dst.write_bytes(b'original')
    text = await call(srv, url=f'http://127.0.0.1:{httpd.server_port}/', dest=str(dst), overwrite=True)
    assert text.startswith('Отказано:'), text
    assert dst.read_bytes() == b'original'
    assert sorted(p.name for p in root.iterdir()) == ['saved.bin']


@pytest.mark.asyncio
async def test_successful_overwrite_replaces_file(tmp_path, monkeypatch, local_http):
    httpd, handler = local_http
    monkeypatch.setattr(server_mod, '_is_public_host', lambda h: (True, ''))
    srv, root = make_server(tmp_path)
    dst = root / 'saved.bin'
    dst.write_bytes(b'old')
    text = await call(srv, url=f'http://127.0.0.1:{httpd.server_port}/', dest=str(dst), overwrite=True)
    assert 'скачано 9' in text
    assert dst.read_bytes() == b'new-bytes'
