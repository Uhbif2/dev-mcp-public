"""17.11: проверка нового gate под общей блокировкой заморозила весь dev-mcp."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from dev_mcp.fleet import goals


def _write(path, gate):
    path.write_text(f'[projects."P"]\ngoal = "g"\ngate = "{gate}"\n', encoding="utf-8")


def test_gate_verification_does_not_block_other_readers(tmp_path, monkeypatch):
    cfg = tmp_path / "fleet-projects.toml"
    _write(cfg, "old")
    monkeypatch.setenv("DEV_MCP_FLEET_PROJECTS", str(cfg))
    monkeypatch.setattr(goals, "_cache", {"path": None, "mtime": None, "data": {},
                                          "rejected_mtime": None})
    assert goals._load_doc()["projects"]["P"]["gate"] == "old"

    started, release = threading.Event(), threading.Event()

    def slow_verify(project, command):
        started.set()
        assert release.wait(10)
        return SimpleNamespace(ok=True, reason="")

    monkeypatch.setattr(goals, "_verify_gate_change", slow_verify)
    time.sleep(0.05)
    _write(cfg, "new")

    verifier = threading.Thread(target=goals._load_doc)
    verifier.start()
    assert started.wait(5)
    # Another caller during verification: immediate, last-known-good config.
    t0 = time.monotonic()
    assert goals._load_doc()["projects"]["P"]["gate"] == "old"
    assert time.monotonic() - t0 < 1.0
    release.set()
    verifier.join(5)
    assert goals._load_doc()["projects"]["P"]["gate"] == "new"


def test_rejected_gate_keeps_old_config(tmp_path, monkeypatch):
    cfg = tmp_path / "fleet-projects.toml"
    _write(cfg, "old")
    monkeypatch.setenv("DEV_MCP_FLEET_PROJECTS", str(cfg))
    monkeypatch.setattr(goals, "_cache", {"path": None, "mtime": None, "data": {},
                                          "rejected_mtime": None})
    goals._load_doc()
    monkeypatch.setattr(goals, "_verify_gate_change",
                        lambda p, c: SimpleNamespace(ok=False, reason="red"))
    monkeypatch.setattr(goals, "_report_gate_rejection", lambda *a, **k: None)
    time.sleep(0.05)
    _write(cfg, "bad")
    assert goals._load_doc()["projects"]["P"]["gate"] == "old"
    assert goals._load_doc()["projects"]["P"]["gate"] == "old"
