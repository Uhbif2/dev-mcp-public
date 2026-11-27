"""Relocation is an exact operational grant, not new source authority."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from dev_mcp.fleet import goals, outcome, queue_intake, relocation
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store
from dev_mcp.intents import stage_check
from dev_mcp.intents.sources.model import revision_for
from dev_mcp.intents.sources.registry import IntentRegistry


@pytest.fixture
def moved(tmp_path):
    platform = tmp_path / 'platform'
    platform.mkdir()
    old_parent = tmp_path / 'former'
    old_parent.mkdir()
    old = old_parent / 'portfolio'
    destination = tmp_path / 'new-location' / 'portfolio'
    (destination / 'intent').mkdir(parents=True)
    name = 'portfolio-existing'
    origin = {'source_id': 'files', 'external_id': 'portfolio.md'}
    manifest = {'kind': 'intent-outcome-tree', 'name': name, 'component_id': 'unchanged-id', 'origin': origin}
    (destination / 'component.json').write_text(json.dumps(manifest), encoding='utf-8')
    understood = 'kind: change\ntarget: ' + str(old) + '\n'
    (destination / 'intent/understood.md').write_text(understood, encoding='utf-8')
    store = Store.open(str(platform / 'data/fleet/fleet.sqlite'))
    outcome.install_schema(store)
    queue_intake.install_schema(store)
    with sqlite3.connect(store.path) as conn:
        IntentRegistry(conn)
    authority = {'create_project': True, 'project_roots': [str(old_parent)]}
    raw = 'Original owner intent, unchanged.'
    revision = revision_for(raw, authority)
    spec = {**origin, 'target': str(old), 'revision': revision, 'kind': 'change'}
    ts = '2026-11-06T00:00:00+00:00'
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root,active) VALUES(?,?,1)', (name, str(old)), event='test')
        for tz, state in [('TZ-open', 'ready'), ('TZ-accepted', 'accepted')]:
            tx.change('INSERT INTO tasks(project,tz,state,owns,spec_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                      (name, tz, state, json.dumps([str(old / 'workloads')]), 'ToDo/tz/work.md', ts, ts), event='test')
        for identifier, parent, state in [('root', None, 'open'), ('root:past', 'root', 'done')]:
            tx.change('INSERT INTO outcomes(id,parent_id,project,title,intent_id,spec,evidence_spec,authority,state,rev,created_ts) VALUES(?,?,?,?,?,?,?,?,?,1,?)',
                      (identifier, parent, name, 'root' if parent is None else 'understood', 'i', json.dumps(spec), '{}', json.dumps(authority), state, ts), event='test')
        tx.change('INSERT INTO queue_sources VALUES(?,?,?,?,?)', (name, norm_path(str(old)), '{}', '[]', ts), event='test')
        tx.change('INSERT INTO intent_source_revisions VALUES(?,?,?,?,?,?,?)',
                  ('files', 'portfolio.md', revision, raw, json.dumps(authority), ts, 'active'), event='test')
    yield store, name, old, destination, spec, platform
    store.close()


def apply_plan(moved, tmp_path):
    store, name, old, new, spec, platform = moved
    plan = relocation.prepare(store, name, str(new))
    backup = tmp_path / 'preimage.json'
    backup.write_bytes(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    event = relocation.apply(store, plan, backup)
    return plan, event


def test_apply_rebases_only_current_bindings_preserves_source_and_accepted(moved, tmp_path, monkeypatch):
    store, name, old, new, original_spec, platform = moved
    plan, event = apply_plan(moved, tmp_path)
    with store.read() as tx:
        assert tx.one('SELECT root FROM projects WHERE name=?', (name,))['root'] == str(new)
        task = tx.one('SELECT owns,state FROM tasks WHERE project=? AND tz=?', (name, 'TZ-open'))
        assert json.loads(task['owns']) == [norm_path(str(new / 'workloads'))]
        assert task['state'] == 'ready'
        accepted = tx.one('SELECT owns FROM tasks WHERE project=? AND tz=?', (name, 'TZ-accepted'))
        assert json.loads(accepted['owns']) == [str(old / 'workloads')]
        historical = tx.one('SELECT spec FROM outcomes WHERE id=?', ('root:past',))
        assert json.loads(historical['spec']) == original_spec
        source = tx.one('SELECT * FROM intent_source_revisions')
        assert dict(source) == plan['preimage']['source_revision']
        current = json.loads(tx.one('SELECT spec FROM outcomes WHERE id=?', ('root',))['spec'])
        assert current['target'] == str(new)
        assert current['revision'] == original_spec['revision']
        assert relocation.authorized_target(tx, name, new, current) == new
        assert tx.one('SELECT kind FROM events WHERE id=?', (event,))['kind'] == relocation.EVENT
    monkeypatch.setattr(goals, 'DEFAULT_PATH', platform / 'fleet-projects.toml')
    assert goals._registered_intent_root(name) == new
    # The trusted override retains the old accepted route bytes and authorizes
    # only this existing root, without extending the former parent grants.
    assert stage_check.routing(new, allowed_roots=(old.parent,), relocated_target=new) == ('change', str(new))
    assert (new / 'intent/understood.md').read_text() == 'kind: change\ntarget: ' + str(old) + '\n'


def test_active_claim_refuses_plan_before_mutation(moved):
    store, name, old, new, spec, _ = moved
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W','now','now')", (), event='test')
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES(?,?,'W','active','now')", (name, 'TZ-open'), event='test')
    with pytest.raises(relocation.RelocationError, match='active project claim'):
        relocation.prepare(store, name, str(new))
    with store.read() as tx:
        assert tx.one('SELECT root FROM projects WHERE name=?', (name,))['root'] == str(old)


def test_identity_collision_refuses_plan(moved):
    store, name, old, new, spec, _ = moved
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root) VALUES(?,?)', ('other', str(new)), event='test')
    with pytest.raises(relocation.RelocationError, match='another project'):
        relocation.prepare(store, name, str(new))


def test_wrong_component_origin_refuses_plan(moved):
    store, name, old, new, spec, _ = moved
    path = new / 'component.json'
    data = json.loads(path.read_text())
    data['origin']['external_id'] = 'another-owner-intent.md'
    path.write_text(json.dumps(data))
    with pytest.raises(relocation.RelocationError, match='origin/identity'):
        relocation.prepare(store, name, str(new))


def test_symlink_destination_is_rejected_even_if_identity_copied(moved, monkeypatch):
    store, name, old, new, spec, _ = moved
    real = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path == new or real(path))
    with pytest.raises(relocation.RelocationError, match='noncanonical'):
        relocation.prepare(store, name, str(new))


def test_receipt_does_not_authorize_sibling_root(moved, tmp_path):
    store, name, old, new, spec, _ = moved
    apply_plan(moved, tmp_path)
    sibling = new.parent / 'sibling'
    sibling.mkdir()
    with store.read() as tx:
        with pytest.raises(relocation.RelocationError, match='not a grant'):
            relocation.authorized_target(tx, name, sibling, spec)
    with pytest.raises(ValueError, match='exact project root'):
        stage_check.routing(new, relocated_target=sibling)


def test_receipt_is_bound_to_source_revision_and_manifest(moved, tmp_path):
    store, name, old, new, spec, _ = moved
    apply_plan(moved, tmp_path)
    with store.read() as tx:
        with pytest.raises(relocation.RelocationError, match='source revision'):
            relocation.authorized_target(tx, name, new, {**spec, 'revision': 'unrelated'})
    manifest = new / 'component.json'
    manifest.write_text(manifest.read_text() + '\n')
    with store.read() as tx:
        with pytest.raises(relocation.RelocationError, match='identity changed'):
            relocation.authorized_target(tx, name, new, spec)


def test_changed_state_fails_cas_without_partial_root_update(moved, tmp_path):
    store, name, old, new, spec, _ = moved
    plan = relocation.prepare(store, name, str(new))
    backup = tmp_path / 'plan.json'
    backup.write_bytes(json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    with store.tx() as tx:
        tx.change("UPDATE tasks SET title='new concurrent work' WHERE project=? AND tz=?", (name, 'TZ-open'), event='test')
    with pytest.raises(relocation.RelocationError, match='plan changed'):
        relocation.apply(store, plan, backup)
    with store.read() as tx:
        assert tx.one('SELECT root FROM projects WHERE name=?', (name,))['root'] == str(old)
        assert not tx.query('SELECT id FROM events WHERE kind=?', (relocation.EVENT,))
