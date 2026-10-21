"""Проверка инструментов через настоящий MCP-клиент (in-memory).

Тесты политики рядом проверяют границу как функцию; эти — что до неё доходит
вызов, собранный клиентом, и что отказ возвращается как понятный текст, а не
падение сервера. Внешнему клиенту падение выглядит как «инструмент сломан», и
он начинает биться вслепую.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastmcp import Client  # noqa: E402

from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.policy import Policy  # noqa: E402
from dev_mcp.server import build_server  # noqa: E402


def test_slow_backup_cannot_block_building_mcp_server(tmp_path, monkeypatch):
    import threading
    import dev_mcp.server as server_mod
    from dev_mcp.fleet.store import Store

    database = tmp_path / "data" / "fleet" / "fleet.sqlite"
    Store.open(str(database))
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def slow_copy(path):
        started.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()
        return None
    monkeypatch.setattr(server_mod, "backup_fleet_db", slow_copy)
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(f"roots = ['{tmp_path.as_posix()}']\n", encoding="utf-8")
    try:
        server = build_server(Policy.load(policy_path), Audit(tmp_path / "logs" / "audit.jsonl"))
        assert server is not None
        assert started.wait(1)
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(2)


def make_server(tmp_path: Path, *, allow_write: bool):
    root = tmp_path / 'workspace'
    (root / 'pkg').mkdir(parents=True)
    (root / 'pkg' / 'main.py').write_text(
        'def run():\n    return "маркер-поиска"\n', encoding='utf-8'
    )
    (root / '.env').write_text('SECRET_KEY=abc123', encoding='utf-8')
    (tmp_path / 'outside').mkdir()
    (tmp_path / 'outside' / 'private.txt').write_text('чужое', encoding='utf-8')

    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{root}']\n"
        f'allow_write = {"true" if allow_write else "false"}\n'
        "deny_patterns = ['.env', '.env.*', '.git']\n"
        "ignore_patterns = ['__pycache__']\n",
        encoding='utf-8',
    )
    policy = Policy.load(config)
    audit = Audit(tmp_path / 'audit.jsonl')
    return build_server(policy, audit), root, audit


async def call(server, name: str, args: dict) -> str:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
        return result.content[0].text


@pytest.mark.asyncio
async def test_policy_info_lists_roots(tmp_path: Path) -> None:
    server, root, _ = make_server(tmp_path, allow_write=False)
    text = await call(server, 'policy_info', {})
    assert str(root) in text
    assert 'ЗАПРЕЩЕНА' in text


@pytest.mark.asyncio
async def test_read_file_inside_root(tmp_path: Path) -> None:
    server, root, _ = make_server(tmp_path, allow_write=False)
    text = await call(server, 'read_file', {'path': str(root / 'pkg' / 'main.py')})
    assert 'маркер-поиска' in text
    assert '1\t' in text  # нумерация строк


@pytest.mark.asyncio
async def test_read_file_outside_root_denied(tmp_path: Path) -> None:
    server, _, _ = make_server(tmp_path, allow_write=False)
    text = await call(server, 'read_file', {'path': str(tmp_path / 'outside' / 'private.txt')})
    assert text.startswith('Отказано')
    assert 'чужое' not in text


@pytest.mark.asyncio
async def test_read_secret_denied(tmp_path: Path) -> None:
    server, root, _ = make_server(tmp_path, allow_write=False)
    text = await call(server, 'read_file', {'path': str(root / '.env')})
    assert text.startswith('Отказано')
    assert 'SECRET_KEY' not in text


@pytest.mark.asyncio
async def test_list_dir_hides_denied_entries(tmp_path: Path) -> None:
    server, root, _ = make_server(tmp_path, allow_write=False)
    text = await call(server, 'list_dir', {'path': str(root)})
    assert 'pkg' in text
    assert '.env' not in text


@pytest.mark.asyncio
async def test_search_finds_content(tmp_path: Path) -> None:
    server, root, _ = make_server(tmp_path, allow_write=False)
    text = await call(server, 'search_files', {'query': 'маркер-поиска', 'glob': '*.py'})
    assert 'main.py' in text


@pytest.mark.asyncio
async def test_search_does_not_leak_denied_files(tmp_path: Path) -> None:
    server, _, _ = make_server(tmp_path, allow_write=False)
    text = await call(server, 'search_files', {'query': 'SECRET_KEY'})
    assert 'SECRET_KEY=abc123' not in text
    assert 'Совпадений нет' in text


@pytest.mark.asyncio
async def test_search_does_not_descend_into_ignored_dir(tmp_path: Path) -> None:
    """search_files не должен даже СПУСКАТЬСЯ в каталог под ignore_patterns —
    не только выбросить найденное там из выдачи (регрессия на Policy.prunes_dir:
    раньше rglob('*') обходил .venv/node_modules/__pycache__ целиком)."""
    server, root, _ = make_server(tmp_path, allow_write=False)
    poisoned = root / 'pkg' / '__pycache__' / 'nested'
    poisoned.mkdir(parents=True)
    (poisoned / 'cached.py').write_text('маркер-поиска', encoding='utf-8')

    text = await call(server, 'search_files', {'query': 'маркер-поиска'})
    assert 'cached.py' not in text
    assert 'main.py' in text  # соседний реальный файл по-прежнему находится


@pytest.mark.asyncio
async def test_list_dir_recursive_prunes_ignored_and_denied(tmp_path: Path) -> None:
    """list_dir(recursive=true) не спускается в .env-подобные и __pycache__-подобные
    каталоги, но по-прежнему находит настоящие вложенные файлы."""
    server, root, _ = make_server(tmp_path, allow_write=False)
    (root / 'pkg' / '__pycache__').mkdir()
    (root / 'pkg' / '__pycache__' / 'cached.pyc').write_text('x', encoding='utf-8')
    (root / 'sub').mkdir()
    (root / 'sub' / 'deep.py').write_text('x', encoding='utf-8')

    text = await call(server, 'list_dir', {'path': str(root), 'recursive': True})
    assert 'deep.py' in text
    assert 'main.py' in text
    assert 'cached.pyc' not in text
    assert '__pycache__' not in text
    assert '.env' not in text


@pytest.mark.asyncio
async def test_write_denied_in_readonly(tmp_path: Path) -> None:
    server, root, _ = make_server(tmp_path, allow_write=False)
    text = await call(server, 'write_file', {'path': str(root / 'new.txt'), 'content': 'x'})
    assert text.startswith('Отказано')
    assert not (root / 'new.txt').exists()


@pytest.mark.asyncio
async def test_write_and_edit_work_when_allowed(tmp_path: Path) -> None:
    server, root, _ = make_server(tmp_path, allow_write=True)
    target = root / 'notes.md'
    await call(server, 'write_file', {'path': str(target), 'content': 'первая строка\n'})
    assert target.read_text(encoding='utf-8') == 'первая строка\n'

    await call(server, 'edit_file', {'path': str(target), 'old_string': 'первая', 'new_string': 'вторая'})
    assert target.read_text(encoding='utf-8') == 'вторая строка\n'

    repeated = await call(server, 'edit_file', {
        'path': str(target), 'old_string': 'первая', 'new_string': 'вторая',
    })
    assert not repeated.startswith('Отказано')
    assert 'уже применена' in repeated
    assert target.read_text(encoding='utf-8') == 'вторая строка\n'


@pytest.mark.asyncio
async def test_edit_missing_old_is_not_noop_when_new_is_ambiguous(tmp_path: Path) -> None:
    server, root, _ = make_server(tmp_path, allow_write=True)
    target = root / 'already-many.txt'
    target.write_text('новое\nновое\n', encoding='utf-8')
    text = await call(server, 'edit_file', {
        'path': str(target), 'old_string': 'старое', 'new_string': 'новое',
    })
    assert text.startswith('Отказано')
    assert target.read_text(encoding='utf-8') == 'новое\nновое\n'


@pytest.mark.asyncio
async def test_edit_refuses_ambiguous_match(tmp_path: Path) -> None:
    """Неоднозначная замена опаснее отказа: молча правит не то место."""
    server, root, _ = make_server(tmp_path, allow_write=True)
    target = root / 'dup.txt'
    target.write_text('повтор\nповтор\n', encoding='utf-8')
    text = await call(server, 'edit_file', {'path': str(target), 'old_string': 'повтор', 'new_string': 'x'})
    assert text.startswith('Отказано')
    assert target.read_text(encoding='utf-8') == 'повтор\nповтор\n'


@pytest.mark.asyncio
async def test_unknown_task_denied_with_hint(tmp_path: Path) -> None:
    server, _, _ = make_server(tmp_path, allow_write=True)
    text = await call(server, 'run_task', {'name': 'rm-rf-everything'})
    assert text.startswith('Отказано')
    assert 'не описана' in text


@pytest.mark.asyncio
async def test_policy_info_flags_unrunnable_task(tmp_path: Path) -> None:
    """policy_info не должен молча объявлять доступной задачу, которую run_task
    отклонит: cwd вне write_roots помечается прямо в выдаче, а run_task на неё
    отвечает отказом (регрессия из баг-репорта 2026-09-10)."""
    root = tmp_path / 'workspace'
    (root / 'core').mkdir(parents=True)
    (tmp_path / 'wr').mkdir()
    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{root}', '{tmp_path / 'wr'}']\n"
        f"write_roots = ['{tmp_path / 'wr'}']\n"
        'allow_write = false\n'
        "deny_patterns = ['.env']\n"
        '[tasks.corecheck]\n'
        f"cwd = '{root / 'core'}'\n"
        "argv = ['cmd', '/c', 'echo', 'ok']\n"
        "description = 'проверка ядра'\n",
        encoding='utf-8',
    )
    policy = Policy.load(config)
    server = build_server(policy, Audit(tmp_path / 'audit.jsonl'))

    info = await call(server, 'policy_info', {})
    assert 'corecheck' in info
    assert 'НЕ ЗАПУСТИТСЯ' in info

    denied = await call(server, 'run_task', {'name': 'corecheck'})
    assert denied.startswith('Отказано')


@pytest.mark.asyncio
async def test_audit_records_allow_and_deny(tmp_path: Path) -> None:
    server, root, audit = make_server(tmp_path, allow_write=False)
    await call(server, 'read_file', {'path': str(root / 'pkg' / 'main.py')})
    await call(server, 'read_file', {'path': str(root / '.env')})

    lines = audit.path.read_text(encoding='utf-8').strip().splitlines()
    assert any('"decision": "allow"' in line for line in lines)
    assert any('"decision": "deny"' in line for line in lines)
    assert 'SECRET_KEY' not in audit.path.read_text(encoding='utf-8')
