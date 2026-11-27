import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet import briefing, instructions, lead_admin, lead_focus, queue
from tests.fleet.test_lead_admin import _task, env  # noqa: F401


@pytest.fixture
def focused(env, monkeypatch):
    store, _ = env
    focus = {"action": "deliver one result", "evidence": "verify exact live route", "witness": "live"}
    monkeypatch.setattr(lead_focus, "settings", lambda project: focus if project == "P" else {})
    with store.tx() as tx:
        for worker in ("W3", "W4", "W5"):
            now = datetime.now(timezone.utc).isoformat()
            tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES(?,?,?)",
                      (worker, now, now), event={"kind": "test_setup"})
    lead_focus.tick(store)
    return store, focus


def _record(store, kind, tz="TZ-LEAD", worker="W1", **payload):
    with store.tx() as tx:
        tx.record_event(kind=kind, project="P", tz=tz, worker_id=worker, payload=payload)
        return tx.scalar("SELECT MAX(id) FROM events")


def _state(store):
    with store.read() as tx:
        return lead_focus.state(tx, "P")[0]


def _finish(store, worker="W1"):
    queue.finish(store, worker, "handoff: focused verification")


def _settle(store, worker, action, **params):
    with store.read() as tx:
        params["claim_id"] = tx.scalar("SELECT id FROM claims WHERE worker_id=? AND state='active'", (worker,))
    return lead_admin.act(store, worker, action, params)


def test_continuous_lead_brief_surfaces_open_delivery_before_choosing(focused, monkeypatch):
    store, focus = focused
    focus["orchestrated"] = True
    monkeypatch.setattr(lead_focus, "continuous", lambda project: project == "P")
    receipt = _record(store, "accept", "TZ-2", worker="W2", review_task="review:TZ-2")
    _record(store, "integrate", "TZ-2", accept_id=receipt, deferred=["report.md"], reason="gate_launch_failed")

    brief = briefing.briefing(store, "W1", None)
    assert brief.index("gate_launch_failed: TZ-2") < brief.index("Перед выбором и новой выдачей")
    assert "проверь, что она открывается в месте использования" in brief
    assert "gate_launch_failed: TZ-2" in "\n".join(lead_focus.lines(store, "P"))

    _record(store, "integrate", "TZ-2", accept_id=receipt, commit="1234567")
    assert "gate_launch_failed: TZ-2" not in briefing.briefing(store, "W1", None)


def test_continuous_focus_allows_parallel_independent_tracks(focused, monkeypatch):
    store, focus = focused
    focus["orchestrated"] = True
    monkeypatch.setattr(lead_focus, "continuous", lambda name: name == "P")
    lead_focus.tick(store)
    with store.read() as tx:
        root = Path(tx.scalar("SELECT root FROM projects WHERE name='P'"))
    (root / "ToDo/lead/LEDGER.json").write_text(
        '{"obligations": [{"id": "O-release"}]}', encoding="utf-8")
    _settle(store, "W1", "choose", obligation="O-release",
            summary="Выпустить проверенный релиз на устройство пилота",
            criterion="Отдельные подписанная версия и нативный замер")
    params = {"body": "Доказать продвижение независимой дорожки без конфликтов кода и устройства. " * 2,
              "criteria": ["Принять независимый результат каждой дорожки"]}
    lead_admin.act(store, "W1", "issue", {
        **params, "title": "L2 integration", "owns": ["integration/l2"], "resources": ["main-branch"],
    })
    lead_admin.act(store, "W1", "issue", {
        **params, "title": "L4 native", "owns": ["native/l4"], "resources": ["phone"],
    })
    with store.read() as tx:
        assert lead_focus.issue_refusal(tx, "P") == ""
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='P' AND tz LIKE 'sub:LEAD-%'") == 2


def test_orchestrated_delivery_reuses_verified_artifact_and_closes_instruction(focused):
    store, focus = focused
    with store.read() as tx:
        root = Path(tx.scalar("SELECT root FROM projects WHERE name='P'"))
    artifact = root / "release.zip"
    handoff = root / "handoff.md"
    artifact.write_bytes(b"accepted release bytes")
    handoff.write_text("handoff", encoding="utf-8")
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    proof = _record(store, "accept", tz="TZ-2", worker="W2", review_task="review:TZ-2",
                    note=f"Verified the exact artifact {artifact_sha}")
    focus.update(orchestrated=True, instruction_source="delivery pilot", prior_delivery={
        "event": proof, "task": "TZ-2", "artifact": artifact.name, "artifact_sha": artifact_sha,
        "handoff": handoff.name, "handoff_sha": hashlib.sha256(handoff.read_bytes()).hexdigest(),
    })
    lead_focus.tick(store)
    iid = instructions.register(store, "P", text="hand over the accepted release", done_when="verified",
                                source="delivery pilot")
    assert "Первый ход" in briefing.briefing(store, "W1", None)
    with pytest.raises(ValueError, match="event_id"):
        instructions.set_status(store, "W1", "P", {"id": iid, "status": "done", "evidence": "green tests passed"})
    artifact.write_bytes(b"different release")
    with pytest.raises(ValueError, match="совпадающие байты"):
        _settle(store, "W1", "result", event_id=proof, summary="Result and exact independent delivery evidence")
    artifact.write_bytes(b"accepted release bytes")
    assert "передан" in _settle(store, "W1", "result", event_id=proof,
                                summary="Result and exact independent delivery evidence")
    item = next(item for item in instructions.items(store, "P") if item["id"] == iid)
    assert item["status"] == "done"
    assert _state(store) == "done"


