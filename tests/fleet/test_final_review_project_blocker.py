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


# --- 03.11.2026: ревью не выдаётся, пока занят ресурс scope="run", нужный её приёмке ----------
# Beta App, 03.11 10:00–14:30 UTC: 416 взятий и 308 блоков при 18 приёмках. Ревью требовало
# `py -3 android/build.py test …`, heavy-build держал операторский check_all; ревьюер
# блокировался, задача оттаивала и выдавалась снова. Занятость, известная до выдачи, не должна
# становиться захватом, блоком и заявкой.

RUN_CFG = {"heavy-build": {"scope": "run", "global": True, "run_match": r"\b(build|check_all)"}}


@pytest.fixture
def admission(tmp_path, monkeypatch):
    from dev_mcp.fleet import world
    monkeypatch.setenv("DEV_MCP_FLEET_GATE_LOCK", "1")
    monkeypatch.setattr(world, "config", lambda: RUN_CFG)
    monkeypatch.setattr(world, "_busy_cache", {"at": -1e9, "busy": {}})
    store = StubStore.open(tmp_path / "admission.sqlite")
    add_project(store, "P", 1)
    add_worker(store, "W-review")
    return store


def _acceptance(store, tz, commands):
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET acceptance=? WHERE project='P' AND tz=?",
                   (json.dumps(commands), tz))


def _busy(monkeypatch, busy):
    from dev_mcp.fleet import world
    monkeypatch.setattr(world, "busy_run_resources", lambda *a, **k: dict(busy))


def test_review_waits_in_queue_while_its_acceptance_resource_is_busy(admission, monkeypatch):
    store = admission
    add_task(store, "review:TZ-1", kind="review", author_worker="W-author")
    _acceptance(store, "review:TZ-1", ["py -3 android/build.py test app"])
    _busy(monkeypatch, {"heavy-build": "вне учёта dev-mcp — pid 1: py -3 tools/check_all.py"})
    assert queue.take_next(store, "W-review") is None
    skipped: list = []
    with store.read() as tx:
        assert list(queue._available(tx, "W-review", skipped=skipped)) == []
        assert tx.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
        assert tx.one("SELECT state FROM tasks WHERE tz='review:TZ-1'")["state"] == "ready"
    assert ("P", "review:TZ-1", "ждёт ресурс heavy-build") in skipped


def test_review_is_issued_once_the_resource_is_free(admission, monkeypatch):
    store = admission
    add_task(store, "review:TZ-1", kind="review", author_worker="W-author")
    _acceptance(store, "review:TZ-1", ["py -3 android/build.py test app"])
    _busy(monkeypatch, {})
    task = queue.take_next(store, "W-review")
    assert task is not None and task.tz == "review:TZ-1"


def test_review_with_light_acceptance_is_not_held(admission, monkeypatch):
    store = admission
    add_task(store, "review:TZ-2", kind="review", author_worker="W-author")
    _acceptance(store, "review:TZ-2", ["py -3 -m pytest test/test_x.py -q"])
    _busy(monkeypatch, {"heavy-build": "check_all"})
    task = queue.take_next(store, "W-review")
    assert task is not None and task.tz == "review:TZ-2"


def test_work_task_is_not_held_by_busy_run_resource(admission, monkeypatch):
    """Автор может делать то, что не требует сборки: «ресурс занят — не ошибка» (протокол)."""
    store = admission
    add_task(store, "TZ-3", kind="work")
    _acceptance(store, "TZ-3", ["py -3 tools/check_all.py"])
    _busy(monkeypatch, {"heavy-build": "check_all"})
    task = queue.take_next(store, "W-review")
    assert task is not None and task.tz == "TZ-3"


def test_acceptance_run_needs_matches_policy_patterns():
    from dev_mcp.fleet import world
    assert world.acceptance_run_needs(["py -3 tools/check_all.py"], RUN_CFG) == ["heavy-build"]
    assert world.acceptance_run_needs(["py -3 -m pytest t/test_x.py"], RUN_CFG) == []
    assert world.acceptance_run_needs([], RUN_CFG) == []


def test_busy_run_resources_gate_lock_switch_and_foreign_process(monkeypatch):
    from dev_mcp.fleet import world
    monkeypatch.setenv("DEV_MCP_FLEET_GATE_LOCK", "0")
    assert world.busy_run_resources(RUN_CFG) == {}
    monkeypatch.setenv("DEV_MCP_FLEET_GATE_LOCK", "1")
    cfg = {"heavy-build": {"scope": "run", "run_match": "check_all", "scan_processes": True}}
    monkeypatch.setattr(world, "_foreign_runs",
                        lambda pattern: ["pid 7: py -3 tools/check_all.py"] if pattern else [])
    busy = world.busy_run_resources(cfg)
    assert list(busy) == ["heavy-build"] and "check_all" in busy["heavy-build"]


@pytest.mark.parametrize("note", [
    "system-blocker: claim_id=25693. Independent mandatory `py -3 android/build.py test app` refused "
    "BEFORE START: shared resource `heavy-build` occupied by operator `py -3 tools/check_all.py`",
    "system-blocker: ресурс «heavy-build» сейчас занят: гейт/приёмка",
    "system-blocker: heavy-build busy (check_all held by gate), Gradle not started",
    "system-blocker: прогон не начат — heavy-build занят операторским check_all",
])
def test_reviewer_phrasings_of_busy_resource_are_recognised(monkeypatch, note):
    from dev_mcp.fleet import world
    monkeypatch.setattr(world, "config", lambda: RUN_CFG)
    assert review._busy_run_resource(note) == "heavy-build"


