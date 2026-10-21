"""TZ-334: server accepts intent stages without burning an agent review."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_mcp.fleet.review import open_review as _open_review
from dev_mcp.fleet.store import Store
from dev_mcp.intents import stages
from dev_mcp.intents.sources.model import IntentEnvelope
from dev_mcp.intents.spawn import spawn_project


def open_review(store,project,tz):
    result = _open_review(store,project,tz)
    if result.startswith('server-pending:'):
        return stages.accept_submitted_stage(store,project,tz)
    return result


def _project(tmp_path: Path, *, triple: bool = False):
    parent = tmp_path / "allowed"
    parent.mkdir()
    env = IntentEnvelope(
        "files", "tz-334", "v1", "Build a verified program",
        {"create_project": True, "project_roots": [str(parent)]},
        "2026-10-14T00:00:00+00:00",
    )
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    spawned = spawn_project(store, env, parent=parent, project_name="StageTest",
                            size="large", allowed_roots=[parent], triple=triple)
    return store, spawned


def _submitted(store: Store, tree, stage: str):
    tz = tree.root_id + "-" + stage
    with store.tx() as tx:
        assert tx.change("UPDATE tasks SET state='submitted' WHERE project=? AND tz=?",
                         (tree.project, tz), event="test.submit").rowcount == 1
    return tz


def _task(store: Store, project: str, tz: str):
    with store.read() as tx:
        return tx.one("SELECT state,body FROM tasks WHERE project=? AND tz=?",
                      (project, tz))


def _review_count(store: Store, project: str):
    with store.read() as tx:
        return int(tx.scalar("SELECT COUNT(*) FROM tasks WHERE project=? AND kind='review'",
                             (project,)))


def _understood(root: Path, kind: str = "build", target: str = "new"):
    doc = root / "intent" / "understood.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    text = (f"kind: {kind}\ntarget: {target}\n"
            + "".join(f"## {h}\n" + "Verified study scope. " * 30 + "\n"
                      for h in ("Интенция", "Готовые решения", "Что уже есть",
                                "Ограничения", "Неясности", "Нужно от владельца")))
    doc.write_text(text, encoding="utf-8")


def test_implemented_green_goes_directly_to_accepted(tmp_path: Path):
    store, spawned = _project(tmp_path)
    path = spawned.directory / "intent" / "scenarios.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text("raise SystemExit(0)\n", encoding="utf-8")
    tz = _submitted(store, spawned.tree, "implemented")
    result = open_review(store, spawned.tree.project, tz)
    assert result == "server-accepted:" + tz
    assert _task(store, spawned.tree.project, tz)["state"] == "accepted"
    assert _review_count(store, spawned.tree.project) == 0
    with store.read() as tx:
        data = [json.loads(row["payload"]) for row in tx.query(
            "SELECT payload FROM events WHERE kind='accept' AND project=? AND tz=?",
            (spawned.tree.project, tz))]
    assert any(d["producer"] == "server-stage-check" for d in data)
    store.close()


def test_stage_scenarios_import_real_project_package_without_pythonpath(tmp_path, monkeypatch):
    from dev_mcp.intents.stage_check import _run
    monkeypatch.delenv('PYTHONPATH', raising=False)
    (tmp_path/'intent').mkdir()
    (tmp_path/'actual_package').mkdir()
    (tmp_path/'actual_package/__init__.py').write_text('VALUE = 42\n')
    scenario = tmp_path/'intent/scenarios.py'
    scenario.write_text('from actual_package import VALUE\nassert VALUE == 42\n')
    assert _run(tmp_path, 'scenarios.py', expect_ok=True) == []
    scenario.write_text('from actual_package import VALUE\nassert VALUE == 43\n')
    assert _run(tmp_path, 'scenarios.py', expect_ok=True)


def test_checker_refresh_requires_known_bytes_and_preserves_backup(tmp_path, monkeypatch):
    import hashlib
    from dev_mcp.intents import stage_check
    store, spawned = _project(tmp_path)
    target = spawned.directory/'.intent/check.py'
    original = b'# known previous server checker\n'
    digest = hashlib.sha256(original).hexdigest()
    monkeypatch.setattr(stages, 'LEGACY_CHECKER_SHA256', {digest})
    target.write_bytes(b'# user custom verifier\n')
    assert stages.refresh_managed_checkers(store) == []
    assert target.read_bytes() == b'# user custom verifier\n'
    target.write_bytes(original)
    assert stages.refresh_managed_checkers(store) == [spawned.project]
    assert target.read_bytes() == Path(stage_check.__file__).read_bytes()
    assert (spawned.directory/'.intent/check-backups'/f'{digest}.py').read_bytes() == original
    assert stages.refresh_managed_checkers(store) == []
    store.close()


def test_mcp_submission_schedules_checker_without_running_it_inline(tmp_path, monkeypatch):
    store, spawned = _project(tmp_path)
    tz = _submitted(store,spawned.tree,'implemented')
    seen = []
    monkeypatch.setattr(stages,'accept_submitted_stage',lambda *a: seen.append(a))
    assert _open_review(store,spawned.tree.project,tz) == 'server-pending:'+tz
    assert _open_review(store,spawned.tree.project,tz) == 'server-pending:'+tz
    assert seen == []
    assert _review_count(store,spawned.tree.project) == 0
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='intent.stage_check_requested'") == 1
    stages.check_submitted_stages(store)
    assert len(seen) == 1


def test_server_result_retires_only_its_old_agent_review(tmp_path):
    from dev_mcp.fleet.review import _open_review_tx
    store, spawned = _project(tmp_path)
    tz = _submitted(store,spawned.tree,'implemented')
    other = _submitted(store,spawned.tree,'clean_start')
    with store.tx() as tx:
        old_review = _open_review_tx(tx,spawned.tree.project,tz)
        other_review = _open_review_tx(tx,spawned.tree.project,other)
    path = spawned.directory/'intent/scenarios.py'
    path.parent.mkdir(exist_ok=True)
    path.write_text('raise SystemExit(0)\n',encoding='utf-8')
    assert stages.accept_submitted_stage(store,spawned.tree.project,tz) == 'server-accepted:'+tz
    assert _task(store,spawned.tree.project,old_review)['state'] == 'accepted'
    assert _task(store,spawned.tree.project,other_review)['state'] == 'ready'


@pytest.mark.parametrize('scheduler_error',[False,True])
def test_delivery_retry_is_scheduled_even_without_a_new_proven_stage(tmp_path,monkeypatch,scheduler_error):
    import threading
    from dev_mcp.intents import remote_repo
    store,_ = _project(tmp_path)
    stop = threading.Event()
    calls=[]
    monkeypatch.setattr(stages,'narrow_stage_owns',lambda *a:None)
    monkeypatch.setattr(stages,'reconcile_all',lambda *a:0)
    monkeypatch.setattr(stages,'_trusted_owner_store',lambda *a:False)
    def prove(_store):
        stop.set()
        return 0
    def schedule(_store):
        calls.append(_store)
        if scheduler_error: raise OSError('fake delivery service offline')
    monkeypatch.setattr(stages,'prove_once',prove)
    monkeypatch.setattr(remote_repo,'schedule_retry',schedule)
    stages.run_stage_prover(store,stop)
    assert calls == [store]


def test_external_wait_is_visible_to_the_launcher_without_author_rework(tmp_path):
    from dev_mcp.fleet.tools import FleetContext,_external_waiting_lines
    store,spawned = _project(tmp_path)
    tz = _submitted(store,spawned.tree,'understood')
    stages._external_pending(store,spawned.tree.project,tz,'digest','down','Gemini offline')
    lines = _external_waiting_lines(FleetContext(store))
    assert any('Gemini offline' in line and 'ожидает внешнюю проверку' in line for line in lines)
    stages._external_resumed(store,spawned.tree.project,tz,'digest')
    assert _external_waiting_lines(FleetContext(store)) == []


def test_implemented_red_reopens_author_with_specific_problem(tmp_path: Path):
    store, spawned = _project(tmp_path)
    path = spawned.directory / "intent" / "scenarios.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text("raise SystemExit(1)\n", encoding="utf-8")
    tz = _submitted(store, spawned.tree, "implemented")
    assert open_review(store, spawned.tree.project, tz) == "server-revise:" + tz
    row = _task(store, spawned.tree.project, tz)
    assert row["state"] == "ready" and "Серверная проверка не прошла" in row["body"]
    assert _review_count(store, spawned.tree.project) == 0
    store.close()


def test_understood_approved_after_single_gemini_critique(tmp_path: Path, monkeypatch):
    store, spawned = _project(tmp_path)
    _understood(spawned.directory)
    seen = []
    def critique(_store, project, stage, root, tz, *, single=False):
        seen.append((project, stage, root, tz, single))
        return []
    monkeypatch.setattr(stages, "_gemini_critique", critique)
    tz = _submitted(store, spawned.tree, "understood")
    assert open_review(store, spawned.tree.project, tz) == "server-accepted:" + tz
    assert seen and seen[0][-1] is True
    assert _task(store, spawned.tree.project, tz)["state"] == "accepted"
    assert _review_count(store, spawned.tree.project) == 0
    store.close()


def test_understood_revise_comes_back_to_author(tmp_path: Path, monkeypatch):
    store, spawned = _project(tmp_path)
    _understood(spawned.directory)
    monkeypatch.setattr(stages, "_gemini_critique",
                        lambda *args, **kwargs: [stages.StageProblem("invalid_result","Gemini потребовал пересмотра: нет доказательств")])
    tz = _submitted(store, spawned.tree, "understood")
    assert open_review(store, spawned.tree.project, tz) == "server-revise:" + tz
    row = _task(store, spawned.tree.project, tz)
    assert row["state"] == "ready" and "нет доказательств" in row["body"]
    assert _review_count(store, spawned.tree.project) == 0
    store.close()


def test_unavailable_gemini_waits_without_reopening_author(tmp_path: Path, monkeypatch):
    store, spawned = _project(tmp_path)
    _understood(spawned.directory)
    monkeypatch.setattr(stages, "_gemini_critique",
                        lambda *args, **kwargs: [
                            stages.StageProblem("external_pending","недоступна независимая критика Gemini (сервер, не агент)")])
    tz = _submitted(store, spawned.tree, "understood")
    assert open_review(store, spawned.tree.project, tz) == "server-wait:" + tz
    assert _task(store, spawned.tree.project, tz)["state"] == "submitted"
    assert _review_count(store, spawned.tree.project) == 0
    store.close()


def test_clean_start_keeps_independent_agent_review(tmp_path: Path):
    store, spawned = _project(tmp_path)
    tz = _submitted(store, spawned.tree, "clean_start")
    reviewed = open_review(store, spawned.tree.project, tz)
    assert reviewed.startswith("review:")
    assert _review_count(store, spawned.tree.project) == 1
    store.close()


def test_regular_project_tz_keeps_independent_agent_review(tmp_path: Path):
    store, spawned = _project(tmp_path)
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (spawned.tree.project, "TZ-777", "Manual", "submitted", 1, "work",
             "", "", "[]", "[]", "[]", "[]", "W-test", "ordinary TZ", "now", "now"),
            event="test.manual_task",
        )
    assert open_review(store, spawned.tree.project, "TZ-777").startswith("review:")
    assert _review_count(store, spawned.tree.project) == 1
    store.close()


def test_triple_candidate_structural_check_replaces_review(tmp_path: Path):
    store, spawned = _project(tmp_path, triple=True)
    stage = "understood"
    tz = stages.candidate_tz(spawned.tree.root_id, stage, 1)
    path = spawned.directory / "intent" / "candidates" / stage / "1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"## {title}\n" + "Proof details. " * 30 + "\n"
                            for title in ("Анализ", "Доказательства", "Вывод")),
                    encoding="utf-8")
    with store.tx() as tx:
        assert tx.change("UPDATE tasks SET state='submitted' WHERE project=? AND tz=?",
                         (spawned.tree.project, tz), event="test.submit").rowcount == 1
    assert open_review(store, spawned.tree.project, tz) == "server-accepted:" + tz
    assert _review_count(store, spawned.tree.project) == 0
    store.close()


def test_understood_invalid_nontriple_routing_not_accepted(tmp_path: Path, monkeypatch):
    store, spawned = _project(tmp_path)
    _understood(spawned.directory, kind="change", target="new")
    monkeypatch.setattr(stages, "_gemini_critique", lambda *args, **kwargs: [])
    tz = _submitted(store, spawned.tree, "understood")
    assert open_review(store, spawned.tree.project, tz) == "server-revise:" + tz
    assert _task(store, spawned.tree.project, tz)["state"] == "ready"
    store.close()