def test_continuous_lead_resumes_after_delivery_and_keeps_choice_through_wait(focused, monkeypatch):
    from dev_mcp.fleet import obstacles

    store, focus = focused
    focus["orchestrated"] = True
    monkeypatch.setattr(lead_focus, "continuous", lambda project: project == "P")
    lead_focus.tick(store)
    with store.read() as tx:
        root = Path(tx.scalar("SELECT root FROM projects WHERE name='P'"))
    (root / "ToDo/lead/LEDGER.json").write_text(
        '{"obligations": [{"id": "O-next"}]}', encoding="utf-8")

    proof = _record(store, "witness_result", id="live", verdict="works_live")
    _settle(store, "W1", "choose", obligation="O-next", summary="Found the next useful result and checked the inputs",
            criterion="The original scenario produces observable evidence")
    with pytest.raises(ValueError, match="нового"):
        _settle(store, "W1", "result", event_id=proof, summary="This evidence predates the selected outcome")
    proof = _record(store, "witness_result", id="live", verdict="works_live")
    _settle(store, "W1", "result", event_id=proof, summary="The original scenario was delivered and verified")
    assert _state(store) == "choose"
    assert queue.take_next(store, "W1", "P").tz == "TZ-LEAD"

    _settle(store, "W1", "choose", obligation="O-next", summary="New evidence justifies the remaining result",
            criterion="An independent reviewer verifies the next scenario")
    _task(store, "P", "TZ-2", "submitted")
    _settle(store, "W1", "wait", tz="TZ-2", summary="The independent reviewer is checking the next scenario",
            result_task=True)
    assert _state(store) == "waiting"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz='TZ-2'", (),
                  event={"kind": "test_setup"})
    _record(store, "accept", tz="TZ-2", worker="W2", review_task="review:TZ-2")
    assert _state(store) == "selected"
    assert queue.take_next(store, "W1", "P").tz == "TZ-LEAD"

    _record(store, obstacles.OPEN, id="ob-test")
    _settle(store, "W1", "park", obstacle="ob-test", summary="No usable next action until the fixture becomes available")
    assert _state(store) == "waiting"
    _record(store, obstacles.CLOSED, id="ob-test")
    assert _state(store) == "selected"
    assert queue.take_next(store, "W1", "P").tz == "TZ-LEAD"


def test_owner_target_prioritizes_work_and_review_until_instruction_closes(focused):
    store, _ = focused
    target = "request:224b7c68"
    review = f"review:{target}:2"
    _task(store, "P", "TZ-2", "ready")
    _task(store, "P", target, "ready")
    _task(store, "Q", "TZ-3", "ready")
    with store.tx() as tx:
        tx.change("UPDATE projects SET priority=0 WHERE name='Q'", (), event={"kind": "test_setup"})
    assert queue.peek_projects(store)[0][0] == "Q"
    iid = instructions.register(store, "P", text="deliver exact owner request", done_when="verified")
    _record(store, instructions.TARGET_KIND, id=iid, task=target)
    assert queue.peek_next(store, "P")[0].tz == target
    assert queue.peek_projects(store)[0][0] == "P"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE project='P' AND tz=?", (target,),
                  event={"kind": "test_setup"})
    _task(store, "P", review, "ready")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET kind='review' WHERE project='P' AND tz=?", (review,),
                  event={"kind": "test_setup"})
    assert queue.peek_next(store, "P")[0].tz == review
    _record(store, instructions.STATUS_KIND, id=iid, status="done")
    assert queue.peek_projects(store)[0][0] == "Q"
    with store.read() as tx:
        assert instructions.queue_target(tx, "P") == ""


def test_two_turns_hold_same_charter_in_preview_and_take_but_not_work(focused):
    store, _ = focused
    _record(store, "checkpoint")
    _finish(store)
    assert _state(store) == "last"
    assert queue.take_next(store, "W2", "P").tz == "TZ-LEAD"
    _record(store, "run", worker="W2")
    _finish(store, "W2")
    assert _state(store) == "needs_recovery"
    assert queue.peek_next(store, "P")[0] is None
    assert queue.take_next(store, "W1", "P") is None
    lead_focus.tick(store)
    assert _state(store) == "recovery"
    assert queue.take_next(store, "W5", "P").tz == "TZ-LEAD"
    _record(store, "checkpoint", worker="W5")
    _finish(store, "W5")
    assert _state(store) == "stalled"
    lead_focus.tick(store)
    assert _state(store) == "stalled"
    _task(store, "P", "TZ-2", "ready")
    assert queue.take_next(store, "W3", "P").tz == "TZ-2"
    instructions.register(store, "P", text="field watcher alert", done_when="observed", author="field")
    assert _state(store) == "stalled"
    instructions.register(store, "P", text="new owner outcome", done_when="observed")
    assert _state(store) == ""
    assert queue.take_next(store, "W4", "P").tz == "TZ-LEAD"


