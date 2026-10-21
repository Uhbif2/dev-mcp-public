"""TZ-302: claim-scoped visual reviews use a fake Gemini, never the network."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet import gemini
from dev_mcp.fleet.look import DEFAULT_RUBRIC, look_for_agent
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.tools import FleetContext, register

PNG = b"\x89PNG\r\n\x1a\n" + b"png frame"
WEBP = b"RIFF" + b"\x10\x00\x00\x00" + b"WEBP" + b"webp frame"


@pytest.fixture
def workspace(tmp_path: Path):
    root = tmp_path / "project"
    owned = root / "ui"
    owned.mkdir(parents=True)
    (owned / "first.png").write_bytes(PNG)
    (owned / "second.webp").write_bytes(WEBP)
    (root / "private.png").write_bytes(PNG)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES (?,?,1,1)",
            ("demo", str(root)), event={"kind": "seed"},
        )
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,owns,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("demo", "TZ-L", "look", "taken", json.dumps([str(owned)]), now, now),
            event={"kind": "seed"},
        )
        tx.change(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES (?,?,?,?,?)",
            ("W1", "test", "chatgpt", now, now), event={"kind": "seed"},
        )
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES (?,?,?,?,?)",
            ("demo", "TZ-L", "W1", "active", now), event={"kind": "seed"},
        )
        tx.change(
            "INSERT INTO claim_paths(claim_id,path_norm) VALUES (?,?)",
            (1, norm_path(str(owned))), event={"kind": "seed"},
        )
        tx.record_event(kind="take", worker_id="W1", project="demo", tz="TZ-L",
                        payload={"claim_id": 1})
    return store, root


def _fake_gemini(monkeypatch, *, result=None):
    called = []

    def ask(prompt, **kwargs):
        called.append((prompt, kwargs))
        return result if result is not None else {
            "ok": False,
            "defects": [{"severity": "P1", "where": "heading", "what": "Low contrast"}],
        }

    monkeypatch.setattr(gemini, "ask", ask)
    return called


def test_owned_png_webp_and_extra_rubric(workspace, monkeypatch):
    store, root = workspace
    called = _fake_gemini(monkeypatch)
    result = look_for_agent(store, "W1",
                            [str(root / "ui" / "first.png"), "ui/second.webp"],
                            "Check the mobile menu")
    assert result["ok"] is False
    assert result["defects"] == [{"severity": "P1", "where": "heading", "what": "Low contrast"}]
    assert len(called) == 1
    assert "анимац" in called[0][0] and "Check the mobile menu" in called[0][0]
    assert "контраст" in DEFAULT_RUBRIC
    assert called[0][1]["images"] == [("image/png", PNG), ("image/webp", WEBP)]
    assert gemini._image(("image/webp", WEBP)) == ("image/webp", WEBP)
    with store.read() as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM events WHERE kind='look'")["n"] == 1


def test_outside_owns_denied_before_model(workspace, monkeypatch):
    store, root = workspace
    called = _fake_gemini(monkeypatch)
    response = look_for_agent(store, "W1", [str(root / "private.png")])
    assert response["ok"] is False and "error" in response
    assert not called
    with store.read() as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM events WHERE kind='look'")["n"] == 0


def test_frame_limit_and_missing_claim(workspace, monkeypatch):
    store, root = workspace
    called = _fake_gemini(monkeypatch)
    image = str(root / "ui" / "first.png")
    assert "1 до 12" in look_for_agent(store, "W1", [image] * 13)["error"]
    assert "нет активного захвата" in look_for_agent(store, "W2", [image])["error"]
    assert not called


def test_hourly_budget_is_fleet_wide_and_atomic(workspace, monkeypatch):
    store, root = workspace
    called = _fake_gemini(monkeypatch)
    image = str(root / "ui" / "first.png")
    for _ in range(60):
        assert "error" not in look_for_agent(store, "W1", [image])
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES (?,?,?,?,?)",
            ("W2", "second", "chatgpt", now, now), event={"kind": "seed"},
        )
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES (?,?,?,?,?)",
            ("demo", "TZ-L", "W2", "active", now), event={"kind": "seed"},
        )
        tx.change(
            "INSERT INTO claim_paths(claim_id,path_norm) VALUES (?,?)",
            (2, norm_path(str(root / "ui"))), event={"kind": "seed"},
        )
        tx.record_event(kind="take", worker_id="W2", project="demo", tz="TZ-L",
                        payload={"claim_id": 2})
    denied = look_for_agent(store, "W1", [image])
    assert "бюджет глаз исчерпан, повтори после" in denied["error"]
    assert "бюджет глаз исчерпан" in look_for_agent(store, "W2", [image])["error"]
    assert len(called) == 60
    with store.read() as tx:
        assert tx.one("SELECT COUNT(*) AS n FROM events WHERE kind='look'")["n"] == 60


def test_gemini_failure_keeps_machine_error(workspace, monkeypatch):
    store, root = workspace
    def fail(*args, **kwargs):
        gemini._fail("network_error", "relay refused")
        return None
    monkeypatch.setattr(gemini, "ask", fail)
    result = look_for_agent(store, "W1", [str(root / "ui" / "first.png")])
    assert result["ok"] is False
    assert result["last_error"]["code"] == "network_error"


def test_rejects_large_or_disguised_images(workspace, monkeypatch):
    store, root = workspace
    called = _fake_gemini(monkeypatch)
    path = root / "ui" / "first.png"
    path.write_bytes(PNG + b"x" * (4 * 1024 * 1024))
    assert "4 МБ" in look_for_agent(store, "W1", [str(path)])["error"]
    path.write_bytes(b"not an image")
    assert "некорректный" in look_for_agent(store, "W1", [str(path)])["error"]
    assert not called


def test_symlink_to_other_file_is_denied(workspace, monkeypatch):
    store, root = workspace
    link = root / "ui" / "link.png"
    try:
        link.symlink_to(root / "private.png")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    called = _fake_gemini(monkeypatch)
    assert "error" in look_for_agent(store, "W1", [str(link)])
    assert not called


def test_mcp_tool_registered_and_returns_defects(workspace, monkeypatch):
    store, root = workspace
    _fake_gemini(monkeypatch)

    class FakeMCP:
        def __init__(self):
            self.tools = {}

        def tool(self, *, name, description):
            def decorator(fn):
                self.tools[name] = fn
                return fn
            return decorator

    mcp = FakeMCP()
    registered = register(mcp, FleetContext(store))
    assert "fleet_look" in registered
    reply = mcp.tools["fleet_look"]("W1", [str(root / "ui" / "first.png")])
    assert "Low contrast" in reply


def test_reviewer_can_inspect_project_images_without_write_ownership(workspace, monkeypatch):
    store, root = workspace
    called = _fake_gemini(monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE tz='TZ-L'", event="seed")
        tx.change("UPDATE claims SET state='released'", event="seed")
        tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W-review',?,?)",
                  (now, now), event="seed")
        tx.change("INSERT INTO tasks(project,tz,state,kind,body,created_at,updated_at) "
                  "VALUES('demo','review:TZ-L','taken','review',?,?,?)",
                  ('[fleet-review] {"project":"demo","tz":"TZ-L"}', now, now), event="seed")
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('demo','review:TZ-L','W-review','active',?)", (now,), event="seed")
        tx.record_event(kind="take", worker_id="W-review", project="demo", tz="review:TZ-L",
                        payload={"claim_id": 2})
    result = look_for_agent(store, "W-review", [str(root / "ui" / "first.png")])
    assert "error" not in result, result
    assert len(called) == 1
    outside = root.parent / "outside.png"
    outside.write_bytes(PNG)
    assert "error" in look_for_agent(store, "W-review", [str(outside)])
    assert len(called) == 1
