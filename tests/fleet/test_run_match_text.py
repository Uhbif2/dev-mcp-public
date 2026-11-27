"""№116 п. 8.4: run-ресурсы по программе и подкоманде, а не по подстроке любого аргумента."""
from __future__ import annotations

import pytest

from dev_mcp.fleet import world

RM = (r"\b(build|bundle|gradle|nuitka|pyinstaller|payload_builder|make_product_runtime|"
      r"product_update|publish_update|check_all|product_release_check|assemble)")
CFG = {"heavy-build": {"scope": "run", "global": True, "run_match": RM},
       "build-disk": {"scope": "run", "global": True, "run_match": RM},
       "test-run": {"scope": "run", "global": True, "run_match": r"\bpytest\b"}}


@pytest.mark.parametrize("argv", [
    ["git", "diff", "--", "build/out.txt"],
    [r"C:\Program Files\Git\cmd\git.exe", "log", "--", "bundle"],
    ["rg", "-n", "build_app", "src"],
    ["adb", "shell", "getprop", "ro.build.version.release"],
    ["adb.exe", "-s", "R58M", "pull", "/sdcard/bundle.zip"],
    ["py", "-3", "tools/product_update.py", "inspect", "--json"],
    ["py", "-3", "tools/product_update.py", "--json", "status"],
    ["python", "-m", "tools.publish_update", "list"],
    ["py", "tools\\product_update.py", "--help"],
])
def test_reading_commands_do_not_wait_for_a_build(argv):
    assert world._run_needs(CFG, argv) == []


@pytest.mark.parametrize("argv,need", [
    (["py", "-3", "packaging/build_app.py", "--target", "app", "--build"], ["build-disk", "heavy-build"]),
    (["py", "-3", "tools/product_update.py", "apply"], ["build-disk", "heavy-build"]),
    (["py", "-3", "tools/product_update.py", "--channel", "beta", "apply"], ["build-disk", "heavy-build"]),
    (["py", "-3", "tools/product_update.py"], ["build-disk", "heavy-build"]),
    (["gradlew.bat", "assembleDebug"], ["build-disk", "heavy-build"]),
    (["py", "-3", "tools/check_all.py"], ["build-disk", "heavy-build"]),
    (["python", "-m", "pytest", "-q"], ["test-run"]),
])
def test_builds_and_tests_still_take_their_resources(argv, need):
    assert world._run_needs(CFG, argv) == need


@pytest.mark.parametrize("argv", [
    ["cmd", "/c", "git diff build"],                 # обёртка: что внутри — не разбираем, строго
    ["powershell", "-Command", "git log -- build"],
    ["py", "-c", "import subprocess; subprocess.run(['nuitka'])"],
])
def test_wrappers_and_inline_code_stay_fail_closed(argv):
    assert "heavy-build" in world._run_needs(CFG, argv)


def test_declared_resource_without_pattern_still_applies_to_every_command():
    cfg = {"device": {"scope": "run"}}
    assert world._run_needs(cfg, ["git", "status"], ["device"]) == ["device"]


def test_acceptance_with_a_read_only_command_does_not_wait_for_heavy_build():
    assert world.acceptance_run_needs(["git diff -- build", "py -3 tools/product_update.py inspect"], CFG) == []
    assert world.acceptance_run_needs(["py -3 packaging/build_app.py --build"], CFG) == ["build-disk", "heavy-build"]
