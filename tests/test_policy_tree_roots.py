"""TZ-08: tree_roots grant subtrees, never root files or tree_deny paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from dev_mcp.policy import Policy, PolicyError  # noqa: E402


def _item(path: Path) -> str:
    return "'" + path.as_posix() + "'"


def _policy(tmp_path: Path, *, tree: bool = True, extras: str = '') -> tuple[Policy, Path, Path, Path]:
    root = tmp_path / 'workspace'
    root.mkdir(exist_ok=True)
    protected = root / 'protected'
    protected.mkdir(exist_ok=True)
    outside = tmp_path / 'outside'
    outside.mkdir(exist_ok=True)
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(
        f'roots = [{_item(root)}]\n'
        + (f'tree_roots = [{_item(root)}]\ntree_deny = [{_item(protected)}]\n' if tree else '')
        + "deny_patterns = ['.env', '*.pem', '*secret*']\n"
        + extras,
        encoding='utf-8',
    )
    return Policy.load(cfg), root, protected, cfg


def test_tree_allows_nested_files_and_creation_of_first_level_folder(tmp_path: Path) -> None:
    policy, root, _, _ = _policy(tmp_path)
    file = root / 'brand-new-project' / 'deep' / 'file.txt'
    assert policy.resolve(str(file), for_write=True) == file
    file.parent.mkdir(parents=True)
    assert (root / 'brand-new-project').is_dir()
    assert policy.resolve_exec_cwd(str(file.parent), '') == file.parent
    assert policy.exec_root_for(file) == root


@pytest.mark.parametrize('extras', ['', 'allow_write = true\n'])
def test_tree_root_file_is_owner_only_even_with_global_write(tmp_path: Path, extras: str) -> None:
    policy, root, _, _ = _policy(tmp_path, extras=extras)
    with pytest.raises(PolicyError, match='файлы корня правит владелец'):
        policy.resolve(str(root / 'new.txt'), for_write=True)
    assert policy.exec_root_for(root / 'script.py') is None
    with pytest.raises(PolicyError, match='файлы корня правит владелец'):
        policy.resolve_exec_cwd(str(root), '')
    assert policy.resolve(str(root / 'new.txt'), for_write=False) == root / 'new.txt'


def test_tree_root_file_still_denied_with_explicit_legacy_grants(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    policy, root, _, _ = _policy(tmp_path, extras=f'write_roots = [{_item(root)}]\nexec_roots = [{_item(root)}]\n')
    with pytest.raises(PolicyError, match='файлы корня правит владелец'):
        policy.resolve(str(root / 'new.txt'), for_write=True)
    assert policy.exec_root_for(root / 'run.py') is None


def test_tree_deny_blocks_write_and_execution_but_not_read(tmp_path: Path) -> None:
    policy, root, denied, _ = _policy(tmp_path)
    target = denied / 'deep' / 'main.py'
    assert policy.resolve(str(target), for_write=False) == target
    with pytest.raises(PolicyError, match='tree_deny'):
        policy.resolve(str(target), for_write=True)
    assert policy.exec_root_for(target) is None
    with pytest.raises(PolicyError, match='tree_deny'):
        policy.resolve_exec_cwd(str(denied), '')


def test_tree_deny_does_not_block_sibling_prefix_and_cannot_be_skipped_by_dotdot(tmp_path: Path) -> None:
    policy, root, denied, _ = _policy(tmp_path)
    assert policy.resolve(str(root / 'protected-copy' / 'file.txt'), for_write=True)
    with pytest.raises(PolicyError, match='tree_deny'):
        policy.resolve(str(root / 'protected' / '..' / 'protected' / 'main.py'), for_write=True)
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        policy.resolve(str(root / '..' / 'outside' / 'file.txt'), for_write=True)


def test_lexical_tree_deny_cannot_escape_using_link_out_of_denied_dir(tmp_path: Path) -> None:
    policy, root, denied, _ = _policy(tmp_path)
    allowed = root / 'allowed'
    allowed.mkdir()
    link = denied / 'link'
    try:
        link.symlink_to(allowed, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'symlink недоступен: {exc}')
    with pytest.raises(PolicyError, match='tree_deny'):
        policy.resolve(str(link / 'new.py'), for_write=True)


def test_alias_into_tree_deny_is_blocked(tmp_path: Path) -> None:
    policy, root, denied, _ = _policy(tmp_path)
    link = root / 'alias'
    try:
        link.symlink_to(denied, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'symlink недоступен: {exc}')
    with pytest.raises(PolicyError, match='tree_deny'):
        policy.resolve(str(link / 'new.py'), for_write=True)


def test_link_out_of_tree_cannot_borrow_parent_read_root_write_grant(tmp_path: Path) -> None:
    """Если roots шире tree_roots, ссылка наружу не наследует write/exec."""
    readable = tmp_path / 'readable'
    project = readable / 'project'
    elsewhere = readable / 'elsewhere'
    project.mkdir(parents=True)
    elsewhere.mkdir()
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(
        f'roots = [{_item(readable)}]\ntree_roots = [{_item(project)}]\n',
        encoding='utf-8',
    )
    policy = Policy.load(cfg)
    with pytest.raises(PolicyError, match='сюда писать нельзя'):
        policy.resolve(str(project / '..' / 'elsewhere' / 'new.py'), for_write=True)

    link = project / 'escape'
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'symlink недоступен: {exc}')
    assert policy.resolve(str(link / 'new.py'), for_write=False) == elsewhere / 'new.py'
    with pytest.raises(PolicyError, match='сюда писать нельзя'):
        policy.resolve(str(link / 'new.py'), for_write=True)
    assert policy.exec_root_for(link / 'new.py') is None


@pytest.mark.skipif(os.name != 'nt', reason='Windows case-insensitive filesystem')
def test_case_and_device_prefix_cannot_bypass_denied_tree(tmp_path: Path) -> None:
    policy, root, denied, _ = _policy(tmp_path)
    with pytest.raises(PolicyError, match='tree_deny'):
        policy.resolve(str(denied / 'main.py').upper(), for_write=True)
    with pytest.raises(PolicyError, match='device'):
        policy.resolve('\\\\?\\' + str(root / 'any' / 'file.txt'), for_write=True)


def test_secret_deny_patterns_still_apply(tmp_path: Path) -> None:
    policy, root, _, _ = _policy(tmp_path)
    with pytest.raises(PolicyError, match='маской'):
        policy.resolve(str(root / 'project' / '.env'), for_write=True)
    with pytest.raises(PolicyError, match='маской'):
        policy.resolve(str(root / 'project' / 'secret.pem'), for_write=False)


def test_tree_roots_must_be_in_roots_and_deny_inside_tree(tmp_path: Path) -> None:
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'other'
    outside.mkdir()
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(f'roots = [{_item(root)}]\ntree_roots = [{_item(outside)}]\n', encoding='utf-8')
    with pytest.raises(PolicyError, match='tree_root'):
        Policy.load(cfg)
    cfg.write_text(
        f'roots = [{_item(root)}, {_item(outside)}]\n'
        f'tree_roots = [{_item(root)}]\n'
        f'tree_deny = [{_item(outside)}]\n', encoding='utf-8',
    )
    with pytest.raises(PolicyError, match='tree_deny'):
        Policy.load(cfg)


def test_nonexistent_tree_deny_still_protects_future_folder(tmp_path: Path) -> None:
    policy, root, _, cfg = _policy(tmp_path)
    future = root / 'future'
    cfg.write_text(
        f'roots = [{_item(root)}]\ntree_roots = [{_item(root)}]\n'
        f'tree_deny = [{_item(future)}]\n', encoding='utf-8',
    )
    policy.reload_from(cfg)
    with pytest.raises(PolicyError, match='tree_deny'):
        policy.resolve(str(future / 'file.txt'), for_write=True)


def test_reload_and_policy_info_expose_tree_keys_atomically(tmp_path: Path) -> None:
    policy, root, denied, cfg = _policy(tmp_path)
    assert policy.describe()['tree_roots'] == [str(root)]
    assert policy.describe()['tree_deny'] == [str(denied)]
    assert policy.resolve(str(root / 'free' / 'file.py'), for_write=True)
    cfg.write_text(f'roots = [{_item(root)}]\n', encoding='utf-8')
    policy.reload_from(cfg)
    assert policy.describe()['tree_roots'] == []
    assert policy.describe()['tree_deny'] == []
    with pytest.raises(PolicyError):
        policy.resolve(str(root / 'free' / 'file.py'), for_write=True)
    cfg.write_text(f'roots = [{_item(root)}]\ntree_roots = [{_item(root)}]\ntree_deny = [{_item(tmp_path)}]\n', encoding='utf-8')
    with pytest.raises(PolicyError):
        policy.reload_from(cfg)
    assert policy.describe()['tree_roots'] == []


def test_legacy_grants_unchanged_without_tree_config(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir()
    writing = root / 'writing'
    running = root / 'running'
    writing.mkdir()
    running.mkdir()
    policy, root, _, _ = _policy(
        tmp_path, tree=False,
        extras=f'write_roots = [{_item(writing)}]\nexec_roots = [{_item(running)}]\n',
    )
    assert policy.resolve(str(writing / 'x.txt'), for_write=True)
    assert policy.exec_root_for(running / 'a.py') == running
    with pytest.raises(PolicyError):
        policy.resolve(str(root / 'new.txt'), for_write=True)
    assert policy.exec_root_for(root / 'a.py') is None


@pytest.mark.asyncio
async def test_real_mcp_tools_accept_tree_without_legacy_exec_roots(tmp_path: Path) -> None:
    """Server run не должен отсекать tree_roots пустым legacy exec_roots."""
    from fastmcp import Client
    from dev_mcp.audit import Audit
    from dev_mcp.server import build_server

    policy, root, denied, _ = _policy(tmp_path)
    assert policy.exec_roots == []
    server = build_server(policy, Audit(tmp_path / 'audit.jsonl'))
    project = root / 'new-project'

    async with Client(server) as client:
        async def call(name: str, **args: object) -> str:
            result = await client.call_tool(name, args)
            return result.content[0].text

        created = await call('write_file', path=str(project / 'file.txt'), content='good')
        assert not created.startswith('Отказано'), created
        assert (project / 'file.txt').read_text(encoding='utf-8') == 'good'

        forbidden_root = await call('write_file', path=str(root / 'file.txt'), content='bad')
        assert 'файлы корня правит владелец' in forbidden_root
        assert not (root / 'file.txt').exists()

        forbidden_subtree = await call('write_file', path=str(denied / 'file.txt'), content='bad')
        assert 'tree_deny' in forbidden_subtree
        assert not (denied / 'file.txt').exists()

        executed = await call('run', root=str(project), argv=['python', '-c', 'print(8675309)'])
        assert '8675309' in executed and 'код возврата: 0' in executed, executed

        denied_run = await call('run', root=str(denied), argv=['python', '-c', 'print(1)'])
        assert 'tree_deny' in denied_run
