"""TZ-03: the independent controller owns effects; fleet_list is read-only."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.audit import Audit
from dev_mcp.fleet import controller as controller_module
from dev_mcp.fleet import outcome, queue, wake, world
from dev_mcp.fleet.controller import Action, Controller, controller_health
from dev_mcp.fleet.store import Store
from dev_mcp.policy import Policy
from dev_mcp.server import build_server


def test_stage_with_resolver_does_not_crash_all_project_discovery(tmp_path):
    store = _store(tmp_path)
    outcome.create_outcome(store, project='demo', title='implemented', intent_id='intent',
                           spec={'stage': 'implemented'}, evidence_spec={'kind': 'run'},
                           authority={'source': 'owner'}, outcome_id='intent:implemented')
    for tz in ('intent-implemented', 'triage:resolver'):
        _task(store, tz, state='accepted')
        outcome.link_work(store, outcome_id='intent:implemented', project='demo', tz=tz)
    controller = Controller(store, maintenance=lambda: None)
    controller.tick()
    with store.read() as tx:
        assert not json.loads(tx.one("SELECT payload FROM events WHERE kind='controller_tick' "
                                     "ORDER BY id DESC LIMIT 1")['payload'])['errors']


def test_discovery_failure_does_not_skip_independent_intent_reconciliation(tmp_path, monkeypatch):
    store = _store(tmp_path)
    controller = Controller(store, maintenance=lambda: None)
    seen = []
    def broken():
        raise RuntimeError('malformed legacy outcome')
    monkeypatch.setattr(controller, '_discover', broken)
    monkeypatch.setattr(controller, '_reconcile_intent_events', lambda errors: seen.append('reconciled') or 0)
    controller.tick()
    assert seen == ['reconciled']
    assert 'ошибки' in controller_health(store)


def _store(tmp_path: Path) -> Store:
    store = Store.open(str(tmp_path / "data" / "fleet" / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("demo", str(tmp_path)), event="seed")
    return store


def _outcome(store: Store, identifier: str, *, evidence: dict | None = None) -> None:
    outcome.create_outcome(store, project="demo", title=identifier, intent_id="intent",
                           spec={"goal": identifier}, evidence_spec=evidence or {"kind": "run"},
                           authority={"source": "owner"}, outcome_id=identifier)


@pytest.mark.parametrize('supplier_state,needs_plan', [('open', False), ('done', True),
                                                    ('dropped', True), ('missing', True)])
def test_future_stage_waits_for_real_supplier_without_a_planning_task(tmp_path, supplier_state, needs_plan):
    store = _store(tmp_path)
    if supplier_state != 'missing':
        _outcome(store, 'supplier')
        if supplier_state != 'open':
            with store.tx() as tx:
                tx.change('UPDATE outcomes SET state=? WHERE id=?', (supplier_state, 'supplier'), event='seed')
    outcome.create_outcome(store, project='demo', title='future', intent_id='intent',
                           spec={'stage': 'delivered', 'depends_on': ['supplier']},
                           evidence_spec={'kind': 'run'}, authority={'source': 'owner'}, outcome_id='future')
    actions = Controller(store, maintenance=lambda: None)._discover()
    assert any(a.outcome_id == 'future' and a.kind == 'plan' for a in actions) is needs_plan


def _task(store: Store, tz: str, *, state: str = "ready", resources: str = "[]") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("""INSERT INTO tasks
            (project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,resources,
             acceptance,author_worker,body,created_at,updated_at)
             VALUES(?,?,?, ?,1,'work','','','[]','[]',?,'[]',NULL,'',?,?)""",
                  ("demo", tz, tz, state, resources, now, now), event="seed")


def _draft(tmp_path: Path, tz: str = "TZ-900") -> Path:
    path = tmp_path / "ToDo" / "tz-drafts" / f"{tz}-controller-integration.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "```yaml\n"
        + f"tz: {tz}\n"
        + "owns: [src/draft_outcome.py]\n"
        + "acceptance: [python -m pytest -q]\n"
        + "```\n\n# Проверяемый результат\n", encoding="utf-8",
    )
    return path


def _draft_policy(tmp_path: Path) -> dict:
    return {"roots": [str(tmp_path)], "write_roots": [str(tmp_path)],
            "allow_write": True}


def test_controller_registers_draft_once_with_trusted_fixture_runner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _draft(tmp_path)
    checks = []

    def fixture_probe(argv, snapshot):
        assert argv == ("python", "-m", "pytest", "-q")
        assert snapshot.is_dir() and snapshot != tmp_path
        assert not any(snapshot.iterdir())
        checks.append(snapshot)
        return 1  # A red baseline in an empty fixture is NOT an acceptance failure.

    controller = Controller(store, policy=_draft_policy(tmp_path),
                            maintenance=lambda: None, draft_runner=fixture_probe)
    assert controller.tick() >= 1
    assert not source.exists()
    assert (tmp_path / "ToDo/tz/TZ-900-controller-integration.md").exists()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-900'") == "ready"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='draft_registered'") == 1
    controller.tick()
    assert len(checks) == 1


def test_foreign_database_cannot_spawn_draft_preflight(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    source = _draft(tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("foreign database invoked a subprocess")

    monkeypatch.setattr(controller_module.subprocess, "run", forbidden)
    controller = Controller(store, policy=_draft_policy(tmp_path),
                            maintenance=lambda: None)
    controller.tick()
    assert source.is_file()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz='TZ-900'") == 0
        invalid = tx.query("SELECT payload FROM events WHERE kind='draft_invalid'")
    assert len(invalid) == 1
    assert "dry_run_unavailable" in invalid[0]["payload"]


def test_draft_runner_only_trusted_local_store_and_isolated_pytest(tmp_path: Path, monkeypatch) -> None:
    own_db = (Path(controller_module.__file__).resolve().parents[3] /
              "data" / "fleet" / "fleet.sqlite")
    assert controller_module._trusted_draft_runner(own_db) is controller_module._draft_fixture_probe
    assert controller_module._trusted_draft_runner(tmp_path / "fleet.sqlite") is None
    launches = []

    def fake_run(argv, **kwargs):
        launches.append((argv, kwargs))
        return type("Result", (), {"returncode": 5})()

    monkeypatch.setattr(controller_module.subprocess, "run", fake_run)
    assert controller_module._draft_fixture_probe(
        ("python", "-m", "pytest", "-q"), tmp_path) == 5
    assert len(launches) == 1
    argv, options = launches[0]
    assert argv[:5] == [sys.executable, "-I", "-m", "pytest", "--noconftest"]
    assert options["cwd"] == str(tmp_path)
    assert options["timeout"] <= 12
    assert options["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    with pytest.raises(ValueError, match="draft_probe_executable_not_allowed"):
        controller_module._draft_fixture_probe(("cmd.exe", "/c", "evil"), tmp_path)
    with pytest.raises(ValueError, match="draft_probe_option_not_allowed"):
        controller_module._draft_fixture_probe((
            "python", "-m", "pytest", "--pyargs", "outside"), tmp_path)
    assert len(launches) == 1


def test_controller_thaws_on_sensor_event_without_fleet_take(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(world, "config", lambda: {"phone": {"sensor": "adb", "world": True}})
    world.record(store, {"phone": (world.DOWN, "disconnected")})
    _task(store, "TZ-186", state="frozen", resources='["phone"]')
    with store.tx() as tx:
        tx.record_event(kind="block", project="demo", tz="TZ-186",
                        payload={"reason": "blocked: phone physically unavailable"})

    monkeypatch.setattr(world, "tick", lambda db: world.record(
        db, {"phone": (world.UP, "connected")}))
    # The real maintenance sequence (sensor then thaw) is driven by the
    # controller's own tick; neither list nor take is called.
    controller = Controller(store)
    controller.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-186'") == "ready"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='controller_tick'") == 1


def test_reservation_crash_replays_idempotently(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _outcome(store, "safe")
    controller = Controller(store, maintenance=lambda: None)
    controller._schema()
    action = Action("safe", 1, "plan", "demo", detail="missing work")
    assert controller._reserve(action)
    assert not controller._reserve(action)
    controller.tick()  # restart after reservation, before effect
    controller.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM outcome_work WHERE outcome_id='safe'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM controller_actions WHERE kind='plan'") == 1
        assert tx.scalar("SELECT status FROM controller_actions WHERE kind='plan'") == "complete"


def test_planning_triage_has_writable_owns_and_linked_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _outcome(store, "unserved")
    controller = Controller(store, maintenance=lambda: None)
    assert controller.tick() == 1
    with store.read() as tx:
        links = tx.query("SELECT project,tz FROM outcome_work WHERE outcome_id='unserved'")
        assert len(links) == 1
        row = tx.one("SELECT kind,state,owns,body FROM tasks WHERE project=? AND tz=?",
                     (links[0]["project"], links[0]["tz"]))
        assert row["kind"] == "triage" and row["state"] == "ready"
        assert json.loads(row["owns"]) and "plan:unserved" in row["body"]
    assert outcome.next_step(store, "unserved").kind == "work"
    assert not any(v.code == "no_next_step" and v.outcome_id == "unserved"
                   for v in outcome.check_invariant(store))
    assert controller.tick() == 0
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM outcome_work WHERE outcome_id='unserved'") == 1
    # A completed planning task without server evidence must lead to a new,
    # executable proof task, not an idempotent dead end.
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project=? AND tz=?",
                  (links[0]["project"], links[0]["tz"]), event="seed")
    assert controller.tick() >= 1
    with store.read() as tx:
        proof = tx.one("SELECT t.state,t.owns FROM outcome_work w JOIN tasks t "
                       "ON w.project=t.project AND w.tz=t.tz "
                       "WHERE w.outcome_id='unserved' AND t.body LIKE '%subject=plan:proof:unserved%'")
        assert proof and proof["state"] == "ready" and json.loads(proof["owns"])
    writer = outcome.server_evidence_writer(store)
    outcome.add_evidence(store, writer=writer, outcome_id="unserved", kind="run",
                         ref="verified by server", subject_version="v1",
                         env_version="e1", verdict="pass", producer="server")
    controller.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM outcomes WHERE id='unserved'") == "done"


def test_presence_is_persisted_separately_and_list_stays_read_only(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    path = tmp_path / "policy.toml"
    path.write_text(f"roots = ['{tmp_path.as_posix()}']\n"
                    f"write_roots = ['{tmp_path.as_posix()}']\n"
                    f"exec_roots = ['{tmp_path.as_posix()}']\n"
                    "exec_allowed_bin = ['python']\nallow_write = false\n",
                    encoding="utf-8")
    server = build_server(Policy.load(path), Audit(tmp_path / "logs" / "audit.jsonl"))
    presence = asyncio.run(server.get_tool("fleet_presence")).fn
    label = "f-test-presence"
    url = "https://chatgpt.com/c/deadbeef"
    assert "1" in presence(launcher_chats=f"{label} {url}")
    book = wake.ChatBook(Path(store.path).with_name("chats.json"))
    assert book.url(label) == url
    stored = book.path.read_bytes()
    listing = asyncio.run(server.get_tool("fleet_list")).fn
    with store.read() as tx:
        before = tx.scalar("SELECT COUNT(*) FROM events")
    assert "controller:" in asyncio.run(listing(launcher_chats=f"{label} https://chatgpt.com/c/changed"))
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events") == before
    assert book.path.read_bytes() == stored
    from dev_mcp.fleet import tools as fleet_tools
    def expensive(*_):
        raise AssertionError('browser loop requested a full diagnostic report')
    monkeypatch.setattr(fleet_tools,'_project_blocker_lines',expensive)
    monkeypatch.setattr(fleet_tools,'_external_waiting_lines',expensive)
    compact=asyncio.run(listing(launcher_version='0.19.2'))
    assert 'очередь:' in compact and 'агенты:' in compact and 'launcher:' in compact
    with store.read() as tx:
        assert tx.scalar('SELECT COUNT(*) FROM events')==before
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _task(store, "TZ-WAKE", state="taken")
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES('W-P','f-test-presence','chatgpt',?,?)",
                  (stale, stale), event="seed")
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('demo','TZ-WAKE','W-P','active',?)", (stale,), event="seed")
    orders = wake.orders(store, book)
    assert len(orders) == 1 and orders[0]["url"] == url


def test_invariant_produces_one_durable_action_and_self_wait_research(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _outcome(store, "orphan")
    _outcome(store, "TZ-186")
    outcome.open_obstacle(store, outcome_id="TZ-186", obstacle_id="self-obstacle",
                          type="needs_result", cause_code="producer_failed",
                          predicate={"requires_outcome": "TZ-186"}, change_source="TZ-186")
    ctl = Controller(store, maintenance=lambda: None)
    ctl.tick()
    ctl.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE body LIKE '%subject=plan:orphan%'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE body LIKE '%subject=research:TZ-186%'") == 1
        assert tx.scalar("SELECT type FROM obstacles WHERE id='self-obstacle'") == "unknown_method"


def test_source_predicate_rechecks_only_for_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _outcome(store, "consumer")
    _outcome(store, "supplier")
    oid = outcome.open_obstacle(store, outcome_id="consumer", type="needs_result",
                                cause_code="producer_failed", predicate={"requires_outcome": "supplier"},
                                change_source="supplier", obstacle_id="waiting")
    ctl = Controller(store, maintenance=lambda: None)
    ctl.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM obstacles WHERE id=?", (oid,)) == "open"
        before = tx.scalar("SELECT COUNT(*) FROM controller_actions WHERE kind LIKE 'source:%'")
    ctl.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM controller_actions WHERE kind LIKE 'source:%'") == before
    outcome.close_outcome(store, "supplier")
    ctl.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM obstacles WHERE id=?", (oid,)) == "resolved"


def test_evidence_required_before_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _outcome(store, "deliver", evidence={"kind": "run", "subject_version": "v1"})
    _task(store, "TZ-1", state="accepted")
    outcome.link_work(store, outcome_id="deliver", project="demo", tz="TZ-1")
    ctl = Controller(store, maintenance=lambda: None)
    ctl.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM outcomes WHERE id='deliver'") == "open"
    with store.tx() as tx:
        tx.change("""INSERT INTO evidence(id,outcome_id,kind,ref,subject_version,env_version,
            verdict,producer,ts) VALUES('e1','deliver','run','verified','v1','env','pass','server',?)""",
                  (datetime.now(timezone.utc).isoformat(),), event="seed")
    ctl.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM outcomes WHERE id='deliver'") == "done"


def test_list_is_pure_read_even_with_stale_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with store.tx() as tx:
        tx.change("""INSERT INTO workers(id,label,kind,first_seen,last_seen)
            VALUES('W1','old','chatgpt',?,?)""", (stale, stale), event="seed")
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(f"roots = ['{tmp_path.as_posix()}']\n"
                           f"write_roots = ['{tmp_path.as_posix()}']\n"
                           f"exec_roots = ['{tmp_path.as_posix()}']\n"
                           "exec_allowed_bin = ['python']\nallow_write = false\n",
                           encoding="utf-8")
    server = build_server(Policy.load(policy_path), Audit(tmp_path / "logs" / "audit.jsonl"))
    fn = asyncio.run(server.get_tool("fleet_list")).fn
    with store.read() as tx:
        before = {table: [tuple(r) for r in tx.query(f"SELECT * FROM {table}")]
                  for table in ("workers", "tasks", "events", "claims", "messages")}
    assert "controller:" in asyncio.run(fn())
    with store.read() as tx:
        after = {table: [tuple(r) for r in tx.query(f"SELECT * FROM {table}")]
                 for table in before}
    assert before == after


def test_empty_corrupt_json_and_large_queue(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctl = Controller(store, maintenance=lambda: None)
    assert ctl.tick() == 0  # empty, no request side effect
    _outcome(store, "broken")
    outcome.open_obstacle(store, outcome_id="broken", type="needs_result",
                          cause_code="procedure_unknown", predicate={"requires_outcome": "x"},
                          change_source="x", obstacle_id="bad")
    with store.tx() as tx:
        tx.change("UPDATE obstacles SET predicate='{broken' WHERE id='bad'", event="seed")
    ctl.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM outcomes WHERE id='broken'") == "open"
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE body LIKE '%subject=research:broken%'") == 1

    timestamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        for i in range(1200):
            tx.change("""INSERT INTO outcomes(id,parent_id,project,title,intent_id,spec,evidence_spec,
                authority,state,rev,created_ts,closed_ts)
                VALUES(?,NULL,'demo','bulk','intent','{}','{}','{}','open',1,?,NULL)""",
                (f"bulk-{i}", timestamp), event="seed")
    # Budget CPU work, not scheduler wait: shared CI hosts can pause this
    # process during a healthy controller tick and falsely fail the gate.
    started = time.process_time()
    ctl.tick()
    assert time.process_time() - started < 2.0
    assert "ПРОПУСК >5 мин" in controller_health(
        store, now=datetime.now(timezone.utc) + timedelta(minutes=6))
