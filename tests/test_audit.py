"""Ротация журнала вызовов по суткам и чтение истории через архивы."""
from __future__ import annotations

import json
from pathlib import Path

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
