"""Host-specific locations are explicit; an unconfigured checkout has no live guards."""
import importlib.util
from pathlib import Path


def load(monkeypatch, path):
    monkeypatch.setenv('DEV_MCP_MACHINE', str(path))
    spec = importlib.util.spec_from_file_location('isolated_machine', Path(__file__).parents[1] / 'src/dev_mcp/machine.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fresh_install_uses_local_workspace_and_disables_live_effects(monkeypatch, tmp_path):
    module = load(monkeypatch, tmp_path / 'absent.toml')
    assert module.HUB == module.HOME.parent
    assert module.WORKSPACES == (module.HUB,)
    assert module.GITHUB_OWNER == ''
    assert module.GATEWAY_HEALTH_URL == ''
    assert module.LIVE_HOME is None
    assert not module.is_live(module.HOME)


def test_explicit_installation_locations_are_loaded(monkeypatch, tmp_path):
    import json
    config = tmp_path / 'machine.toml'
    home = Path(__file__).parents[1].resolve()
    config.write_text(f'live_home = {json.dumps(str(home))}\n'
                      f'hub = {json.dumps(str(tmp_path))}\n'
                      f'workspaces = [{json.dumps(str(tmp_path / "work"))}]\n'
                      'github_owner = "example-owner"\n', encoding='utf-8')
    module = load(monkeypatch, config)
    assert module.is_live(home)
    assert not module.is_live(tmp_path)
    assert module.WORKSPACES == (tmp_path / 'work',)
    assert module.OWNER_INBOX == tmp_path / '_todo' / 'owner-inbox.md'
    assert module.GITHUB_OWNER == 'example-owner'


def test_broken_config_does_not_enable_live_guards(monkeypatch, tmp_path):
    config = tmp_path / 'machine.toml'
    config.write_text('live_home = [broken TOML', encoding='utf-8')
    module = load(monkeypatch, config)
    assert module.LIVE_HOME is None
    assert module.GITHUB_OWNER == ''


def test_stage_instructions_use_configured_locations(monkeypatch, tmp_path):
    from dev_mcp.intents import stages
    hub = tmp_path / 'New Hub'
    workspace = tmp_path / 'New Workspace'
    monkeypatch.setattr(stages.machine, 'HUB', hub)
    monkeypatch.setattr(stages.machine, 'STACK_JSON', hub / 'stack.json')
    monkeypatch.setattr(stages.machine, 'WORKSPACES', (workspace,))
    body = stages.task_body('demo', 'understood', 'evidence')
    assert (hub / 'MAP.md').as_posix() in body
    assert (hub / 'stack.json').as_posix() in body
    assert workspace.as_posix() in body
    assert '{workspaces}' not in body
    assert '14 ГБ' not in body
