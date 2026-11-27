"""The 15.11 operator switch is fail-closed across all task producers."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import generators
from dev_mcp.fleet.store import Store
from dev_mcp.fleet import rudiments

_REAL_ENABLED = generators.enabled


def test_disabled_by_default_and_only_explicit_enable(monkeypatch):
    from dev_mcp.fleet import goals

    monkeypatch.setattr(generators, "enabled", _REAL_ENABLED)
    monkeypatch.setattr(goals, "_load_doc", lambda: {})
    assert not generators.enabled()
    monkeypatch.setattr(goals, "_load_doc", lambda: {"platform": {"task_generators": {"enabled": "true"}}})
    assert not generators.enabled()
    monkeypatch.setattr(goals, "_load_doc", lambda: {"platform": {"task_generators": {"enabled": True}}})
    assert generators.enabled()


def test_only_direct_issues_and_primary_reviews_survive(monkeypatch):
    monkeypatch.setattr(generators, "enabled", lambda: False)
    insert = "INSERT INTO tasks(project,tz,state,created_at,updated_at) VALUES(?,?,?,?,?)"
    assert generators.permitted(insert, {"kind": "lead_issue"})
    assert generators.permitted(insert, {"kind": "review_open"})
    assert generators.permitted(insert, {"kind": "run", "tool": "sync_backlog",
                                       "payload": '{"action":"selected_insert"}'})
    for kind in ("triage_open", "styk_open", "request_draft", "reject",
                 "intent.work_created", "queue_source_repair_opened"):
        assert not generators.permitted(insert, {"kind": kind}), kind
    assert generators.permitted("UPDATE tasks SET state='accepted'", {"kind": "reject"})
    assert not generators.permitted("UPDATE tasks SET state='ready'", {"kind": "auto_reactivate"})
    assert generators.permitted("UPDATE tasks SET state='ready'", {"kind": "lead_reactivate"})


def test_store_blocks_auto_insert_in_both_write_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(generators, "enabled", lambda: False)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P','C:/p')", event="setup")
        sql = ("INSERT INTO tasks(project,tz,state,created_at,updated_at) "
               "VALUES('P',?,'ready','2026-11-15','2026-11-15')")
        tx.change(sql, ("auto",), event={"kind": "triage_open"})
        tx.changes([(sql, ("auto-two",))], event={"kind": "reject"})
        tx.change(sql, ("lead",), event={"kind": "lead_issue"})
        tx.change(sql, ("review",), event={"kind": "review_open"})
    with store.read() as tx:
        assert [r["tz"] for r in tx.query("SELECT tz FROM tasks ORDER BY tz")] == ["lead", "review"]


def test_auto_insert_works_after_explicit_enable(tmp_path, monkeypatch):
    monkeypatch.setattr(generators, "enabled", lambda: True)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P','C:/p')", event="setup")
        tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) "
                  "VALUES('P','triage:1','ready','2026-11-15','2026-11-15')",
                  event={"kind": "triage_open"})
    with store.read() as tx:
        assert tx.scalar("SELECT count(*) FROM tasks") == 1


def test_retirement_candidates_keep_submissions_and_reviews(tmp_path, monkeypatch):
    monkeypatch.setattr(generators, "enabled", lambda: True)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P','C:/p')", event="setup")
        sql = ("INSERT INTO tasks(project,tz,kind,state,created_at,updated_at) "
               "VALUES('P',?,?,?,'2026-11-15','2026-11-15')")
        for tz, kind, state in (
            ("triage:1", "triage", "ready"),
            ("triage:2", "triage", "submitted"),
            ("review:triage:2", "review", "ready"),
            ("request:abc", "request", "frozen"),
        ):
            tx.change(sql, (tz, kind, state), event="setup")
    with store.read() as tx:
        found = rudiments.candidates(tx, ("judgment_generated", "request_generated"))
    assert [(x["tz"], x["state"]) for x in found] == [("triage:1", "ready")]
