"""Integration contract for old two-field fleet_submit bite transport.

The exercised facade uses a synthetic policy/store and never starts a real
bite worktree or connects to the live fleet database.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dev_mcp.audit import Audit
from dev_mcp.fleet import bite
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store
from dev_mcp.policy import Policy
from dev_mcp.server import build_server


TASK = "fix:TZ-BITE"


def _tool(server, name: str):
    descriptor = asyncio.run(server.get_tool(name))
    assert descriptor is not None
    if asyncio.iscoroutinefunction(descriptor.fn):
        return lambda *args, **kwargs: asyncio.run(descriptor.fn(*args, **kwargs))
    return descriptor.fn


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bite, "ENABLED", True)
    root = tmp_path / "project"
    root.mkdir()
    (root / "owned.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{root}']\n"
        f"write_roots = ['{root}']\n"
        f"exec_roots = ['{root}']\n"
        "exec_allowed_bin = ['python', 'py']\n"
        "allow_write = false\n"
        "allow_network_fetch = false\n"
        "deny_patterns = ['.env', '.git']\n"
        "exec_timeout_cap_s = 30\n",
        encoding="utf-8",
    )
    server = build_server(Policy.load(config), Audit(tmp_path / "logs" / "audit.jsonl"))
    store = Store.open(str(tmp_path / "data" / "fleet" / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES (?,?,?,1)",
            ("demo", str(root), 1),
            event={"kind": "seed"},
        )
        tx.change(
            """INSERT INTO tasks(
                 project,tz,title,state,priority,kind,spec_path,spec_sha,
                 needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "demo", TASK, "Synthetic fix", "ready", 1, "fix",
                f"ToDo/{TASK}.md", "sha", "[]",
                json.dumps([norm_path(str(root / "owned.py"))]),
                "[]", "[]", None, "Fix the synthetic regression.", now, now,
            ),
            event={"kind": "seed"},
        )
    assert "worker_id: W1" in _tool(server, "fleet_join")("old-schema worker")
    assert f"demo/{TASK}" in _tool(server, "fleet_take")("W1")
    # Submit gate must see a real post-claim edit in the fix-owned path.
    (root / "owned.py").write_text("VALUE = 2\n", encoding="utf-8")
    return server, store, root


def _state(store: Store) -> tuple[str, str | None, dict | None]:
    with store.read() as tx:
        task = tx.one(
            "SELECT state FROM tasks WHERE project=? AND tz=?", ("demo", TASK),
        )
        review = tx.one(
            "SELECT state FROM tasks WHERE project=? AND tz=?",
            ("demo", f"review:{TASK}"),
        )
        event = tx.one(
            "SELECT payload FROM events WHERE kind='bite' AND project=? AND tz=? "
            "ORDER BY id DESC LIMIT 1",
            ("demo", TASK),
        )
    assert task is not None
    return (
        str(task["state"]),
        str(review["state"]) if review is not None else None,
        json.loads(str(event["payload"])) if event is not None else None,
    )


def _spy_launch(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    launched: list[tuple] = []

    def fake_start(store, project, tz, root, claim_id, argv, record):
        launched.append((project, tz, root, claim_id, argv[:]))
        return None

    monkeypatch.setattr(bite, "start", fake_start)
    return launched


def test_legacy_two_field_bite_submits_and_launches(tmp_path, monkeypatch):
    server, store, root = _fixture(tmp_path, monkeypatch)
    launched = _spy_launch(monkeypatch)
    result = _tool(server, "fleet_submit")(
        "W1", "Fixed actual bug.\nbite: py -3 -m pytest tests/test_fix.py -q",
    )
    assert "сдана на приёмку" in result
    task_state, review_state, payload = _state(store)
    assert task_state == "submitted"
    assert review_state == "ready"
    assert payload == {
        "verdict": "pending",
        "command": "py -3 -m pytest tests/test_fix.py -q",
        "reason": "проверка идёт в отдельной копии, результат появится здесь",
    }
    assert len(launched) == 1
    project, tz, work_root, claim_id, argv = launched[0]
    assert (project, tz, work_root) == ("demo", TASK, str(root))
    assert isinstance(claim_id, int) and claim_id > 0
    assert argv == ["py", "-3", "-m", "pytest", "tests/test_fix.py", "-q"]


def test_fix_missing_bite_rejects_before_submit(tmp_path, monkeypatch):
    server, store, _ = _fixture(tmp_path, monkeypatch)
    launched = _spy_launch(monkeypatch)
    result = _tool(server, "fleet_submit")("W1", "Fixed without regression test")
    assert "кусающим тестом" in result
    assert _state(store) == ("taken", None, None)
    assert launched == []


def test_legacy_opt_out_records_skipped_without_launch(tmp_path, monkeypatch):
    server, store, _ = _fixture(tmp_path, monkeypatch)
    launched = _spy_launch(monkeypatch)
    result = _tool(server, "fleet_submit")(
        "W1", "Visual-only.\nbite: n/a — нет программного теста",
    )
    assert "сдана на приёмку" in result
    state, review, payload = _state(store)
    assert (state, review) == ("submitted", "ready")
    assert payload == {"verdict": "skipped", "reason": "нет программного теста"}
    assert launched == []


def test_explicit_bite_test_overrides_note_fallback(tmp_path, monkeypatch):
    server, store, _ = _fixture(tmp_path, monkeypatch)
    launched = _spy_launch(monkeypatch)
    result = _tool(server, "fleet_submit")(
        "W1", "bite: powershell -NoProfile", "python -m pytest explicit.py -q",
    )
    assert "сдана на приёмку" in result
    state, review, payload = _state(store)
    assert (state, review) == ("submitted", "ready")
    assert payload is not None and payload["command"] == "python -m pytest explicit.py -q"
    assert len(launched) == 1
    assert launched[0][4] == ["python", "-m", "pytest", "explicit.py", "-q"]


@pytest.mark.parametrize("line", [
    "bite: powershell -NoProfile",
    "bite: python -m pytest ../outside.py",
    "bite: bogus",
])
def test_invalid_or_disallowed_legacy_bite_does_not_submit(line, tmp_path, monkeypatch):
    server, store, _ = _fixture(tmp_path, monkeypatch)
    launched = _spy_launch(monkeypatch)
    result = _tool(server, "fleet_submit")("W1", "Fixed.\n" + line)
    assert "Отказано:" in result
    assert _state(store) == ("taken", None, None)
    assert launched == []