def test_wait_releases_claim_and_reopens_only_on_source_verdict(focused):
    store, _ = focused
    dep = "sub:LEAD-1"
    _task(store, "P", dep, "submitted")
    _settle(store, "W1", "wait", tz=dep, summary="Точная проверка пользовательского сценария")
    assert _state(store) == "waiting"
    assert queue.take_next(store, "W1", "P") is None
    _record(store, "accept", f"review:{dep}:2")
    assert _state(store) == "waiting"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz=?", (dep,),
                  event={"kind": "accept", "project": "P", "tz": dep})
    assert _state(store) == ""
    assert queue.take_next(store, "W2", "P").tz == "TZ-LEAD"
    proof = _record(store, "witness_result", id="wrong", verdict="works_live")
    with pytest.raises(ValueError, match="действующий works_live"):
        _settle(store, "W2", "result", event_id=proof, summary="Verified delivery on the live route.")
    proof = _record(store, "witness_result", id="live", verdict="works_live")
    _settle(store, "W2", "result", event_id=proof, summary="Verified delivery on the live route.")
    assert _state(store) == "done"
    assert queue.take_next(store, "W1", "P") is None


@pytest.mark.parametrize("rounds", [1, 2])
def test_wait_resumes_on_independent_acceptance_of_linked_repair(focused, rounds):
    store, _ = focused
    source = "sub:LEAD-1"
    _task(store, "P", source, "submitted")
    with store.tx() as tx:
        for tz in (f"fix:{source}", f"fix:{source}:2", f"fix:{source}:3"):
            tx.change("INSERT INTO tasks(project,tz,title,state,kind,created_at,updated_at) "
                      "VALUES('P',?,?,'submitted','fix',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                      (tz, tz), event={"kind": "test_setup"})
    first = f"fix:{source}"
    _record(store, "reject", source, worker="W2", review_task=f"review:{source}", fix_task=first)
    _settle(store, "W1", "wait", tz=source, summary="Waiting on the independent review of the requested change",
            result_task=True)
    repair = first
    if rounds == 2:
        repair = f"fix:{source}:2"
        _record(store, "reject", first, worker="W3", review_task=f"review:{first}", fix_task=repair)
    assert _state(store) == "waiting"

    unlinked = f"fix:{source}:3"
    _record(store, "accept", unlinked, worker="W3", review_task=f"review:{unlinked}")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz IN (?,?)", (source, repair),
                  event={"kind": "test_setup"})
    assert _state(store) == "waiting"
    assert queue.peek_next(store, "P")[0] is None

    proof = _record(store, "accept", repair, worker="W3", review_task=f"review:{repair}")
    assert queue.peek_next(store, "P")[0].tz == "TZ-LEAD"
    assert queue.take_next(store, "W2", "P").tz == "TZ-LEAD"
    _settle(store, "W2", "result", event_id=proof, summary="The independent repair review proves the requested result.")
    assert _state(store) == "done"


def test_automatic_handoff_binds_exact_task_and_ignores_bare_launch(focused):
    store, focus = focused
    focus["dependency"] = "sub:LEAD-1"
    lead_focus.tick(store)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
                  "VALUES('P','TZ-LEAD','W2','released',?,?)", (old, datetime.now(timezone.utc).isoformat()),
                  event={"kind": "test_setup"})
    _task(store, "P", "sub:LEAD-1", "submitted")
    _task(store, "P", "sub:LEAD-2", "ready")
    _record(store, "run")
    _finish(store)
    assert _state(store) == "last"
    assert queue.take_next(store, "W3", "P").tz == "TZ-LEAD"
    _record(store, "run", worker="W3")
    _finish(store, "W3")
    assert _state(store) == "handoff"
    lead_focus.tick(store)
    lead_focus.tick(store)
    assert _state(store) == "waiting"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind=?", (lead_focus.WAIT,)) == 1
        assert "sub:LEAD-1" in lead_focus.hold_reason(tx, "P", "TZ-LEAD")


def test_install_parks_on_exact_existing_review_and_resumes_for_delivery(focused):
    store, focus = focused
    target = "sub:LEAD-8"
    _task(store, "P", target, "submitted")
    focus.update(dependency=target, dependency_delivers=True)
    lead_focus.tick(store)
    _finish(store)
    assert _state(store) == "waiting"
    assert queue.take_next(store, "W2", "P") is None
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz=?", (target,),
                  event={"kind": "accept", "project": "P", "tz": target, "worker_id": "W3",
                         "payload": {"review_task": f"review:{target}:2"}})
        proof = tx.scalar("SELECT MAX(id) FROM events")
    assert queue.take_next(store, "W2", "P").tz == "TZ-LEAD"
    assert f"#{proof}" in lead_focus.lines(store, "P")[0]
    _settle(store, "W2", "result", event_id=proof,
            summary="Published the checked bundle and the scope of the field result.")
    assert _state(store) == "done"
