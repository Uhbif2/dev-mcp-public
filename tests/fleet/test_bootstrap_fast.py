"""24.11.2026 (владелец): регистрация ТЗ мгновенная и доступна агентам, которых запустил не флот.

1. «любые ТЗ сразу в работу»: при выключенном [platform.bootstrap] acceptance_trial регистрация не
   гоняет пробный прогон приёмки (раньше 37–117 с на ТЗ) — ТЗ сразу ready, даже с красной приёмкой.
2. «если обращается не worker, запущенный системой, — может регистрировать»: чат флота (метка
   fMMDD-HHMMxxx) и быстрый агент — системные; остальные (чаты и клиенты владельца) — нет.
"""
from __future__ import annotations

from types import SimpleNamespace

from dev_mcp.fleet import spec, tools
from dev_mcp.fleet.spec import sync_selected
from dev_mcp.fleet.store import Store


def _project(tmp_path):
    root = tmp_path / "product"
    directory = root / "ToDo" / "tz"
    directory.mkdir(parents=True)
    (directory / "TZ-1-work.md").write_text(
        "```yaml\ntz: TZ-1\nowns: [src/a.py]\nacceptance: [\"python -m pytest tests/test_missing.py\"]\n```\n# Work",
        encoding="utf-8")
    return root


def test_registration_without_trial_is_immediate_and_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(spec, "acceptance_trial_enabled", lambda: False)

    def forbidden(*args, **kwargs):
        raise AssertionError("пробный прогон приёмки при выключенной проверке")
    monkeypatch.setattr(spec, "_checked_acceptance_updates", forbidden)
    stages = []
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        result = sync_selected(store, "Demo", str(_project(tmp_path)), ["TZ-1"],
                               timing=lambda stage, seconds, details: stages.append(stage))
        assert not result.get("errors"), result
        with store.read() as tx:
            row = tx.one("SELECT state FROM tasks WHERE project='Demo' AND tz='TZ-1'")
        assert row is not None and row["state"] == "ready"
        assert "acceptance_skipped" in stages
    finally:
        store.close()


def test_trial_still_runs_when_switched_on(tmp_path, monkeypatch):
    monkeypatch.setattr(spec, "acceptance_trial_enabled", lambda: True)
    called = []
    monkeypatch.setattr(spec, "_checked_acceptance_updates",
                        lambda *a, **k: called.append(1) or set())
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        sync_selected(store, "Demo", str(_project(tmp_path)), ["TZ-1"])
        assert called == [1]
    finally:
        store.close()


def test_switch_reads_platform_bootstrap():
    assert spec.acceptance_trial_from({}) is False
    assert spec.acceptance_trial_from({"platform": {"bootstrap": {"acceptance_trial": False}}}) is False
    assert spec.acceptance_trial_from({"platform": {"bootstrap": {"acceptance_trial": True}}}) is True


def _ctx(tmp_path, rows):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        for wid, label, kind in rows:
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason) "
                      "VALUES(?,?,?,'2026-11-24','2026-11-24',NULL,'')", (wid, label, kind), event="seed")
    return SimpleNamespace(store=store)


def test_self_started_agents_versus_system_launched(tmp_path):
    ctx = _ctx(tmp_path, [
        ("W1", "Nova lead", "chatgpt"),          # чат владельца
        ("W2", "f1124-1530abc", "chatgpt"),        # открыт расширением флота
        ("W3", "nightly", "quick"),                # быстрый агент
        ("W4", "claude", "claude-code"),           # клиент владельца
    ])
    try:
        assert tools._self_started(ctx, "W1") is True
        assert tools._self_started(ctx, "W2") is False
        assert tools._self_started(ctx, "W3") is False
        assert tools._self_started(ctx, "W4") is True
        assert tools._self_started(ctx, "Q12-ABCDEF12") is False
        assert tools._self_started(ctx, "W999") is False
        assert tools._self_started(ctx, "") is False
        assert "bootstrap" in tools.SELF_STARTED_COMMANDS
        assert "grant" not in tools.SELF_STARTED_COMMANDS and "recover" not in tools.SELF_STARTED_COMMANDS
    finally:
        ctx.store.close()


def test_revoked_owner_chat_keeps_registration(tmp_path):
    ctx = _ctx(tmp_path, [("W5", "my chat", "chatgpt")])
    try:
        with ctx.store.tx() as tx:
            tx.change("UPDATE workers SET revoked_at='2026-11-24' WHERE id='W5'", (), event="seed")
        assert tools._self_started(ctx, "W5") is True
    finally:
        ctx.store.close()
