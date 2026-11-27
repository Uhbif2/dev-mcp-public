"""Read-only Memory Rules pilot; all memory_service/ambient retrieval is stubbed."""
from __future__ import annotations

import json
import hashlib
import threading
import tomllib
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import pytest

from dev_mcp.fleet import memory_rules, quick
from test_briefing import briefing, insert_task, setup_store, task


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    memory_rules._cache.clear()
    memory_rules._circuit_open_until = 0.0
    memory_rules._circuit_probe = False
    monkeypatch.setattr(memory_rules, "LOG_PATH", tmp_path / "memory-rules.jsonl")
    monkeypatch.setattr(memory_rules.goals, "_load_doc", lambda: {
        "platform": {"memory_rules": {
            "quick_claude": True, "quick_other": False, "fleet_default": False,
        }},
        "projects": {"Beta App": {"memory_rules": True}},
    })
    yield
    memory_rules._cache.clear()
    memory_rules._circuit_open_until = 0.0
    memory_rules._circuit_probe = False


def response(*rules):
    rows = [[f"id-{i}", "rul", 0, 0.95, "- [правило] " + item, "", None]
            for i, item in enumerate(rules)]
    return {"trace": rows + [["organic", "org", 0, 0.99, "- тайная органика", "", None]]}


def test_selected_rules_only_and_project_acl(monkeypatch):
    monkeypatch.setattr(memory_rules, "_read_ambient", lambda *_: {
        "trace": response(
            "Нельзя публиковать секреты. Когда: project: Beta App",
            "Правило чужого проекта. Проект: TSPU Bypass",
            "Не переписывать канон. Триггер: изменение документа",
        )["trace"],
        "context": "- [канон] не выводить\n- органика не выводить",
    })
    block = memory_rules.render("Beta App", "Исправить релиз", "review")
    assert "Нельзя публиковать секреты" in block
    assert "Не переписывать канон" in block
    assert "чужого проекта" not in block
    assert "органика" not in block
    assert len(block) <= 1500


def test_missing_daemon_is_explicit_and_short_retry_cache(monkeypatch):
    calls = []

    def offline(*_):
        calls.append(1)
        raise ConnectionRefusedError("no daemon")

    monkeypatch.setattr(memory_rules, "_read_ambient", offline)
    first = memory_rules.render("Beta App", "Fix", "executor")
    second = memory_rules.render("Beta App", "Fix", "executor")
    assert first == second
    assert "правила памяти недоступны (ConnectionRefusedError)" in first
    assert len(calls) == 1
    metrics = [json.loads(line) for line in memory_rules.LOG_PATH.read_text(encoding="utf-8").splitlines()]
    assert [row["unavailable"] for row in metrics] == [True, True]
    assert metrics[1]["cache_hit"] is True
    assert "Fix" not in memory_rules.LOG_PATH.read_text(encoding="utf-8")


def test_timeout(monkeypatch):
    class Never:
        cancelled = False

        def result(self, *, timeout):
            assert timeout == memory_rules.TIMEOUT_S
            raise FutureTimeout()

        def cancel(self):
            self.cancelled = True
            return True

    pending = Never()
    monkeypatch.setattr(memory_rules._pool, "submit", lambda *args: pending)
    block = memory_rules.render("Beta App", "timeout case", "lead")
    assert "правила памяти недоступны (таймаут recall" in block
    assert pending.cancelled


