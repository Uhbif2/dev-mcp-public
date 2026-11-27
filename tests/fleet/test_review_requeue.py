"""№118 (21.11.2026): reject независимого ревью возвращает ТУ ЖЕ ТЗ в ready, без fix:/генераторов."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import briefing, generators, goals, review  # noqa: E402
from dev_mcp.fleet.queue import submit, take_next  # noqa: E402
from dev_mcp.fleet.review import decide, open_review  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402

CMD = "python -m pytest tests/unit.py -q"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


GATE = {"open": False}


@pytest.fixture(autouse=True)
def generators_off(monkeypatch):
    # Боевой режим: глобальные генераторы выключены (conftest по умолчанию включает их для старых тестов).
    # GATE открывается только на вставку исходных задач теста.
    GATE["open"] = False
    monkeypatch.setattr(generators, "enabled", lambda: GATE["open"])
    monkeypatch.setattr(goals, "_load_doc", lambda: {})


@pytest.fixture
def store(tmp_path):
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    with s.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("P", str(tmp_path / "P")), event={"kind": "test_setup"})
        for worker in ("W1", "W2", "W3", "W4"):
            stamp = now()
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
                      "VALUES(?,?,?,?,?,'')", (worker, worker, "chatgpt", stamp, stamp),
                      event={"kind": "test_setup", "worker_id": worker})
    yield s
    s.close()


def add_task(store, tz, *, state="ready", kind="work", needs=(), body=""):
    stamp = now()
    GATE["open"] = True
    try:
        _insert(store, tz, state, kind, needs, body, stamp)
    finally:
        GATE["open"] = False


def _insert(store, tz, state, kind, needs, body, stamp):
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P", tz, tz, state, 5, kind, f"ToDo/{tz}.md", "sha", json.dumps(list(needs)),
             json.dumps(["src/work.py"]), "[]", json.dumps([CMD]), "W1", body, stamp, stamp),
            event={"kind": "test_setup", "project": "P", "tz": tz})


def claim(store, tz, worker="W1"):
    with store.tx() as tx:
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES(?,?,?,'active',?)",
                  ("P", tz, worker, now()), event={"kind": "test_setup", "worker_id": worker,
                                                   "project": "P", "tz": tz})


def seal(store, tz, revision):
    with store.tx() as tx:
        tx.record_event(kind="claim_submission", project="P", tz=tz, payload={"revision": revision})


def row(store, tz):
    with store.read() as tx:
        return tx.one("SELECT * FROM tasks WHERE project='P' AND tz=?", (tz,))


def tasks(store):
    with store.read() as tx:
        return {r["tz"]: r["state"] for r in tx.query("SELECT tz,state FROM tasks WHERE project='P'")}


def rejects(store, tz):
    with store.read() as tx:
        return [json.loads(r["payload"]) for r in tx.query(
            "SELECT payload FROM events WHERE project='P' AND tz=? AND kind='reject' ORDER BY id", (tz,))]


def reject_by(store, tz, reviewer, note):
    review_tz = open_review(store, "P", tz)
    taken = take_next(store, reviewer, "P")
    assert taken is not None and taken.tz == review_tz
    decide(store, reviewer, "reject", note)
    return review_tz


def test_reject_requeues_same_task_then_accept_unblocks_downstream(store):
    add_task(store, "TZ-1", state="submitted")
    add_task(store, "TZ-2", needs=("TZ-1",))
    claim(store, "TZ-1")
    review_tz = reject_by(store, "TZ-1", "W2", "Нет проверки пустого ввода: test_empty падает")

    states = tasks(store)
    assert states["TZ-1"] == "ready" and states[review_tz] == "accepted"
    assert not [tz for tz in states if tz.startswith(("fix:", "request:", "triage:"))]
    payload = rejects(store, "TZ-1")[-1]
    assert payload["requeue"] == 1 and payload["attempt"] == 1 and not payload["requeue_exhausted"]
    assert payload["fix_task"] is None
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE project='P' AND tz='TZ-1' AND state='active'") == 0

    again = take_next(store, "W3", "P")
    assert again is not None and again.tz == "TZ-1"
    text = briefing.briefing(store, "W3", again)
    assert "Нет проверки пустого ввода: test_empty падает" in text
    assert "ревьюер W2" in text and "reject #" in text

    submit(store, "W3", "исправил пустой ввод")
    second_review = open_review(store, "P", "TZ-1")
    taken = take_next(store, "W4", "P")
    assert taken is not None and taken.tz == second_review
    with store.tx() as tx:
        tx.record_event(kind="run", worker_id="W4", tool="run", path_norm="", rc=0,
                        payload={"argv": ["python", "-m", "pytest", "tests/unit.py", "-q"]})
    decide(store, "W4", "accept", "пустой ввод проверен, test_empty проходит")
    assert tasks(store)["TZ-1"] == "accepted"
    nxt = take_next(store, "W2", "P")
    assert nxt is not None and nxt.tz == "TZ-2"
    # Принятая задача больше не показывает старый отказ как текущую причину.
    assert "test_empty падает" not in briefing.briefing(store, "W3", briefing._task_from_row(row(store, "TZ-1")))


def test_two_rejects_keep_same_id_history_and_single_claim(store):
    add_task(store, "TZ-1", state="submitted")
    claim(store, "TZ-1")
    seal(store, "TZ-1", "a" * 40)
    reject_by(store, "TZ-1", "W2", "первый отказ")
    taken = take_next(store, "W3", "P")
    assert taken.tz == "TZ-1"
    submit(store, "W3", "попытка 2")
    seal(store, "TZ-1", "b" * 40)
    reject_by(store, "TZ-1", "W4", "второй отказ")
    assert tasks(store)["TZ-1"] == "ready"
    assert [p["attempt"] for p in rejects(store, "TZ-1")] == [1, 2]
    assert take_next(store, "W2", "P") is None   # 25.11: прежний ревьюер возвращённую задачу не берёт (№118)
    add_worker(store, "W5")
    taken = take_next(store, "W5", "P")
    assert taken.tz == "TZ-1"
    text = briefing.briefing(store, "W5", taken)
    assert text.index("второй отказ") < text.index("первый отказ")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE project='P' AND tz='TZ-1' AND state='active'") == 1
    assert len(tasks(store)) == 3  # TZ-1 и два закрытых review:, ни одной новой работы


def test_same_rejected_revision_resubmitted_stops_without_new_tasks(store):
    add_task(store, "TZ-1", state="submitted")
    claim(store, "TZ-1")
    seal(store, "TZ-1", "c" * 40)
    reject_by(store, "TZ-1", "W2", "отказ")
    take_next(store, "W3", "P")
    submit(store, "W3", "без изменений")
    seal(store, "TZ-1", "c" * 40)
    reject_by(store, "TZ-1", "W4", "та же ошибка")
    assert tasks(store)["TZ-1"] == "rejected"
    last = rejects(store, "TZ-1")[-1]
    assert last["requeue_exhausted"] and "та же ревизия" in last["exhausted_reason"]


def add_worker(store, worker):
    stamp = now()
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
                  "VALUES(?,?,?,?,?,'')", (worker, worker, "chatgpt", stamp, stamp),
                  event={"kind": "test_setup", "worker_id": worker})


def test_previous_attempt_authors_do_not_retake_but_new_agent_does(store):
    add_task(store, "TZ-1", state="submitted")
    claim(store, "TZ-1")
    reject_by(store, "TZ-1", "W2", "отказ")
    assert take_next(store, "W1", "P") is None      # автор отвергнутой попытки
    assert take_next(store, "W3", "P").tz == "TZ-1"  # новый агент


def test_requeue_limit_parks_task_in_rejected(store):
    add_task(store, "TZ-1", state="submitted")
    claim(store, "TZ-1")
    for i in range(review.MAX_REQUEUE + 1):
        seal(store, "TZ-1", f"{i:040x}")
        reviewer = f"WR{i}"
        add_worker(store, reviewer)
        reject_by(store, "TZ-1", reviewer, f"отказ {i}")
        if i < review.MAX_REQUEUE:
            assert tasks(store)["TZ-1"] == "ready"
            worker = f"WN{i}"
            add_worker(store, worker)
            assert take_next(store, worker, "P").tz == "TZ-1"
            submit(store, worker, f"попытка {i + 2}")
    assert tasks(store)["TZ-1"] == "rejected"
    assert rejects(store, "TZ-1")[-1]["requeue_exhausted"]
    assert not [tz for tz in tasks(store) if tz.startswith("fix:")]


def test_historical_fix_is_requeued_itself_and_root_stays_submitted(store):
    add_task(store, "TZ-1", state="submitted")
    add_task(store, "fix:TZ-1", state="submitted", kind="fix",
             body="[fleet-fix] source=P/TZ-1\nЧто исправить: стык")
    claim(store, "fix:TZ-1")
    reject_by(store, "fix:TZ-1", "W2", "fix не закрыл стык")
    states = tasks(store)
    assert states["fix:TZ-1"] == "ready" and states["TZ-1"] == "submitted"
    assert "fix:TZ-1:2" not in states


def test_requeue_switch_off_restores_old_rejected(store, monkeypatch):
    monkeypatch.setattr(goals, "_load_doc", lambda: {"platform": {"review_requeue": {"enabled": False}}})
    add_task(store, "TZ-1", state="submitted")
    claim(store, "TZ-1")
    reject_by(store, "TZ-1", "W2", "отказ")
    assert tasks(store)["TZ-1"] == "rejected"
    assert "requeue" not in rejects(store, "TZ-1")[-1]


def test_generators_on_keeps_fix_path(store, monkeypatch):
    add_task(store, "TZ-1", state="submitted")
    monkeypatch.setattr(generators, "enabled", lambda: True)
    claim(store, "TZ-1")
    reject_by(store, "TZ-1", "W2", "отказ")
    states = tasks(store)
    assert states["TZ-1"] == "submitted" and states["fix:TZ-1"] == "ready"


def test_store_gate_still_blocks_auto_inserts_with_requeue_on(store):
    with store.tx() as tx:
        sql = "INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES('P',?,'ready','t','t')"
        tx.change(sql, ("auto",), event={"kind": "triage_open"})
        tx.change(sql, ("auto2",), event={"kind": "reject"})
    assert "auto" not in tasks(store) and "auto2" not in tasks(store)


def test_rejecting_reviewer_does_not_retake_requeued_task(store):
    """№118: прежние ревьюеры возвращённой задачи её не получают (живой TZ-511, 24.11.2026)."""
    add_task(store, "TZ-1", state="submitted")
    claim(store, "TZ-1")
    reject_by(store, "TZ-1", "W2", "отказ")
    assert take_next(store, "W2", "P") is None        # отклонивший ревьюер
    assert take_next(store, "W3", "P").tz == "TZ-1"   # новый агент
