"""Снимок по обходу дерева переживает файл, исчезнувший после обхода (13.10.2026)."""
import os

from dev_mcp.fleet import gate_snapshot
from dev_mcp.fleet.gate_snapshot import SnapshotStore


def test_file_vanishing_after_walk_is_skipped(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.py").write_text("x = 1\n", encoding="utf-8")
    gone = src / "gone.md"
    gone.write_text("draft\n", encoding="utf-8")
    real_walk = os.walk

    def walk_then_delete(*args, **kwargs):
        for item in real_walk(*args, **kwargs):
            yield item
        if gone.exists():
            gone.unlink()

    monkeypatch.setattr(gate_snapshot.os, "walk", walk_then_delete)
    ref = SnapshotStore(tmp_path / "objects").create(src)
    assert ref.snapshot_id
