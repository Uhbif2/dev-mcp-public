"""TZ-314: one routed platform defect, durable source wait, exact deploy thaw."""
from __future__ import annotations

import json

import pytest

from test_queue import StubStore, add_project, add_task, add_worker
from dev_mcp.fleet import platform_route as route, queue
from dev_mcp.fleet.review import open_request

SHA = "a" * 40
OTHER = "b" * 40


@pytest.fixture()
def store(tmp_path):
    registry = StubStore.open(tmp_path / "fleet.sqlite")
    add_project(registry, "Fleet Platform")
    add_project(registry, "Main Hub")
    add_project(registry, "Mill Works")
    for project, task in (("Main Hub", "TZ-520"),
                          ("Main Hub", "TZ-521"),
                          ("Mill Works", "TZ-80")):
        add_task(registry, task, project=project, state="taken")
    return registry


def _state(store, project, tz):
    with store.read() as tx:
        return tx.one("SELECT state FROM tasks WHERE project=? AND tz=?",
                      (project, tz))["state"]


def _record(store, kind, project, tz, **payload):
    with store.tx() as tx:
        tx.record_event(kind=kind, project=project, tz=tz, payload=payload)


def _report(store, project="Main Hub", tz="TZ-520", **kwargs):
    return route.route_request(
        store, source_project=project, source_tz=tz,
        summary=kwargs.pop("summary", "Дефект: сопоставление команд приёмки TZ-520"),
        detail=kwargs.pop("detail", ""), worker_id="W42", **kwargs,
    )


def _accepted_and_deployed(store, task, *, commit=SHA, deployed=SHA):
    tz = task.split("/", 1)[1]
    with store.tx() as tx:
        tx.execute(
            "UPDATE tasks SET state='accepted' WHERE project=? AND tz=?",
            ("Fleet Platform", tz),
        )
        tx.record_event(kind="accept", project="Fleet Platform", tz=tz,
                        worker_id="W-review", payload={"verdict": "accept"})
        tx.record_event(kind="integrate", project="Fleet Platform", tz=tz,
                        payload={"commit": commit[:12], "files": 2})
        tx.record_event(kind="platform_deployed", payload={
            "candidate": deployed, "known_good": deployed})


def test_same_subject_cross_project_dedup_and_count(store):
    first = _report(store)
    second = _report(store, "Mill Works", "TZ-80",
                     summary="Ошибка сравнения команд приемки TZ-80",
                     path="C:/hub/dev-mcp/src/dev_mcp/fleet/acceptance_guard.py")
    assert first.status == "created"
    assert second.status == "linked"
    assert second.task == first.task
    assert second.affected_tasks == 2
    with store.read() as tx:
        rows = tx.query("SELECT tz,body,priority,owns FROM tasks WHERE project='Fleet Platform'")
    assert len(rows) == 1
    assert "затронуто задач: 2" in rows[0]["body"]
    assert rows[0]["priority"] == -10
    assert "acceptance_guard.py" in rows[0]["owns"]
    assert _state(store, "Main Hub", "TZ-520") == "frozen"
    assert _state(store, "Mill Works", "TZ-80") == "frozen"


def test_repeat_from_same_project_rejected_with_reference(store):
    created = _report(store)
    retry = _report(store, "Main Hub", "TZ-521",
                    summary="Сбой: сопоставление команд приёмки TZ-521")
    assert retry.status == "rejected"
    assert retry.task == created.task
    assert created.task in retry.detail
    assert retry.affected_tasks == 1
    assert _state(store, "Main Hub", "TZ-521") == "taken"
    with pytest.raises(route.PlatformRoutePending, match="запрещены"):
        route.require_request_allowed(store, "Main Hub", "TZ-520")
    with store.read() as tx:
        assert len(tx.query("SELECT tz FROM tasks WHERE project='Fleet Platform'")) == 1


