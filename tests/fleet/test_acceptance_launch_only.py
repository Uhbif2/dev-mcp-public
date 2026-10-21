"""Регистрация ТЗ проверяет, что приёмка ЗАПУСКАЕТСЯ, а не что она зелёная (11.10.2026)."""
from types import SimpleNamespace as NS

from dev_mcp.fleet.spec import _launch_failure


def _res(rc, out="", err=""):
    return NS(reason="gate_launch_failed", launch=NS(returncode=rc, stdout=out, stderr=err))


def test_red_acceptance_of_new_tz_is_not_a_launch_failure():
    # pytest: 1 — тесты красные, 4 — файла теста ещё нет, 5 — тестов не найдено
    for rc, err in ((1, "1 failed"), (4, "ERROR: file or directory not found: tests/x.py"), (5, "")):
        assert _launch_failure(_res(rc, err=err)) == ""


def test_unlaunchable_command_is_rejected():
    assert _launch_failure(_res(127, err="FileNotFoundError"))
    assert _launch_failure(_res(124, err="timeout"))
    assert _launch_failure(_res(1, err="python.exe: No module named pytest"))


def test_unittest_module_the_tz_will_write_is_not_a_launch_failure(tmp_path):
    # deep-research TZ-11, 13.10.2026: tests/ есть, tests/test_clean_start.py ещё нет.
    (tmp_path / "tests").mkdir()
    err = "ModuleNotFoundError: No module named 'tests.test_clean_start'"
    assert _launch_failure(_res(1, err=err), tmp_path) == ""
    # пакета нет — опечатка в команде, а не будущий файл
    assert _launch_failure(_res(1, err="No module named 'nope.test_x'"), tmp_path)
    # без корня — по-старому строго
    assert _launch_failure(_res(1, err=err))


def test_long_gate_timeout_is_launched_but_busy_lock_is_transient() -> None:
    from types import SimpleNamespace
    from dev_mcp.fleet.spec import _launch_failure

    def result(rc: int, stderr: str, stdout: str = "") -> SimpleNamespace:
        return SimpleNamespace(launch=SimpleNamespace(returncode=rc, stdout=stdout,
                                                      stderr=stderr), reason="gate_launch_failed")

    assert _launch_failure(result(124, "timeout: 180s", "running 512 checks")) == ""
    assert _launch_failure(result(124, "resource busy: heavy-build pid 1")).startswith("resource_busy")
    assert _launch_failure(result(124, "timeout: 180s", "No module named 'comtypes'")).startswith(
        "gate_launch_failed")
    assert _launch_failure(result(127, "FileNotFoundError")).startswith("gate_launch_failed")


def test_heavy_command_is_checked_statically_not_run(tmp_path, monkeypatch) -> None:
    from dev_mcp.fleet import spec, world

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "check_all.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    monkeypatch.setattr(world, "config", lambda: {"heavy-build": {
        "scope": "run", "global": True, "scan_processes": True,
        "run_match": r"\bcheck_all"}})
    import sys
    exe = sys.executable
    assert spec._heavy_static_launch(f'"{exe}" tools/check_all.py', tmp_path) == ""
    assert spec._heavy_static_launch(f'"{exe}" tools/check_all_x.py', tmp_path).startswith(
        "script not found")
    assert spec._heavy_static_launch(f'"{exe}" -m pytest -q', tmp_path) is None
