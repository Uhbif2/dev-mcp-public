"""Cross-model outbox must audit immutable isolated submission revisions."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dev_mcp.fleet import cross_review as cr, queue
from dev_mcp.fleet.claim_review_bite import capture_submission
from test_claim_review_bite import take, git

pytest_plugins = ('test_claim_review_bite',)


def _isolated_submitted(repo):
    root, store = repo
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET owns=?,acceptance='[]' WHERE project='Unit' AND tz='TZ-A'",
            (json.dumps([str(root / 'src')]),), event={"kind": "seed"},
        )
    claim_id, ctx = take(store, 'W-A', 'TZ-A')
    (ctx.tree_path / 'src' / 'state.txt').write_text('good from isolated claim\n', encoding='utf-8')
    (ctx.tree_path / 'src' / 'new-report.md').write_text('sealed report\n', encoding='utf-8')
    seal = capture_submission(store, claim_id, 'W-A')
    assert seal is not None
    queue.submit(store, 'W-A', 'Independent outcome for isolated submission')
    with store.read() as tx:
        record = tx.one("SELECT id,payload FROM events WHERE kind='cross_review_requested' "
                        "AND project='Unit' AND tz='TZ-A' ORDER BY id DESC LIMIT 1")
    assert record is not None
    return root, store, claim_id, seal, json.loads(record['payload'])


def test_isolated_submission_uses_pinned_revision_and_yields_independent_verdict(world):
    root, store, claim_id, seal, outbox = _isolated_submitted(world)
    assert not (root / 'src' / 'new-report.md').exists()
    assert outbox['tree_sha'] != seal.tree_sha  # old outbox hashes trunk; snapshot must not
    snap = cr._submitted_snapshot(store, 'Unit', 'TZ-A', outbox)
    assert snap.tree_sha == seal.tree_sha
    assert 'new-report.md' in snap.diff and 'sealed report' in snap.diff
    assert 'good from isolated claim' in snap.diff
    seen = []

    def ask(prompt, *, schema):
        seen.append(prompt)
        return {'verdict': 'clear', 'findings': []}

    result = cr.process_pending(store, ask=ask)
    assert len(result) == 1 and result[0]['state'] == 'clear'
    assert result[0]['tree_sha'] == seal.tree_sha
    assert 'sealed report' in seen[0]
    assert cr.process_pending(store, ask=ask) == []


def test_broken_seal_records_diagnostic_and_sixth_same_failure_is_terminal(world):
    root, store, _, seal, _ = _isolated_submitted(world)
    git(root, 'update-ref', '-d', f'refs/fleet/submissions/{seal.submission_id}')
    start = datetime.now(timezone.utc)
    for attempt in range(1, 7):
        result = cr.process_pending(
            store, ask=lambda *_a, **_k: pytest.fail('invalid seal must never call Gemini'),
            now=start + timedelta(seconds=cr.RETRY_SECONDS * (attempt - 1)),
        )
        assert len(result) == 1
        item = result[0]
        assert item['same_cause_count'] == attempt
        assert item['reason'] == 'unavailable_submission_snapshot'
        assert 'ValueError:' in item['detail'] and 'sealed Git object unavailable' in item['detail']
        assert item['state'] == ('unavailable' if attempt == 6 else 'wait')
        if attempt < 6:
            assert cr.process_pending(store, now=start + timedelta(
                seconds=cr.RETRY_SECONDS * (attempt - 1) + 1)) == []
    assert cr.process_pending(store, now=start + timedelta(hours=1)) == []


def test_exception_detail_scrubs_local_paths_and_credentials(world, monkeypatch):
    _, store, _, _, _ = _isolated_submitted(world)

    def broken(*_args, **_kwargs):
        raise ValueError('C:\\users\\owner\\private.txt token=topsecret')

    monkeypatch.setattr(cr, '_submitted_snapshot', broken)
    item = cr.process_pending(store)[0]
    assert item['state'] == 'wait'
    assert 'ValueError:' in item['detail']
    assert 'owner' not in item['detail'].lower()
    assert 'topsecret' not in item['detail']
    assert '/opt/private' not in cr._safe_detail(ValueError('/opt/private/config.yaml'))
