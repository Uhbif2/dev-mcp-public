"""Standalone installs need no private siblings; all optional services are faked."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sys

import pytest

from dev_mcp.fleet import memory_rules, nightly_review, overseer, overseer_proxy, owner_telegram

CID = "12345678-1234-1234-1234-123456789abc"
URL = f"https://chatgpt.com/c/{CID}"


def snapshot(n=1):
    return {"url": URL, "user_messages": ["Complete the example task."],
            "last_assistant": "Work is in progress.", "last_role": "assistant",
            "n_messages": n, "composer_empty": True}


def test_nightly_reports_priority(tmp_path, monkeypatch):
    home = tmp_path / "dev-mcp"
    home.mkdir()
    monkeypatch.delenv("DEV_MCP_NIGHTLY_REPORTS", raising=False)
    assert nightly_review._reports_path(home) == home / "data" / "nightly"
    (tmp_path / "_todo").mkdir()
    assert nightly_review._reports_path(home) == tmp_path / "_todo" / "nightly"
    monkeypatch.setenv("DEV_MCP_NIGHTLY_REPORTS", str(tmp_path / "custom"))
    assert nightly_review._reports_path(home) == tmp_path / "custom"


def test_nightly_local_timezone_and_override(tmp_path, monkeypatch):
    utc = datetime(2026, 11, 27, 0, 30, tzinfo=timezone.utc)
    monkeypatch.delenv("DEV_MCP_NIGHTLY_TZ_OFFSET", raising=False)
    assert nightly_review._local_moment(utc) == utc.astimezone()
    assert nightly_review._local_moment(utc).utcoffset() == utc.astimezone().utcoffset()
    monkeypatch.setenv("DEV_MCP_NIGHTLY_TZ_OFFSET", "invalid")
    assert nightly_review._local_moment(utc).utcoffset() == utc.astimezone().utcoffset()
    monkeypatch.setattr(nightly_review, "plan", lambda *a, **k: ([], 0))
    monkeypatch.setenv("DEV_MCP_NIGHTLY_TZ_OFFSET", "3")
    assert nightly_review._local_moment(utc).utcoffset() == timedelta(hours=3)
    for mode, hour in (("code", 2), ("gap", 3), ("docs", 4)):
        for offset, expected in ((hour - 1, False), (hour, True), (5, False)):
            monkeypatch.setenv("DEV_MCP_NIGHTLY_TZ_OFFSET", str(offset))
            result = nightly_review.drive(["demo"], now=utc, mode=mode, dry=True,
                                          home=tmp_path, data=tmp_path / "state",
                                          roots={"demo": tmp_path})
            assert result["demo"]["would_start"] is expected


@pytest.fixture
def isolated_overseer(tmp_path, monkeypatch):
    root = tmp_path / "dev-mcp"
    monkeypatch.setattr(overseer, "ROOT", root)
    monkeypatch.setattr(overseer_proxy, "__file__",
                        str(root / "src" / "dev_mcp" / "fleet" / "overseer_proxy.py"))
    monkeypatch.setattr(overseer, "_proxy_module", lambda: overseer_proxy)
    monkeypatch.setattr(overseer, "agent_history", lambda *a, **k: {"actions": [], "count": 0})
    cached_redactor = overseer._redactor
    cached_redactor.cache_clear()
    yield root
    cached_redactor.cache_clear()


@pytest.mark.parametrize("engine", ["proxy", "semantic"])
def test_missing_mill_disables_once_without_content_or_notifications(
        tmp_path, monkeypatch, isolated_overseer, engine):
    book = overseer.Book(tmp_path / "data")
    overseer.add(book, URL, "Demo", "send", engine)
    writes = []
    original = book.save_chats
    monkeypatch.setattr(book, "save_chats", lambda rows: (writes.append(rows), original(rows)))
    monkeypatch.setattr(overseer.urllib.request, "build_opener",
                        lambda *a: pytest.fail("no backend request without redactor"))
    for n in range(3):
        result = overseer.observe(book, snapshot(n),
                                  notify_fn=lambda *a: pytest.fail("no notice"))
        assert result == {"action": "none", "why": overseer.NO_DECISION_BACKEND}
    assert len(writes) == 1
    assert overseer.watched(book)[0]["status"] == "disabled: no decision backend"
    assert overseer.proxy_wake(book, CID, None) == ""
    assert book.card(CID) is None
    assert not book.journal.exists()
    assert overseer._relay_decide([{"type": "image", "probe_image": True}], {}) is None
    assert overseer._jev_decide([], {}) is None


def test_proxy_missing_backend_with_redactor_is_not_technical_pause(
        tmp_path, monkeypatch, isolated_overseer):
    # Simulate a trusted sanitizer available independently of the missing Jev.
    monkeypatch.setattr(overseer, "_redactor", lambda: lambda value: (value, {}))
    book = overseer.Book(tmp_path / "data")
    overseer.add(book, URL, "Demo", "send", "proxy")
    calls = []
    original = overseer_proxy.default_ask
    monkeypatch.setattr(overseer_proxy, "default_ask",
                        lambda *args: (calls.append(1), original(*args))[1])
    for n in range(3):
        assert overseer.observe(book, snapshot(n),
                                 notify_fn=lambda *a: pytest.fail("no notice")) == {
                                     "action": "none", "why": overseer.NO_DECISION_BACKEND}
    assert calls == [1]
    assert overseer.watched(book)[0]["status"] == overseer.NO_DECISION_BACKEND
    assert not book.card(CID)["jev"].get("tech_retry")
    assert overseer.proxy_wake(book, CID, None) == ""
    overseer.set_mode(book, CID, "send", "proxy")
    assert "status" not in overseer.watched(book)[0]


def test_present_mill_preserves_redaction_and_jev_route(tmp_path, monkeypatch, isolated_overseer):
    mill = tmp_path / "mill-works" / "mill"
    mill.mkdir(parents=True)
    (mill / "redact.py").write_text(
        "def redact(value):\n    return value.replace('example-secret', '[MASKED]'), {}\n",
        encoding="utf-8")
    assert overseer._redact_text("example-secret") == "[MASKED]"
    calls = []
    response = {"action": {"probabilities": {"WAIT": 1.0}}}
    jev = SimpleNamespace(ask_system_one=lambda state, questions:
                          (calls.append((state, questions)), response)[1])
    monkeypatch.setitem(sys.modules, "mill", SimpleNamespace(jev=jev))
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert overseer_proxy.default_ask({"safe": True}, {"q": {}}) == response
    assert calls == [({"safe": True}, {"q": {}})]


def test_missing_relay_does_not_launch(tmp_path, monkeypatch):
    import shutil
    relay = tmp_path / "ai-relay"
    monkeypatch.setattr(overseer, "_RELAY_DIR", relay)
    monkeypatch.setattr(overseer, "RELAY_URL", "http://127.0.0.1:8790/v1beta/decisions")
    monkeypatch.setattr(overseer, "_relay_started", [-1000.0])
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(shutil, "which", lambda *_: "node")
    calls = []
    monkeypatch.setattr(overseer.subprocess, "Popen",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    assert not overseer._start_relay()
    assert not calls
    relay.mkdir()
    (relay / "relay.mjs").touch()
    assert overseer._start_relay()
    assert len(calls) == 1 and calls[0][1]["cwd"] == str(relay)


def test_missing_ambient_is_quiet_but_existing_root_still_spawns(tmp_path, monkeypatch):
    root = tmp_path / "ambient-memory"
    monkeypatch.setattr(memory_rules, "_daemon_ping", lambda port: False)
    monkeypatch.setattr(memory_rules, "_daemon_listening", lambda port: False)
    monkeypatch.setattr(memory_rules, "_keeper_next_attempt", 0)
    calls, logs = [], []
    monkeypatch.setattr(memory_rules, "_start_ambient_daemon",
                        lambda root: calls.append(root) or 42)
    monkeypatch.setattr(memory_rules, "_keeper_log",
                        lambda status, **kw: logs.append(status))
    for _ in range(2):
        assert memory_rules.daemon_keeper_tick(root=root, port=11437) == "disabled: ambient-memory missing"
    assert not calls and not logs
    root.mkdir()
    assert memory_rules.daemon_keeper_tick(root=root, port=11437) == "spawn_requested"
    assert calls == [root] and logs == ["spawn_requested"]


def test_owner_projection_and_notification_optional(tmp_path, monkeypatch):
    decisions = tmp_path / "_lib" / "owner_decisions.py"
    server = tmp_path / "tg-notify" / "server.py"
    monkeypatch.setattr(owner_telegram, "DECISIONS", str(decisions))
    monkeypatch.setattr(owner_telegram, "TG_SERVER", str(server))
    calls = []
    monkeypatch.setattr(owner_telegram.subprocess, "run",
                        lambda argv, **kw: (calls.append(argv),
                            SimpleNamespace(returncode=0, stdout='{"message_ids":[12]}'))[1])
    assert owner_telegram._decisions() == []
    assert owner_telegram._send("example") is False
    assert not calls
    decisions.parent.mkdir()
    decisions.write_text("def collect():\n    return {'decisions': [{'id': 'example'}]}\n",
                         encoding="utf-8")
    server.parent.mkdir()
    server.touch()
    assert owner_telegram._decisions() == [{"id": "example"}]
    assert owner_telegram._send("example") == [12]
    assert calls[0][1:] == [str(server), "send", "example"]
