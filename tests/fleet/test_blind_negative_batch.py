"""Negative-only mining results still require independent, fresh blind evidence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_mcp.fleet.briefing import _blind_is_sealed
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.queue import blind_read_denial, take_next
from test_queue import now
from test_tools import _make_server, _tool_fn


BATCH = 'batch-abc123def456'
TZ = 'TZ-MR-' + BATCH


def _negative_batch(tmp_path):
    server, store, root, _ = _make_server(tmp_path)
    cases = root / 'data' / 'batches' / 'cases' / BATCH
    cases.mkdir(parents=True)
    ids = ['case-01', 'case-02']
    for cid in ids:
        (cases / (cid + '.md')).write_text('Synthetic independent Case', encoding='utf-8')
    manifest = root / 'data' / 'batches' / 'out' / BATCH / 'MANIFEST.md'
    manifest.parent.mkdir(parents=True)
    payload = {'batch_id': BATCH, 'case_ids': ids,
               'entries': {ids[0]: {'outcome': 'skip'}, ids[1]: {'outcome': 'need_more'}}}
    _manifest(manifest, payload)
    zone = root / 'data' / 'reviews' / BATCH
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)',
                   ('Mill Works', str(root)), event={'kind': 'seed'})
        tx.change('INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,?,?,?)',
                   ('worker', 'worker', 'chatgpt', now(), now()), event={'kind': 'seed'})
        tx.change("INSERT INTO tasks(project,tz,state,kind,owns,created_at,updated_at) "
                   "VALUES(?,?,'ready','work',?,?,?)",
                   ('Mill Works', TZ, json.dumps([norm_path(str(zone))]), now(), now()), event={'kind': 'seed'})
    task = take_next(store, 'worker')
    assert task is not None
    return server, store, root, manifest, payload, task


def _manifest(path, payload):
    path.write_text('<!-- MILL_MANIFEST_JSON\n' + json.dumps(payload)
                    + '\nMILL_MANIFEST_JSON -->\n', encoding='utf-8')


def _blind(store, root, cid, *, record=True):
    path = root / 'data' / 'reviews' / BATCH / cid / 'blind.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'case_id': cid, 'independent_finding': 'Bounded synthetic finding',
        'alternative': 'Insufficient evidence is possible',
        'hidden_queries': [{'query': 'first facet', 'negative': False},
                           {'query': 'second facet', 'negative': False},
                           {'query': 'false transfer', 'negative': True}],
    }), encoding='utf-8')
    if record:
        with store.tx() as tx:
            tx.record_event(kind='write', worker_id='worker', project='Mill Works',
                            tz=TZ, path_norm=norm_path(str(path)), payload={})
    return path


@pytest.mark.parametrize('first,second', [('skip', 'need_more'), ('support', 'contradict')])
def test_negative_only_seal_needs_every_fresh_case_and_binds_bytes(tmp_path, first, second):
    server, store, root, manifest, payload, task = _negative_batch(tmp_path)
    payload['entries']['case-01']['outcome'] = first
    payload['entries']['case-02']['outcome'] = second
    _manifest(manifest, payload)
    seal = _tool_fn(server, 'fleet_blind_seal')
    assert 'FLEET_BLIND_SEAL_REQUIRED' in blind_read_denial(store, 'worker', str(manifest))
    _blind(store, root, 'case-01')
    assert 'blind_seal:' not in seal(agent='worker')
    second_path = _blind(store, root, 'case-02', record=False)
    assert 'blind_seal:' not in seal(agent='worker')  # existing bytes are not fresh proof
    _blind(store, root, 'case-02')
    assert 'blind_seal:' in seal(agent='worker')
    assert _blind_is_sealed(store, 'worker', task, BATCH)
    assert blind_read_denial(store, 'worker', str(manifest)) is None
    assert 'MILL_MANIFEST_JSON' in _tool_fn(server, 'read_file')(path=str(manifest), agent='worker')
    second_path.write_text(second_path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    assert not _blind_is_sealed(store, 'worker', task, BATCH)
    assert 'blind_seal:' not in seal(agent='worker')  # disclosure cannot be undone by resealing


@pytest.mark.parametrize('bad', ['unknown', 'entry_type', 'case_id', 'missing_case',
                                  'extra_case', 'duplicate_ids', 'null_ids', 'wrong_batch', 'duplicate_key'])
def test_negative_manifest_cannot_hide_or_invent_cases(tmp_path, bad):
    server, store, root, manifest, payload, _ = _negative_batch(tmp_path)
    for cid in ('case-01', 'case-02'):
        _blind(store, root, cid)
    if bad == 'unknown':
        payload['entries']['case-02']['outcome'] = 'not_a_known_outcome'
    elif bad == 'entry_type':
        payload['entries']['case-02'] = []
    elif bad == 'case_id':
        payload['entries']['../outside'] = payload['entries'].pop('case-02')
        payload['case_ids'][1] = '../outside'
    elif bad == 'missing_case':
        payload['entries'].pop('case-02')
        payload['case_ids'].pop()
    elif bad == 'extra_case':
        payload['entries']['case-03'] = {'outcome': 'skip'}
        payload['case_ids'].append('case-03')
    elif bad == 'duplicate_ids':
        payload['case_ids'] = ['case-01', 'case-01']
    elif bad == 'null_ids':
        payload['case_ids'] = None
    elif bad == 'wrong_batch':
        payload['batch_id'] = 'batch-other'
    _manifest(manifest, payload)
    if bad == 'duplicate_key':
        raw = manifest.read_text(encoding='utf-8')
        manifest.write_text(raw.replace('"outcome": "skip"',
                                       '"outcome": "candidate", "outcome": "skip"'), encoding='utf-8')
    assert 'blind_seal:' not in _tool_fn(server, 'fleet_blind_seal')(agent='worker')
    assert blind_read_denial(store, 'worker', str(manifest)) is not None


def test_negative_seal_inventory_change_revokes_public_read(tmp_path):
    server, store, root, manifest, _, task = _negative_batch(tmp_path)
    for cid in ('case-01', 'case-02'):
        _blind(store, root, cid)
    assert 'blind_seal:' in _tool_fn(server, 'fleet_blind_seal')(agent='worker')
    (root / 'data' / 'batches' / 'cases' / BATCH / 'case-omitted.md').write_text(
        'Another synthetic original Case', encoding='utf-8')
    assert not _blind_is_sealed(store, 'worker', task, BATCH)
    assert 'FLEET_BLIND_SEAL_REQUIRED' in _tool_fn(server, 'read_file')(
        path=str(manifest), agent='worker')


def test_negative_seal_cannot_outlive_active_claim(tmp_path, monkeypatch):
    server, store, root, _, _, _ = _negative_batch(tmp_path)
    for cid in ('case-01', 'case-02'):
        _blind(store, root, cid)
    from dev_mcp.fleet import blind_manifest
    select = blind_manifest.blind_case_ids

    def releasing_selector(*args):
        result = select(*args)
        with store.tx() as tx:
            tx.change("UPDATE claims SET state='released' WHERE worker_id='worker'",
                      (), event={'kind': 'synthetic_race'})
        return result

    monkeypatch.setattr(blind_manifest, 'blind_case_ids', releasing_selector)
    assert 'blind_seal:' not in _tool_fn(server, 'fleet_blind_seal')(agent='worker')
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='blind_seal'") == 0


def test_candidate_does_not_hide_an_unknown_other_outcome(tmp_path):
    server, store, root, manifest, payload, _ = _negative_batch(tmp_path)
    payload['entries']['case-01']['outcome'] = 'candidate'
    payload['entries']['case-02']['outcome'] = 'unrecognized'
    _manifest(manifest, payload)
    _blind(store, root, 'case-01')
    assert 'blind_seal:' not in _tool_fn(server, 'fleet_blind_seal')(agent='worker')


def test_negative_submission_still_needs_same_claim_seal_and_current_bytes(tmp_path):
    from dev_mcp.fleet.review import _blind_submission_sealed

    server, store, root, _, _, _ = _negative_batch(tmp_path)
    for cid in ('case-01', 'case-02'):
        path = _blind(store, root, cid)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET author_worker='worker' WHERE project='Mill Works' AND tz=?",
                  (TZ,), event={'kind': 'seed_author'})
        row = tx.one("SELECT * FROM tasks WHERE project='Mill Works' AND tz=?", (TZ,))
        assert not _blind_submission_sealed(tx, row)
    assert 'blind_seal:' in _tool_fn(server, 'fleet_blind_seal')(agent='worker')
    with store.tx() as tx:
        tx.change("UPDATE claims SET state='released' WHERE worker_id='worker'",
                  (), event={'kind': 'synthetic_submit_release'})
        row = tx.one("SELECT * FROM tasks WHERE project='Mill Works' AND tz=?", (TZ,))
        assert _blind_submission_sealed(tx, row)
        path.write_bytes(path.read_bytes() + b' ')
        assert not _blind_submission_sealed(tx, row)
