"""Ротация журнала вызовов по суткам и чтение истории через архивы."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dev_mcp.audit import Audit, archives, iter_entries


def _line(ts: str, tool: str) -> str:
    return json.dumps({'ts': ts, 'tool': tool}) + '\n'


def test_rotates_previous_day_and_reads_history(tmp_path: Path) -> None:
    path = tmp_path / 'audit.jsonl'
    path.write_text(_line('2026-09-20T10:00:00+00:00', 'old') + _line('2026-09-21T10:00:00+00:00', 'mid'), encoding='utf-8')

    Audit(path).record('read_file', {}, decision='allow')

    arch = archives(path)
    assert [a.name for a in arch] == ['audit-2026-09-20.jsonl']
    assert len(path.read_text(encoding='utf-8').splitlines()) == 1
    tools = [e['tool'] for e in iter_entries(path)]
    assert tools == ['old', 'mid', 'read_file']
    assert [e['tool'] for e in iter_entries(path, since='2026-09-21')] == ['mid', 'read_file']


def test_same_day_file_is_not_rotated(tmp_path: Path) -> None:
    path = tmp_path / 'audit.jsonl'
    audit = Audit(path)
    audit.record('a', {}, decision='allow')
    audit.record('b', {}, decision='allow')
    assert archives(path) == []
    assert len(path.read_text(encoding='utf-8').splitlines()) == 2


def test_iter_entries_skips_archives_before_since(tmp_path: Path) -> None:
    path = tmp_path / 'audit.jsonl'
    (tmp_path / 'audit-2026-09-01.jsonl').write_text(_line('2026-09-01T00:00:00+00:00', 'x'), encoding='utf-8')
    (tmp_path / 'audit-2026-09-10.jsonl').write_text(_line('2026-09-10T00:00:00+00:00', 'y'), encoding='utf-8')
    path.write_text(_line('2026-09-11T00:00:00+00:00', 'z'), encoding='utf-8')
    assert [e['tool'] for e in iter_entries(path, since='2026-09-10T12')] == ['y', 'z'] or \
        [e['tool'] for e in iter_entries(path, since='2026-09-10T12')] == ['z']
    assert [e['tool'] for e in iter_entries(path, since='2026-09-05')] == ['y', 'z']


def _recorded_args(tmp_path: Path, args: dict) -> dict:
    path = tmp_path / 'audit.jsonl'
    Audit(path).record('write_file', args, decision='allow')
    return json.loads(path.read_text(encoding='utf-8').splitlines()[-1])['args']


@pytest.mark.parametrize('name', [
    'content', 'old_string', 'new_string', 'patch', 'text', 'body', 'data',
    'detail', 'summary', 'payload', 'script', 'stdin', 'file_content',
    'patch_text', 'CONTENT',
])
def test_content_arguments_keep_only_length_and_sha256_prefix(tmp_path: Path, name: str) -> None:
    source = 'private source content: пароль, text\n'
    args = _recorded_args(tmp_path, {'path': 'src/main.py', name: source})
    assert args['path'] == 'src/main.py'
    assert args[name] == {'length': len(source), 'sha256': hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]}
    assert source not in json.dumps(args, ensure_ascii=False)


@pytest.mark.parametrize('name', ['api_key', 'accessToken', 'secret_hint', 'PASSWORD', 'monkey'])
def test_secret_named_arguments_always_redacted(tmp_path: Path, name: str) -> None:
    args = _recorded_args(tmp_path, {name: 'ordinary secret with no recognizable prefix'})
    assert args[name] == '[REDACTED]'


@pytest.mark.parametrize('secret', [
    'Bearer a_b.c-def', 'sk-1234567890123456', 'ghp_AbCdEf12345678',
    'github_pat_AbCdEf12345678', 'xoxb-1234567890123',
    'AKIAABCDEFGHIJKLMNOP',
])
def test_secret_patterns_redact_anywhere_in_arguments(tmp_path: Path, secret: str) -> None:
    args = _recorded_args(tmp_path, {'path': 'src/main.py', 'argv': ['safe', f'--header={secret}']})
    assert args['argv'] == ['safe', '[REDACTED]']
    assert secret not in json.dumps(args)


def test_nested_values_and_cli_secret_flags_are_redacted(tmp_path: Path) -> None:
    args = _recorded_args(tmp_path, {
        'path': 'src/main.py', 'limit': 25, 'argv': ['--token', 'not-a-recognizable-token', '--dry-run'],
        'nested': [{'new_string': 'source code', 'access_token': 'plain secret'}, 'visible'],
        'description': 'short safe string',
    })
    assert args['path'] == 'src/main.py'
    assert args['limit'] == 25
    assert args['description'] == 'short safe string'
    assert args['argv'] == ['--token', '[REDACTED]', '--dry-run']
    assert args['nested'] == [
        {'new_string': {'length': len('source code'), 'sha256': hashlib.sha256(b'source code').hexdigest()[:12]},
         'access_token': '[REDACTED]'}, 'visible',
    ]


def test_existing_string_truncation_still_applies(tmp_path: Path) -> None:
    args = _recorded_args(tmp_path, {'path': 'x' * 310})
    assert args['path'] == 'x' * 300 + '…(+10)'


def test_secret_assignment_and_short_prefix_are_hidden(tmp_path: Path) -> None:
    args = _recorded_args(tmp_path, {
        'argv': ['--password=undetectable', 'https://example.org/?access_token=opaque', 'sk-abc'],
    })
    assert args['argv'] == ['[REDACTED]', '[REDACTED]', '[REDACTED]']


def test_content_mapping_fingerprint_does_not_expose_nested_values(tmp_path: Path) -> None:
    data = {'password': 'ordinary', 'nested': {'text': 'source'}}
    args = _recorded_args(tmp_path, {'data': data})
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True)
    assert args['data'] == {
        'length': len(serialized), 'sha256': hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:12],
    }
    assert 'ordinary' not in json.dumps(args)


def test_reason_does_not_leak_secret_or_source_snippet(tmp_path: Path) -> None:
    path = tmp_path / 'audit.jsonl'
    audit = Audit(path)
    audit.record('edit_file', {}, decision='deny', reason=(
        'old_string в файле не найден\nБлиже всего — строка 3 (сходство 80%); сверь текст:\n3\tprivate line'
    ))
    audit.record('read_file', {}, decision='deny', reason='Authorization: Bearer somecredential')
    first, second = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert first['reason'] == 'old_string в файле не найден\n[content redacted]'
    assert second['reason'] == '[REDACTED]'


def test_secret_in_agent_identity_is_not_logged(tmp_path: Path) -> None:
    path = tmp_path / 'audit.jsonl'
    Audit(path).record('read_file', {}, decision='deny', agent='Bearer an-embedded-secret')
    entry = json.loads(path.read_text(encoding='utf-8'))
    assert entry['agent'] == '[REDACTED]'
