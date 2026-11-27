"""Resource-budget contract for the operator's affected-tests runner."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "affected_tests.py"
SPEC = importlib.util.spec_from_file_location("affected_tests_w10_3", SCRIPT)
assert SPEC and SPEC.loader
affected = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(affected)


@pytest.mark.parametrize(
    ("env", "args", "expected"),
    [
        ({}, ["--run"], 3),
        ({"AFFECTED_TESTS_WORKERS": "2"}, ["--run"], 2),
        ({"AFFECTED_TESTS_WORKERS": "5"}, ["--run", "-n", "4"], 4),
        ({}, ["--run", "--workers", "1"], 1),
    ],
)
def test_worker_count_default_environment_and_cli(monkeypatch, tmp_path, env, args, expected):
    monkeypatch.setattr(affected, "REPO", tmp_path)
    monkeypatch.delenv("AFFECTED_TESTS_WORKERS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(affected, "changed", lambda base: (["src/x.py"], {"changed_func"}))
    monkeypatch.setattr(affected, "select", lambda files, names: ["tests/test_a.py", "tests/test_b.py"])
    calls = []
    monkeypatch.setattr(affected.subprocess, "run",
                        lambda cmd, **kwargs: (calls.append(cmd), SimpleNamespace(returncode=0))[1])
    assert affected.main(args) == 0
    pytest_args = calls[0]
    if expected == 1:
        assert "-n" not in pytest_args
    else:
        assert pytest_args[pytest_args.index("-n") + 1] == str(expected)


@pytest.mark.parametrize("value", ["0", "-2", "abc", "1.5"])
def test_invalid_worker_count_rejected(monkeypatch, value):
    monkeypatch.setattr(affected, "changed", lambda base: ([], set()))
    with pytest.raises(SystemExit) as raised:
        affected.main(["-n", value])
    assert raised.value.code == 2
