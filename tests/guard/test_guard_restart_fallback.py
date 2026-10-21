"""Урок 37: job автозапуска запрещает breakaway; откат не уходит на старую метку."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.guard.test_guard_platform_core import git, lane, make_guard  # noqa: F401


def codes(guard):
    return [json.loads(x) for x in guard.log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.skipif(os.name != "nt", reason="Windows job semantics")
def test_breakaway_denied_falls_back_inside_job(lane, monkeypatch):
    live, *_ = lane
    venv = live / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_bytes(b"")
    guard, _ = make_guard(lane)
    calls = []

    class Child:
        returncode = 0
        def communicate(self, timeout=None):
            return "ok", None
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def popen(args, **kw):
        calls.append(kw.get("creationflags", 0))
        if kw.get("creationflags", 0) & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Отказано в доступе")
        return Child()

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert guard._restart(live) is True
    assert len(calls) == 2
    assert not calls[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    detail = [e for e in codes(guard) if e["code"] == "restart_detail"]
    assert detail and "PermissionError" in detail[0]["error"] and detail[0]["breakaway"] == 1


def test_broken_restarter_keeps_prior_head_not_stale_tag(lane):
    live, work, _, _ = lane
    stale = git(live, "rev-parse", "HEAD")
    git(live, "tag", "-f", "platform/known-good", stale)
    # Ручная синхронизация live вперёд (метка не двигалась) — как 11.10.
    (work / "other.txt").write_text("owner commit", encoding="utf-8")
    git(work, "add", "other.txt")
    git(work, "commit", "-m", "manual sync")
    git(live, "merge", "--ff-only", "platform/work")
    prior = git(live, "rev-parse", "HEAD")
    (work / "payload.txt").write_text("after", encoding="utf-8")
    git(work, "add", "payload.txt")
    git(work, "commit", "--allow-empty", "-m", "candidate 2")
    guard, _ = make_guard(lane, restarter=lambda _p: False)
    result = guard.deploy()
    # Инвариант: live не уходит назад от HEAD до попытки. Отказ выкатывать
    # без подтверждённой цели отката (окно выпуска TZ-59) — тоже допустим.
    assert result in {"restart_error", "known_good_missing"}
    assert git(live, "rev-parse", "HEAD") == prior != stale
    entries = [e["code"] for e in codes(guard)]
    assert "safe_mode" not in entries
    if result == "restart_error":
        assert "safe_mode_skipped" in entries
