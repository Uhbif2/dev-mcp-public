"""result / watch / decisions / старые тревоги / сводка для SessionStart — всё только читает реестр."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fleet_console_views", REPO / "tools" / "fleet.py")
assert SPEC and SPEC.loader
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)

BRIEF_SPEC = importlib.util.spec_from_file_location("fleet_brief", REPO / "tools" / "fleet_session_brief.py")
assert BRIEF_SPEC and BRIEF_SPEC.loader
brief_mod = importlib.util.module_from_spec(BRIEF_SPEC)
BRIEF_SPEC.loader.exec_module(brief_mod)


def ago(**kw: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    fleet._create_schema(conn)
    conn.execute("INSERT INTO projects(name,root) VALUES('P','C:/tmp/P')")
    return conn


def add_task(conn: sqlite3.Connection, tz: str, state: str, *, needs: list[str] | None = None,
             acceptance: list[str] | None = None, title: str = "", project: str = "P") -> None:
    now = fleet._now()
    conn.execute(
        "INSERT INTO tasks(project,tz,title,state,needs,acceptance,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (project, tz, title or tz, state, json.dumps(needs or []), json.dumps(acceptance or []), now, now),
    )


def add_event(conn: sqlite3.Connection, kind: str, tz: str | None, *, ts: str | None = None, worker: str | None = "W1",
              payload: dict | None = None, rc: int | None = None, path: str = "", project: str | None = "P") -> int:
    cur = conn.execute(
        "INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload) VALUES(?,?,?,?,?,'',?,?,?)",
        (ts or fleet._now(), kind, worker, project, tz, path, rc, json.dumps(payload or {}, ensure_ascii=False)),
    )
    return int(cur.lastrowid)


# --- result ---------------------------------------------------------------------------

def chain_conn() -> sqlite3.Connection:
    conn = make_conn()
    add_task(conn, "TZ-7", "accepted", acceptance=["py -3 -m pytest tests -q"], title="TZ-7 — сделать X")
    add_task(conn, "review:TZ-7", "accepted")
    add_task(conn, "fix:TZ-7", "accepted", acceptance=["py -3 -m pytest tests -q"])
    add_task(conn, "review:fix:TZ-7", "accepted")
    add_task(conn, "TZ-8", "ready")  # чужая задача не должна попасть в цепочку
    add_event(conn, "take", "TZ-7", ts=ago(hours=3), worker="W1")
    add_event(conn, "write", "TZ-7", ts=ago(hours=3), path="c:/p/a.py")
    add_event(conn, "submit", "TZ-7", ts=ago(hours=2, minutes=50), payload={"note": "сделано, тесты зелёные"})
    add_event(conn, "take", "review:TZ-7", ts=ago(hours=2, minutes=40), worker="W2")
    add_event(conn, "run", "review:TZ-7", ts=ago(hours=2, minutes=35), worker="W2", rc=1,
              payload={"argv": ["py", "-3", "-m", "pytest", "tests", "-q"]})
    add_event(conn, "reject", "TZ-7", ts=ago(hours=2, minutes=30), worker="W2",
              payload={"note": "ПОЛНАЯ ЗАМЕТКА РЕВЬЮЕРА: падает test_a", "fix_task": "fix:TZ-7"})
    add_event(conn, "take", "fix:TZ-7", ts=ago(hours=2), worker="W3")
    add_event(conn, "write", "fix:TZ-7", ts=ago(hours=2), worker="W3", path="c:/p/b.py")
    add_event(conn, "write", "TZ-8", worker="W9", path="c:/p/other.py")
    add_event(conn, "submit", "fix:TZ-7", ts=ago(hours=1, minutes=30), worker="W3", payload={"note": "починил test_a"})
    add_event(conn, "run", "review:fix:TZ-7", ts=ago(hours=1, minutes=10), worker="W4", rc=0,
              payload={"argv": ["py", "-3", "-m", "pytest", "tests", "-q"]})
    add_event(conn, "accept", "fix:TZ-7", ts=ago(hours=1), worker="W4", payload={"note": "принято: rc=0"})
    return conn


def test_result_shows_whole_chain_verdicts_acceptance_and_files() -> None:
    conn = chain_conn()
    text = fleet.task_result(conn, "P/fix:TZ-7")  # любой член цепочки ведёт к одному отчёту
    lines = text.splitlines()
    assert lines[0] == "P/TZ-7 — TZ-7 — сделать X"
    assert "цепочка: TZ-7 (accepted) → review:TZ-7" in lines[1]
    assert "время: 2ч" in text and "держит сейчас: никто" in text
    assert lines[4].startswith("ИТОГ: accept fix:TZ-7 W4") and "принято: rc=0" in lines[4]
    assert "ПОЛНАЯ ЗАМЕТКА РЕВЬЮЕРА: падает test_a" in text
    assert "сделано, тесты зелёные" in text and "починил test_a" in text
    # приёмка берёт rc последнего запуска той же команды, а не первого
    assert "rc=0  py -3 -m pytest tests -q (review:fix:TZ-7 W4" in text
    assert "c:/p/a.py" in text and "c:/p/b.py" in text and "other.py" not in text
    order = [text.index(s) for s in ("submit TZ-7", "reject TZ-7", "take fix:TZ-7", "submit fix:TZ-7")]
    assert order == sorted(order)


def test_result_reports_holder_and_unrun_acceptance_and_cap() -> None:
    conn = make_conn()
    add_task(conn, "TZ-1", "taken", acceptance=["make check"])
    now = fleet._now()
    conn.execute("INSERT INTO workers(id,first_seen,last_seen) VALUES('W5',?,?)", (now, now))
    conn.execute("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('P','TZ-1','W5','active',?)", (now,))
    add_event(conn, "take", "TZ-1", worker="W5")
    for i in range(60):
        add_event(conn, "block", "TZ-1", payload={"reason": f"причина {i} " + "x" * 400})
    text = fleet.task_result(conn, "TZ-1")
    assert "держит сейчас: W5 (TZ-1" in text
    assert "rc=—  make check (не запускалась)" in text
    assert "вердикта ещё нет" in text and "СЕЙЧАС: block TZ-1" in text
    assert len(text) <= fleet.RESULT_CAP


def test_result_unknown_task_raises() -> None:
    with pytest.raises(ValueError):
        fleet.task_result(make_conn(), "P/TZ-404")


def test_base_tz_strips_review_fix_and_rounds() -> None:
    assert fleet._base_tz("review:fix:TZ-187:2:2") == "TZ-187"
    assert fleet._base_tz("fix:request:ab12cd34") == "request:ab12cd34"
    assert fleet._base_tz("request:12345678") == "request:12345678"
    assert fleet._base_tz("fix:styk:4") == "styk:4"  # fix стыка — в цепочке стыка (07.10.2026)


# --- watch ----------------------------------------------------------------------------

def test_watch_lines_filter_and_format() -> None:
    conn = make_conn()
    add_event(conn, "read", "TZ-1")
    add_event(conn, "reject", "TZ-1", worker="W12", payload={"note": "плохо " + "я" * 300})
    add_event(conn, "park", None, payload={"outcome": "timeout"})
    add_event(conn, "park", "TZ-2", payload={"outcome": "task"})
    add_event(conn, "gate", None, worker=None, payload={"rc": 1, "dur_s": 12, "summary": "3 failed"})
    add_event(conn, "integrate", "TZ-1", payload={"commit": "abcdef1234567890", "paths": ["a", "b"]})
    add_event(conn, "accept", "TZ-9", project="Q")
    lines, last = fleet.watch_events(conn)
    assert last == 7
    assert len(lines) == 5
    assert lines[0].split(" ", 1)[1].startswith("reject P/TZ-1 W12 — плохо")
    assert len(lines[0].split(" — ", 1)[1]) <= 160
    assert "park P/TZ-2" in lines[1]
    assert lines[2].endswith("gate P - — rc=1 12с 3 failed")
    assert "коммит abcdef123456, путей 2" in lines[3]
    only_p, _ = fleet.watch_events(conn, project="P", after_id=2)
    assert all(" P/" in line or " P " in line for line in only_p) and len(only_p) == 3
    fresh, _ = fleet.watch_events(conn, since=ago(minutes=-5))
    assert fresh == []


def test_watch_stream_starts_from_now_and_prints_new(tmp_path: Path, monkeypatch, capsys) -> None:
    db = tmp_path / "fleet.sqlite"
    conn = sqlite3.connect(db)
    fleet._create_schema(conn)
    conn.execute("INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
                 (fleet._now(), "accept", "P", "TZ-OLD", "{}"))
    conn.commit()
    calls = {"n": 0}

    def fake_sleep(_s: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            conn.execute("INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
                         (fleet._now(), "reject", "P", "TZ-NEW", json.dumps({"note": "новое"})))
            conn.commit()
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(fleet.time, "sleep", fake_sleep)
    assert fleet._cmd_watch(db, True, None, None) == 0
    out = capsys.readouterr().out
    assert "TZ-OLD" not in out and "reject P/TZ-NEW - — новое" in out
    conn.close()


# --- decisions ------------------------------------------------------------------------

def decisions_conn() -> sqlite3.Connection:
    conn = make_conn()
    # Корень A (frozen) держит цепочку B → C → D; корень X (block) — одну Y.
    add_task(conn, "TZ-A", "frozen", title="телефон")
    add_task(conn, "TZ-B", "ready", needs=["TZ-A"])
    add_task(conn, "TZ-C", "ready", needs=["TZ-B"])
    add_task(conn, "TZ-D", "ready", needs=["TZ-C", "TZ-OK"])
    add_task(conn, "TZ-OK", "accepted")
    add_task(conn, "TZ-X", "taken")
    add_task(conn, "TZ-Y", "ready", needs=["TZ-X"])
    add_event(conn, "freeze", "TZ-A", ts=ago(days=2), worker=None, payload={"previous_state": "ready"})
    add_event(conn, "block", "TZ-A", ts=ago(days=1), payload={"reason": "нужен телефон владельца"})
    add_event(conn, "block", "TZ-X", ts=ago(hours=3), payload={"reason": "бюджет: нужно одобрение трат"})
    # Сданная задача без ревью больше часа; и сданная с живым fix — не владельцу.
    add_task(conn, "TZ-S", "submitted")
    add_event(conn, "submit", "TZ-S", ts=ago(hours=2), payload={"note": "готово"})
    add_task(conn, "TZ-R", "submitted")
    add_task(conn, "fix:TZ-R", "ready")
    add_event(conn, "submit", "TZ-R", ts=ago(hours=5))
    # Эскалация, по которой уже принято, — не решение.
    add_task(conn, "review:TZ-Z", "accepted")
    add_event(conn, "escalate", "review:TZ-Z", payload={"note": "старое"})
    return conn


def test_decisions_rank_by_transitive_blocked_work() -> None:
    items = fleet.decisions(decisions_conn())
    keys = [(it["tz"], it["blocks"]) for it in items]
    assert keys == [("TZ-A", 3), ("TZ-X", 1), ("TZ-S", 0)]
    top = items[0]
    assert top["why"] == "заморожена" and top["reason"] == "нужен телефон владельца"
    assert top["blocks_list"] == ["TZ-B", "TZ-C", "TZ-D"]
    assert items[1]["why"] == "заблокирована"
    assert items[2]["why"].startswith("сдана")


def test_decisions_fix_counts_its_base_and_auto_block_edges() -> None:
    conn = make_conn()
    add_task(conn, "TZ-62", "submitted")
    add_task(conn, "fix:TZ-62", "frozen")
    add_task(conn, "TZ-100", "ready", needs=["TZ-62"])
    add_task(conn, "request:abcd1234", "frozen")
    add_task(conn, "TZ-303", "frozen")
    add_event(conn, "block", "fix:TZ-62", payload={"reason": "мало памяти хоста"})
    add_event(conn, "block", "TZ-303", payload={"reason": "автоблок: ждём request:abcd1234; пересобрать"})
    items = {it["tz"]: it for it in fleet.decisions(conn)}
    assert "TZ-62" not in items  # сданная с замороженным fix — это один пункт, fix
    assert items["fix:TZ-62"]["blocks_list"] == ["TZ-100", "TZ-62"]
    assert items["request:abcd1234"]["blocks_list"] == ["TZ-303"]


def test_decisions_red_gate_and_queue_summary() -> None:
    conn = decisions_conn()
    add_event(conn, "gate", None, worker=None, rc=0, payload={"rc": 1, "summary": "старый красный"})
    add_event(conn, "gate", None, worker=None, payload={"rc": 2, "summary": "pytest 3 failed"})
    items = fleet.decisions(conn)
    gate = next(it for it in items if it["tz"] == "(гейт)")
    assert gate["reason"].startswith("rc=2") and "pytest 3 failed" in gate["reason"]
    assert len(fleet._red_gates(conn)) == 1
    add_event(conn, "gate", None, worker=None, payload={"rc": 0})
    assert fleet._red_gates(conn) == []
    summary = fleet.queue_summary(conn)
    assert "ready 5, из них ждут непринятых needs 4, можно брать 1" in summary
    lines = fleet.format_decisions(fleet.decisions(conn), 2)
    assert lines[0].startswith("1. P/TZ-A — телефон")
    assert "одни эти ворота пустят 3 задачи, нужны для 3 задач: TZ-B, TZ-C, TZ-D" in lines[1]


# --- старые тревоги -------------------------------------------------------------------

def test_default_report_hides_alarms_older_than_a_day() -> None:
    items = [("БЕЗ ACK m1 → W1: 4д", ago(days=4)), ("ЧУЖОЙ ПУТЬ P/TZ-1 W2: x", ago(hours=1)),
             ("ЖДЁТ ПРИЁМКИ P/TZ-2: 3д", None)]
    shown, hidden = fleet.split_alarms(items, show_all=False)
    assert shown == ["ЧУЖОЙ ПУТЬ P/TZ-1 W2: x", "ЖДЁТ ПРИЁМКИ P/TZ-2: 3д"] and hidden == 1
    shown, hidden = fleet.split_alarms(items, show_all=True)
    assert len(shown) == 3 and hidden == 0


def test_main_default_prints_hidden_summary(tmp_path: Path, monkeypatch, capsys) -> None:
    db = tmp_path / "fleet.sqlite"
    conn = sqlite3.connect(db)
    fleet._create_schema(conn)
    conn.execute("INSERT INTO events(ts,kind,worker_id,project,tz,path_norm,payload) VALUES(?,?,?,?,?,?,?)",
                 (ago(days=3), "deny", "W1", "P", "TZ-1", "c:/x", json.dumps({"code": "FLEET_FOREIGN_PATH"})))
    conn.commit()
    conn.close()
    monkeypatch.setattr(fleet, "platform_line", lambda *a, **k: "платформа: тест")
    assert fleet.main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "! ЧУЖОЙ ПУТЬ" not in out and "скрыто 1 старых тревог" in out
    assert fleet.main(["--db", str(db), "--all"]) == 0
    assert "! ЧУЖОЙ ПУТЬ P/TZ-1 W1: c:/x" in capsys.readouterr().out


def test_main_decisions_and_result_commands(tmp_path: Path, capsys) -> None:
    db = tmp_path / "fleet.sqlite"
    disk = sqlite3.connect(db)
    source = decisions_conn()
    source.commit()
    source.backup(disk)
    disk.close()
    assert fleet.main(["--db", str(db), "decisions", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "ждут владельца: 3" in out and "1. P/TZ-A" in out and "2. " not in out
    assert fleet.main(["--db", str(db), "result", "P/TZ-A"]) == 0
    assert "P/TZ-A — телефон" in capsys.readouterr().out
    assert fleet.main(["--db", str(db), "result", "P/TZ-404"]) == 2


# --- SessionStart-сводка --------------------------------------------------------------

def test_session_brief_is_short_and_never_raises(tmp_path: Path, monkeypatch, capsys) -> None:
    db = tmp_path / "fleet.sqlite"
    disk = sqlite3.connect(db)
    source = decisions_conn()
    source.commit()
    source.backup(disk)
    disk.close()
    real_load = brief_mod._load_console

    def load() -> object:
        module = real_load()
        module.platform_line = lambda *a, **k: "платформа: тест"
        return module

    monkeypatch.setattr(brief_mod, "_load_console", load)
    lines, sig = brief_mod.brief("*", db)
    # Платформа не «жива» по строке теста — значит, её показываем; решения — только те,
    # что правда ждут владельца (держат хотя бы одну задачу), не больше трёх.
    assert lines[0] == "платформа: тест"
    assert any(line.startswith("P: стоят") and "нужны ворота" in line for line in lines)
    assert any(line.startswith("1. P/TZ-A") for line in lines)
    assert not any("TZ-S" in line for line in lines)  # сданная без ревью — не ворота владельца
    assert len(lines) <= brief_mod.MAX_LINES and sig
    # Чужой проект: решений нет, платформа в порядке — полная тишина.
    healthy = "платформа: dev:8766 жив · core:8765 жив · шлюз ок"

    def load_ok() -> object:
        module = real_load()
        module.platform_line = lambda *a, **k: healthy
        return module

    monkeypatch.setattr(brief_mod, "_load_console", load_ok)
    assert brief_mod.brief("Нет такого", db)[0] == []

    monkeypatch.setattr(brief_mod, "brief", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("нет реестра")))
    assert brief_mod.main("*", dedup=False) == 0
    assert "сводка недоступна — RuntimeError: нет реестра" in capsys.readouterr().out
    # В хуке ошибка не шумит в каждую сессию.
    assert brief_mod.main("*", dedup=True) == 0
    assert capsys.readouterr().out == ""


def test_stalls_name_every_gate_the_chain_needs() -> None:
    """27.09: телефон для TZ-78 дали, а цепочка встала на покупке входа TZ-175 и на fix:TZ-62.
    Пульт обязан сразу назвать ВСЕ ворота, а звено цепочки не выдавать за решение владельца."""
    conn = make_conn()
    add_task(conn, "TZ-175", "frozen")
    add_task(conn, "fix:TZ-62", "frozen")
    add_task(conn, "TZ-62", "submitted")
    add_task(conn, "request:aaaa1111", "frozen")
    add_task(conn, "TZ-78", "frozen")
    add_task(conn, "TZ-61", "ready", needs=["TZ-78", "TZ-62"])
    add_task(conn, "TZ-66", "ready", needs=["TZ-61"])
    add_task(conn, "TZ-160", "ready", needs=["TZ-62"])
    add_event(conn, "block", "request:aaaa1111",
              payload={"reason": "blocked: нужен owner-approved ingress (TZ-175/spend gate)"})
    add_event(conn, "block", "TZ-78", payload={"reason": "автоблок: ждём request:aaaa1111; dataplane"})
    add_event(conn, "freeze", "TZ-175", payload={"reason": "owner/spend gate"})
    add_event(conn, "freeze", "fix:TZ-62", payload={"reason": "host memory"})
    clusters = {tuple(c["gates"]): c["tasks"] for c in fleet.stalls(conn)}
    assert clusters[("TZ-175", "fix:TZ-62")] == ["TZ-61", "TZ-66"]
    assert clusters[("fix:TZ-62",)] == ["TZ-160", "TZ-62"] or clusters[("fix:TZ-62",)] == ["TZ-160"]
    items = {it["tz"]: it for it in fleet.decisions(conn)}
    assert items["TZ-175"]["gate"] and items["TZ-175"]["frees"] == 0 and items["TZ-175"]["needed_for"] == 2
    assert not items["TZ-78"]["gate"]
    head = fleet.format_stalls(fleet.stalls(conn))
    assert any("нужны ВСЕ ворота: TZ-175, fix:TZ-62" in line for line in head)
