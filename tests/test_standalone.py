from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture
def standalone():
    spec = importlib.util.spec_from_file_location('standalone', Path(__file__).resolve().parents[1] / 'tools/standalone.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_registers_narrow_workspace_and_never_overwrites(standalone, tmp_path):
    from dev_mcp.policy import Policy
    import sqlite3

    workspace, root = tmp_path / 'space with spaces', tmp_path / 'install'
    workspace.mkdir()
    root.mkdir()
    standalone.init(root, workspace, 'Demo')
    policy = Policy.load(str(root / 'policy.toml'))
    assert policy.resolve(str(workspace / 'created.txt'), for_write=True)
    with pytest.raises(Exception):
        policy.resolve(str(tmp_path / 'outside.txt'), for_write=True)
    with sqlite3.connect(root / 'data/fleet/fleet.sqlite') as conn:
        assert conn.execute('SELECT name,root FROM projects').fetchall() == [('Demo', str(workspace))]
    original = (root / 'policy.toml').read_bytes()
    with pytest.raises(ValueError, match='never overwrites'):
        standalone.init(root, workspace, 'Other')
    assert (root / 'policy.toml').read_bytes() == original


def test_autostart_install_remove_preserves_other_entries_and_data(standalone, tmp_path):
    manifest = tmp_path / 'autostart.json'
    original = {'items': [{'name': 'unrelated', 'enabled': True}], 'version': 1}
    manifest.write_text(json.dumps(original), encoding='utf-8')
    root = tmp_path / 'install'
    root.mkdir()
    proof = standalone.autostart(root, manifest, 18766)
    observed = json.loads(manifest.read_text(encoding='utf-8'))
    assert observed['items'][0] == original['items'][0]
    assert observed['items'][1]['start']['hidden'] is True
    assert json.loads(Path(proof['backup']).read_text()) == original
    standalone.autostart(root, manifest, 18766, remove=True)
    assert json.loads(manifest.read_text(encoding='utf-8')) == original
    assert Path(proof['backup']).exists()


def test_pid_reuse_cannot_authorize_stopping_another_process(standalone, tmp_path):
    import psutil

    (tmp_path / 'data').mkdir()
    record = {'pid': __import__('os').getpid(), 'created': psutil.Process().create_time() - 30, 'port': 1}
    (tmp_path / 'data/standalone-process.json').write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match='identity changed'):
        standalone.identity(tmp_path)
