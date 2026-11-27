"""Select the evidence that must be committed before revealing a Mill manifest.

Negative-only batches have no candidate selector: every original Case must be
covered, including a Case the miner declined to turn into a candidate.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate MANIFEST key')
        result[key] = value
    return result


def blind_case_ids(raw: bytes, root: Path, batch: str) -> list[str]:
    """Return candidate IDs, or the complete verified negative-only Case set.

    Only bounded metadata and directory names are inspected here; source Case
    contents are neither interpreted nor disclosed by this gate.
    """
    if not raw or len(raw) > 1024 * 1024:
        raise ValueError('MANIFEST outside size limit')
    text = raw.decode('utf-8')
    start, end = '<!-- MILL_MANIFEST_JSON', 'MILL_MANIFEST_JSON -->'
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError('missing unique MANIFEST section')
    payload = json.loads(text.split(start, 1)[1].split(end, 1)[0],
                         object_pairs_hook=_unique_object)
    entries = payload.get('entries') if isinstance(payload, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ValueError('empty or malformed MANIFEST entries')
    outcomes = {'candidate', 'support', 'contradict', 'skip', 'need_more'}
    for cid, entry in entries.items():
        if (not isinstance(cid, str) or cid in ('.', '..')
                or not re.fullmatch(r'[A-Za-z0-9._:-]{1,240}', cid)
                or not isinstance(entry, dict)
                or not isinstance(entry.get('outcome'), str)
                or entry['outcome'] not in outcomes):
            raise ValueError('invalid MANIFEST case ID, entry or outcome')
    declared = payload.get('case_ids')
    if 'case_ids' in payload and (not isinstance(declared, list)
            or any(not isinstance(cid, str) for cid in declared)
            or len(set(declared)) != len(declared) or set(declared) != set(entries)):
        raise ValueError('MANIFEST case_ids mismatch')
    if 'batch_id' in payload and payload['batch_id'] != batch:
        raise ValueError('MANIFEST batch_id mismatch')
    candidates = sorted(cid for cid, entry in entries.items()
                        if entry['outcome'] == 'candidate')
    if candidates:
        return candidates  # Preserve candidate-batch seal compatibility.
    if declared is None or payload.get('batch_id') != batch:
        raise ValueError('negative MANIFEST requires batch_id and complete case_ids')
    cases = root / 'data' / 'batches' / 'cases' / batch
    if cases.is_symlink() or not cases.is_dir():
        raise ValueError('negative MANIFEST requires safe original Case inventory')
    expected = {
        (cid if re.fullmatch(r'[A-Za-z0-9._-]{1,120}', cid) else
         'case-' + hashlib.sha256(cid.encode('utf-8')).hexdigest()[:24]) + '.md'
        for cid in entries
    }
    paths = list(cases.iterdir())
    if (len(expected) != len(entries) or {p.name for p in paths} != expected
            or any(p.is_symlink() or not p.is_file() for p in paths)):
        raise ValueError('negative MANIFEST does not cover the original Case inventory')
    return sorted(entries)
