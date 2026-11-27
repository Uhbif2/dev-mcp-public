"""Выкатка принятого кода проекта в его живую поверхность (06.11.2026)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import lead, live_deploy  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402

LIVE = {"gate": "python -m pytest", "live": {
    "instance": "lib", "port": 8768, "probe": "local-library", "every_min": 10}}


def _store(tmp_path: Path) -> Store:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)",
                  (str(tmp_path),), event={"kind": "test_setup"})
    return store


def _events(store: Store) -> list[dict]:
    with store.read() as tx:
        return [json.loads(r["payload"]) for r in tx.query(
            "SELECT payload FROM events WHERE kind='live_deployed' ORDER BY id")]


def _gate(store: Store, revision: str, rc: int = 0, *,
          command: str = "python -m pytest") -> None:
    with store.tx() as tx:
        tx.record_event(kind="gate", project="P", payload={
            "tested_revision": revision, "command": command, "rc": rc})


def test_new_head_is_deployed_once_and_failure_is_retried(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _gate(store, "a" * 40)
    calls = []
    monkeypatch.setattr(live_deploy, "_git_head", lambda root: ("a" * 40, 1000.0))
    monkeypatch.setattr(live_deploy, "deploy", lambda i, p, s: calls.append(i) or {"ok": False, "detail": "x"})
    assert live_deploy.tick(store, lambda p: LIVE, now=1000.0 + 60) == []  # ещё не осел
    assert len(live_deploy.tick(store, lambda p: LIVE, now=1000.0 + 600)) == 1
    assert live_deploy.tick(store, lambda p: LIVE, now=1000.0 + 610) == []  # не чаще every_min
    monkeypatch.setattr(live_deploy, "deploy", lambda i, p, s: calls.append(i) or {"ok": True, "detail": "ok"})
    # провал повторяется после окна, успех той же ревизии — нет
    later = [e for e in _events(store)]
    assert later[-1]["ok"] is False
    import time
    assert len(live_deploy.tick(store, lambda p: LIVE, now=time.time() + 3600)) == 1
    assert live_deploy.tick(store, lambda p: LIVE, now=time.time() + 7200) == []
    assert calls == ["lib", "lib"]


def test_gate_failed_prevents_deploy_and_is_visible_in_status(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _gate(store, "a" * 40, rc=1)
    monkeypatch.setattr(live_deploy, "_git_head", lambda root: ("a" * 40, 1000.0))
    calls = []
    monkeypatch.setattr(live_deploy, "deploy", lambda *a: calls.append(a))
    assert live_deploy.tick(store, lambda p: LIVE, now=1700) == []
    assert live_deploy.tick(store, lambda p: LIVE, now=1800) == []
    assert calls == []
    assert len(_events(store)) == 1
    assert _events(store)[0]["blocked"] and "не зелёный" in _events(store)[0]["detail"]


def test_missing_proof_and_changed_gate_command_fail_closed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(live_deploy, "_git_head", lambda root: ("a" * 40, 1000.0))
    monkeypatch.setattr(live_deploy, "deploy", lambda *a: (_ for _ in ()).throw(
        AssertionError("deploy without proof")))
    assert live_deploy.tick(store, lambda p: LIVE, now=1700) == []
    assert "нет зелёного" in _events(store)[-1]["detail"]
    _gate(store, "a" * 40, command="different gate")
    assert live_deploy.tick(store, lambda p: LIVE, now=1800) == []
    assert "другой командой" in _events(store)[-1]["detail"]


def test_head_changed_after_gate_check_never_deploys(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _gate(store, "a" * 40)
    heads = iter([("a" * 40, 1000.0), ("b" * 40, 1000.0)])
    monkeypatch.setattr(live_deploy, "_git_head", lambda root: next(heads))
    monkeypatch.setattr(live_deploy, "deploy", lambda *a: (_ for _ in ()).throw(
        AssertionError("deploy after HEAD moved")))
    assert live_deploy.tick(store, lambda p: LIVE, now=1700) == []
    assert "HEAD изменился" in _events(store)[-1]["detail"]


def test_claim_mode_rejects_legacy_gate_without_claim_proof(tmp_path, monkeypatch):
    store = _store(tmp_path)
    rev = "a" * 40
    _gate(store, rev)
    with store.tx() as tx:
        tx.record_event(kind="claim_integrated", project="P", payload={
            "claim_id": 1, "submission_revision": "b" * 40, "integrated_sha": rev})
    monkeypatch.setattr(live_deploy, "_git_head", lambda root: (rev, 1000.0))
    monkeypatch.setattr(live_deploy, "deploy", lambda *a: (_ for _ in ()).throw(
        AssertionError("legacy gate is not claim proof")))
    assert live_deploy.tick(store, lambda p: {**LIVE, "commit": True, "claim_trees": True},
                            now=1700) == []
    assert "release_ready" in _events(store)[-1]["detail"]


def test_project_without_live_surface_is_untouched(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(live_deploy, "deploy", lambda *a: (_ for _ in ()).throw(AssertionError))
    assert live_deploy.tick(store, lambda p: {"commit": True}, now=10**10) == []


def test_lead_brief_names_own_live_route_as_product_work(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr("dev_mcp.fleet.goals.project_info", lambda p: LIVE)
    text = "\n".join(lead.live_lines(store, "P"))
    assert "lib :8768" in text and "выкаток ещё не было" in text and "продуктовая веха" in text
    with store.tx() as tx:
        tx.record_event(kind="live_deployed", project="P", payload={"rev": "b" * 40, "ok": True})
    assert "ревизия bbbbbbbbbb: работает" in "\n".join(lead.live_lines(store, "P"))


def test_lead_brief_lists_undelivered_accepts_and_sweep_leftovers(tmp_path):
    store = _store(tmp_path)
    with store.tx() as tx:
        tx.record_event(kind="integrate", project="P", tz="fix:TZ-LEAD:3",
                        payload={"accept_id": 1, "reason_code": "batch_candidate_failed", "deferred": ["a"]})
        tx.record_event(kind="integrate", project="P", tz="TZ-LEAD",
                        payload={"accept_id": 2, "commit": "abc"})
        tx.record_event(kind="integrate_sweep", project="P",
                        payload={"ready": True, "left_paths": ["deploy/x.py"]})
    text = "\n".join(lead.delivery_lines(store, "P"))
    assert "1 принятых не в истории" in text and "fix:TZ-LEAD:3" in text
    assert "deploy/x.py" in text and "TZ-LEAD," not in text