def test_shared_circuit_breaker_and_recovery(monkeypatch):
    calls = []

    def offline(*_):
        calls.append("offline")
        raise ConnectionRefusedError("database unavailable")

    monkeypatch.setattr(memory_rules, "_read_ambient", offline)
    first = memory_rules.render("Beta App", "first", "executor")
    second = memory_rules.render("Beta App", "different task", "executor")
    assert "ConnectionRefusedError" in first
    assert "circuit breaker" in second
    assert calls == ["offline"]  # a global cooldown, not a per-task 20-second wait
    assert memory_rules._circuit_open_until > memory_rules.time.monotonic()
    assert memory_rules.ERROR_TTL_S <= 60
    metrics = [json.loads(row) for row in memory_rules.LOG_PATH.read_text(encoding="utf-8").splitlines()]
    assert [(row["status"], row["source"]) for row in metrics] == [
        ("unavailable", "ambient"), ("unavailable", "circuit"),
    ]

    # A circuit opened by a failed backend allows exactly one recovery probe
    # after the cooldown; success re-enables other task lookups.
    memory_rules._circuit_open_until = memory_rules.time.monotonic() - 1
    monkeypatch.setattr(memory_rules, "_read_ambient",
                        lambda *_: response("Восстановлено"))
    assert "Восстановлено" in memory_rules.render("Beta App", "probe", "executor")
    assert memory_rules._circuit_open_until == 0.0
    assert "Восстановлено" in memory_rules.render("Beta App", "new task", "executor")


def test_explicit_degraded_graph_is_not_misreported_as_empty_success(monkeypatch):
    monkeypatch.setattr(memory_rules, "_read_ambient", lambda *_: {
        "search_status": "degraded",
        "trace": response("Правило из неполного поиска")["trace"],
    })
    block = memory_rules.render("Beta App", "degraded", "executor")
    assert "недоступны (RuntimeError)" in block
    assert "Правило из неполного поиска" not in block
    row = json.loads(memory_rules.LOG_PATH.read_text(encoding="utf-8").splitlines()[-1])
    assert row["failure_kind"] == "RuntimeError"
    assert row["source"] == "ambient"


def test_only_one_recovery_probe_runs_and_positive_cache_survives(monkeypatch):
    monkeypatch.setattr(memory_rules, "_read_ambient",
                        lambda *_: response("Сохранённый ответ"))
    assert "Сохранённый ответ" in memory_rules.render("Beta App", "known", "executor")

    def offline(*_):
        raise ConnectionRefusedError("database unavailable")

    monkeypatch.setattr(memory_rules, "_read_ambient", offline)
    assert "недоступны" in memory_rules.render("Beta App", "offline", "executor")
    # Previously cached successes remain useful while fresh searches fail.
    assert "Сохранённый ответ" in memory_rules.render("Beta App", "known", "executor")
    memory_rules._circuit_open_until = memory_rules.time.monotonic() - 1

    entered, release = threading.Event(), threading.Event()
    calls, outcomes = [], []

    def slow_probe(*_):
        calls.append(1)
        entered.set()
        assert release.wait(5), "test recovery probe was not released"
        return response("После пробы")

    monkeypatch.setattr(memory_rules, "_read_ambient", slow_probe)
    worker = threading.Thread(target=lambda: outcomes.append(
        memory_rules.render("Beta App", "probing", "executor")))
    worker.start()
    try:
        assert entered.wait(5)
        assert "circuit breaker" in memory_rules.render(
            "Beta App", "another uncached task", "executor")
        assert calls == [1]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(outcomes) == 1 and "После пробы" in outcomes[0]
    assert memory_rules._circuit_open_until == 0.0


def test_realistic_daemon_latency_budget_and_refresh_has_new_session(monkeypatch):
    calls = []

    def fetch(question, session):
        calls.append((question, session))
        return response("Проверяй реальный результат, а не только зелёный тест")

    monkeypatch.setattr(memory_rules, "_read_ambient", fetch)
    assert 18 <= memory_rules.TIMEOUT_S < 25  # loaded ambientd has taken 15-17 s
    task = "TZ-800: Android screenshot tests\n" + "- test " * 500
    first = memory_rules.render("Beta App", task, "executor")
    assert "Проверяй реальный результат" in first
    assert len(calls) == 1
    assert "\n" not in calls[0][0]
    assert len(calls[0][0]) <= 1100
    assert first == memory_rules.render("Beta App", task, "executor")
    assert len(calls) == 1
    key = ("Beta App", "executor", hashlib.sha256(task.encode()).hexdigest())
    expiry, status, items, reason = memory_rules._cache[key]
    memory_rules._cache[key] = (0, status, items, reason)
    refreshed = memory_rules.render("Beta App", task, "executor")
    assert "Проверяй реальный результат" in refreshed
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]  # ambientd must not suppress redelivery


