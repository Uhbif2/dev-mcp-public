"""End-to-end lead decision route, all model calls stubbed."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import inbox, lead_admin, lead_decision, obstacles  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402


@pytest.fixture()
def lead(tmp_path: Path):
    root = tmp_path / "Project"
    book = root / "ToDo" / "lead"
    book.mkdir(parents=True)
    (book / "LEDGER.json").write_text(
        json.dumps({"obligations": [{"id": "O-fast", "status": "unknown"}]}),
        encoding="utf-8",
    )
    (root / "GOAL.md").write_text("Цель проекта: повысить надёжность проверки.", encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)",
                  (str(root),), event="setup")
        tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W1',?,?)",
                  (now, now), event="setup")
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES('P','TZ-LEAD','lead','taken',5,'work','','','[]','[]','[]','[]',NULL,'',?,?)",
            (now, now), event="setup")
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P','TZ-LEAD','W1','active',?)", (now,), event="setup")
    return store, root


def params():
    return {
        "question": "Какую проверку выбрать для локального обратимого изменения?",
        "options": ["Прогнать узкий тест", "Прогнать полный набор тестов"],
        "obligations": ["O-fast"],
    }


def answer(context, questions):
    assert context["project"] == "P"
    assert "повысить надёжность" in context["goal"]
    assert questions["choice"]["criteria"]["v0"] == "Прогнать узкий тест"
    return {"owner_only": {"noul": 0.01},
            "choice": {"probabilities": {"v0": 0.91, "v1": 0.09}}}, "gemini-decision-1"


def test_judgment_to_persisted_decision_and_next_envelope(lead, monkeypatch):
    store, _ = lead
    monkeypatch.setattr(lead_decision, "_model_decide", answer)
    result = lead_admin.act(store, "W1", "ask", params())
    assert "Прогнать узкий тест" in result
    assert "model:gemini-decision-1" in result
    with store.read() as tx:
        row = tx.one("SELECT id,text,source,reversible,rollback FROM decisions")
        assert row["source"] == "model:gemini-decision-1"
        assert row["reversible"] == 1
        assert "fleet.cmd undo" in row["rollback"]
        envelope = tx.one("SELECT kind,body,to_worker FROM messages WHERE to_worker='W1'")
        assert envelope["kind"] == "lead_decision"
        assert "Прогнать узкий тест" in envelope["body"]
        assert tx.one("SELECT 1 FROM events WHERE kind='lead_decision_answered'")
    assert any(m.kind == "lead_decision" for m in inbox.pending_for(store, "W1"))
    assert "Прогнать узкий тест" in lead_admin.act(store, "W1", "ask", params())


def test_owner_only_is_never_sent_to_model(lead, monkeypatch):
    store, _ = lead

    def forbidden(*_args):
        raise AssertionError("model must not see owner-only data")

    monkeypatch.setattr(lead_decision, "_model_decide", forbidden)
    request = {**params(), "question": "Можно ли сменить архитектуру хранения проекта?",
               "owner_decision": True}
    result = lead_admin.act(store, "W1", "ask", request)
    assert "ждёт владельца" in result
    assert obstacles.open_items(store, "P")
    with store.read() as tx:
        assert tx.one("SELECT id FROM decisions") is None
        assert tx.one("SELECT 1 FROM events WHERE kind='lead_decision_pending'")


def test_ledger_owner_question_is_never_sent_to_model(lead, monkeypatch):
    store, root = lead
    (root / "ToDo" / "lead" / "LEDGER.json").write_text(
        json.dumps({"obligations": [{"id": "O-fast", "status": "owner_question"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(lead_decision, "_model_decide", lambda *_: pytest.fail("owner-only"))
    result = lead_admin.act(store, "W1", "ask", params())
    assert "ждёт владельца" in result
    assert len(obstacles.open_items(store, "P")) == 1


def test_model_refusal_leaves_owner_card_and_no_fake_answer(lead, monkeypatch):
    store, _ = lead
    monkeypatch.setattr(lead_decision, "_model_decide", lambda *_: (None, ""))
    result = lead_admin.act(store, "W1", "ask", params())
    assert "ждёт владельца" in result
    pending = obstacles.open_items(store, "P")
    assert len(pending) == 1 and pending[0]["obligations"] == ["O-fast"]
    second = lead_admin.act(store, "W1", "ask", {
        **params(), "question": "Можно ли выбрать другой вариант, если модель недоступна?"
    })
    assert "ждёт владельца" in second
    assert len(obstacles.open_items(store, "P")) == 2
    with store.read() as tx:
        assert tx.one("SELECT 1 FROM decisions") is None
        assert tx.one("SELECT COUNT(*) n FROM events WHERE kind='lead_decision_pending'")["n"] >= 1


def test_yes_no_question_without_options_gets_safe_binary_choices(lead, monkeypatch):
    store, _ = lead
    seen = []

    def model(context, questions):
        seen.append(questions["choice"]["criteria"])
        return ({"owner_only": {"noul": 0.0},
                 "choice": {"probabilities": {"v0": 0.8, "v1": 0.2}}}, "jev")

    monkeypatch.setattr(lead_decision, "_model_decide", model)
    result = lead_admin.act(store, "W1", "ask", {
        "question": "Можно ли выполнить этот локальный обратимый тестовый прогон?",
        "obligations": ["O-fast"],
    })
    assert "model:jev" in result
    assert seen[0]["v0"] == "Да, в пределах существующих полномочий"


def test_low_confidence_escalates_instead_of_guessing(lead, monkeypatch):
    store, _ = lead
    monkeypatch.setattr(
        lead_decision, "_model_decide",
        lambda *_: ({"owner_only": {"noul": 0.1},
                    "choice": {"probabilities": {"v0": 0.51, "v1": 0.49}}}, "jev"))
    assert "ждёт владельца" in lead_admin.act(store, "W1", "ask", params())
    assert len(obstacles.open_items(store, "P")) == 1


def test_owner_undo_is_durable_and_lead_is_notified(lead, monkeypatch):
    store, _ = lead
    monkeypatch.setattr(lead_decision, "_model_decide", answer)
    lead_admin.act(store, "W1", "ask", params())
    with store.read() as tx:
        ident = str(tx.one("SELECT id FROM decisions")["id"])
    assert "отменён владельцем" in lead_decision.undo(store, "P", ident)
    with pytest.raises(ValueError, match="уже отменено"):
        lead_decision.undo(store, "P", ident)
    assert "повторно модель не вызывается" in lead_admin.act(store, "W1", "ask", params())
    with store.read() as tx:
        revoke = tx.one("SELECT body FROM messages WHERE from_id='fleet.cmd'")
        assert "Не применять" in revoke["body"]
        assert tx.one("SELECT 1 FROM events WHERE kind='lead_decision_undo'")


def test_malformed_gemini_uses_jev_fallback(lead, monkeypatch):
    store, _ = lead
    from dev_mcp.fleet import overseer
    calls = []
    monkeypatch.setattr(overseer, "_relay_decide", lambda *_args, **_kwargs: {"choice": {}})

    def fallback(blocks, questions):
        calls.append((blocks, questions))
        return answer(json.loads(blocks[0]["text"]), questions)[0]

    monkeypatch.setattr(overseer, "_jev_decide", fallback)
    result = lead_admin.act(store, "W1", "ask", params())
    assert "model:jev" in result
    assert len(calls) == 1


def test_only_active_lead_can_request_model(lead, monkeypatch):
    store, _ = lead
    monkeypatch.setattr(lead_decision, "_model_decide", lambda *_: pytest.fail("not called"))
    with pytest.raises(ValueError, match="держатель"):
        lead_admin.act(store, "unclaimed", "ask", params())
    with pytest.raises(ValueError, match="obligations"):
        lead_admin.act(store, "W1", "ask", {**params(), "obligations": ["O-not-in-ledger"]})
