"""Windows replace/read races for shared JSON books."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dev_mcp.json_book import read_json, write_json


def test_replace_permission_error_is_retried(tmp_path: Path):
    path = tmp_path / "book.json"
    path.write_text('{"old": true}', encoding="utf-8")
    import dev_mcp.json_book as module
    original = module.os.replace
    calls = []

    def flaky(src, dst):
        calls.append((src, dst))
        if len(calls) < 3:
            raise PermissionError("Windows sharing violation")
        return original(src, dst)

    with patch.object(module.os, "replace", side_effect=flaky):
        write_json(path, {"new": 1}, attempts=3, delay=0)
    assert len(calls) == 3
    assert read_json(path, strict=True, attempts=1) == {"new": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_exhausted_replace_does_not_clobber_existing(tmp_path: Path):
    path = tmp_path / "book.json"
    path.write_text('{"old": true}', encoding="utf-8")
    with patch("dev_mcp.json_book.os.replace", side_effect=PermissionError):
        with pytest.raises(PermissionError):
            write_json(path, {"new": 1}, attempts=2, delay=0)
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert not list(tmp_path.glob('.book.json.*.tmp'))


def test_reader_retries_transient_permission_error(tmp_path: Path):
    path = tmp_path / "book.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    original = Path.read_text
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("sharing violation")
        return original(self, *args, **kwargs)

    with patch.object(Path, "read_text", flaky):
        assert read_json(path, strict=True, attempts=3, delay=0) == {"ok": True}
    assert len(calls) == 3


def test_strict_rejects_corrupt_existing_book(tmp_path: Path):
    path = tmp_path / "book.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        read_json(path, default={}, strict=True, attempts=2, delay=0)
    assert read_json(path, default={}, attempts=2, delay=0) == {}
    assert read_json(tmp_path / "missing.json", default={}, strict=True, attempts=1) == {}