def test_cache_ttl_key_and_metrics(monkeypatch):
    count = []

    def fetch(*args):
        count.append(args)
        return response("Всегда проверяй факт")

    monkeypatch.setattr(memory_rules, "_read_ambient", fetch)
    first = memory_rules.render("Beta App", "task one", "executor")
    assert first == memory_rules.render("Beta App", "task one", "executor")
    assert len(count) == 1
    assert "Всегда" in first
    memory_rules.render("Beta App", "task two", "executor")
    memory_rules.render("Beta App", "task one", "review")
    assert len(count) == 3
    assert len(memory_rules._cache) == 3
    # Force only this exact entry past its 600-second TTL; next read refreshes.
    key = ("Beta App", "executor", hashlib.sha256(b"task one").hexdigest())
    # A specific entry can expire while other task/role entries stay cached.
    expiry, status, items, reason = memory_rules._cache[key]
    memory_rules._cache[key] = (0, status, items, reason)
    memory_rules.render("Beta App", "task one", "executor")
    assert len(count) == 4
    metrics = [json.loads(x) for x in memory_rules.LOG_PATH.read_text(encoding="utf-8").splitlines()]
    assert metrics[1]["cache_hit"] and metrics[1]["latency_ms"] >= 0
    assert all(x["rules"] == 1 and not x["unavailable"] for x in metrics)


def test_six_items_and_1500_chars(monkeypatch):
    monkeypatch.setattr(memory_rules, "_read_ambient", lambda *_: response(
        *("Правило " + str(i) + ": " + "действуй безопасно " * 22 for i in range(10))))
    block = memory_rules.render("Beta App", "task", "lead")
    assert len(block) <= 1500
    assert block.count("\n  - ") <= 6
    assert "Правило 6:" not in block


def test_quick_claude_and_beta_app_and_disabled_default(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_rules, "_read_ambient", lambda *_: response("Не ломай конфигурацию"))
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude:abcdef", prompt="проверить", project="Other")
    output = quick.first_message(job)
    assert output.index("Правила владельца") < output.index("Задача:")
    assert "Не ломай конфигурацию" in output
    other = quick.create(book, owner="W42", prompt="проверить", project="Other")
    assert "Правила владельца" not in quick.first_message(other)
    bs = quick.create(book, owner="W42", prompt="проверить", project="Beta App")
    assert "Правила владельца" in quick.first_message(bs)