def test_wait_does_not_thaw_before_accept_and_exact_verified_deploy(store):
    routed = _report(store)
    assert route.thaw_deployed(store) == []
    _record(store, "platform_deployed", None, None,
            candidate=SHA, known_good=SHA)
    assert route.thaw_deployed(store) == []  # No independent acceptance.
    tz = routed.task.split("/", 1)[1]
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='accepted' WHERE project=? AND tz=?",
                   ("Fleet Platform", tz))
        tx.record_event(kind="accept", project="Fleet Platform", tz=tz)
        tx.record_event(kind="integrate", project="Fleet Platform", tz=tz,
                        payload={"commit": SHA[:12]})
    assert route.thaw_deployed(store) == []  # Old receipt is not proof.
    _record(store, "platform_deployed", None, None,
            candidate=OTHER, known_good=OTHER)
    assert route.thaw_deployed(store) == []  # Wrong SHA.
    _record(store, "platform_rolled_back", None, None, candidate=SHA)
    assert route.thaw_deployed(store) == []  # Rollback revokes evidence.
    _record(store, "platform_deployed", None, None,
            candidate=SHA, known_good=SHA)
    assert route.thaw_deployed(store) == ["Main Hub/TZ-520"]
    assert _state(store, "Main Hub", "TZ-520") == "ready"
    route.require_request_allowed(store, "Main Hub", "TZ-520")
    assert route.thaw_deployed(store) == []


def test_wait_is_not_satisfied_by_unverified_or_wrong_commit(store):
    routed = _report(store)
    _accepted_and_deployed(store, routed.task, commit=OTHER, deployed=SHA)
    assert route.thaw_deployed(store) == []
    _record(store, "platform_deployed", None, None,
            candidate=SHA, known_good=OTHER)
    assert route.thaw_deployed(store) == []
    assert _state(store, "Main Hub", "TZ-520") == "frozen"


def test_non_platform_request_does_not_route(store):
    ignored = _report(store, summary="Поправить кнопку входа в приложении",
                      path="src/frontend/login.ts")
    assert ignored.status == "ignored"
    assert _state(store, "Main Hub", "TZ-520") == "taken"


def test_g1_executable_plan_acceptance_criteria_do_not_route(store):
    ignored = _report(
        store,
        summary="Выдать исполнимую G1-задачу: intent/plan.md + ToDo/tz/TZ-*.md",
        detail=(
            "Создать содержательный план с ## Работы / ## Зависимости / ## Приёмка. "
            "Предварительная команда приёмки: py -3 .intent/check.py executable_plan; "
            "ожидается rc=0 после документов, а не изменение кода dev-mcp."
        ),
    )
    assert ignored.status == "ignored"
    assert _state(store, "Main Hub", "TZ-520") == "taken"
    with store.read() as tx:
        assert not tx.query("SELECT tz FROM tasks WHERE project='Fleet Platform'")


def test_non_platform_defect_with_acceptance_in_detail_does_not_route(store):
    ignored = _report(
        store,
        summary="Ошибка отображения кнопки входа",
        detail="Критерии приёмки: логин работает, review завершается успешно",
    )
    assert ignored.status == "ignored"


def test_platform_take_defect_not_redirected_by_acceptance_in_detail(store):
    created = _report(
        store,
        summary="Неправильная выдача задач флоту TZ-520",
        detail="Проверить команды приёмки исправления после выкатки",
    )
    assert created.status == "created"
    with store.read() as tx:
        row = tx.one("SELECT owns FROM tasks WHERE project='Fleet Platform'")
    assert "src/dev_mcp/fleet/queue.py" in row["owns"]


def test_source_wait_has_blocking_deploy_marker_for_queue(store):
    routed = _report(store)
    with store.read() as tx:
        block = tx.one(
            "SELECT payload FROM events WHERE kind='block' "
            "AND project='Main Hub' AND tz='TZ-520'"
        )
        wait = tx.one(
            "SELECT payload FROM events WHERE kind='platform_route_wait' "
            "AND project='Main Hub' AND tz='TZ-520'"
        )
    assert json.loads(block["payload"])["change_source"] == "platform_deploy"
    assert json.loads(wait["payload"])["platform_task"] == routed.task
    assert queue.thaw_frozen(store, after_min=0) == []
    assert _state(store, "Main Hub", "TZ-520") == "frozen"


