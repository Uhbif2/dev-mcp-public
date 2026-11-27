"""fleet_owner bootstrap не убивается по сроку вызова и не плодит процессы (20.11.2026).

Регрессия: сервер зовёт tools/fleet.py со сроком 30 с (OWNER_PULT_TIMEOUT_S) и убивал пульт
на стадии snapshot, до записи в реестр, — ТЗ Beta App не регистрировались вовсе.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

_FLEET = Path(__file__).resolve().parents[2] / "tools" / "fleet.py"
_spec = importlib.util.spec_from_file_location("fleet_console_detach_test", _FLEET)
fleet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet)

FAKE = r'''
import os, sys, time, json, pathlib
here = pathlib.Path(__file__).parent
(here / "starts.txt").open("a").write("x\n")
print("bootstrap: lock 0.00 с", file=sys.stderr, flush=True)
time.sleep(float((here / "delay.txt").read_text()))
print("bootstrap: registry 0.01 с", file=sys.stderr, flush=True)
print(json.dumps({"inserted": 1}))
pathlib.Path(os.environ["FLEET_BOOTSTRAP_RC_FILE"]).write_text("0")
'''

ARGS = ["--db", "X.sqlite", "bootstrap", "P", "C:/p", "TZ-1", "TZ-2"]


def _setup(tmp_path: Path, delay: float) -> tuple[list[str], Path]:
    fake = tmp_path / "fake_pult.py"
    fake.write_text(FAKE, encoding="utf-8")
    (tmp_path / "delay.txt").write_text(str(delay), encoding="utf-8")
    return [sys.executable, str(fake)], tmp_path / "jobs"


def test_not_server_mode_runs_synchronously(monkeypatch) -> None:
    monkeypatch.delenv(fleet.FOREGROUND_ENV, raising=False)
    assert fleet._owner_bootstrap_detach(["--db", "X", "active"]) is None
    assert fleet._owner_bootstrap_detach(["bootstrap", "P", "C:/p", "TZ-1"]) is None
    monkeypatch.setenv(fleet.FOREGROUND_ENV, "1")
    assert fleet._owner_bootstrap_detach(ARGS) is None


def test_fast_bootstrap_answers_in_the_same_call(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv(fleet.FOREGROUND_ENV, raising=False)
    command, jobs = _setup(tmp_path, 0.0)
    rc = fleet._owner_bootstrap_detach(ARGS, command=command, folder=jobs, wait_s=1e6)
    out = capsys.readouterr().out
    assert rc == 0 and '"inserted": 1' in out and "registry" in out
    assert not list(jobs.glob("*.json"))


def test_slow_bootstrap_survives_and_repeat_returns_its_result(tmp_path: Path, capsys,
                                                                monkeypatch) -> None:
    monkeypatch.delenv(fleet.FOREGROUND_ENV, raising=False)
    command, jobs = _setup(tmp_path, 4.0)
    starts = tmp_path / "starts.txt"

    assert fleet._owner_bootstrap_detach(ARGS, command=command, folder=jobs, wait_s=0.0) == 0
    first = capsys.readouterr().out
    assert "фоном" in first and "не прерывается" in first
    assert fleet._owner_bootstrap_detach(ARGS, command=command, folder=jobs, wait_s=0.0) == 0
    assert "ещё идёт" in capsys.readouterr().out

    marker = next(jobs.glob("*.json"))
    deadline = time.monotonic() + 30
    while not list(jobs.glob("*.rc")) and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(0.3)
    assert fleet._owner_bootstrap_detach(ARGS, command=command, folder=jobs, wait_s=0.0) == 0
    done = capsys.readouterr().out
    assert "завершён" in done and '"inserted": 1' in done
    assert starts.read_text().count("x") == 1  # повтор не запустил второй пульт
    assert not marker.exists()
