"""syntax_check: однострочный вердикт после записи и молчание там, где проверять нечем."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from dev_mcp import syntax_check  # noqa: E402
from dev_mcp.syntax_check import check  # noqa: E402

from fastmcp import Client  # noqa: E402

from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.policy import Policy  # noqa: E402
from dev_mcp.server import build_server  # noqa: E402


def make_server(tmp_path: Path):
    root = tmp_path / 'workspace'
    (root / 'pkg').mkdir(parents=True)
    config = tmp_path / 'policy.toml'
    config.write_text(f"roots = ['{root}']\nallow_write = true\n", encoding='utf-8')
    return build_server(Policy.load(config), Audit(tmp_path / 'audit.jsonl')), root


async def call(server, name: str, args: dict) -> str:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
        return result.content[0].text


def _write(path: Path, text: str, *, bom: bool = False) -> Path:
    path.write_bytes((b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8'))
    return path


def test_py_ok(tmp_path: Path) -> None:
    f = _write(tmp_path / 'ok.py', 'def f():\n    return "привет"\n')
    assert check(f) == 'проверка синтаксиса (py): ok'


def test_py_ok_with_bom(tmp_path: Path) -> None:
    f = _write(tmp_path / 'bom.py', 'x = 1\n', bom=True)
    assert check(f) == 'проверка синтаксиса (py): ok'


def test_py_broken_reports_line_and_col(tmp_path: Path) -> None:
    f = _write(tmp_path / 'bad.py', 'x = 1\ndef f(:\n    pass\n')
    verdict = check(f)
    assert verdict is not None
    assert verdict.startswith('проверка синтаксиса (py): ОШИБКА ')
    assert f'{f}:2:' in verdict
    assert '\n' not in verdict


def test_py_syntax_warning_is_not_error(tmp_path: Path) -> None:
    f = _write(tmp_path / 'warn.py', 's = "\\d"\n')
    assert check(f) == 'проверка синтаксиса (py): ok'


def test_json_ok_and_broken(tmp_path: Path) -> None:
    ok = _write(tmp_path / 'ok.json', '{"a": [1, 2]}', bom=True)
    assert check(ok) == 'проверка синтаксиса (json): ok'
    bad = _write(tmp_path / 'bad.json', '{\n  "a": 1,\n}\n')
    verdict = check(bad)
    assert verdict is not None and 'ОШИБКА' in verdict
    # позиция ошибки зависит от версии Python: 3.12 — 2:9 (запятая), 3.13 — 3:1 (скобка)
    assert f'{bad}:2:9' in verdict or f'{bad}:3:1' in verdict


def test_toml_ok_and_broken(tmp_path: Path) -> None:
    assert check(_write(tmp_path / 'ok.toml', 'a = 1\n[b]\nc = "x"\n')) == 'проверка синтаксиса (toml): ok'
    verdict = check(_write(tmp_path / 'bad.toml', 'a = \n'))
    assert verdict is not None and verdict.startswith('проверка синтаксиса (toml): ОШИБКА')


def test_yaml_when_available(tmp_path: Path) -> None:
    f = _write(tmp_path / 'ok.yml', 'a: 1\nb: [1, 2]\n')
    try:
        import yaml  # noqa: F401
    except ImportError:
        assert check(f) is None
        return
    assert check(f) == 'проверка синтаксиса (yaml): ok'
    verdict = check(_write(tmp_path / 'bad.yaml', 'a: [1, 2\nb: 3\n'))
    assert verdict is not None and 'ОШИБКА' in verdict


@pytest.mark.parametrize('name', ['notes.md', 'script.ps1', 'data.txt', 'noext'])
def test_unknown_types_are_skipped(tmp_path: Path, name: str) -> None:
    assert check(_write(tmp_path / name, 'что угодно {')) is None


def test_missing_and_huge_files_are_skipped(tmp_path: Path, monkeypatch) -> None:
    assert check(tmp_path / 'nope.py') is None
    f = _write(tmp_path / 'big.py', 'x = 1\n')
    monkeypatch.setattr(syntax_check, 'MAX_CHECK_BYTES', 3)
    assert check(f) is None


def test_js_without_node_is_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(syntax_check.shutil, 'which', lambda name: None)
    assert check(_write(tmp_path / 'a.js', 'let x = ;')) is None


def test_js_timeout_is_skipped(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(syntax_check.shutil, 'which', lambda name: 'node')

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired('node', 5)

    monkeypatch.setattr(syntax_check.subprocess, 'run', boom)
    assert check(_write(tmp_path / 'a.mjs', 'export const x = 1;\n')) is None


def test_unexpected_exception_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **kw):
        raise RuntimeError('внезапно')

    monkeypatch.setattr(syntax_check, '_check_json', boom)
    assert check(_write(tmp_path / 'a.json', '{}')) is None


@pytest.mark.skipif(shutil.which('node') is None, reason='node нет в PATH')
def test_js_with_real_node(tmp_path: Path) -> None:
    assert check(_write(tmp_path / 'ok.js', 'const x = 1;\n')) == 'проверка синтаксиса (js): ok'
    verdict = check(_write(tmp_path / 'bad.js', 'const x = ;\n'))
    assert verdict is not None and verdict.startswith('проверка синтаксиса (js): ОШИБКА')
    assert 'SyntaxError' in verdict and '\n' not in verdict


# ---------- через настоящий MCP-клиент ----------


@pytest.mark.asyncio
async def test_write_and_edit_responses_carry_verdict(tmp_path: Path) -> None:
    server, root = make_server(tmp_path)
    target = root / 'pkg' / 'new.py'

    text = await call(server, 'write_file', {'path': str(target), 'content': 'def f(:\n    pass\n'})
    last = text.rstrip().splitlines()[-1]
    assert last.startswith('проверка синтаксиса (py): ОШИБКА')
    assert f'{target}:1:' in last

    text = await call(server, 'edit_file', {
        'path': str(target), 'old_string': 'def f(:', 'new_string': 'def f():',
    })
    assert text.rstrip().splitlines()[-1] == 'проверка синтаксиса (py): ok'

    text = await call(server, 'write_file', {'path': str(root / 'notes.md'), 'content': '# x'})
    assert 'проверка синтаксиса' not in text


@pytest.mark.asyncio
async def test_verdict_survives_clipping(tmp_path: Path, monkeypatch) -> None:
    from dev_mcp import server as srv

    server, root = make_server(tmp_path)
    # Ответ заведомо длиннее потолка: обрезка обязана случиться, а вердикт — остаться.
    monkeypatch.setattr(srv, 'MAX_RESPONSE_CHARS', 120)
    target = root / 'pkg' / ('очень_длинное_имя_' * 6 + '.json')
    text = await call(server, 'write_file', {'path': str(target), 'content': '{"a": 1'})
    assert 'обрезано' in text
    assert text.rstrip().splitlines()[-1].startswith('проверка синтаксиса (json): ОШИБКА')
