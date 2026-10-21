"""Урок 36: страж обновляет пульс во время долгих тестов кандидата, иначе супервизор снимает его через 180 с."""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_guard_platform_core import lane, make_guard  # noqa: E402,F401


def test_pulse_refreshes_heartbeat_during_long_pytest(lane, monkeypatch, tmp_path):
    live, *_ = lane
    guard, _ = make_guard(lane)
    guard.TEST_PULSE_S = 0.05
    cand = tmp_path / "cand"
    (cand / ".venv" / "Scripts").mkdir(parents=True)
    (cand / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (cand / ".venv" / "bin").mkdir(parents=True)
    (cand / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    stamps = []
    pulse = live / "data" / "guard-heartbeat.json"

    def fake_exec(argv, *, cwd, timeout=180, env=None):
        if "pytest" in argv:
            for _ in range(6):
                time.sleep(0.05)
                if pulse.exists():
                    stamps.append(json.loads(pulse.read_text(encoding="utf-8"))["time"])
        return subprocess.CompletedProcess(argv, 0, "1 passed", "")

    monkeypatch.setattr(guard, "_exec", fake_exec)
    assert guard._validate(cand) is True
    assert len(set(stamps)) >= 2, "пульс должен обновляться, пока идут тесты"
