"""A retired project must not take down the MCP transport or reopen access."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

from dev_mcp.policy import Policy, PolicyError


def config(tmp_path, scope='write_roots', *, global_write=False, missing_outside=False):
    root = tmp_path / 'workspace'
    root.mkdir()
    live = root / 'live'
    live.mkdir()
    missing = (tmp_path if missing_outside else root) / 'offline'
    cfg = tmp_path / 'policy.toml'
    extra_root = f", '{missing}'" if scope == 'roots' else ''
    scope_text = f"{scope} = ['{missing}']" if scope != 'roots' else ''
    cfg.write_text(
        f"roots = ['{root}'{extra_root}]\n{scope_text}\n"
        f"allow_write = {'true' if global_write else 'false'}\n"
        f"[tasks.offline]\ncwd = '{missing}'\nargv = ['python']\n",
        encoding='utf-8',
    )
    return cfg, root, live, missing


@pytest.mark.parametrize('scope', ['roots', 'write_roots', 'exec_roots', 'tree_roots'])
def test_missing_scope_is_quarantined_until_reload(tmp_path, scope):
    cfg, root, live, missing = config(tmp_path, scope, global_write=True)
    with pytest.raises(PolicyError, match='не существует'):
        Policy.load(cfg)
    policy = Policy.load(cfg, allow_unavailable_roots=True)
    assert policy.unavailable_roots
    assert policy.resolve(str(live / 'new.txt'), for_write=True)
    assert not policy.describe()['tasks']['offline']['runnable']
    assert str(missing) not in policy.describe()[scope]

    # A returning drive/directory must not reactivate a quarantined grant.
    missing.mkdir()
    (missing / 'hidden.txt').write_text('private', encoding='utf-8')
    for write in (False, True):
        with pytest.raises(PolicyError, match='недоступен'):
            policy.resolve(str(missing / 'hidden.txt'), for_write=write)
    assert policy.exec_root_for(missing) is None
    assert not any(item.name == 'hidden.txt' for item in policy.scan(root))
    policy.reload_from(cfg)
    assert not policy.unavailable_roots
    assert policy.resolve(str(missing / 'hidden.txt'), for_write=False)


def test_runtime_still_rejects_existing_outside_grant(tmp_path):
    cfg, _, _, missing = config(tmp_path, missing_outside=True)
    policy = Policy.load(cfg, allow_unavailable_roots=True)
    missing.mkdir()
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        policy.reload_from(cfg)
    # Failed reload preserves the quarantine even with global/parent grants.
    with pytest.raises(PolicyError, match='недоступен'):
        policy.resolve(str(missing), for_write=False)


def test_runtime_reload_handles_disappearance_and_invalid_toml(tmp_path):
    cfg, _, live, missing = config(tmp_path, 'exec_roots')
    missing.mkdir()
    policy = Policy.load(cfg, allow_unavailable_roots=True)
    missing.rmdir()
    policy.reload_from(cfg)
    assert policy.unavailable_roots
    assert policy.resolve(str(live), for_write=False)
    cfg.write_text('invalid [[[', encoding='utf-8')
    with pytest.raises(PolicyError, match='TOML'):
        policy.reload_from(cfg)
    assert policy.unavailable_roots


def test_runtime_rejects_file_as_tree_root(tmp_path):
    cfg, _, _, missing = config(tmp_path, 'tree_roots')
    missing.write_text('not a directory', encoding='utf-8')
    with pytest.raises(PolicyError, match='должен быть каталогом'):
        Policy.load(cfg, allow_unavailable_roots=True)


def test_dangling_link_quarantines_alias_and_target(tmp_path):
    cfg, _, live, missing = config(tmp_path)
    target = tmp_path / 'absent-target'
    try:
        missing.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    policy = Policy.load(cfg, allow_unavailable_roots=True)
    assert policy.unavailable_roots
    target.mkdir()
    for path in (missing, target):
        with pytest.raises(PolicyError, match='недоступен'):
            policy.resolve(str(path), for_write=False)
    # Retargeting the alias inside the read scope still cannot bypass quarantine.
    missing.unlink()
    missing.symlink_to(live, target_is_directory=True)
    with pytest.raises(PolicyError, match='недоступен'):
        policy.resolve(str(missing), for_write=False)


@pytest.mark.asyncio
async def test_real_entrypoint_keeps_mcp_available_with_missing_project(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    cfg, _, live, missing = config(tmp_path)
    (live / 'proof.txt').write_text('serving-proof', encoding='utf-8')
    repo = Path(__file__).resolve().parents[1]
    check = subprocess.run(
        [sys.executable, '-m', 'dev_mcp', '--policy', str(cfg), '--check'],
        cwd=repo, capture_output=True, timeout=30,
        # cp1252 console: Cyrillic diagnostics once crashed here (public CI, 27.11).
        env={**os.environ, 'PYTHONPATH': str(repo / 'src'),
             'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'},
    )
    assert check.returncode == 1  # Operator validation still catches retired paths.
    assert b'write_root' in check.stdout

    transport = StdioTransport(
        command=sys.executable,
        args=['-m', 'dev_mcp', '--policy', str(cfg), '--core-only',
              '--audit', str(tmp_path / 'logs' / 'audit.jsonl')],
        cwd=str(repo), env={'PYTHONPATH': str(repo / 'src')},
    )
    async with Client(transport, timeout=30) as client:
        info = await client.call_tool('policy_info', {})
        assert 'Недоступные каталоги' in info.content[0].text
        assert str(missing) in info.content[0].text
        proof = await client.call_tool('read_file', {'path': str(live / 'proof.txt')})
        assert 'serving-proof' in proof.content[0].text
        missing.mkdir()
        (missing / 'hidden.txt').write_text('must-stay-hidden', encoding='utf-8')
        denied = await client.call_tool('read_file', {'path': str(missing / 'hidden.txt')})
        assert 'отказ' in denied.content[0].text.casefold()
        assert 'must-stay-hidden' not in denied.content[0].text
        await client.call_tool('policy_reload', {})
        restored = await client.call_tool('read_file', {'path': str(missing / 'hidden.txt')})
        assert 'must-stay-hidden' in restored.content[0].text