def test_routing_releases_active_source_claim_atomically(store):
    add_worker(store, "W42")
    with store.tx() as tx:
        tx.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('Main Hub','TZ-520','W42','active','2026-10-13T12:00:00+00:00')"
        )
    routed = _report(store)
    assert routed.status == "created"
    with store.read() as tx:
        claim = tx.one(
            "SELECT state,released_at FROM claims WHERE project='Main Hub' AND tz='TZ-520'"
        )
    assert claim["state"] == "released"
    assert claim["released_at"]


def test_distinct_defects_create_distinct_platform_tasks(store):
    one = _report(store)
    two = _report(store, "Main Hub", "TZ-521",
                  summary="Неправильная выдача задач флоту TZ-521")
    assert two.status == "created"
    assert one.task != two.task


def test_unrelated_thaw_event_does_not_bypass_pending_platform_gate(store):
    routed = _report(store)
    _record(store, "thaw", "Main Hub", "TZ-520",
            why="retry timer elapsed")
    with pytest.raises(route.PlatformRoutePending, match=r"запрещены"):
        route.require_request_allowed(store, "Main Hub", "TZ-520")
    assert _report(store).task == routed.task
    assert _report(store).status == "rejected"


def test_open_request_routes_foreign_platform_defect_to_platform(store):
    routed_tz = open_request(
        store, "d3e4f5a6", kind="other",
        summary="Сбой сопоставления команд приёмки TZ-520",
        project="Main Hub", source_tz="TZ-520", to="queue",
        worker_id="W42", path="C:/hub/dev-mcp-work/src/dev_mcp/fleet/acceptance_guard.py",
    )
    assert routed_tz and routed_tz.startswith("request:")
    with store.read() as tx:
        rows = tx.query("SELECT project,tz FROM tasks WHERE kind='request'")
    assert [(row["project"], row["tz"]) for row in rows] == [
        ("Fleet Platform", routed_tz)
    ]
    assert _state(store, "Main Hub", "TZ-520") == "frozen"
    with pytest.raises(route.PlatformRoutePending, match="запрещены"):
        open_request(
            store, "f5e4d3c2", kind="other",
            summary="Ещё одна заявка из ожидающей задачи",
            project="Main Hub", source_tz="TZ-520", to="queue",
            worker_id="W42", path="src/frontend/login.ts",
        )


def test_open_request_keeps_ordinary_foreign_project_request(store):
    new_tz = open_request(
        store, "1122aabb", kind="other",
        summary="Поправить кнопку входа",
        project="Main Hub", source_tz="TZ-520", to="queue",
        worker_id="W42", path="src/frontend/login.ts",
    )
    assert new_tz == "request:1122aabb"
    assert _state(store, "Main Hub", "TZ-520") == "taken"
    assert _state(store, "Main Hub", new_tz) == "ready"


def test_open_request_g1_plan_does_not_freeze_source(store):
    new_tz = open_request(
        store, "aabb1133", kind="other",
        summary="Выдать исполнимую G1-задачу: intent/plan.md + ToDo/tz/TZ-*.md",
        detail="План должен содержать ## Работы / ## Зависимости / ## Приёмка",
        project="Main Hub", source_tz="TZ-520", to="queue", worker_id="W42",
        path="ToDo/tz/tz-g1-offline-code.md",
    )
    assert new_tz == "request:aabb1133"
    assert _state(store, "Main Hub", "TZ-520") == "taken"
    assert _state(store, "Main Hub", new_tz) == "ready"
    with store.read() as tx:
        assert not tx.query("SELECT tz FROM tasks WHERE project='Fleet Platform'")
