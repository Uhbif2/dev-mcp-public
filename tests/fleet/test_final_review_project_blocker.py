"""Final-review system-blocker: a project cause waits for a project request, not a platform deploy."""
from __future__ import annotations

import json

import pytest

from dev_mcp.fleet import deploy_events, queue, review
from test_queue import StubStore, add_project, add_task, add_worker, now


@pytest.fixture
def lane(tmp_path, monkeypatch):
    store = StubStore.open(tmp_path / "final_review_project.sqlite")
    monkeypatch.setattr(deploy_events, "tick", lambda _: {})
    add_project(store, "P")
    for worker in ("W-author", "W-review", "W-fixer", "W-checker"):
        add_worker(store, worker)
    add_task(store, "TZ-source", state="submitted", kind="work", author_worker="W-author")
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET owns=?, acceptance=? WHERE project='P' AND tz='TZ-source'",
                   (json.dumps(["c:/p/src/a.py"]), json.dumps(["py -3 -m pytest -q"])))
    final = review.open_review(store, "P", "TZ-source", final_note="second round")
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='taken' WHERE project='P' AND tz=?", (final,))
        tx.execute("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                   "VALUES('P',?,'W-review','active',?)", (final, now(-1)))
    return store, final


def _state(store, tz):
    with store.read() as tx:
        return str(tx.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (tz,))["state"])


def _last_block(store, tz):
    with store.read() as tx:
        row = tx.one("SELECT payload FROM events WHERE project='P' AND tz=? AND kind='block' "
                     "ORDER BY id DESC LIMIT 1", (tz,))
    return json.loads(row["payload"])


def test_project_cause_opens_request_and_thaws_on_its_accept(lane):
    store, final = lane
    review.decide(store, "W-review", "escalate",
                  "system-blocker: tests/test_live_e2e.py падает в общем discover проекта, "
                  "нужен живой сервис; py -3 -m pytest -q rc=1")
    assert _state(store, final) == "frozen"
    block = _last_block(store, final)
    assert block["gate"] == "repair"
    request = block["request"]
    assert request.startswith("request:")
    assert block["reactivate_policy"] == {"kind": "accepted_work", "target": f"P/{request}",
                                          "authority": "fleet_task_events"}
    assert _state(store, request) == "ready"
    assert queue.thaw_frozen(store, after_min=0) == []

    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='accepted', author_worker='W-fixer' "
                   "WHERE project='P' AND tz=?", (request,))
        tx.record_event(kind="accept", project="P", tz=request, worker_id="W-checker")
    assert queue.thaw_frozen(store, after_min=999) == [f"P/{final}: verified independent evidence"]
    assert _state(store, final) == "ready"


def test_platform_cause_keeps_waiting_for_deploy(lane):
    store, final = lane
    review.decide(store, "W-review", "escalate",
                  "system-blocker: fleet_run обрывается FLEET_EXEC_TIMEOUT на шлюзе, "
                  "любая команда приёмки")
    assert _state(store, final) == "frozen"
    block = _last_block(store, final)
    assert block["reactivate_policy"]["kind"] == "platform_deploy_after"
    with store.read() as tx:
        assert tx.one("SELECT 1 FROM tasks WHERE project='P' AND kind='request'") is None


@pytest.mark.parametrize("text, platform", [
    ("dev-mcp exec_preflight snapshot_failed", True),
    ("инструмент fleet_checkpoint не отвечает", True),
    ("WinError 5 Access is denied при запуске", True),
    ("зависимость TZ-15 accepted, но файлы не в HEAD проекта", False),
    ("устаревший checker tools/check_stage.py требует RED-базу", False),
])
def test_blocker_classifier(text, platform):
    assert review.is_platform_blocker(text) is platform


def test_blocker_request_does_not_breed_another_request(lane):
    store, final = lane
    with store.tx() as tx:  # the source is itself a blocker-removal request
        tx.execute("UPDATE tasks SET title=? WHERE project='P' AND tz='TZ-source'",
                   (f"Request: {review.BLOCKER_REQUEST_MARK} P/TZ-old",))
    review.decide(store, "W-review", "escalate",
                  "system-blocker: tests/test_live_e2e.py падает в общем discover проекта; rc=1")
    assert _state(store, final) == "frozen"
    assert _last_block(store, final)["reactivate_policy"]["kind"] == "platform_deploy_after"
    with store.read() as tx:
        assert tx.one("SELECT 1 FROM tasks WHERE project='P' AND tz LIKE 'request:%'") is None
        assert tx.one("SELECT 1 FROM events WHERE kind='manual_platform_required'") is not None
        assert tx.one("SELECT 1 FROM events WHERE kind='blocker_loop_stopped'") is not None


def test_down_world_resource_waits_for_its_sensor_not_a_deploy(lane, monkeypatch):
    """20.10.2026: Beta App reviews froze on a full C: and waited for a deploy."""
    from dev_mcp.fleet import world
    store, final = lane
    monkeypatch.setattr(world, "config", lambda: {
        "build-disk": {"sensor": "disk", "path": "C:/", "scope": "run"}})
    world.record(store, {"build-disk": (world.DOWN, "свободно 2.1 ГиБ; требуется 8.0 ГиБ")})
    review.decide(store, "W-review", "escalate",
                  "system-blocker: py -3 tools/product_release_check.py --mode beta: "
                  "ресурс build-disk недоступен, свободно 2.1 ГиБ")
    assert _state(store, final) == "frozen"
    assert _last_block(store, final)["reactivate_policy"] == {
        "kind": "world_resource_up", "target": "build-disk", "authority": "world_sensor"}
    with store.read() as tx:
        assert tx.one("SELECT 1 FROM tasks WHERE project='P' AND kind='request'") is None
    assert queue.thaw_frozen(store, after_min=999) == []

    world.record(store, {"build-disk": (world.UP, "свободно 54.0 ГиБ; требуется 8.0 ГиБ")})
    assert queue.thaw_frozen(store, after_min=999) == [f"P/{final}: verified independent evidence"]
    assert _state(store, final) == "ready"


def test_up_world_resource_named_in_note_keeps_deploy_wait(lane, monkeypatch):
    from dev_mcp.fleet import world
    store, final = lane
    monkeypatch.setattr(world, "config", lambda: {
        "build-disk": {"sensor": "disk", "path": "C:/", "scope": "run"}})
    world.record(store, {"build-disk": (world.UP, "ok")})
    review.decide(store, "W-review", "escalate",
                  "system-blocker: dev-mcp exec_preflight snapshot_failed рядом с build-disk")
    assert _last_block(store, final)["reactivate_policy"]["kind"] == "platform_deploy_after"


def test_busy_run_resource_requeues_review_instead_of_freezing(lane, monkeypatch):
    """21.10.2026: «ресурс heavy-build сейчас занят» заморозил ревью и вырастил цепочку заявок."""
    from dev_mcp.fleet import world
    store, final = lane
    monkeypatch.setattr(world, "config", lambda: {
        "heavy-build": {"scope": "run", "run_match": "check_all"}})
    review.decide(store, "W-review", "escalate",
                  "system-blocker: independent mandatory acceptance NOT RUN: ресурс «heavy-build» "
                  "сейчас занят: гейт/приёмка: py -3 tools/check_all.py с 22:05:12. Команда НЕ запущена")
    assert _state(store, final) == "ready"
    with store.read() as tx:
        assert tx.one("SELECT 1 FROM tasks WHERE project='P' AND kind='request'") is None
        assert tx.one("SELECT 1 FROM events WHERE kind='resource_busy_requeue'") is not None
