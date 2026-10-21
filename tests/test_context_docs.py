"""Инструкции папки при первом касании (dev_mcp.context_docs) — через настоящий MCP-клиент."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastmcp import Client  # noqa: E402

from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.policy import Policy  # noqa: E402
from dev_mcp.server import MAX_RESPONSE_CHARS, build_server  # noqa: E402


def make(tmp_path: Path, agents_text: str = 'ПРАВИЛО-КОРНЯ: тесты перед сдачей\n'):
    root = tmp_path / 'proj'
    (root / 'app' / 'core').mkdir(parents=True)
    (root / 'AGENTS.md').write_text(agents_text, encoding='utf-8')
    (root / 'app' / 'CLAUDE.md').write_text('ПРАВИЛО-APP: не трогать схему\n', encoding='utf-8')
    (root / 'app' / 'core' / 'x.py').write_text('X = 1\n', encoding='utf-8')
    config = tmp_path / 'policy.toml'
    config.write_text(f"roots = ['{root}']\nallow_write = false\n", encoding='utf-8')
    return build_server(Policy.load(config), Audit(tmp_path / 'audit.jsonl')), root


async def calls(server, seq: list[tuple[str, dict]]) -> list[str]:
    out = []
    async with Client(server) as client:
        for name, args in seq:
            out.append((await client.call_tool(name, args)).content[0].text)
    return out


@pytest.mark.asyncio
async def test_first_touch_delivers_chain_once(tmp_path: Path) -> None:
    server, root = make(tmp_path)
    x = str(root / 'app' / 'core' / 'x.py')
    first, second = await calls(server, [
        ('read_file', {'path': x, 'agent': 'W1'}),
        ('list_dir', {'path': str(root / 'app'), 'agent': 'W1'}),
    ])
    assert 'ПРАВИЛО-КОРНЯ' in first and 'ПРАВИЛО-APP' in first
    assert first.index('ПРАВИЛО-КОРНЯ') < first.index('ПРАВИЛО-APP') < first.index('X = 1')
    assert 'инструкции папки' not in second


@pytest.mark.asyncio
async def test_other_agent_and_anonymous(tmp_path: Path) -> None:
    server, root = make(tmp_path)
    x = str(root / 'app' / 'core' / 'x.py')
    anon, w1, w2 = await calls(server, [
        ('read_file', {'path': x}),
        ('read_file', {'path': x, 'agent': 'W1'}),
        ('read_file', {'path': x, 'agent': 'W2'}),
    ])
    assert 'инструкции папки' not in anon
    assert 'ПРАВИЛО-КОРНЯ' in w1 and 'ПРАВИЛО-КОРНЯ' in w2


@pytest.mark.asyncio
async def test_own_read_counts_and_change_redelivers(tmp_path: Path) -> None:
    server, root = make(tmp_path)
    agents = root / 'AGENTS.md'
    own, touch = await calls(server, [
        ('read_file', {'path': str(agents), 'agent': 'W1'}),
        ('list_dir', {'path': str(root), 'agent': 'W1'}),
    ])
    assert own.count('ПРАВИЛО-КОРНЯ') == 1  # сам файл, без дубля сверху
    assert 'инструкции папки' not in touch
    import os, time
    agents.write_text('ПРАВИЛО-КОРНЯ v2\n', encoding='utf-8')
    later = time.time() + 5
    os.utime(agents, (later, later))
    (after,) = await calls(server, [('list_dir', {'path': str(root), 'agent': 'W1'})])
    assert 'ПРАВИЛО-КОРНЯ v2' in after


@pytest.mark.asyncio
async def test_large_doc_gives_head_and_continuation(tmp_path: Path) -> None:
    big = ''.join(f'строка {i} правил проекта\n' for i in range(3000))
    server, root = make(tmp_path, agents_text=big)
    (text,) = await calls(server, [('list_dir', {'path': str(root), 'agent': 'W1'})])
    assert len(text) <= MAX_RESPONSE_CHARS + 200
    assert 'строка 0 правил' in text
    assert 'read_file(' in text and 'offset=' in text
    assert str(root / 'app') in text or 'app/' in text  # полезная нагрузка не вытеснена