@pytest.mark.parametrize("note", [
    "system-blocker: py -3 tools/check_all.py rc=1: 22 collection errors ImportError PIL.Image",
    "system-blocker: android/build.py test overlay упал: AssertionError в OverlayTest",
])
def test_real_red_acceptance_is_not_mistaken_for_busy(monkeypatch, note):
    from dev_mcp.fleet import world
    monkeypatch.setattr(world, "config", lambda: RUN_CFG)
    assert review._busy_run_resource(note) is None


def _retake(store, final):
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='taken' WHERE project='P' AND tz=?", (final,))
        tx.record_event(kind="auto_reactivate", project="P", tz=final, payload={"reason": "deploy"})
        tx.execute("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                   "VALUES('P',?,'W-review','active',?)", (final, now(-1)))


def test_repeat_infra_block_becomes_operator_condition_not_deploy_loop(lane):
    # 04.11.2026: 465 из 467 блоков ждали деплоя; каждый деплой размораживал их все, ревью
    # снова упиралось в ту же среду (до 9 раз). Второй блок той же сдачи — условие у оператора.
    store, final = lane
    note = "system-blocker: fleet_run обрывается FLEET_EXEC_TIMEOUT на шлюзе, любая команда приёмки"
    with store.tx() as tx:
        tx.record_event(kind="submit", project="P", tz="TZ-source", worker_id="W-author")
    review.decide(store, "W-review", "escalate", note)
    assert _last_block(store, final)["reactivate_policy"]["kind"] == "platform_deploy_after"
    _retake(store, final)
    review.decide(store, "W-review", "escalate", note)
    block = _last_block(store, final)
    assert block["reactivate_policy"]["kind"] == "operator_condition"
    assert block["reactivate_policy"]["target"].startswith("manual-platform:")
    with store.read() as tx:
        rows = tx.query("SELECT payload FROM events WHERE kind='manual_platform_required'")
        assert len(rows) == 1 and "Условие не выполнено" in json.loads(rows[0]["payload"])["title"]
        assert tx.one("SELECT 1 FROM tasks WHERE project='P' AND kind='request'") is None
        tx_deployed = tx.one("SELECT 1 FROM events WHERE kind='platform_deployed'")
    assert tx_deployed is None
    with store.tx() as tx:
        sha = "a" * 40
        tx.record_event(kind="platform_deployed", payload={"candidate": sha, "known_good": sha,
                                                           "ts": now(1)})
    assert queue.thaw_frozen(store, after_min=999) == []
    assert _state(store, final) == "frozen"


def test_new_submission_resets_the_repeat_counter(lane):
    store, final = lane
    note = "system-blocker: fleet_run обрывается FLEET_EXEC_TIMEOUT на шлюзе, любая команда приёмки"
    review.decide(store, "W-review", "escalate", note)
    with store.tx() as tx:
        tx.record_event(kind="submit", project="P", tz="TZ-source", worker_id="W-author")
    _retake(store, final)
    review.decide(store, "W-review", "escalate", note)
    assert _last_block(store, final)["reactivate_policy"]["kind"] == "platform_deploy_after"


PHONE = {"телефон": {"world": True, "sensor": "adb",
                     "match": r"\badb\b|phoneb|телефон|android[- ]?(?:device|устройств)"}}
ADB_BLOCK = ("system-blocker: TZ-282 requires exclusive authorized Android/ADB device session; "
             "claim resources=(none). AGENTS.md prohibits touching phone without resource.")


def _resources(store, tz):
    with store.read() as tx:
        return json.loads(tx.one("SELECT resources FROM tasks WHERE project='P' AND tz=?",
                                 (tz,))["resources"])


def test_connected_phone_missing_from_task_is_declared_not_sent_to_owner(lane, monkeypatch):
    """04.11.2026: телефон подключён, а ревью без ресурса «телефон» ушли владельцу «подключи PHONEB»."""
    from dev_mcp.fleet import world
    store, final = lane
    monkeypatch.setattr(world, "config", lambda: PHONE)
    world.record(store, {"телефон": (world.UP, "устройства: 085562527M002215")})
    review.decide(store, "W-review", "escalate", ADB_BLOCK)
    assert _state(store, final) == "ready"
    assert _resources(store, final) == ["телефон"]
    assert _resources(store, "TZ-source") == ["телефон"]
    with store.read() as tx:
        assert tx.one("SELECT 1 FROM events WHERE kind='manual_platform_required'") is None
        assert tx.one("SELECT 1 FROM tasks WHERE project='P' AND kind='request'") is None
        assert tx.one("SELECT 1 FROM events WHERE kind='resource_declared'") is not None


def test_declared_phone_that_is_down_waits_for_its_sensor(lane, monkeypatch):
    from dev_mcp.fleet import world
    store, final = lane
    monkeypatch.setattr(world, "config", lambda: PHONE)
    world.record(store, {"телефон": (world.DOWN, "нет готовых устройств")})
    review.decide(store, "W-review", "escalate", ADB_BLOCK)
    assert _state(store, final) == "frozen"
    assert _resources(store, final) == ["телефон"]
    assert _last_block(store, final)["reactivate_policy"] == {
        "kind": "world_resource_up", "target": "телефон", "authority": "world_sensor"}
    world.record(store, {"телефон": (world.UP, "устройства: 085562527M002215")})
    assert queue.thaw_frozen(store, after_min=999) == [f"P/{final}: verified independent evidence"]
