"""Actual FastMCP image blocks and policy boundaries for visual reviews."""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dev_mcp.audit import Audit
from dev_mcp.policy import Policy
from dev_mcp.server import _image_payload, build_server


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    row = b'\x00' + b'\x89\xaa\xbb' * width
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(row * height)) + chunk(b'IEND', b''))


def setup(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    config = tmp_path / 'policy.toml'
    config.write_text(f"roots = ['{root.as_posix()}']\ndeny_patterns = ['*secret*']\n", encoding='utf-8')
    server = build_server(Policy.load(config), Audit(tmp_path / 'audit.jsonl'))
    return server, root


@pytest.mark.asyncio
async def test_read_image_1x1_and_batch(tmp_path):
    server, root = setup(tmp_path)
    one = root / 'one.png'
    one.write_bytes(png(1, 1))
    async with Client(server) as client:
        result = await client.call_tool('read_image', {'path': str(one)})
        assert '1×1 px' in result.content[0].text
        assert result.content[1].type == 'image'
        assert result.content[1].mime_type == 'image/png'
        result = await client.call_tool('read_images', {'paths': [str(one), str(one)]})
        assert sum(block.type == 'image' for block in result.content) == 2
        result = await client.call_tool('read_images', {'paths': [str(one)] * 13})
        assert 'Отказано' in result.content[0].text


@pytest.mark.asyncio
async def test_image_batch_byte_budget_skips_unread_tail(tmp_path, monkeypatch):
    import dev_mcp.server as server_module
    # Exercise the real byte-accounting code with tiny files, not 24 MiB of
    # synthetic images on a host whose idle RAM is already constrained.
    monkeypatch.setattr(server_module, '_IMAGE_LIMIT', 200)
    server, root = setup(tmp_path)
    sample = png(1, 1)
    assert len(sample) < 170
    # Simulate a resized preview: the budget must count full source files.
    monkeypatch.setattr(server_module, '_image_payload',
                        lambda raw, suffix, side: (sample, 'png', 1, 1))
    paths = []
    for name in ('one', 'two', 'three', 'four'):
        path = root / f'{name}.png'
        path.write_bytes(sample + b' ' * (180 - len(sample)))
        paths.append(str(path))
    async with Client(server) as client:
        result = await client.call_tool('read_images', {'paths': paths})
        assert sum(block.type == 'image' for block in result.content) == 2
        denied = ' '.join(block.text for block in result.content if block.type == 'text')
        assert 'общий лимит изображений' in denied
        assert 'three.png' in denied and 'four.png' in denied
        assert 'не вошли 2' in denied


@pytest.mark.asyncio
async def test_image_batch_rejects_hostile_path_lists_without_reading(tmp_path):
    server, root = setup(tmp_path)
    one = root / 'one.png'
    one.write_bytes(png(1, 1))
    async with Client(server) as client:
        for paths in ([], [str(one)] * 20_000):
            result = await client.call_tool('read_images', {'paths': paths})
            assert 'от 1 до 12 путей' in result.content[0].text
            assert not any(block.type == 'image' for block in result.content)
        # Duplicates are explicitly separate images, and consume the budget.
        result = await client.call_tool('read_images', {'paths': [str(one)] * 12})
        assert sum(block.type == 'image' for block in result.content) == 12


@pytest.mark.asyncio
async def test_image_denials_and_audit(tmp_path):
    server, root = setup(tmp_path)
    outside = tmp_path / 'outside.png'
    outside.write_bytes(png(1, 1))
    denied = root / 'mysecret.png'
    denied.write_bytes(png(1, 1))
    nonimage = root / 'note.txt'
    nonimage.write_text('not an image')
    counterfeit = root / 'counterfeit.png'
    counterfeit.write_text('not an image')
    async with Client(server) as client:
        for path in (outside, denied, nonimage, counterfeit):
            result = await client.call_tool('read_image', {'path': str(path)})
            assert 'Отказано' in result.content[0].text
            assert not any(block.type == 'image' for block in result.content)
    log = (tmp_path / 'audit.jsonl').read_text(encoding='utf-8')
    assert 'read_image' in log and 'deny' in log


def test_large_image_scaling_if_pillow_available():
    raw = png(2000, 1000)
    try:
        import PIL  # noqa: F401
    except ImportError:
        data, fmt, w, h = _image_payload(raw, '.png', 400)
        assert data == raw and (w, h) == (2000, 1000)
    else:
        from io import BytesIO
        from PIL import Image as PILImage
        data, fmt, w, h = _image_payload(raw, '.png', 400)
        assert fmt == 'png' and (w, h) == (2000, 1000)
        with PILImage.open(BytesIO(data)) as pic:
            assert pic.size == (400, 200)
