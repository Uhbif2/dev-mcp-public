from pathlib import Path

import pytest

from dev_mcp.fleet.project_identity import ProjectIdentityError, projects_at
from dev_mcp.fleet.spec import sync_selected
from dev_mcp.fleet.store import Store


def test_same_canonical_root_cannot_get_a_second_active_queue(tmp_path):
    root = tmp_path/'product'
    directory = root/'ToDo'/'tz'
    directory.mkdir(parents=True)
    (directory/'TZ-1-work.md').write_text('```yaml\ntz: TZ-1\nowns: [src/a.py]\nacceptance: []\n```\n# Work', encoding='utf-8')
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    try:
        with store.tx() as tx:
            tx.change('INSERT INTO projects(name,root) VALUES(?,?)', ('Canonical', str(root)), event='seed')
        with pytest.raises(ProjectIdentityError, match='already_registered: Canonical'):
            sync_selected(store, 'Accidental copy', str(root/'..'/'product'), ['TZ-1'])
        with store.read() as tx:
            assert tx.scalar('SELECT count(*) FROM projects') == 1
            assert not tx.one('SELECT 1 FROM tasks')
            assert projects_at(tx, str(root))[0]['name'] == 'Canonical'
    finally:
        store.close()


def test_disabled_project_is_history_not_implicit_reactivation(tmp_path):
    root = tmp_path/'product'
    root.mkdir()
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    try:
        with store.tx() as tx:
            tx.change('INSERT INTO projects(name,root,active) VALUES(?,?,0)', ('Retired', str(root)), event='seed')
        with pytest.raises(ProjectIdentityError, match='explicit_owner_reactivation'):
            sync_selected(store, 'Retired', str(root), ['TZ-1'])
        with store.read() as tx:
            assert tx.one('SELECT active FROM projects')['active'] == 0
    finally:
        store.close()
