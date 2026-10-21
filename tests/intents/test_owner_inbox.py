"""Owner intent inbox → registry → controller → spawned project and outcome tree."""
from pathlib import Path

from dev_mcp.fleet import controller as ctl
from dev_mcp.fleet.store import Store


def test_owner_intent_file_spawns_project_and_outcomes(monkeypatch, tmp_path: Path) -> None:
    inbox, parent = tmp_path / "intents", tmp_path / "hub"
    inbox.mkdir(); parent.mkdir()
    monkeypatch.setattr(ctl, "INTENT_INBOX", inbox)
    monkeypatch.setattr(ctl, "INTENT_PARENT", parent)
    monkeypatch.setattr(ctl, "INTENT_ROOTS", (parent,))
    monkeypatch.setattr(ctl, "wants_triple", lambda _raw: True)  # тройка — только по просьбе (14.10)
    (inbox / "05-Example Library.md").write_text("хочу локальный архив", encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))

    ctl._poll_owner_intents(store)
    controller = ctl.Controller(store, intent_project=ctl.owner_intent_project,
                                maintenance=lambda: None)
    controller.tick()

    assert (parent / "example-library" / "component.json").is_file()
    with store.read() as tx:
        assert tx.one("SELECT name FROM projects WHERE name='example-library'")
        stages = tx.query("SELECT id FROM outcomes WHERE project='example-library'")
    assert len(stages) >= 9
    # A second poll of the unchanged file is not a new intent.
    ctl._poll_owner_intents(store)
    controller.tick()
    with store.read() as tx:
        assert len(tx.query("SELECT id FROM outcomes WHERE project='example-library'")) == len(stages)


def test_unreadable_inbox_is_visible_not_silent(monkeypatch, tmp_path: Path) -> None:
    inbox = tmp_path / "intents"; inbox.mkdir()
    monkeypatch.setattr(ctl, "INTENT_INBOX", inbox)
    (inbox / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    ctl._poll_owner_intents(store)
    with store.read() as tx:
        assert tx.one("SELECT id FROM events WHERE kind='intent_poll_incomplete'")


def _spawned(monkeypatch, tmp_path: Path):
    inbox, parent = tmp_path / "intents", tmp_path / "hub"
    inbox.mkdir(); parent.mkdir()
    monkeypatch.setattr(ctl, "INTENT_INBOX", inbox)
    monkeypatch.setattr(ctl, "INTENT_PARENT", parent)
    monkeypatch.setattr(ctl, "INTENT_ROOTS", (parent,))
    monkeypatch.setattr(ctl, "wants_triple", lambda _raw: True)  # тройка — только по просьбе (14.10)
    (inbox / "demo.md").write_text("сделай демо", encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    ctl._poll_owner_intents(store)
    controller = ctl.Controller(store, intent_project=ctl.owner_intent_project,
                                maintenance=lambda: None)
    controller.tick()
    return store, controller, parent / "demo"


def _state(store, stage):
    with store.read() as tx:
        return tx.one("SELECT state,body,acceptance FROM tasks WHERE project='demo' AND tz LIKE ?",
                      (f"%-{stage}",))


def _accept(store, stage):
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='demo' AND tz LIKE ?",
                  (f"%-{stage}",), event="test.accept")


def test_stage_needs_server_proof_then_unlocks_next(monkeypatch, tmp_path: Path) -> None:
    from dev_mcp.intents import stages
    from dev_mcp.fleet import gemini
    store, controller, project = _spawned(monkeypatch, tmp_path)
    task = _state(store, "understood")
    assert "Готовые решения" in task["body"] and "check.py understood" in task["acceptance"]
    assert (project / ".intent" / "check.py").is_file()
    assert (project / ".intent" / "intent.md").read_text(encoding="utf-8") == "сделай демо"

    _accept(store, "understood")            # accepted without the document
    stages.prove_once(store)
    assert _state(store, "understood")["state"] == "ready"
    assert "нет intent/understood.md" in _state(store, "understood")["body"]

    (project / "intent").mkdir()
    sections = ["Интенция", "Готовые решения", "Что уже есть", "Ограничения",
                "Неясности", "Нужно от владельца"]
    (project / "intent" / "understood.md").write_text(
        "kind: build\ntarget: new\n" +
        "\n".join(f"## {h}\n" + "текст " * 20 for h in sections), encoding="utf-8")
    with store.read() as tx:
        stage_tz = tx.one("SELECT tz FROM tasks WHERE project='demo' "
                          "AND tz LIKE '%-understood'")["tz"]
    root_id = stage_tz.removesuffix("-understood")
    for i in (1, 2, 3):
        path = project / "intent" / "candidates" / "understood" / f"{i}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"## {h}\n" + "вывод " * 30
                                  for h in ("Анализ", "Доказательства", "Вывод")),
                        encoding="utf-8")
    consensus = project / "intent" / "consensus" / "understood.md"
    consensus.parent.mkdir(parents=True, exist_ok=True)
    consensus.write_text("\n".join(f"## {h}\n" + "замер " * 35 for h in
                                   ("Сравнение", "Расхождения", "Спайк", "Решение")),
                         encoding="utf-8")
    with store.tx() as tx:
        for i in range(1, 5):
            worker = f"worker-{i}"
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES(?,?,'chatgpt','now','now')", (worker, worker),
                      event="test.worker")
            task = stages.candidate_tz(root_id, "understood", i) if i <= 3 else stage_tz
            if i <= 3:
                tx.change("UPDATE tasks SET state='accepted' WHERE project='demo' AND tz=?",
                          (task,), event="test.accept")
            tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
                      "VALUES('demo',?,?,'released','now','now')",
                      (task, worker), event="test.claim")
    monkeypatch.setattr(gemini, "ask", lambda *args, **kwargs: {
        "verdict": "approve", "critique": "Независимая критика и проверка спайков"})
    _accept(store, "understood")
    assert stages.prove_once(store) == 1
    controller.tick(); controller.tick()
    assert _state(store, "executable_goal")["state"] == "ready"