def test_briefing_for_executor_reviewer_and_lead(monkeypatch, tmp_path):
    from dataclasses import replace

    monkeypatch.setattr(memory_rules, "_read_ambient", lambda *_: response("Проверять целиком"))
    store = setup_store(tmp_path)
    for suffix, kind in (("TZ-1", "work"), ("review:TZ-2", "review"), ("TZ-LEAD", "work")):
        value = replace(task(suffix), project="Beta App", kind=kind)
        with store.tx() as tx:
            tx.change("INSERT OR IGNORE INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                      ("Beta App", str(tmp_path / "P")), event={"kind": "test_setup"})
        insert_task(store, value)
        output = briefing(store, "W1", value)
        assert "Правила владельца для этой задачи:" in output
        assert "Проверять целиком" in output


def test_live_config_enables_all_fleet_roles_and_other_quick_agents(monkeypatch):
    config = Path(__file__).resolve().parents[2] / "fleet-projects.toml"
    if not config.is_file():
        pytest.skip("owner's live fleet-projects.toml is absent (public installation)")
    raw = tomllib.loads(config.read_text(encoding="utf-8"))
    monkeypatch.setattr(memory_rules.goals, "_load_doc", lambda: raw)
    for role in ("lead", "executor", "review"):
        assert memory_rules.enabled("TSPU Bypass", role)
    assert memory_rules.enabled("TSPU Bypass", "quick", quick_owner="W123")
    assert memory_rules.enabled("unconfigured project", "quick", quick_owner="claude:abc")
    assert memory_rules.enabled("unconfigured project", "executor")


def test_keeper_pings_without_spawning_and_does_not_duplicate_listeners(monkeypatch, tmp_path):
    outcomes = []
    monkeypatch.setattr(memory_rules, "_keeper_log",
                        lambda status, **kwargs: outcomes.append(status))
    monkeypatch.setattr(memory_rules, "_start_ambient_daemon",
                        lambda *_: pytest.fail("must not spawn when listener exists"))
    monkeypatch.setattr(memory_rules, "_daemon_ping", lambda port: True)
    monkeypatch.setattr(memory_rules, "_daemon_listening", lambda port: False)
    assert memory_rules.daemon_keeper_tick(root=tmp_path, port=11437) == "alive"
    monkeypatch.setattr(memory_rules, "_daemon_ping", lambda port: False)
    monkeypatch.setattr(memory_rules, "_daemon_listening", lambda port: True)
    assert memory_rules.daemon_keeper_tick(root=tmp_path, port=11437) == "unresponsive"
    assert outcomes == ["unresponsive"]


def test_keeper_spawns_once_and_cools_down(monkeypatch, tmp_path):
    launches = []
    monkeypatch.setattr(memory_rules, "_daemon_ping", lambda port: False)
    monkeypatch.setattr(memory_rules, "_daemon_listening", lambda port: False)
    monkeypatch.setattr(memory_rules, "_start_ambient_daemon",
                        lambda root: launches.append(root) or 1234)
    monkeypatch.setattr(memory_rules, "_keeper_log", lambda *a, **kw: None)
    monkeypatch.setattr(memory_rules, "_keeper_next_attempt", 0)
    assert memory_rules.daemon_keeper_tick(root=tmp_path, port=11437) == "spawn_requested"
    assert memory_rules.daemon_keeper_tick(root=tmp_path, port=11437) == "cooldown"
    assert launches == [tmp_path]


def test_keeper_uses_trusted_hook_python_and_fixed_daemon(monkeypatch, tmp_path):
    root = tmp_path / "ambient-memory"
    (root / "hooks").mkdir(parents=True)
    (root / "ambientd.py").write_text("", encoding="utf-8")
    python = tmp_path / "memory_service" / "mcp_server" / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (root / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": {"UserPromptSubmit": [{"hooks": [{"command": str(python)}]}]},
    }), encoding="utf-8")
    calls = []

    class Started:
        pid = 4321

    monkeypatch.setattr(memory_rules.subprocess, "Popen",
                        lambda argv, **kwargs: calls.append((argv, kwargs)) or Started())
    assert memory_rules._start_ambient_daemon(root) == 4321
    assert Path(calls[0][0][0]) == python
    assert Path(calls[0][0][1]) == root / "ambientd.py"
    assert calls[0][1]["creationflags"] & 0x08000000  # no console window
    # A config file cannot convert the keeper into an arbitrary-command launcher.
    (root / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": {"UserPromptSubmit": [{"hooks": [{"command": str(tmp_path / "other.exe")}]}]},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="untrusted_ambient_interpreter"):
        memory_rules._start_ambient_daemon(root)
    assert len(calls) == 1


def test_keeper_stops_without_waiting_for_brief(monkeypatch):
    ticks = []
    stop = threading.Event()

    def tick():
        ticks.append(1)
        stop.set()

    monkeypatch.setattr(memory_rules, "daemon_keeper_tick", tick)
    memory_rules.run_daemon_keeper(stop, interval_s=0.01)
    assert ticks == [1]
