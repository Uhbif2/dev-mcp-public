import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dev_mcp.fleet import activity_history as history


def point(regular=2):
    return {"working_regular": regular, "working_quick": 1,
            "waiting": 3, "open_quick": 4}


def test_history_minute_dedup_and_24h_trim(tmp_path):
    path = tmp_path / "activity-24h.jsonl"
    t = 1_800_000_000.0
    history.append(path, point(10), t - 86401)
    history.append(path, point(2), t - 120)
    history.append(path, point(4), t)
    history.append(path, point(99), t + 10)
    rows = history.read(path, t + 10)
    assert [r["working_regular"] for r in rows] == [2, 4]
    assert all(set(row) == {"ts", *history.FIELDS} for row in rows)
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_atomic_replace_failure_preserves_previous_file(tmp_path):
    path = tmp_path / "activity-24h.jsonl"
    t = 1_800_000_000.0
    history.append(path, point(), t)
    before = path.read_bytes()
    with patch.object(history.os, "replace", side_effect=OSError("failed")):
        with pytest.raises(OSError, match="failed"):
            history.append(path, point(5), t + 61)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
    assert len(history.read(path, t + 61)) == 1


def test_history_jsonl_round_trip_and_missing_file(tmp_path):
    path = tmp_path / "activity-24h.jsonl"
    assert history.read(path, 100) == []
    history.append(path, point(), 120)
    assert json.loads(path.read_text(encoding="utf-8").strip()) == {
        "ts": 120, **point()}
    assert history.read(path, 121) == [{"ts": 120, **point()}]