def test_stage_reopen_is_bounded_then_stuck(monkeypatch, tmp_path: Path) -> None:
    from dev_mcp.intents import stages
    store, controller, _ = _spawned(monkeypatch, tmp_path)
    for _ in range(stages.MAX_REOPEN):
        _accept(store, "understood"); stages.prove_once(store)
    _accept(store, "understood"); stages.prove_once(store); stages.prove_once(store)
    assert _state(store, "understood")["state"] == "accepted"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='intent.stage_stuck'") == 1


def test_stage_owns_are_narrow_and_old_wide_zones_heal(monkeypatch, tmp_path: Path) -> None:
    import json
    from dev_mcp.intents import stages
    store, _, project = _spawned(monkeypatch, tmp_path)
    with store.read() as tx:
        owns = json.loads(tx.one("SELECT owns FROM tasks WHERE project='demo' AND tz LIKE '%-executable_plan'")["owns"])
    assert owns == [str(project / "intent/plan.md"), str(project / "ToDo/tz-drafts")]
    with store.tx() as tx:
        tx.change("UPDATE tasks SET owns=? WHERE project='demo' AND tz LIKE '%-quality'",
                  (json.dumps([str(project)]),), event="test.widen")
    assert stages.narrow_stage_owns(store) == 1
    with store.read() as tx:
        owns = json.loads(tx.one("SELECT owns FROM tasks WHERE project='demo' AND tz LIKE '%-quality'")["owns"])
    assert owns == [str(project / "intent/lenses.md"), str(project / "ToDo/tz-drafts")]


def test_stage_workspace_migration_preserves_taken_claims(monkeypatch, tmp_path):
    import json
    from dev_mcp.intents import stages
    store, _, project = _spawned(monkeypatch, tmp_path)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET owns=?,state='taken' WHERE project='demo' AND tz LIKE '%-quality'",
                  (json.dumps([str(project)]),), event="test.active")
    assert stages.narrow_stage_owns(store) == 0
    with store.read() as tx:
        assert json.loads(tx.one("SELECT owns FROM tasks WHERE tz LIKE '%-quality'")["owns"]) == [str(project)]


def test_plan_stage_needs_registered_tz(monkeypatch, tmp_path: Path) -> None:
    from dev_mcp.intents import stages
    store, _, project = _spawned(monkeypatch, tmp_path)
    (project / "ToDo" / "tz").mkdir(parents=True)
    (project / "ToDo" / "tz" / "TZ-01-x.md").write_text("x", encoding="utf-8")
    assert stages._plan_registered(store, "demo", project) == ["ТЗ не зарегистрированы во флоте: TZ-01"]
    (project / "ToDo" / "tz-drafts").mkdir()
    (project / "ToDo" / "tz-drafts" / "TZ-02-y.md").write_text("y", encoding="utf-8")
    assert "черновики" in stages._plan_registered(store, "demo", project)[0]


def test_intent_project_gets_git_and_commit_default(monkeypatch, tmp_path: Path) -> None:
    store, _, project = _spawned(monkeypatch, tmp_path)
    assert (project / ".git").is_dir()
    from dev_mcp.fleet import goals
    monkeypatch.setattr(goals, "_load", lambda: {})
    import dev_mcp.fleet.goals as g
    monkeypatch.setattr(g.machine, 'WORKSPACES', (tmp_path / 'hub',))
    assert goals.project_info("demo") == {"commit": True}
    assert goals.project_info("nope") == {}


def test_intent_name_colliding_with_component_folder_gets_suffix(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace
    parent = tmp_path / "hub"; (parent / "ai-relay").mkdir(parents=True)
    monkeypatch.setattr(ctl, "INTENT_PARENT", parent)
    change = SimpleNamespace(envelope=SimpleNamespace(external_id="ai-relay.md"))
    assert ctl.owner_intent_project(change).project_name == "ai-relay-intent"
