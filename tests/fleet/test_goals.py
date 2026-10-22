"""Цели проектов (fleet-projects.toml) в памятке к задаче."""
from __future__ import annotations

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from dev_mcp.fleet import goals  # noqa: E402


def test_goal_block_from_file(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / 'p.toml'
    cfg.write_text('[projects."Demo"]\ngoal = """\nСделать X\nдля Y.\n"""\nsources = ["GOAL.md"]\n', encoding='utf-8')
    monkeypatch.setenv('DEV_MCP_FLEET_PROJECTS', str(cfg))
    text = goals.goal_block('Demo')
    assert 'Цель проекта Demo: Сделать X для Y.' in text
    assert 'GOAL.md' in text
    assert goals.goal_block('Other') == ''


def test_missing_or_broken_file_is_silent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv('DEV_MCP_FLEET_PROJECTS', str(tmp_path / 'nope.toml'))
    assert goals.goal_block('Demo') == ''
    bad = tmp_path / 'bad.toml'
    bad.write_text('[projects\n', encoding='utf-8')
    monkeypatch.setenv('DEV_MCP_FLEET_PROJECTS', str(bad))
    assert goals.goal_block('Demo') == ''


@pytest.mark.production
def test_repo_file_has_fleet_projects() -> None:
    import os
    os.environ.pop('DEV_MCP_FLEET_PROJECTS', None)
    for name in ('Beta App', 'Nova VPN', 'EchoRedactor'):
        assert goals.goal_block(name).startswith(f'Цель проекта {name}:')


def test_retarget_commit_defaults_use_authorized_registered_root(tmp_path, monkeypatch):
    import json
    from dev_mcp.fleet import outcome
    from dev_mcp.fleet.store import Store
    monkeypatch.setattr(goals, 'DEFAULT_PATH', tmp_path/'fleet-projects.toml')
    root = tmp_path/'canonical'
    root.mkdir()
    (root/'.git').mkdir()
    origin = {'source_id':'files', 'external_id':'change.md'}
    (root/'component.json').write_text(json.dumps({'kind':'intent-outcome-tree','origin':origin}))
    store = Store.open(str(tmp_path/'data/fleet/fleet.sqlite'))
    outcome.install_schema(store)
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root,priority,active) VALUES(?,?,0,1)',
                  ('canonical-intent-hash',str(root)),event='test')
    outcome.create_outcome(store, project='canonical-intent-hash', title='root',intent_id='i',
                           spec=origin, evidence_spec={'kind':'run'},
                           authority={'project_roots':[str(tmp_path)]},outcome_id='root')
    assert goals._intent_project_defaults('canonical-intent-hash') == {'commit':True}
    (root/'component.json').write_text(json.dumps({'kind':'intent-outcome-tree','origin':{}}))
    assert goals._intent_project_defaults('canonical-intent-hash') == {}
    store.close()
