from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import process_idle


def command():
    return ["process-idle", "tools/check.py", "--mode", "beta"]


def test_inline_observer_and_launcher_do_not_find_themselves(tmp_path):
    code = "product_release_check.py --mode beta tools/check.py"
    rows = [{"cmdline": [exe, "-3", "-c", code], "cwd": str(tmp_path)}
            for exe in ("py.exe", "python.exe")]
    assert process_idle.evaluate(command(), str(tmp_path), rows) == 0


def test_actual_script_matches_only_project_and_exact_arguments(tmp_path):
    row = {"cmdline": ["python.exe", "-X", "utf8", "tools/check.py", "--mode", "beta"],
           "cwd": str(tmp_path)}
    assert process_idle.evaluate(command(), str(tmp_path), [row]) == 1
    assert process_idle.evaluate(command(), str(tmp_path / "other"), [row]) == 0
    row["cmdline"][-1] = "beta-preview"
    assert process_idle.evaluate(command(), str(tmp_path), [row]) == 0


def test_missing_inspection_is_not_a_success(tmp_path):
    assert process_idle.evaluate(command(), str(tmp_path), [{"cmdline": None}]) == -1
    row = {"cmdline": ["python", "tools/check.py", "--mode", "beta"], "cwd": None}
    assert process_idle.evaluate(command(), str(tmp_path), [row]) == -1


@pytest.mark.parametrize("path", ["../check.py", "C:/outside/check.py", "/check.py", "check.exe"])
def test_process_observer_rejects_paths_outside_project(path):
    with pytest.raises(ValueError):
        process_idle.validate(["process-idle", path])


def test_real_process_is_busy_then_idle_without_matching_our_own_test(tmp_path):
    script = tmp_path / "tools" / "check.py"
    script.parent.mkdir()
    script.write_text("import time; time.sleep(30)", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script), "--mode", "beta"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process_idle.run(command(), str(tmp_path)) == 1:
                break
        else:
            pytest.fail("real checker was not observed")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert process_idle.run(command(), str(tmp_path)) == 0
