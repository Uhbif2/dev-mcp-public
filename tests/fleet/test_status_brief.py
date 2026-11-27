"""Итог статуса флота для агентов (20.11.2026, №116 п. 2).

Регрессия: агент с ролью владельца по флагам fleet_list (autostart=off, extension=stale,
runnable=0, генераторы выключены) решал, что флот стоит, и просил включить Supervisor/Autostart
и генераторы. Итог обязан сказать, работает ли флот, и не просить включать выключенное намеренно.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import status_brief as sb  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
BROWSER_OK = {"autostart": False, "halt": None, "by": "fleet.cmd", "at": "2026-11-16T22:00:00+00:00",
              "browser_ready": True, "action": "idle", "runnable": 0, "ext_age": 20,
              "ext_version": "0.19.63", "stale_s": 90}


def _db(tmp_path: Path, tasks: list[tuple[str, str]] = (), claims: int = 0) -> tuple[Path, sqlite3.Connection]:
    db = tmp_path / "repo" / "data" / "fleet" / "fleet.sqlite"
    db.parent.mkdir(parents=True)
    store = Store.open(str(db))
    stamp = NOW.isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P','C:/P',1,1)",
                  (), event={"kind": "test_setup"})
        for tz, state in tasks:
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                "resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES('P',?,?,?,5,'work','','','[]','[]','[]','[]',NULL,'b',?,?)",
                (tz, tz, state, stamp, stamp), event={"kind": "test_setup"})
        for n in range(claims):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,'chatgpt',?,?)",
                      (f"W{n}", f"l{n}", stamp, stamp), event={"kind": "test_setup"})
            tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('P','TZ-T',?,'active',?)",
                      (f"W{n}", stamp), event={"kind": "test_setup"})
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return db, conn


def _brief(tmp_path: Path, **kw) -> list[str]:
    db, conn = _db(tmp_path, kw.pop("tasks", ()), kw.pop("claims", 0))
    try:
        return sb.brief_lines(conn, db, tmp_path, now=NOW, actors=kw.pop("actors", {}),
                              browser=kw.pop("browser", BROWSER_OK),
                              generators_on=kw.pop("generators_on", False),
                              quick_open=kw.pop("quick_open", (0, 0)))
    finally:
        conn.close()


def test_idle_fleet_is_not_broken_and_flags_are_intentional(tmp_path: Path) -> None:
    lines = _brief(tmp_path)
    text = "\n".join(lines)
    assert lines[0].startswith("ИТОГ: флот исправен и простаивает") and "не поломка" in lines[0]
    # 24.11.2026: агенты пересказывали «выкл — намеренно» как «создание задач отключено» — это ложь.
    assert "Регистрацию ТЗ это НЕ запрещает" in text and "fleet_owner bootstrap" in text
    assert "autostart=off (поставил fleet.cmd, 16.11 22:00 UTC)" in text and "Сейчас не мешает" in text
    assert "0 при пустой очереди — норма" in text
    assert lines[-1].startswith("Не проси включать генераторы, autostart или Supervisor")


def test_working_fleet_counts_agents_by_fresh_actions(tmp_path: Path) -> None:
    actors = {"W10458": NOW.isoformat(), "Q12": NOW.isoformat(), "q13": NOW.isoformat()}
    lines = _brief(tmp_path, tasks=[("TZ-1", "ready"), ("TZ-2", "submitted"), ("TZ-T", "taken")],
                   claims=1, actors=actors, quick_open=(4, 0))
    assert lines[0].startswith("ИТОГ: флот работает — 3 агентов с предметными действиями за 15 мин")
    assert "(обычных 1, быстрых 2)" in lines[0]
    assert "можно брать 1" in lines[0] and "на ревью 1" in lines[0] and "захватов 1" in lines[0]
    assert "быстрых заданий открыто 4" in lines[0]


def test_halt_is_reported_first(tmp_path: Path) -> None:
    browser = {**BROWSER_OK, "halt": {"at": "2026-11-20T17:00:00+00:00", "reason": "suspicious activity"}}
    lines = _brief(tmp_path, actors={"W1": NOW.isoformat()}, browser=browser)
    assert lines[0].startswith("ИТОГ: флот ОСТАНОВЛЕН аварийно с 20.11 17:00 UTC: suspicious activity")
    assert "fleet.cmd browser resume" in lines[0]


def test_work_but_nobody_acting_points_to_browser_and_stale_extension_hurts(tmp_path: Path) -> None:
    browser = {**BROWSER_OK, "browser_ready": False, "ext_age": 1200}
    lines = _brief(tmp_path, tasks=[("TZ-1", "ready")], browser=browser)
    text = "\n".join(lines)
    assert lines[0].startswith("ИТОГ: работа есть (1 можно брать), но никто не действует")
    assert "Сейчас мешает быстрым агентам: браузер не готов" in text
    assert "extension=stale: последний отчёт 20 мин назад" in text and "мешает:" in text


def test_stale_extension_without_work_does_not_hurt(tmp_path: Path) -> None:
    lines = _brief(tmp_path, browser={**BROWSER_OK, "ext_age": 4000})
    assert any("extension=stale" in l and "сейчас не мешает" in l for l in lines)


def test_fresh_actors_reads_only_substantive_allowed_recent_calls(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    rows = [
        {"ts": (NOW - timedelta(minutes=2)).isoformat(), "tool": "run", "decision": "allow", "agent": "W1"},
        {"ts": (NOW - timedelta(minutes=1)).isoformat(), "tool": "fleet_list", "decision": "allow", "agent": "W2"},
        {"ts": (NOW - timedelta(minutes=1)).isoformat(), "tool": "edit_file", "decision": "deny", "agent": "W3"},
        {"ts": (NOW - timedelta(minutes=40)).isoformat(), "tool": "edit_file", "decision": "allow", "agent": "W4"},
        {"ts": (NOW - timedelta(minutes=3)).isoformat(), "tool": "fleet_quick", "decision": "allow", "agent": "Q5"},
        {"ts": (NOW - timedelta(minutes=3)).isoformat(), "tool": "read_file", "decision": "allow", "agent": "Q6"},
        {"ts": (NOW - timedelta(minutes=3)).isoformat(), "tool": "read_file", "decision": "allow", "agent": ""},
    ]
    audit.write_text("skipped partial line\n" + "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert sorted(sb.fresh_actors(audit, now=NOW)) == ["Q6", "W1"]
    assert sb.fresh_actors(tmp_path / "missing.jsonl", now=NOW) == {}


def test_safe_brief_never_raises(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    lines = list(sb.safe_brief_lines(conn, tmp_path / "x.sqlite", tmp_path))
    assert lines[0].startswith("ИТОГ недоступен")


def test_pult_status_prints_brief_right_after_platform_line(tmp_path: Path, monkeypatch, capsys) -> None:
    fleet_path = Path(__file__).resolve().parents[2] / "tools" / "fleet.py"
    spec = importlib.util.spec_from_file_location("fleet_console_brief_test", fleet_path)
    fleet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fleet)
    db, conn = _db(tmp_path)
    conn.close()
    monkeypatch.setattr(fleet, "platform_line", lambda *a, **k: "платформа: тест")
    monkeypatch.setattr(fleet, "chatgpt_owner_alert", lambda *a, **k: "")
    monkeypatch.setattr(fleet, "REPO", tmp_path / "repo")
    assert fleet.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out.splitlines()
    # Строка транспорта остаётся первой (тревога VPN — над ней), итог — сразу под ней.
    assert out[0] == "платформа: тест" and out[1].startswith("ИТОГ: "), out[:3]
