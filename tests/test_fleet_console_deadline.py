from __future__ import annotations

import importlib.util
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fleet_console_deadline", REPO / "tools" / "fleet.py")
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)


def test_hanging_memory_probe_is_cut_by_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "MEMORY_PROBE_CACHE", tmp_path / "probe.json")
    monkeypatch.setattr(fleet, "MEMORY_PROBE_DEADLINE_S", 0.3)
    started = time.monotonic()
    line = fleet.memory_state(True, probe=lambda: time.sleep(5))
    assert time.monotonic() - started < 2
    assert line == "память НЕ ОТВЕЧАЕТ (TimeoutError)"


def test_fast_memory_probe_passes_through(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "MEMORY_PROBE_CACHE", tmp_path / "probe.json")
    assert fleet.memory_state(True, probe=lambda: {"status": "ok", "checks": {}}) == "память ок"
