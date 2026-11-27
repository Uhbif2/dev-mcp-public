"""W9-6: request_note must never turn an intentional legacy/disabled route into 500."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from dev_mcp.audit import Audit
from dev_mcp.fleet import generators
from dev_mcp.policy import Policy
from dev_mcp.server import build_server


def _note_tool(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{root.as_posix()}']\n"
        f"write_roots = ['{root.as_posix()}']\n"
        f"exec_roots = ['{root.as_posix()}']\n"
        "exec_allowed_bin = ['python']\n"
        "allow_write = false\n"
        "deny_patterns = ['.env', '.git']\n",
        encoding="utf-8",
    )
    server = build_server(Policy.load(config), Audit(tmp_path / "logs" / "audit.jsonl"))
    tool = asyncio.run(server.get_tool("request_note"))
    assert tool is not None
    return tool.fn


@pytest.mark.parametrize(
    ("kind", "target"),
    [("other", "owner"), ("access", "queue"), ("network", "owner")],
)
def test_legacy_requests_return_a_decision_not_an_exception(
    tmp_path: Path, kind: str, target: str,
) -> None:
    note = _note_tool(tmp_path)
    result = note(kind=kind, summary="check a legacy route", to=target)
    assert "разобрана автоматически:" in result
    assert "unexpectedly routed" not in result
    history = tmp_path / "logs" / "requests.history.jsonl"
    records = [json.loads(s) for s in history.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1 and records[0]["kind"] == kind
    assert records[0]["decision"] in ("approved", "denied")


def test_disabled_queue_generators_explain_nondelivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = _note_tool(tmp_path)
    monkeypatch.setattr(generators, "enabled", lambda: False)
    result = note(kind="other", summary="Create task when generator is off", to="queue")
    # 20.11.2026 (№116 п. 2): агенту — прямо, что это намеренно и что делать; оператору — запись.
    # 24.11.2026: правда вместо «создание задач отключено» — как зарегистрировать ТЗ самому.
    assert "Регистрация ТЗ работает" in result and "bootstrap" in result
    assert "НЕ сообщай владельцу" in result
    assert "не поломка флота" in result and "fleet_owner bootstrap" in result
    assert "unexpectedly routed" not in result
    history = tmp_path / "logs" / "requests.history.jsonl"
    records = [json.loads(s) for s in history.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1 and records[0]["decision"] == "not_created"
    assert records[0]["summary"] == "Create task when generator is off" and records[0]["auto"] is True


def test_unexpected_none_still_returns_actionable_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dev_mcp.fleet import review

    note = _note_tool(tmp_path)
    monkeypatch.setattr(generators, "enabled", lambda: True)
    monkeypatch.setattr(review, "open_request", lambda *_a, **_kw: None)
    result = note(kind="other", summary="Unexpected fallback", to="queue")
    assert "не принята:" in result
    assert "legacy-маршрута" in result
    assert "Задача в очереди не создана" in result
    assert "unexpectedly routed" not in result
