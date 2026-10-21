"""Regression: MCP file tools must paginate/search Case files larger than 400 KB.

Kept next to the implementation because fleet request dd729c92 owns only
src/dev_mcp/. Run explicitly: py -3 -m pytest src/dev_mcp/test_large_file_tools.py -q
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastmcp import Client  # noqa: E402
from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.policy import Policy  # noqa: E402
from dev_mcp.server import build_server  # noqa: E402


@pytest.mark.asyncio
async def test_large_case_chunk_and_search(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    case = root / 'case-big.md'
    case.write_text('# Case case:example\n' + 'я' * 210_000 + '\nTailMarker\n', encoding='utf-8')
    assert case.stat().st_size > 400_000
    policy_file = tmp_path / 'policy.toml'
    policy_file.write_text(
        f"roots = ['{root}']\nallow_write = false\n"
        "deny_patterns = ['.env', '.git']\n",
        encoding='utf-8',
    )
    server = build_server(Policy.load(policy_file), Audit(tmp_path / 'audit.jsonl'))
    async with Client(server) as client:
        result = await client.call_tool(
            'read_file', {'path': str(case), 'offset': 3, 'limit': 1}
        )
        read_text = result.content[0].text
        assert 'строк всего: 3' in read_text
        assert '3\tTailMarker' in read_text
        assert 'файл больше лимита' not in read_text

        result = await client.call_tool(
            'read_file', {'path': str(case), 'offset': 2, 'limit': 1}
        )
        bounded_text = result.content[0].text
        assert '2\t' in bounded_text
        assert 'строка обрезана' in bounded_text
        assert len(bounded_text.encode('utf-8')) < 120_000

        result = await client.call_tool(
            'search_files',
            {'path': str(root), 'glob': '*.md', 'query': '^# Case', 'regex': True},
        )
        assert 'case-big.md:1: # Case case:example' in result.content[0].text

        result = await client.call_tool(
            'search_files', {'path': str(root), 'glob': '*.md', 'query': 'TailMarker'},
        )
        assert 'case-big.md:3: TailMarker' in result.content[0].text

        # An exact file locator must work too, not only a directory search.
        result = await client.call_tool(
            'search_files', {'path': str(case), 'query': '^# Case', 'regex': True},
        )
        assert 'case-big.md:1: # Case case:example' in result.content[0].text


@pytest.mark.asyncio
async def test_large_binary_disguised_as_text_is_not_searched(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'pretend.md').write_bytes(b'\x00' + b'needle' * 70_000)
    config = tmp_path / 'policy.toml'
    config.write_text(f"roots = ['{root}']\nallow_write = false\n", encoding='utf-8')
    server = build_server(Policy.load(config), Audit(tmp_path / 'audit.jsonl'))
    async with Client(server) as client:
        result = await client.call_tool(
            'search_files', {'path': str(root), 'glob': '*.md', 'query': 'needle'},
        )
        assert 'Совпадений нет' in result.content[0].text
