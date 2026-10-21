"""apply_patch на сервере, переводы строк при записи и подсказки после промахов."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastmcp import Client  # noqa: E402

from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.policy import Policy  # noqa: E402
from dev_mcp.server import build_server  # noqa: E402


def make_server(tmp_path: Path):
    root = tmp_path / 'workspace'
    (root / 'pkg').mkdir(parents=True)
    config = tmp_path / 'policy.toml'
    config.write_text(f"roots = ['{root}']\nallow_write = true\n", encoding='utf-8')
    return build_server(Policy.load(config), Audit(tmp_path / 'logs' / 'audit.jsonl')), root


async def call(server, name: str, args: dict) -> str:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
        return result.content[0].text


@pytest.mark.asyncio
async def test_patch_updates_and_adds_files_keeping_eol(tmp_path: Path) -> None:
    server, root = make_server(tmp_path)
    (root / 'pkg' / 'a.py').write_bytes(b'x = 1\r\ny = 2\r\n')
    (root / 'pkg' / 'b.py').write_bytes(b'z = 1\n')
    patch = (
        '*** Begin Patch\n'
        '*** Update File: pkg/a.py\n'
        ' x = 1\n-y = 2\n+y = 3\n'
        '*** Update File: pkg/b.py\n'
        '-z = 1\n+z = 2\n'
        '*** Add File: pkg/c.py\n'
        '+w = 1\n'
        '*** End Patch'
    )
    text = await call(server, 'apply_patch', {'patch': patch, 'root': str(root)})
    assert 'применено файлов: 3' in text
    assert (root / 'pkg' / 'a.py').read_bytes() == b'x = 1\r\ny = 3\r\n'
    assert (root / 'pkg' / 'b.py').read_bytes() == b'z = 2\n'
    assert (root / 'pkg' / 'c.py').read_text(encoding='utf-8').strip() == 'w = 1'
    assert 'проверка синтаксиса (py): ok' in text


@pytest.mark.asyncio
async def test_patch_is_all_or_nothing(tmp_path: Path) -> None:
    server, root = make_server(tmp_path)
    (root / 'pkg' / 'a.py').write_text('x = 1\n', encoding='utf-8')
    patch = (
        '*** Begin Patch\n'
        '*** Update File: pkg/a.py\n-x = 1\n+x = 2\n'
        '*** Update File: pkg/missing.py\n-q\n+r\n'
        '*** End Patch'
    )
    text = await call(server, 'apply_patch', {'patch': patch, 'root': str(root)})
    assert text.startswith('Отказано: pkg/missing.py: файл не найден')
    assert (root / 'pkg' / 'a.py').read_text(encoding='utf-8') == 'x = 1\n'


@pytest.mark.asyncio
async def test_patch_respects_roots(tmp_path: Path) -> None:
    server, root = make_server(tmp_path)
    outside = tmp_path / 'outside.py'
    patch = f'*** Begin Patch\n*** Add File: {outside}\n+x = 1\n*** End Patch'
    text = await call(server, 'apply_patch', {'patch': patch})
    assert text.startswith('Отказано:')
    assert not outside.exists()


@pytest.mark.asyncio
async def test_relative_path_needs_root(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    text = await call(server, 'apply_patch', {'patch': '*** Begin Patch\n*** Add File: x.py\n+1\n*** End Patch'})
    assert 'передай root' in text


@pytest.mark.asyncio
async def test_edit_file_keeps_lf_and_crlf(tmp_path: Path) -> None:
    server, root = make_server(tmp_path)
    lf, crlf = root / 'pkg' / 'lf.txt', root / 'pkg' / 'crlf.txt'
    lf.write_bytes(b'a\nb\nc\n')
    crlf.write_bytes(b'a\r\nb\r\nc\r\n')
    await call(server, 'edit_file', {'path': str(lf), 'old_string': 'b\nc', 'new_string': 'B\nC'})
    await call(server, 'edit_file', {'path': str(crlf), 'old_string': 'b\nc', 'new_string': 'B\nC'})
    assert lf.read_bytes() == b'a\nB\nC\n'
    assert crlf.read_bytes() == b'a\r\nB\r\nC\r\n'


@pytest.mark.asyncio
async def test_write_file_overwrite_keeps_lf(tmp_path: Path) -> None:
    server, root = make_server(tmp_path)
    f = root / 'pkg' / 'lf.txt'
    f.write_bytes(b'old\n')
    await call(server, 'write_file', {'path': str(f), 'content': 'one\ntwo\n', 'overwrite': True})
    assert f.read_bytes() == b'one\ntwo\n'


@pytest.mark.asyncio
async def test_edit_miss_shows_closest_lines(tmp_path: Path) -> None:
    server, root = make_server(tmp_path)
    f = root / 'pkg' / 'm.py'
    f.write_text('def alpha():\n    return compute(1, 2)\n', encoding='utf-8')
    text = await call(server, 'edit_file', {
        'path': str(f), 'old_string': '    return compute(1,2)', 'new_string': 'x'})
    assert 'old_string в файле не найден' in text
    assert 'Ближе всего — строка 2' in text


@pytest.mark.asyncio
async def test_read_missing_file_suggests_neighbours(tmp_path: Path) -> None:
    server, root = make_server(tmp_path)
    (root / 'pkg' / 'catalog_check.py').write_text('x\n', encoding='utf-8')
    text = await call(server, 'read_file', {'path': str(root / 'pkg' / 'catalog_chek.py')})
    assert 'файл не найден' in text and 'catalog_check.py' in text
