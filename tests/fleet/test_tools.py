from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.audit import Audit
from dev_mcp.fleet import guard, hazard, inbox
from dev_mcp.fleet.model import Decision, norm_path
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.controller import Controller
from dev_mcp.fleet.tools import OWNER_COMMANDS
from dev_mcp.fleet import net_sensor, supervisor, wake, world
from dev_mcp.fleet.tools import FleetContext, _tick_chatgpt_sensor
from dev_mcp.policy import Policy
import dev_mcp.server as server_mod
from dev_mcp.server import build_server


def test_web_owner_console_has_no_permanent_freeze_gate() -> None:
    assert "freeze" not in OWNER_COMMANDS
    assert "approve" not in OWNER_COMMANDS
    assert "reactivate" in OWNER_COMMANDS
    assert "recover" in OWNER_COMMANDS


def _make_server(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{root}']\n"
        f"write_roots = ['{root}']\n"
        f"exec_roots = ['{root}']\n"
        "exec_allowed_bin = ['python']\n"
        "allow_write = false\n"
        "allow_network_fetch = true\n"
        "deny_patterns = ['.env', '.git']\n"
        "exec_timeout_cap_s = 30\n"
        "[tasks.echo]\n"
        f"cwd = '{root}'\n"
        "argv = ['python', '-c', 'print(\"task-ok\")']\n"
        "description = 'test task'\n",
        encoding="utf-8",
    )
    policy = Policy.load(config)
    audit = Audit(tmp_path / "logs" / "audit.jsonl")
    server = build_server(policy, audit)
    store = Store.open(str(tmp_path / "data" / "fleet" / "fleet.sqlite"))
    return server, store, root, audit


def _tool_fn(server, name: str):
    tool = asyncio.run(server.get_tool(name))
    assert tool is not None
    if asyncio.iscoroutinefunction(tool.fn):
        return lambda *args, **kwargs: asyncio.run(tool.fn(*args, **kwargs))
    return tool.fn


def test_queue_query_survives_saturated_mcp_thread_pool(tmp_path: Path) -> None:
    import threading
    import anyio

    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)

    async def scenario() -> None:
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        started, release = threading.Event(), threading.Event()
        def occupying_call() -> None:
            started.set()
            release.wait(10)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(anyio.to_thread.run_sync, occupying_call)
                while not started.is_set():
                    await anyio.sleep(0.01)
                try:
                    with anyio.fail_after(3):
                        tool = await server.get_tool("fleet_list")
                        result = await tool.run({})
                        assert not result.is_error
                        assert any("очередь:" in c.text for c in result.content)
                finally:
                    release.set()
        finally:
            release.set()
            limiter.total_tokens = previous
    anyio.run(scenario)


def _seed_task(store: Store, root: Path, *, tz: str = "TZ-X") -> None:
    now = datetime.now(timezone.utc).isoformat()
    owned = norm_path(str(root / "owned.txt"))
    with store.tx() as tx:
        if tx.one("SELECT name FROM projects WHERE name=?", ("demo",)) is None:
            tx.change(
                "INSERT INTO projects(name,root,priority,active) VALUES (?,?,?,1)",
                ("demo", str(root), 1),
                event={"kind": "seed"},
            )
        tx.change(
            """INSERT INTO tasks(
                   project,tz,title,state,priority,kind,spec_path,spec_sha,
                   needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "demo",
                tz,
                "Facade task",
                "ready",
                1,
                "work",
                f"ToDo/{tz}.md",
                "sha",
                "[]",
                json.dumps([owned]),
                json.dumps(["stand"]),
                json.dumps(["pytest -q"]),
                None,
                "Do the facade work.",
                now,
                now,
            ),
            event={"kind": "seed"},
        )


def test_shared_review_generic_submission_does_not_require_isolated_git_seal(tmp_path):
    server,store,root,_audit=_make_server(tmp_path)
    _seed_task(store,root)
    (root/'AGENTS.md').write_text('shared reviewer instructions',encoding='utf-8')
    owned=root/'owned.txt'
    owned.write_text('before',encoding='utf-8')
    join=_tool_fn(server,'fleet_join')
    take=_tool_fn(server,'fleet_take')
    join(label='shared-author')
    take(agent='W1')
    _tool_fn(server,'write_file')(path=str(owned),content='after',overwrite=True,agent='W1')
    submitted=_tool_fn(server,'fleet_submit')(agent='W1',note='changed owned file')
    assert 'Отказ' not in submitted,submitted
    with store.tx() as tx:
        claim=tx.one("SELECT id FROM claims WHERE worker_id='W1' ORDER BY id DESC LIMIT 1")
        tx.record_event(kind='claim_submission',project='demo',tz='TZ-X',worker_id='W1',
                        payload={'claim_id':claim['id'],'author':'W1','manifest':'shared source record'})
    join(label='shared-reviewer')
    taken=take(agent='W2')
    assert 'review:TZ-X' in taken,taken
    read=_tool_fn(server,'read_file')(path=str(root/'AGENTS.md'),agent='W2')
    assert 'shared reviewer instructions' in read,read
    assert 'FLEET_SEALED_REVIEW' not in read


@pytest.mark.parametrize("exit_code", [0, 7])
def test_explicit_background_run_records_final_rc_in_original_claim(tmp_path, exit_code):
    import time

    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    _tool_fn(server, "fleet_join")("background-proof")
    _tool_fn(server, "fleet_take")("W1")
    launched = _tool_fn(server, "run")(
        root=str(root), argv=["python", "-c", f"import sys; sys.exit({exit_code})"],
        background=True, agent="W1")
    match = re.search(r"фоновая задача: ([0-9a-f]{12})", launched)
    assert match, launched
    _tool_fn(server, "run_status")(match.group(1))
    deadline = time.monotonic() + 3
    rows = []
    while time.monotonic() < deadline:
        with store.read() as tx:
            rows = tx.query("SELECT project,tz,rc,payload FROM events WHERE kind='run' AND rc IS NOT NULL")
        if rows:
            break
        time.sleep(0.01)
    assert [(r["project"], r["tz"], r["rc"]) for r in rows] == [("demo", "TZ-X", exit_code)]
    assert json.loads(rows[0]["payload"])["promoted"] == match.group(1)


def test_background_completion_cannot_credit_successor_after_handoff(tmp_path, monkeypatch):
    import threading
    from dev_mcp.runner import JobRegistry

    original = JobRegistry.when_done
    completed, callbacks = threading.Event(), []
    def defer(registry, job_id, callback):
        def capture(job):
            callbacks.append(lambda: callback(job))
            completed.set()
        original(registry, job_id, capture)
    monkeypatch.setattr(JobRegistry, "when_done", defer)
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    _tool_fn(server, "fleet_join")("background-handoff")
    _tool_fn(server, "fleet_take")("W1")
    launched = _tool_fn(server, "run")(
        root=str(root), argv=["python", "-c", "print('done')"],
        background=True, agent="W1")
    assert "фоновая задача:" in launched, launched
    assert completed.wait(5)
    _seed_task(store, root, tz="TZ-NEXT")
    with store.tx() as tx:
        tx.change("UPDATE claims SET state='released' WHERE worker_id='W1'", event="test_handoff")
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='TZ-X'", event="test_handoff")
    _tool_fn(server, "fleet_take")("W1")
    callbacks[0]()
    with store.read() as tx:
        rows = tx.query("SELECT tz,rc FROM events WHERE kind='run' AND rc IS NOT NULL")
    assert [(r["tz"], r["rc"]) for r in rows] == [("TZ-X", 0)]


def test_old_client_can_use_visual_service_through_existing_run(tmp_path, monkeypatch):
    from dev_mcp.fleet import gemini

    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    (root / "owned.txt").write_bytes(b"test")
    frame = root / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nframe")
    _tool_fn(server, "fleet_join")("visual-old-client")
    _tool_fn(server, "fleet_take")("W1")
    with store.tx() as tx:
        claim = tx.one("SELECT id FROM claims WHERE worker_id='W1' AND state='active'")
        tx.change("INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
                  (claim["id"], norm_path(str(frame))), event="seed")
    calls = []
    def inspect(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": False, "defects": [{"severity": "P1", "where": "heading", "what": "bad spacing"}]}
    monkeypatch.setattr(gemini, "ask", inspect)
    response = _tool_fn(server, "run")(
        root=str(root), argv=["fleet.cmd", "look", "frame.png", "--rubric", "readability"], agent="W1")
    assert "bad spacing" in response, response
    assert len(calls) == 1
    denied = _tool_fn(server, "run")(
        root=str(root), argv=["fleet.cmd", "look", "frame.png"], agent="W-missing")
    assert "нет активного" in denied
    assert len(calls) == 1


def test_all_fleet_tools_are_directly_callable(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)

    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_finish = _tool_fn(server, "fleet_finish")
    fleet_submit = _tool_fn(server, "fleet_submit")
    fleet_list = _tool_fn(server, "fleet_list")
    fleet_inbox = _tool_fn(server, "fleet_inbox")
    fleet_ack = _tool_fn(server, "fleet_ack")
    fleet_send = _tool_fn(server, "fleet_send")

    joined = fleet_join("tab one")
    assert "worker_id: W1" in joined
    second = fleet_join("tab two")
    assert "worker_id: W2" in second

    taken = fleet_take("W1")
    assert "demo/TZ-X" in taken
    assert str(root / "ToDo" / "TZ-X.md") in taken
    assert "owns:" in taken
    assert "resources:" in taken
    assert "acceptance:" in taken
    assert "Соседи по путям:" in taken
    assert "Рабочая памятка:" in taken

    listed = fleet_list()
    assert "W1" in listed
    assert "demo/TZ-X" in listed
    assert "launcher:" in listed

    launched = fleet_list("0.15.0", 3, 1)
    # TZ-03: list is a pure query, so launcher telemetry does not update DB.
    assert "extension=alive v0.15.0" not in launched

    sent = fleet_send("W1", "W2", "note", "interface published")
    match = re.search(r"(m-[0-9a-f]+)", sent)
    assert match is not None
    message_id = match.group(1)

    received = fleet_inbox("W2")
    assert message_id in received
    assert "interface published" in received

    acked = fleet_ack("W2", [message_id])
    assert "1" in acked
    assert message_id not in fleet_inbox("W2")

    (root / "owned.txt").write_text("done\n", encoding="utf-8")  # работа в зоне задачи
    submitted = fleet_submit("W1", "ready for review")
    assert "сдана на приёмку" in submitted
    assert "review:TZ-X" in submitted
    with store.tx() as tx:
        review_row = tx.one(
            "SELECT kind,state,author_worker FROM tasks WHERE project=? AND tz=?",
            ("demo", "review:TZ-X"),
        )
    assert review_row is not None
    assert review_row["kind"] == "review"
    assert review_row["state"] == "ready"
    assert review_row["author_worker"] == "W1"

    finished = fleet_finish("W1", "done")
    assert "освобождены" in finished


def test_cached_blind_seal_cannot_supply_claim_or_skip_verifier(tmp_path):
    server, store, root, _ = _make_server(tmp_path)
    run = _tool_fn(server, 'run')
    assert 'FLEET_BLIND' in run(root=str(root), argv=['fleet.cmd', 'blind-seal'], agent='missing')
    malformed = run(root=str(root), argv=['fleet.cmd', 'blind-seal', 'fake-digest'], agent='missing')
    assert 'только на чтение' in malformed
    with store.read() as tx:
        assert not tx.one("SELECT id FROM events WHERE kind='blind_seal'")


def test_chatgpt_extension_report_real_registered_tool_and_global_launch_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    listed = _tool_fn(server, "fleet_list")
    report = _tool_fn(server, "fleet_extension_report")
    assert "можно выдать сейчас: 1" in listed()

    # No network or Windows registry access: the signal comes from the browser.
    monkeypatch.setattr(net_sensor, "windows_route", lambda: net_sensor.Route(False))
    assert "lease updated" in report("0.18.1", 3, 1)
    lease = supervisor._read_json(supervisor._status_path(store))["extension"]
    assert lease["version"] == "0.18.1" and lease["tabs"] == 3

    for n in range(1, 4):
        output = report("0.18.1", 3, 1, "не найдено поле ввода", "Unable to load site")
        assert f"failures: {n}" in output
    with store.read() as tx:
        state, _ = world.last_state(tx, "chatgpt-reachable")
    assert state == world.DOWN
    listing = listed()
    assert "gate: chatgpt-reachable=down; launches=0" in listing
    assert "очередь: выдавать нечего" in listing
    assert "проект: demo — выдавать нечего" in listing

    # Separate telemetry tool owns the lease; fleet_list remains pure read.
    before = supervisor._read_json(supervisor._status_path(store))
    listed("fake", 42, 99)
    assert supervisor._read_json(supervisor._status_path(store)) == before
    sensor_events = net_sensor._alert_path(store).read_text(encoding="utf-8").splitlines()
    assert len(sensor_events) == 1
    assert json.loads(sensor_events[0])["kind"] == "chatgpt_unreachable"


def test_extension_wake_state_is_validated_routed_without_worker_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, store, _root, _ = _make_server(tmp_path)
    report = _tool_fn(server, "fleet_extension_report")
    join = _tool_fn(server, "fleet_join")
    assert "worker_id:" in join("wake-state-synthetic")
    with store.read() as tx:
        before = [(row["id"], row["last_seen"]) for row in
                  tx.query("SELECT id,last_seen FROM workers ORDER BY id")]
    wake_calls: list[tuple[object, str, str]] = []
    sensor_calls: list[tuple[object, str, str]] = []

    def fake_wake(db, *, wake_id: str, state: str):
        wake_calls.append((db, wake_id, state))
        return True

    def fake_sensor(db, detail: str, page_text: str):
        sensor_calls.append((db, detail, page_text))
        return 0

    monkeypatch.setattr(wake, "report_state", fake_wake, raising=False)
    monkeypatch.setattr(net_sensor, "report_extension", fake_sensor)
    encoded = 'wake_state:{"wake":"w-abcdef","state":"generating"}'
    assert "wake state recorded" in report(detail=encoded, page_text="not a sensor error")
    assert len(wake_calls) == 1
    assert str(wake_calls[0][0].path) == str(store.path)
    assert wake_calls[0][1:] == ("w-abcdef", "generating")
    assert sensor_calls == []
    with store.read() as tx:
        after = [(row["id"], row["last_seen"]) for row in
                 tx.query("SELECT id,last_seen FROM workers ORDER BY id")]
    assert after == before, "extension telemetry must not heartbeat a model worker"

    for invalid in (
        'wake_state:{}',
        'wake_state:{"wake":"w-abcdef","state":"unknown"}',
        'wake_state:{"wake":"w-ABCDEF","state":"generating"}',
        'wake_state:{"wake":"w-abcdef","state":"idle","extra":1}',
        'wake_state:{"wake":"w-abcdef","state":null}',
        'wake_state:not-json',
        'wake_state:' + "x" * 260,
    ):
        result = report(detail=invalid, page_text="ignored for wake events")
        assert "wake state recorded" not in result
    assert len(wake_calls) == 1
    assert sensor_calls == []

    assert "lease updated" in report(detail="обычная ошибка страницы",
                                     page_text="Page unavailable")
    assert len(sensor_calls) == 1
    assert str(sensor_calls[0][0].path) == str(store.path)
    assert sensor_calls[0][1:] == ("обычная ошибка страницы", "Page unavailable")


def test_chatgpt_sensor_maintenance_uses_opt_in_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, _, _ = _make_server(tmp_path)
    calls = []

    class StubSensor:
        @staticmethod
        def tick(db):
            calls.append(db)

    ctx = FleetContext(store=store, modules={"net_sensor": StubSensor})
    monkeypatch.setattr(world, "config", lambda: {})
    _tick_chatgpt_sensor(ctx)
    assert not calls
    monkeypatch.setattr(world, "config", lambda: {"chatgpt-reachable": {"sensor": "chatgpt"}})
    _tick_chatgpt_sensor(ctx)
    assert not calls  # temp test registry never makes a network probe
    # Synthetic server registry identity, stubbed sensor: no disk/network I/O.
    fake_store = type("SensorStore", (), {
        "path": str(Path(__file__).resolve().parents[2] / "data" / "fleet" / "fleet.sqlite"),
    })()
    ctx.store = fake_store
    _tick_chatgpt_sensor(ctx)
    assert calls == [fake_store]


def test_chatgpt_owner_alert_is_first_pult_line_and_clears_on_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, _, _ = _make_server(tmp_path)
    module_path = Path(__file__).resolve().parents[2] / "tools" / "fleet.py"
    spec = importlib.util.spec_from_file_location("test_fleet_alert_console", module_path)
    assert spec is not None and spec.loader is not None
    console = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(console)
    monkeypatch.setattr(console, "platform_line", lambda: "platform: fixture")
    alerts = Path(store.path).parent.parent / "owner-alerts.jsonl"
    alerts.write_text(
        json.dumps({"kind": "chatgpt_unreachable", "state": "active",
                    "message": "Включи VPN: порт 10808 не слушает."}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert console.main(["--db", str(store.path)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "! Включи VPN: порт 10808 не слушает."
    with alerts.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "chatgpt_reachable", "state": "resolved"}) + "\n")
    assert console.main(["--db", str(store.path)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "platform: fixture"


def test_agent_parameter_is_additive_on_existing_tools(tmp_path: Path) -> None:
    server, _, _, _ = _make_server(tmp_path)
    for name in (
        "write_file",
        "edit_file",
        "run",
        "run_task",
        "fetch_url",
        "read_file",
        "list_dir",
        "search_files",
        "request_note",
    ):
        tool = asyncio.run(server.get_tool(name))
        assert tool is not None
        props = tool.parameters["properties"]
        assert "agent" in props
        assert props["agent"].get("default") == ""


def test_write_guard_gets_agent_denial_is_audited_and_no_write_occurs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _, root, audit = _make_server(tmp_path)
    seen: dict[str, str | None] = {}

    def deny(_store, worker_id: str | None, abs_path: str) -> Decision:
        seen["worker_id"] = worker_id
        seen["path"] = abs_path
        return Decision(False, "FLEET_REVOKED", "owner stopped this worker", worker_id, False)

    monkeypatch.setattr(guard, "check_write", deny)
    target = root / "blocked.txt"
    result = _tool_fn(server, "write_file")(
        str(target), "must not appear", False, "W7"
    )

    assert seen == {"worker_id": "W7", "path": str(target)}
    assert result.startswith("Отказано:")
    assert "FLEET_REVOKED" in result
    assert not target.exists()

    rows = [
        json.loads(line)
        for line in audit.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    denied = [row for row in rows if row["tool"] == "write_file"][-1]
    assert denied["decision"] == "deny"
    assert denied["agent"] == "W7"
    assert "FLEET_REVOKED" in denied["reason"]


def test_exec_guard_gets_agent_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _, root, audit = _make_server(tmp_path)
    seen: list[tuple[str | None, str]] = []

    def deny(_store, worker_id: str | None, cwd: str) -> Decision:
        seen.append((worker_id, cwd))
        return Decision(False, "FLEET_REVOKED", "revoked before exec", worker_id, False)

    monkeypatch.setattr(guard, "check_exec", deny)

    result = _tool_fn(server, "run")(
        str(root), ["python", "-c", "print('must-not-run')"], "", 30, False, "W8"
    )
    assert seen == [("W8", str(root))]
    assert "FLEET_REVOKED" in result

    task_result = _tool_fn(server, "run_task")("echo", "W8")
    assert seen[-1] == ("W8", str(root))
    assert "FLEET_REVOKED" in task_result

    rows = [
        json.loads(line)
        for line in audit.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        row["tool"] == "run" and row["decision"] == "deny" and row["agent"] == "W8"
        for row in rows
    )
    assert any(
        row["tool"] == "run_task" and row["decision"] == "deny" and row["agent"] == "W8"
        for row in rows
    )


def test_envelope_is_present_on_success_and_error(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    joined = _tool_fn(server, "fleet_join")("envelope worker")
    assert "worker_id: W1" in joined
    message_id = inbox.send(
        store,
        to="W1",
        from_kind="system",
        from_id="",
        kind="note",
        body="important fleet notice",
    )

    file_path = root / "note.txt"
    file_path.write_text("payload-body", encoding="utf-8")

    success = _tool_fn(server, "read_file")(str(file_path), 1, 400, "W1")
    assert message_id in success
    assert "important fleet notice" in success
    assert "--- dev-mcp · ответ инструмента ---" in success
    assert "payload-body" in success

    failure = _tool_fn(server, "read_file")(str(root / "missing.txt"), 1, 400, "W1")
    assert message_id in failure
    assert "important fleet notice" in failure
    assert "--- dev-mcp · ответ инструмента ---" in failure
    assert "Отказано:" in failure


def test_read_file_records_read_event_and_agent_is_optional(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _tool_fn(server, "fleet_join")("reader")
    file_path = root / "read-me.txt"
    file_path.write_text("hello", encoding="utf-8")

    with_agent = _tool_fn(server, "read_file")(str(file_path), 1, 400, "W1")
    assert "hello" in with_agent

    with store.tx() as tx:
        row = tx.one(
            "SELECT kind,worker_id,tool,path_norm FROM events "
            "WHERE kind='read' AND worker_id=? ORDER BY id DESC LIMIT 1",
            ("W1",),
        )
    assert row is not None
    assert row["tool"] == "read_file"
    assert row["path_norm"] == norm_path(str(file_path))

    without_agent = _tool_fn(server, "read_file")(str(file_path))
    assert "hello" in without_agent


def test_write_without_agent_keeps_old_call_shape_working(tmp_path: Path) -> None:
    server, _, root, _ = _make_server(tmp_path)
    target = root / "legacy.txt"
    result = _tool_fn(server, "write_file")(str(target), "legacy")
    assert "создан" in result
    assert target.read_text(encoding="utf-8") == "legacy"


def test_run_events_keep_argv_for_review_evidence(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _tool_fn(server, "fleet_join")("review evidence")

    argv = ["python", "-c", "print('run-ok')"]
    run_result = _tool_fn(server, "run")(str(root), argv, "", 30, False, "W1")
    assert "код возврата: 0" in run_result

    task_result = _tool_fn(server, "run_task")("echo", "W1")
    assert "код возврата: 0" in task_result

    with store.tx() as tx:
        rows = tx.query(
            "SELECT tool,rc,payload FROM events "
            "WHERE worker_id=? AND kind='run' ORDER BY id",
            ("W1",),
        )
    by_tool = {str(row["tool"]): row for row in rows}
    assert by_tool["run"]["rc"] == 0
    assert json.loads(by_tool["run"]["payload"])["argv"] == argv
    assert by_tool["run_task"]["rc"] == 0
    assert json.loads(by_tool["run_task"]["payload"])["argv"] == [
        "python",
        "-c",
        'print("task-ok")',
    ]


def test_request_note_auto_expands_free_same_project_path_and_triages_access_immediately(
    tmp_path: Path,
) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    _tool_fn(server, "fleet_join")("request author")
    _tool_fn(server, "fleet_take")("W1")
    request_note = _tool_fn(server, "request_note")

    requested = root / "requested-followup.txt"
    routed = request_note(
        "other",
        "Need a follow-up task",
        "integration detail",
        str(requested),
        None,
        "queue",
        "W1",
    )
    assert "request-задача не создана" in routed
    assert "owner approve не нужен" in routed
    requested_norm = norm_path(str(requested))
    with store.tx() as tx:
        request_count = tx.scalar(
            "SELECT COUNT(*) FROM tasks WHERE project=? AND kind='request'",
            ("demo",),
        )
        claim = tx.one("SELECT id FROM claims WHERE worker_id='W1' AND state='active'")
        paths = [str(row["path_norm"]) for row in tx.query(
            "SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim["id"],)
        )]
    assert request_count == 0
    assert requested_norm in paths
    assert not (tmp_path / "logs" / "requests.jsonl").exists()

    triaged = request_note(
        "access",
        "Need policy access",
        "",
        str(root),
        ["write_roots"],
        "queue",
        "W1",
    )
    assert "разобрана автоматически: approved" in triaged
    history = [
        json.loads(line)
        for line in (tmp_path / "logs" / "requests.history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(history) == 1
    assert history[0]["kind"] == "access"
    assert history[0]["decision"] == "approved"
    assert not (tmp_path / "logs" / "requests.jsonl").exists()


def test_denied_permission_request_releases_claim_without_owner_wait(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    assert "worker_id: W1" in _tool_fn(server, "fleet_join")("permission-blocked")
    assert "demo/TZ-X" in _tool_fn(server, "fleet_take")("W1")

    result = _tool_fn(server, "request_note")(
        "bin",
        "blocked: нужен docker для обязательной проверки",
        "cannot continue without this executable",
        "docker",
        ["exec_allowed_bin"],
        "owner",
        "W1",
    )
    assert "разобрана автоматически: denied" in result
    assert "claim автоматически освобождён" in result
    assert "не жди владельца" in result
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-X'")["state"] == "frozen"
        assert tx.one("SELECT state FROM claims WHERE worker_id='W1' ORDER BY id DESC LIMIT 1")["state"] == "released"


def test_fleet_join_same_autostart_label_is_idempotent(tmp_path: Path) -> None:
    server, store, _root, _ = _make_server(tmp_path)
    join = _tool_fn(server, "fleet_join")
    label = "f0925-1400abc"

    first = join(label)
    second = join(label)
    assert "worker_id: W1" in first
    assert "worker_id: W1" in second
    with store.tx() as tx:
        assert tx.scalar(
            "SELECT COUNT(*) FROM workers WHERE label=? AND revoked_at IS NULL",
            (label,),
        ) == 1
        assert tx.scalar(
            "SELECT COUNT(*) FROM events WHERE kind='join_reuse' AND worker_id='W1'",
        ) == 1


def test_fleet_list_dedupes_existing_autostart_workers_and_requeues_older_claim(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-X")
    _seed_task(store, root, tz="TZ-Y")
    join = _tool_fn(server, "fleet_join")
    take = _tool_fn(server, "fleet_take")
    label = "f0925-1401abc"

    assert "worker_id: W1" in join(label)
    assert "demo/TZ-X" in take("W1")

    future = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    with store.tx() as tx:
        tx.changes(
            [
                (
                    """INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason)
                       VALUES ('W2',?,'chatgpt',?,?,NULL,'')""",
                    (label, future, future),
                ),
                (
                    "UPDATE tasks SET state='taken',updated_at=? WHERE project='demo' AND tz='TZ-Y'",
                    (future,),
                ),
                (
                    """INSERT INTO claims(project,tz,worker_id,state,started_at)
                       VALUES ('demo','TZ-Y','W2','active',?)""",
                    (future,),
                ),
            ],
            event={"kind": "test_duplicate_worker", "worker_id": "W2"},
        )

    # Maintenance runs independently of list/take as of TZ-03.
    Controller(store).tick()
    listing = _tool_fn(server, "fleet_list")()
    assert "W2 (" + label + ")" in listing
    assert "W1 (" + label + ")" not in listing
    with store.tx() as tx:
        w1 = tx.one("SELECT revoked_at,revoke_reason FROM workers WHERE id='W1'")
        w2 = tx.one("SELECT revoked_at FROM workers WHERE id='W2'")
        assert w1["revoked_at"] is not None
        assert "merged duplicate browser label" in w1["revoke_reason"]
        assert w2["revoked_at"] is None
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-X'")["state"] == "ready"
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-Y'")["state"] == "taken"
        claims = list(tx.query("SELECT worker_id,state,tz FROM claims ORDER BY id"))
        assert {row["worker_id"] for row in claims} == {"W2"}
        assert any(row["tz"] == "TZ-X" and row["state"] == "requeued" for row in claims)
        assert any(row["tz"] == "TZ-Y" and row["state"] == "active" for row in claims)


def test_fleet_list_repairs_false_heartbeat_from_old_dedupe_revision(tmp_path: Path) -> None:
    server, store, _root, _ = _make_server(tmp_path)
    label = "f0923-1402abc"
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.changes(
            [
                (
                    """INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason)
                       VALUES ('W1',?,'chatgpt',?,?,NULL,'')""",
                    (label, old, now),
                ),
                (
                    """INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason)
                       VALUES ('W2',?,'chatgpt',?,?,?,'merged duplicate browser label')""",
                    (label, old, old, now),
                ),
            ],
            event={"kind": "seed_old_dedupe"},
        )
        tx.record_event(
            kind="worker_dedupe",
            worker_id="W1",
            payload={"label": label, "merged": ["W2"]},
        )

    Controller(store).tick()
    _tool_fn(server, "fleet_list")()
    with store.tx() as tx:
        restored = datetime.fromisoformat(str(
            tx.one("SELECT last_seen FROM workers WHERE id='W1'")["last_seen"]
        ))
    assert restored < datetime.now(timezone.utc) - timedelta(days=1)


def test_fleet_resume_returns_active_task_and_durable_evidence(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    assert "worker_id: W1" in _tool_fn(server, "fleet_join")("recoverable")
    assert "demo/TZ-X" in _tool_fn(server, "fleet_take")("W1")

    target = root / "owned.txt"
    assert "создан" in _tool_fn(server, "write_file")(
        str(target), "checkpoint", False, "W1"
    )
    resumed = _tool_fn(server, "fleet_resume")("W1")

    assert "demo/TZ-X" in resumed
    assert "Возобновление: demo/TZ-X, worker W1" in resumed
    assert norm_path(str(target)) in resumed
    assert "Уже написано:" in resumed


def test_public_review_cycle_and_submit_refuses_review_task(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET acceptance='[]' WHERE project=? AND tz=?",
            ("demo", "TZ-X"),
            event={"kind": "seed"},
        )

    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_submit = _tool_fn(server, "fleet_submit")
    fleet_finish = _tool_fn(server, "fleet_finish")
    fleet_review = _tool_fn(server, "fleet_review")

    assert "worker_id: W1" in fleet_join("author")
    assert "worker_id: W2" in fleet_join("reviewer")
    assert "demo/TZ-X" in fleet_take("W1")
    (root / "owned.txt").write_text("done\n", encoding="utf-8")  # работа в зоне задачи
    submitted = fleet_submit("W1", "ready")
    assert "fleet_finish НЕ нужен" in submitted
    assert "review:TZ-X" in submitted
    with store.tx() as tx:
        assert tx.one("SELECT state FROM claims WHERE worker_id='W1' ORDER BY id DESC LIMIT 1")["state"] == "released"

    review_task = fleet_take("W2")
    assert "demo/review:TZ-X" in review_task
    wrong = fleet_submit("W2", "this must not recursively open review")
    assert wrong.startswith("Отказано:")
    assert "fleet_review" in wrong

    accepted = fleet_review("W2", "accept", "checked")
    assert "Вердикт accept" in accepted
    with store.tx() as tx:
        source = tx.one(
            "SELECT state FROM tasks WHERE project=? AND tz=?",
            ("demo", "TZ-X"),
        )
        review = tx.one(
            "SELECT state FROM tasks WHERE project=? AND tz=?",
            ("demo", "review:TZ-X"),
        )
        claim = tx.one(
            "SELECT state FROM claims WHERE worker_id=? ORDER BY id DESC LIMIT 1",
            ("W2",),
        )
    assert source["state"] == "accepted"
    assert review["state"] == "accepted"
    assert claim["state"] == "released"


def test_write_without_agent_is_inferred_and_revoked_inference_is_denied(
    tmp_path: Path,
) -> None:
    server, store, root, audit = _make_server(tmp_path)
    _seed_task(store, root)
    _tool_fn(server, "fleet_join")("worker")
    _tool_fn(server, "fleet_take")("W1")
    target = root / "owned.txt"

    created = _tool_fn(server, "write_file")(str(target), "one")
    assert "создан" in created
    with store.tx() as tx:
        event = tx.one(
            "SELECT worker_id FROM events WHERE kind='write' AND tool='write_file' "
            "ORDER BY id DESC LIMIT 1"
        )
        tx.change(
            "UPDATE workers SET revoked_at=?,revoke_reason=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), "owner stop", "W1"),
            event={"kind": "revoke", "worker_id": "W1"},
        )
    assert event["worker_id"] == "W1"

    denied = _tool_fn(server, "write_file")(str(target), "two", True)
    assert denied.startswith("Отказано:")
    assert "FLEET_REVOKED" in denied
    assert target.read_text(encoding="utf-8") == "one"

    rows = [
        json.loads(line)
        for line in audit.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    allowed = [row for row in rows if row["tool"] == "write_file" and row["decision"] == "allow"][-1]
    blocked = [row for row in rows if row["tool"] == "write_file" and row["decision"] == "deny"][-1]
    assert allowed["agent"] == "W1"
    assert allowed["inferred"] is True
    assert blocked["agent"] == "W1"
    assert blocked["inferred"] is True


def test_search_result_file_becomes_stale_notification_on_next_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hazard, "_is_noisy", lambda _path, _payload, _roots: False)
    from dev_mcp.fleet import tools as fleet_tools
    monkeypatch.setattr(fleet_tools, 'ADVISORY_EVERY_S', 0)
    server, store, root, _ = _make_server(tmp_path)
    fleet_join = _tool_fn(server, "fleet_join")
    assert "worker_id: W1" in fleet_join("reader")
    assert "worker_id: W2" in fleet_join("writer")

    target = root / "shared.txt"
    target.write_text("needle\n", encoding="utf-8")

    found = _tool_fn(server, "search_files")(
        "needle", str(root), "*.txt", False, 10, "W1"
    )
    assert str(target) in found

    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    with store.tx() as tx:
        row = tx.one(
            "SELECT id,path_norm FROM events WHERE kind='read' AND worker_id=? "
            "AND tool='search_files' ORDER BY id DESC LIMIT 1",
            ("W1",),
        )
        assert row is not None
        assert row["path_norm"] == norm_path(str(target))
        tx.change(
            "UPDATE events SET ts=? WHERE id=?",
            (old, row["id"]),
            event={"kind": "test_backdate", "worker_id": "W1"},
        )

    written = _tool_fn(server, "write_file")(
        str(target), "changed\n", True, "W2"
    )
    assert "перезаписан" in written

    next_response = _tool_fn(server, "list_dir")(str(root), "*", False, "W1")
    # Advisory calculation is independent of the response. Its durable
    # notification appears on a following response without blocking file IO.
    import time
    until = time.monotonic() + 2
    while 'устаревшее чтение' not in next_response and time.monotonic() < until:
        time.sleep(.02)
        next_response = _tool_fn(server, "list_dir")(str(root), "*", False, "W1")
    assert "устаревшее чтение" in next_response
    assert "shared.txt" in next_response


def test_fetch_url_checks_fleet_guard_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _, root, _ = _make_server(tmp_path)
    seen: dict[str, str | None] = {}

    def deny(_store, worker_id: str | None, abs_path: str) -> Decision:
        seen["worker_id"] = worker_id
        seen["path"] = abs_path
        return Decision(False, "FLEET_FOREIGN_PATH", "held by neighbour", worker_id, False)

    def must_not_open(*_args, **_kwargs):
        raise AssertionError("network must not start before fleet guard")

    monkeypatch.setattr(guard, "check_write", deny)
    monkeypatch.setattr(server_mod.urllib.request, "urlopen", must_not_open)
    target = root / "download.bin"
    result = _tool_fn(server, "fetch_url")(
        "https://example.com/file.bin", str(target), False, "W9"
    )

    assert result.startswith("Отказано:")
    assert "FLEET_FOREIGN_PATH" in result
    assert seen == {"worker_id": "W9", "path": str(target)}
    assert not target.exists()

def _seed_other_project(store: Store, root: Path) -> None:
    """Второй проект с более высоким приоритетом: без закрепления его задача идёт первой."""
    now = datetime.now(timezone.utc).isoformat()
    other = root / "other"
    other.mkdir()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES (?,?,?,1)",
            ("other", str(other), 0),
            event={"kind": "seed"},
        )
        tx.change(
            """INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,owns,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("other", "TZ-O", "Other task", "ready", 1, "work", "ToDo/TZ-O.md",
             json.dumps([norm_path(str(other / "o.txt"))]), now, now),
            event={"kind": "seed"},
        )


def test_expect_pins_launched_tab_to_its_project(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    _seed_other_project(store, root)
    fleet_expect = _tool_fn(server, "fleet_expect")
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")

    listed = _tool_fn(server, "fleet_list")()
    assert "проект: other — можно выдать 1; следующая TZ-O [work]" in listed
    assert "проект: demo — можно выдать 1; следующая TZ-X [work]" in listed
    assert listed.index("проект: other") < listed.index("проект: demo")
    assert listed.index("проект: other") < listed.index("(активных воркеров нет)")
    assert listed.index("очередь: следующая к выдаче") < listed.index("(активных воркеров нет)")

    assert "Отказано" in fleet_expect("f0921-a", "nope")
    assert "закреплена" in fleet_expect("f0921-a", "demo")
    fleet_join("f0921-a")
    assert "demo/TZ-X" in fleet_take("W1")

    # Pin нужен только для первого take. После закрытия первой задачи тот же чат
    # использует оставшийся budget и переходит в общую очередь вместо нового чата.
    with store.tx() as tx:
        tx.change(
            "UPDATE claims SET state='released',released_at=? WHERE worker_id='W1' AND state='active'",
            (datetime.now(timezone.utc).isoformat(),),
            event={"kind": "seed_release", "worker_id": "W1", "project": "demo", "tz": "TZ-X"},
        )
        tx.change(
            "UPDATE tasks SET state='accepted' WHERE project='demo' AND tz='TZ-X'",
            (),
            event={"kind": "seed_accept", "worker_id": "W1", "project": "demo", "tz": "TZ-X"},
        )
    assert "other/TZ-O" in fleet_take("W1")

    # Без закрепления — обычный порядок очереди (добавим ещё одну other-задачу).
    with store.tx() as tx:
        now = datetime.now(timezone.utc).isoformat()
        tx.change(
            """INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,owns,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("other", "TZ-O2", "Other task 2", "ready", 1, "work", "ToDo/TZ-O2.md",
             json.dumps([norm_path(str(root / "other" / "o2.txt"))]), now, now),
            event={"kind": "seed"},
        )
    fleet_join("manual")
    assert "other/TZ-O2" in fleet_take("W2")

    # Закреплённый проект пуст — чужую задачу не берёт.
    fleet_expect("f0921-b", "demo")
    fleet_join("f0921-b")
    taken = fleet_take("W3")
    assert "В проекте demo доступных готовых задач сейчас нет" in taken
    assert "проект: demo — выдавать нечего" in _tool_fn(server, "fleet_list")()


def test_fleet_list_auto_promotes_legacy_draft_fix_instead_of_waiting_for_owner(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change(
            """INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,created_at,updated_at)
               VALUES ('demo','fix:TZ-X:3','Fix TZ-X','draft',1,'fix','ToDo/TZ-X.md',?,?)""",
            (now, now),
            event={"kind": "seed"},
        )
        tx.record_event(
            kind="escalate",
            project="demo",
            tz="fix:TZ-X:3",
            payload={"note": "порочный круг: 3-й отказ подряд по TZ-X"},
        )
    Controller(store).tick()
    listed = _tool_fn(server, "fleet_list")()
    assert "нужен владелец" not in listed
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='fix:TZ-X:3'")["state"] == "ready"


def test_blocked_finish_freezes_instead_of_requeue_loop(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    join = _tool_fn(server, "fleet_join")
    take = _tool_fn(server, "fleet_take")
    finish = _tool_fn(server, "fleet_finish")
    assert "worker_id: W1" in join("blocked-tab")
    assert "demo/TZ-X" in take("W1")
    result = finish("W1", "blocked: нужен внешний безопасный named task")
    assert "frozen" in result
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-X'")["state"] == "frozen"
        assert tx.one("SELECT state FROM claims WHERE worker_id='W1' ORDER BY id DESC LIMIT 1")["state"] == "released"


def test_acceptance_briefing_points_shell_like_exact_command_to_named_run_task(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET acceptance=? WHERE project='demo' AND tz='TZ-X'",
            (json.dumps(['python -c print("task-ok")']),),
            event={"kind": "seed_acceptance"},
        )
    assert "worker_id: W1" in _tool_fn(server, "fleet_join")("hint-tab")
    briefing = _tool_fn(server, "fleet_take")("W1")
    assert "Приёмка через безопасный named task" in briefing
    assert "run_task(name='echo', agent='W1')" in briefing


def test_request_note_explicit_real_conflict_parks_source_claim_automatically(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-X")
    _seed_task(store, root, tz="TZ-Y")
    foreign = root / "foreign.txt"
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET owns=?,resources='[]' WHERE project='demo' AND tz='TZ-Y'",
            (json.dumps([norm_path(str(foreign))]),),
            event={"kind": "test_zone"},
        )

    join = _tool_fn(server, "fleet_join")
    take = _tool_fn(server, "fleet_take")
    assert "worker_id: W1" in join("blocked-request")
    assert "demo/TZ-X" in take("W1")
    assert "worker_id: W2" in join("foreign-holder")
    assert "demo/TZ-Y" in take("W2")

    result = _tool_fn(server, "request_note")(
        "other",
        "blocked: нужна правка в чужой зоне",
        "cannot finish before this dependency",
        str(foreign),
        None,
        "queue",
        "W1",
    )
    assert "автоматически освобождена" in result
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-X'")["state"] == "frozen"
        assert tx.one("SELECT state FROM claims WHERE worker_id='W1' ORDER BY id DESC LIMIT 1")["state"] == "released"
        assert tx.one("SELECT state FROM claims WHERE worker_id='W2' ORDER BY id DESC LIMIT 1")["state"] == "active"
        req = tx.one("SELECT state,kind FROM tasks WHERE project='demo' AND kind='request' ORDER BY created_at DESC LIMIT 1")
        assert req["state"] == "ready" and req["kind"] == "request"


def test_fleet_list_omits_old_idle_workers_but_keeps_fresh_workers(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    join = _tool_fn(server, "fleet_join")
    fleet_list = _tool_fn(server, "fleet_list")

    assert "worker_id: W1" in join("old-idle")
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    with store.tx() as tx:
        tx.change(
            "UPDATE workers SET last_seen=? WHERE id='W1'",
            (old,),
            event={"kind": "test_old_idle", "worker_id": "W1"},
        )

    assert "worker_id: W2" in join("fresh-idle")
    listing = fleet_list()
    assert "W1 (old-idle)" not in listing
    assert "W2 (fresh-idle)" in listing


def test_fleet_list_maintenance_requeues_abandoned_claim_without_waiting_for_take(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    assert "worker_id: W1" in _tool_fn(server, "fleet_join")("stale-tab")
    assert "demo/TZ-X" in _tool_fn(server, "fleet_take")("W1")
    old = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    with store.tx() as tx:
        tx.change(
            "UPDATE workers SET last_seen=? WHERE id='W1'",
            (old,),
            event={"kind": "test_stale", "worker_id": "W1"},
        )
    Controller(store).tick()
    _tool_fn(server, "fleet_list")()
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-X'")["state"] == "ready"
        assert tx.one("SELECT state FROM claims WHERE worker_id='W1' ORDER BY id DESC LIMIT 1")["state"] == "requeued"


def test_chat_is_told_to_take_the_next_task_until_its_budget_runs_out(tmp_path: Path) -> None:
    from dev_mcp.fleet import queue

    server, store, root, _ = _make_server(tmp_path)
    for i in range(queue.CHAT_TASK_BUDGET + 1):
        _seed_task(store, root, tz=f"TZ-{i}")
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_finish = _tool_fn(server, "fleet_finish")
    fleet_list = _tool_fn(server, "fleet_list")

    assert "worker_id: W1" in fleet_join("tab one")
    first = fleet_take("W1")
    assert "задача №" not in first
    finished = fleet_finish("W1", "back to queue")
    assert "fleet_take(agent='W1')" in finished
    assert f"осталось задач: {queue.CHAT_TASK_BUDGET - 1}" in finished
    assert f"чат W1: взято задач 1 из {queue.CHAT_TASK_BUDGET}" in fleet_list()

    second = fleet_take("W1")
    assert "Это задача №2 в этом чате" in second
    for _ in range(queue.CHAT_TASK_BUDGET - 2):
        fleet_finish("W1", "x")
        fleet_take("W1")
    last = fleet_finish("W1", "x")
    assert "лимит задач" in last and "остановись" in last
    refused = fleet_take("W1")
    assert "лимит задач" in refused


def test_owner_console_needs_password_and_stays_inside_its_command_list(tmp_path: Path) -> None:
    from dev_mcp.fleet.owner import secret_path, set_password

    server, store, root, audit = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    _seed_task(store, root, tz="TZ-2")
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET state='submitted' WHERE project='demo' AND tz='TZ-2'",
            event={"kind": "seed_submitted"},
        )
    fleet_join = _tool_fn(server, "fleet_join")
    unlock = _tool_fn(server, "fleet_owner_unlock")
    console = _tool_fn(server, "fleet_owner")

    assert "worker_id: W1" in fleet_join("owner-tab")
    refused = console("W1", "active")
    assert "owner-grant W1" in refused and "demo/TZ-1" not in refused

    set_password(secret_path(tmp_path), "correct-horse-battery")
    assert "неверный пароль" in unlock("W1", "wrong-password-here")
    opened = unlock("W1", "correct-horse-battery")
    assert "сессия владельца открыта" in opened

    assert "неизвестная команда" in console("W1", "revoke", ["W2", "x"])
    legacy_approve = console("W1", "approve", ["anything"])
    assert "неизвестная команда" in legacy_approve
    assert "bootstrap: ПРОЕКТ TZ-NN" in console("W1", "bootstrap", ["demo"])
    assert "во флоте нет" in console("W1", "bootstrap", ["nope", "TZ-1"])
    # Permanent freeze is a local emergency control only. A web owner-agent must
    # never be able to create a task that cannot thaw automatically.
    frozen = console("W1", "freeze", ["demo", "TZ-1"])
    assert "неизвестная команда" in frozen
    submitted_frozen = console("W1", "freeze", ["demo", "TZ-2"])
    assert "неизвестная команда" in submitted_frozen
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-1'")["state"] == "ready"
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-2'")["state"] == "submitted"
    assert "Сессия владельца закрыта" in console("W1", "lock")
    assert "owner-grant W1" in console("W1", "active")

    # пароль не попадает в аудит
    assert "correct-horse-battery" not in audit.path.read_text(encoding="utf-8")


def test_chat_cannot_submit_a_task_whose_zone_it_never_touched(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_submit = _tool_fn(server, "fleet_submit")

    assert "worker_id: W1" in fleet_join("tab")
    assert "demo/TZ-X" in fleet_take("W1")
    (root / "elsewhere.txt").write_text("работа прошлой задачи\n", encoding="utf-8")
    refused = fleet_submit("W1", "ready")
    assert "FLEET_EMPTY_SUBMIT" in refused and "TZ-X" in refused
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-X'")["state"] == "taken"

    (root / "owned.txt").write_text("теперь по делу\n", encoding="utf-8")
    assert "сдана на приёмку" in fleet_submit("W1", "ready")


@pytest.mark.parametrize("scenario", [
    "vanished", "missing_event", "wrong_fingerprint", "wrong_owns",
    "still_exists", "missing_directory", "not_a_fix",
])
def test_vanished_planner_draft_fix_requires_trusted_proof(
    tmp_path: Path, scenario: str,
) -> None:
    """A stale fix:draft is reviewable, not a blanket empty-submit exemption."""
    from dev_mcp.fleet.drafts import _record_invalid

    server, store, root, _ = _make_server(tmp_path)
    draft = root / "ToDo" / "tz-drafts" / "TZ-87-claim-trees-transition.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("invalid duplicate draft\n", encoding="utf-8")
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES (?,?,1,1)",
            ("demo", str(root)), event={"kind": "seed"},
        )
    assert _record_invalid(store, "demo", root, draft, "duplicate TZ-87")
    rel = draft.relative_to(root).as_posix()
    fix_tz = "fix:draft:" + hashlib.sha256(rel.casefold().encode()).hexdigest()[:16]

    if scenario != "still_exists":
        draft.unlink()
    if scenario == "missing_directory":
        draft.parent.rmdir()
    with store.tx() as tx:
        if scenario == "missing_event":
            tx.change(
                "UPDATE events SET kind='test_untrusted' "
                "WHERE project='demo' AND tz=? AND kind='draft_fix_open'",
                (fix_tz,), event={"kind": "test_tamper"},
            )
        elif scenario == "wrong_fingerprint":
            tx.change(
                "UPDATE tasks SET spec_sha=? WHERE project='demo' AND tz=?",
                ("0" * 64, fix_tz), event={"kind": "test_tamper"},
            )
        elif scenario == "wrong_owns":
            tx.change(
                "UPDATE tasks SET owns=? WHERE project='demo' AND tz=?",
                (json.dumps([norm_path(str(root / "different.md"))]), fix_tz),
                event={"kind": "test_tamper"},
            )
        elif scenario == "not_a_fix":
            tx.change(
                "UPDATE tasks SET kind='work' WHERE project='demo' AND tz=?",
                (fix_tz,), event={"kind": "test_tamper"},
            )

    assert "worker_id: W1" in _tool_fn(server, "fleet_join")("stale-fix")
    assert f"demo/{fix_tz}" in _tool_fn(server, "fleet_take")("W1")
    outcome = _tool_fn(server, "fleet_submit")(
        "W1", "bite: n/a — draft was removed before claim; no source to edit."
    )
    allowed = scenario == "vanished"
    assert ("сдана на приёмку" if allowed else "FLEET_EMPTY_SUBMIT") in outcome
    with store.read() as tx:
        state = tx.one("SELECT state FROM tasks WHERE project='demo' AND tz=?", (fix_tz,))
        review = tx.one("SELECT state FROM tasks WHERE project='demo' AND tz=?",
                        (f"review:{fix_tz}",))
    assert state["state"] == ("submitted" if allowed else "taken")
    assert (review is not None) == allowed


def test_noop_task_may_submit_after_all_acceptance_runs_are_fresh_green(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_submit = _tool_fn(server, "fleet_submit")

    assert "worker_id: W1" in fleet_join("tab-noop")
    assert "demo/TZ-X" in fleet_take("W1")
    with store.tx() as tx:
        tx.record_event(
            kind="run",
            worker_id="W1",
            project="demo",
            tz="TZ-X",
            tool="run",
            rc=0,
            payload={"argv": ["pytest", "-q"]},
        )

    accepted = fleet_submit("W1", "Fresh acceptance is green; implementation already satisfied the task.")
    assert "сдана на приёмку" in accepted
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-X'")["state"] == "submitted"


@pytest.mark.parametrize(
    ("scenario", "allowed"),
    [
        ("accepted_independent_release", True),
        ("missing_reject", False),
        ("no_review_reference", False),
        ("ambiguous_review_reference", False),
        ("wrong_fix_task", False),
        ("missing_external_task", False),
        ("unaccepted_external_task", False),
        ("missing_external_accept_event", False),
        ("external_accept_before_reject", False),
        ("self_accepted_external_request", False),
        ("missing_fresh_run", False),
        ("failed_fresh_run", False),
        ("stale_run_only", False),
        ("ordinary_fix", False),
        ("ordinary_work", False),
    ],
)
def test_empty_submit_external_release_requires_independent_proof(
    tmp_path: Path, scenario: str, allowed: bool,
) -> None:
    """A no-diff request fix needs a reviewer-bound external accept and a fresh run."""
    server, store, root, _ = _make_server(tmp_path)
    source = "request:1234abcd"
    fix = "fix:request:1234abcd"
    tz = ("TZ-ordinary" if scenario == "ordinary_work" else
          "fix:TZ-ordinary" if scenario == "ordinary_fix" else fix)
    _seed_task(store, root, tz=tz)
    external = "request:deadbeef"
    now = datetime.now(timezone.utc)
    reject_ts = (now - timedelta(minutes=3)).isoformat()
    accept_ts = (now - timedelta(minutes=2)).isoformat()
    if scenario == "external_accept_before_reject":
        accept_ts = (now - timedelta(minutes=4)).isoformat()
    reference = f"Fleet Platform/{external}"
    if scenario == "no_review_reference":
        reference = "unrelated bug"
    elif scenario == "ambiguous_review_reference":
        reference += " and Fleet Platform/request:cafefeed"

    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET kind=?, acceptance='[]', spec_path='ToDo/external-bridge.md' "
            "WHERE project='demo' AND tz=?",
            ("work" if scenario == "ordinary_work" else "fix", tz),
            event={"kind": "test_setup"},
        )
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES (?,?,?,1)",
            ("Fleet Platform", str(tmp_path / "external"), 2),
            event={"kind": "test_setup"},
        )
        tx.change(
            """INSERT INTO tasks(
                 project,tz,title,state,priority,kind,spec_path,spec_sha,
                 needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("demo", source, "Source request", "rejected", 1, "request",
             "", "", "[]", "[]", "[]", "[]", None, "", reject_ts, reject_ts),
            event={"kind": "test_setup"},
        )
        if scenario != "missing_external_task":
            tx.change(
                """INSERT INTO tasks(
                     project,tz,title,state,priority,kind,spec_path,spec_sha,
                     needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("Fleet Platform", external, "External release",
                 "submitted" if scenario == "unaccepted_external_task" else "accepted",
                 1, "request", "", "", "[]", "[]", "[]", "[]", None, "",
                 accept_ts, accept_ts),
                event={"kind": "test_setup"},
            )
        if scenario != "missing_reject":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
                "VALUES (?,?,?,?,?,?)",
                (reject_ts, "reject", "W2", "demo", source,
                 json.dumps({"fix_task": "fix:request:ffffeeee"
                             if scenario == "wrong_fix_task" else fix,
                             "note": reference})),
                event={"kind": "test_setup"},
            )
        if scenario != "missing_external_accept_event":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
                "VALUES (?,?,?,?,?,?)",
                (accept_ts, "accept",
                 "W1" if scenario == "self_accepted_external_request" else "W2",
                 "Fleet Platform", external, "{}"),
                event={"kind": "test_setup"},
            )
        if scenario == "stale_run_only":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,rc,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                ((now - timedelta(minutes=5)).isoformat(), "run", "W1",
                 "demo", tz, 0, "{}"),
                event={"kind": "test_setup"},
            )

    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_submit = _tool_fn(server, "fleet_submit")
    assert "worker_id: W1" in fleet_join("submitter")
    assert "worker_id: W2" in fleet_join("reviewer")
    assert f"demo/{tz}" in fleet_take("W1")
    if scenario not in {"missing_fresh_run", "stale_run_only"}:
        with store.tx() as tx:
            tx.record_event(
                kind="run", worker_id="W1", project="demo", tz=tz,
                tool="run", rc=1 if scenario == "failed_fresh_run" else 0,
                payload={"argv": ["python", "-m", "pytest", "-q"]},
            )
    outcome = fleet_submit("W1", f"bite: n/a — independently accepted external release; {reference}.")
    if allowed:
        assert "сдана на приёмку" in outcome
    else:
        assert "FLEET_EMPTY_SUBMIT" in outcome
    with store.tx() as tx:
        task = tx.one("SELECT state FROM tasks WHERE project='demo' AND tz=?", (tz,))
        assert task is not None
        assert task["state"] == ("submitted" if allowed else "taken")


@pytest.mark.parametrize(
    ("scenario", "allowed"),
    [
        ("review_failed_then_other_worker_fixed", True),
        ("missing_reject", False),
        ("review_by_same_worker", False),
        ("wrong_fix_task", False),
        ("no_outside_owns_diagnosis", False),
        ("no_external_source_reference", False),
        ("external_path_inside_owns", False),
        ("missing_original_write", False),
        ("original_write_elsewhere", False),
        ("missing_failed_review_run", False),
        ("failed_run_after_reject", False),
        ("failed_run_by_same_worker", False),
        ("failed_run_by_unrelated_worker", False),
        ("failed_run_different_tests", False),
        ("missing_external_write", False),
        ("external_write_before_reject", False),
        ("external_write_by_submitter", False),
        ("no_fresh_test_run", False),
        ("fresh_run_failed", False),
        ("only_one_test_rerun", False),
        ("fresh_run_not_pytest", False),
        ("ordinary_fix", False),
        ("ordinary_work", False),
    ],
)
def test_empty_submit_retested_outside_owns_requires_complete_independent_proof(
    tmp_path: Path, scenario: str, allowed: bool,
) -> None:
    """No-diff fix waiver requires independent failing review and identical green retest."""
    server, store, root, _ = _make_server(tmp_path)
    source = "request:feed1234"
    fix = "fix:request:feed1234"
    tz = ("TZ-ordinary" if scenario == "ordinary_work" else
          "fix:TZ-ordinary" if scenario == "ordinary_fix" else fix)
    _seed_task(store, root, tz=tz)

    now = datetime.now(timezone.utc)
    original_ts = (now - timedelta(minutes=6)).isoformat()
    failure_ts = (now - timedelta(minutes=5)).isoformat()
    rejection_ts = (now - timedelta(minutes=4)).isoformat()
    external_ts = (now - timedelta(minutes=3)).isoformat()
    tests = ("tests/fleet/test_a.py", "tests/fleet/test_b.py")
    external_path = "src/dev_mcp/independent_repair.py"
    note = f"outside owns: {external_path}; failed {tests[0]} {tests[1]}"
    if scenario == "no_outside_owns_diagnosis":
        note = note.replace("outside owns", "elsewhere")
    elif scenario == "no_external_source_reference":
        note = note.replace(external_path, "unspecified file")
    elif scenario == "external_path_inside_owns":
        note = note.replace(external_path, "src/dev_mcp/owned.py")
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET kind=?, acceptance='[]', spec_path='ToDo/outside-bridge.md', "
            "owns=? WHERE project='demo' AND tz=?",
            ("work" if scenario == "ordinary_work" else "fix",
             json.dumps([norm_path(str(root / "src" / "dev_mcp" / "owned.py"))])
             if scenario == "external_path_inside_owns" else
             json.dumps([norm_path(str(root / "owned.txt"))]), tz),
            event={"kind": "test_setup"},
        )
        tx.change(
            """INSERT INTO tasks(
                 project,tz,title,state,priority,kind,spec_path,spec_sha,
                 needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("demo", source, "Rejected source request", "rejected", 1,
             "request", "", "", "[]", "[]", "[]", "[]", None,
             "", original_ts, original_ts),
            event={"kind": "test_setup"},
        )
        if scenario != "missing_original_write":
            source_path = (
                root / "unrelated.txt" if scenario == "original_write_elsewhere"
                else root / "src" / "dev_mcp" / "owned.py"
                if scenario == "external_path_inside_owns"
                else root / "owned.txt"
            )
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,path_norm,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (original_ts, "write", "W3", "demo", source,
                 norm_path(str(source_path)), "{}"),
                event={"kind": "test_setup"},
            )
        if scenario != "missing_failed_review_run":
            failing_tests = [tests[0]] if scenario == "failed_run_different_tests" else list(tests)
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,rc,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (external_ts if scenario == "failed_run_after_reject" else failure_ts,
                 "run", "W1" if scenario == "failed_run_by_same_worker" else
                 "W3" if scenario == "failed_run_by_unrelated_worker" else "W2",
                 "demo", "review:" + source, 1,
                 json.dumps({"argv": ["pytest", "-q", *failing_tests]})),
                event={"kind": "test_setup"},
            )
        if scenario != "missing_reject":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
                "VALUES (?,?,?,?,?,?)",
                (rejection_ts, "reject",
                 "W1" if scenario == "review_by_same_worker" else "W2",
                 "demo", source, json.dumps({
                     "fix_task": "fix:request:11111111"
                     if scenario == "wrong_fix_task" else fix,
                     "note": note,
                 })),
                event={"kind": "test_setup"},
            )
        if scenario != "missing_external_write":
            path = root / "src" / "dev_mcp" / (
                "owned.py" if scenario == "external_path_inside_owns"
                else "independent_repair.py"
            )
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,path_norm,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (failure_ts if scenario == "external_write_before_reject" else external_ts,
                 "write", "W1" if scenario == "external_write_by_submitter" else "W3",
                 "demo", "request:outside777", norm_path(str(path)), "{}"),
                event={"kind": "test_setup"},
            )

    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_submit = _tool_fn(server, "fleet_submit")
    assert "worker_id: W1" in fleet_join("fix-submitter")
    assert "worker_id: W2" in fleet_join("independent-reviewer")
    assert "worker_id: W3" in fleet_join("external-repairer")
    assert f"demo/{tz}" in fleet_take("W1")
    if scenario != "no_fresh_test_run":
        fresh_tests = [tests[0]] if scenario == "only_one_test_rerun" else list(tests)
        argv = ["python", "check.py", *fresh_tests] if scenario == "fresh_run_not_pytest" else [
            "pytest", "-q", *fresh_tests,
        ]
        with store.tx() as tx:
            tx.record_event(
                kind="run", worker_id="W1", project="demo", tz=tz, tool="run",
                rc=1 if scenario == "fresh_run_failed" else 0, payload={"argv": argv},
            )
    output = fleet_submit("W1", "bite: n/a — re-test after independent outside-owns repair.")
    if allowed:
        assert "сдана на приёмку" in output
    else:
        assert "FLEET_EMPTY_SUBMIT" in output
    with store.tx() as tx:
        task = tx.one("SELECT state FROM tasks WHERE project='demo' AND tz=?", (tz,))
        assert task is not None
        assert task["state"] == ("submitted" if allowed else "taken")


@pytest.mark.parametrize(
    "scenario",
    [
        "valid_accepted_external_regression",
        "missing_reject",
        "wrong_fix_task",
        "reject_by_submitter",
        "reject_does_not_name_test",
        "missing_prior_own_write",
        "prior_write_outside_owns",
        "missing_block",
        "block_before_reject",
        "block_does_not_name_test",
        "block_does_not_name_request",
        "block_names_two_requests",
        "external_task_missing",
        "external_task_not_accepted",
        "external_task_not_request",
        "test_outside_external_owns",
        "external_accept_missing",
        "external_accept_before_block",
        "accept_by_submitter",
        "accept_by_rejecting_reviewer",
        "external_test_write_missing",
        "external_test_write_elsewhere",
        "external_test_write_before_block",
        "external_test_write_by_submitter",
        "external_test_write_by_accepting_reviewer",
        "fresh_exact_pytest_missing",
        "fresh_exact_pytest_failed",
        "fresh_run_not_pytest",
        "fresh_run_wrong_test",
        "fresh_run_collect_only",
        "fresh_run_help",
        "fresh_run_filtered_out",
        "fresh_run_deselect",
        "fresh_run_python_script_bait",
        "fresh_run_python_misplaced_module",
        "stale_pytest_run_only",
    ],
)
def test_empty_submit_blocked_accepted_regression_bridge_is_fail_closed(
    tmp_path: Path, scenario: str,
) -> None:
    """A numbered fix retry with no diff needs the full independent test receipt chain.

    Previous fix write -> independent reject -> named block -> externally
    authored and reviewed test -> fresh exact green pytest -> real fleet_submit.
    Every omitted or substituted receipt must leave the retry taken.
    """
    server, store, root, _ = _make_server(tmp_path)
    previous = "fix:request:1234abcd"
    retry = previous + ":2"
    external_tz = "request:deadbeef"
    named_test = "tests/guard/test_guard_commit_review.py"
    own_file = norm_path(str(root / "owned.txt"))
    test_file = norm_path(str(root / named_test))
    _seed_task(store, root, tz=retry)

    now = datetime.now(timezone.utc)
    original_at = (now - timedelta(minutes=10)).isoformat()
    rejected_at = (now - timedelta(minutes=9)).isoformat()
    blocked_at = (now - timedelta(minutes=8)).isoformat()
    wrote_test_at = (now - timedelta(minutes=7)).isoformat()
    accepted_at = (now - timedelta(minutes=6)).isoformat()
    rejection_note = (
        "Review rejected missing independent regression in "
        + named_test if scenario != "reject_does_not_name_test"
        else "Review rejected missing independent regression"
    )
    block_reason = f"blocked: waiting on {named_test} in {external_tz}"
    if scenario == "block_does_not_name_test":
        block_reason = f"blocked: waiting on regression in {external_tz}"
    elif scenario == "block_does_not_name_request":
        block_reason = f"blocked: waiting on {named_test}"
    elif scenario == "block_names_two_requests":
        block_reason += " and request:cafefeed"

    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET kind='fix', acceptance='[]' "
            "WHERE project='demo' AND tz=?",
            (retry,),
            event={"kind": "test_setup"},
        )
        tx.change(
            """INSERT INTO tasks(
                 project,tz,title,state,priority,kind,spec_path,spec_sha,
                 needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("demo", previous, "Earlier rejected fix", "rejected", 1, "fix",
             "", "", "[]", json.dumps([own_file]), "[]", "[]", None,
             "", original_at, rejected_at),
            event={"kind": "test_setup"},
        )
        if scenario != "external_task_missing":
            external_owns = (
                norm_path(str(root / "tests" / "guard" / "unrelated_test.py"))
                if scenario == "test_outside_external_owns" else test_file
            )
            tx.change(
                """INSERT INTO tasks(
                     project,tz,title,state,priority,kind,spec_path,spec_sha,
                     needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("demo", external_tz, "Independent regression", "submitted"
                 if scenario == "external_task_not_accepted" else "accepted", 1,
                 "work" if scenario == "external_task_not_request" else "request",
                 "", "", "[]", json.dumps([external_owns]), "[]", "[]",
                 None, "", blocked_at, accepted_at),
                event={"kind": "test_setup"},
            )
        if scenario != "missing_prior_own_write":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,path_norm,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (original_at, "write", "W3", "demo", previous,
                 norm_path(str(root / "elsewhere.txt"))
                 if scenario == "prior_write_outside_owns" else own_file, "{}"),
                event={"kind": "test_setup"},
            )
        if scenario != "missing_reject":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
                "VALUES (?,?,?,?,?,?)",
                (rejected_at, "reject",
                 "W1" if scenario == "reject_by_submitter" else "W2",
                 "demo", previous, json.dumps({
                     "fix_task": "fix:request:ffffeeee:2"
                     if scenario == "wrong_fix_task" else retry,
                     "note": rejection_note,
                 })),
                event={"kind": "test_setup"},
            )
        if scenario != "missing_block":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
                "VALUES (?,?,?,?,?,?)",
                (original_at if scenario == "block_before_reject" else blocked_at,
                 "block", "W1", "demo", retry,
                 json.dumps({"reason": block_reason})),
                event={"kind": "test_setup"},
            )
        if scenario != "external_test_write_missing":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,path_norm,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (original_at if scenario == "external_test_write_before_block"
                 else wrote_test_at,
                 "write", "W1" if scenario == "external_test_write_by_submitter"
                 else "W4" if scenario == "external_test_write_by_accepting_reviewer"
                 else "W3", "demo", external_tz,
                 norm_path(str(root / "tests" / "guard" / "other_test.py"))
                 if scenario == "external_test_write_elsewhere" else test_file,
                 "{}"),
                event={"kind": "test_setup"},
            )
        if scenario != "external_accept_missing":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
                "VALUES (?,?,?,?,?,?)",
                (original_at if scenario == "external_accept_before_block"
                 else accepted_at, "accept",
                 "W1" if scenario == "accept_by_submitter"
                 else "W2" if scenario == "accept_by_rejecting_reviewer"
                 else "W4", "demo", external_tz, "{}"),
                event={"kind": "test_setup"},
            )
        if scenario == "stale_pytest_run_only":
            tx.change(
                "INSERT INTO events(ts,kind,worker_id,project,tz,rc,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (accepted_at, "run", "W1", "demo", retry, 0,
                 json.dumps({"argv": ["py", "-3", "-m", "pytest", named_test]})),
                event={"kind": "test_setup"},
            )

    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_submit = _tool_fn(server, "fleet_submit")
    assert "worker_id: W1" in fleet_join("blocked-fix-submitter")
    assert f"demo/{retry}" in fleet_take("W1")

    if scenario not in {"fresh_exact_pytest_missing", "stale_pytest_run_only"}:
        argv = ["py", "-3", "-m", "pytest", named_test]
        if scenario == "fresh_run_not_pytest":
            argv = ["python", "check.py", named_test]
        elif scenario == "fresh_run_wrong_test":
            argv = ["py", "-3", "-m", "pytest", "tests/guard/other_test.py"]
        elif scenario == "fresh_run_collect_only":
            argv = ["python", "-m", "pytest", "--collect-only", named_test]
        elif scenario == "fresh_run_help":
            argv = ["pytest", "--help", named_test]
        elif scenario == "fresh_run_filtered_out":
            argv = ["pytest", named_test, "-k", "no_matching_tests"]
        elif scenario == "fresh_run_deselect":
            argv = ["pytest", named_test, f"--deselect={named_test}::test_example"]
        elif scenario == "fresh_run_python_script_bait":
            argv = ["python", "-c", "print('no tests')", "-m", "pytest", named_test]
        elif scenario == "fresh_run_python_misplaced_module":
            argv = ["python", "-W", "ignore", "-m", "pytest", named_test]
        with store.tx() as tx:
            tx.record_event(
                kind="run", worker_id="W1", project="demo", tz=retry,
                tool="run", rc=1 if scenario == "fresh_exact_pytest_failed" else 0,
                payload={"argv": argv},
            )

    submitted = fleet_submit("W1", "bite: n/a — independent regression already accepted.")
    allowed = scenario == "valid_accepted_external_regression"
    assert ("сдана на приёмку" if allowed else "FLEET_EMPTY_SUBMIT") in submitted
    with store.tx() as tx:
        task = tx.one(
            "SELECT state FROM tasks WHERE project='demo' AND tz=?", (retry,),
        )
        assert task is not None
        assert task["state"] == ("submitted" if allowed else "taken")


def test_recovery_chat_may_submit_preserved_work_after_fresh_green_run(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_finish = _tool_fn(server, "fleet_finish")
    fleet_submit = _tool_fn(server, "fleet_submit")
    write_file = _tool_fn(server, "write_file")

    assert "worker_id: W1" in fleet_join("tab-one")
    assert "demo/TZ-X" in fleet_take("W1")
    assert "создан" in write_file(str(root / "owned.txt"), "implemented\n", False, "W1")
    assert "Claim и ресурсы освобождены" in fleet_finish("W1", "handoff after implementation")

    assert "worker_id: W2" in fleet_join("tab-two")
    assert "demo/TZ-X" in fleet_take("W2")
    with store.tx() as tx:
        tx.record_event(
            kind="run",
            worker_id="W2",
            project="demo",
            tz="TZ-X",
            tool="run",
            rc=0,
            payload={"argv": ["python", "-m", "pytest", "-q"]},
        )

    accepted = fleet_submit("W2", "fresh verification of preserved implementation")
    assert "сдана на приёмку" in accepted
    with store.tx() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='demo' AND tz='TZ-X'")["state"] == "submitted"


def test_cli_worker_may_submit_without_zone_changes(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_submit = _tool_fn(server, "fleet_submit")

    assert "worker_id: W1" in fleet_join("cc-demo", "claude-code")
    assert "demo/TZ-X" in fleet_take("W1")
    assert "сдана на приёмку" in fleet_submit("W1", "ready")  # его записи идут мимо стражи


def test_chat_active_task_auto_expands_free_path_inside_same_project(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    write_file = _tool_fn(server, "write_file")

    assert "worker_id: W1" in fleet_join("tab")
    assert "demo/TZ-X" in fleet_take("W1")
    extra = root / "previous_task.py"
    assert "создан" in write_file(str(extra), "part of the same large task", False, "W1")
    assert extra.read_text(encoding="utf-8") == "part of the same large task"
    assert "создан" in write_file(str(root / "owned.txt"), "in zone", True, "W1")

    extra_norm = norm_path(str(extra))
    with store.tx() as tx:
        claim = tx.one("SELECT id FROM claims WHERE worker_id='W1' AND state='active'")
        paths = [str(row["path_norm"]) for row in tx.query(
            "SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim["id"],)
        )]
        task = tx.one("SELECT owns FROM tasks WHERE project='demo' AND tz='TZ-X'")
        events = tx.query(
            "SELECT kind,payload FROM events WHERE project='demo' AND tz='TZ-X' ORDER BY id"
        )
    assert extra_norm in paths
    assert extra_norm in json.loads(str(task["owns"]))
    assert any(str(row["kind"]) == "claim_expand" for row in events)


def test_cli_worker_and_chat_between_tasks_keep_the_old_warning_only(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    write_file = _tool_fn(server, "write_file")

    assert "worker_id: W1" in fleet_join("cc-demo", "claude-code")
    assert "demo/TZ-X" in fleet_take("W1")
    assert "создан" in write_file(str(root / "notes.md"), "cli may", False, "W1")

    assert "worker_id: W2" in fleet_join("tab")  # у вкладки нет активной задачи
    assert "создан" in write_file(str(root / "free.md"), "no claim", False, "W2")


def _reject_cycle(tmp_path: Path, monkeypatch, p: float | None) -> tuple[str, str]:
    import dev_mcp.fleet.tools as fleet_tools

    monkeypatch.setattr(fleet_tools, "owner_gate_probability", lambda note: p)
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET acceptance='[]' WHERE project='demo' AND tz='TZ-X'", (), event={"kind": "seed"})
    fleet_join, fleet_take = _tool_fn(server, "fleet_join"), _tool_fn(server, "fleet_take")
    fleet_join("author")
    fleet_join("reviewer")
    fleet_take("W1")
    (root / "owned.txt").write_text("done\n", encoding="utf-8")
    _tool_fn(server, "fleet_submit")("W1", "ready")
    fleet_take("W2")
    out = _tool_fn(server, "fleet_review")("W2", "reject", "нужен визуальный вердикт владельца на реальном телефоне")
    with store.tx() as tx:
        fix = tx.one("SELECT state FROM tasks WHERE project='demo' AND tz LIKE 'fix:%TZ-X%'")
    return out, str(fix["state"])


def test_owner_gated_reject_freezes_fix_instead_of_new_round(tmp_path: Path, monkeypatch) -> None:
    out, state = _reject_cycle(tmp_path, monkeypatch, 0.97)
    assert state == "frozen"
    assert "заморожена" in out and "очередь его решений" in out


def test_ordinary_or_unknown_reject_keeps_fix_ready(tmp_path: Path, monkeypatch) -> None:
    for p in (0.3, None):
        sub = tmp_path / str(p)
        sub.mkdir()
        out, state = _reject_cycle(sub, monkeypatch, p)
        # не заморожена: fix в раздаче (после вердикта ревьюер может сразу получить его сам)
        assert state in ("ready", "taken") and "заморожена" not in out


def test_submit_requires_green_acceptance_on_current_files(tmp_path: Path, monkeypatch) -> None:
    """Руль §4: без прогона приёмки — отказ с командой; правка после прогона — отказ; выход — в note."""
    from dev_mcp.fleet import evidence, queue

    monkeypatch.setattr(queue, "REQUIRE_GREEN_SUBMIT", True)
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    fleet_join = _tool_fn(server, "fleet_join")
    fleet_take = _tool_fn(server, "fleet_take")
    fleet_submit = _tool_fn(server, "fleet_submit")
    write_file = _tool_fn(server, "write_file")

    assert "worker_id: W1" in fleet_join("tab-green")
    assert "demo/TZ-X" in fleet_take("W1")
    assert "создан" in write_file(str(root / "owned.txt"), "v1\n", False, "W1")
    refused = fleet_submit("W1", "готово")
    assert "без зелёной приёмки" in refused and "`pytest -q`" in refused

    def green_run() -> None:
        with store.tx() as tx:
            claim = tx.one("SELECT id FROM claims WHERE worker_id='W1' AND state='active'")
            paths = [r["path_norm"] for r in tx.query("SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim["id"],))]
            tx.record_event(kind="run", worker_id="W1", project="demo", tz="TZ-X", tool="run", rc=0,
                            payload={"argv": ["pytest", "-q"], "tree": evidence.tree_hash(paths)})

    green_run()
    write_file(str(root / "owned.txt"), "v2\n", True, "W1")  # правка после зелёного прогона
    stale = fleet_submit("W1", "готово")
    assert "менялись после последнего зелёного прогона" in stale

    green_run()
    assert "сдана на приёмку" in fleet_submit("W1", "готово, приёмка зелёная")


def test_submit_waiver_when_acceptance_cannot_run(tmp_path: Path, monkeypatch) -> None:
    from dev_mcp.fleet import queue

    monkeypatch.setattr(queue, "REQUIRE_GREEN_SUBMIT", True)
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    assert "worker_id: W1" in _tool_fn(server, "fleet_join")("tab-waive")
    assert "demo/TZ-X" in _tool_fn(server, "fleet_take")("W1")
    assert "создан" in _tool_fn(server, "write_file")(str(root / "owned.txt"), "v1\n", False, "W1")
    answer = _tool_fn(server, "fleet_submit")("W1", "сделано\nприёмка: невозможна — нужен телефон владельца")
    assert "сдана на приёмку" in answer


def test_autostart_join_hands_out_the_task_at_once(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    join = _tool_fn(server, "fleet_join")
    first = join("f1007-1300abc")
    assert "уже взята за тобой" in first and "TZ-X" in first
    with store.tx() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-X'") == "taken"
    again = join("f1007-1300abc")  # повторный join того же чата не берёт вторую
    assert "уже взята за тобой" not in again
    assert "уже взята за тобой" not in join("manual tab")  # чат не от расширения — как раньше


def _legacy_bite_fix_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise the real fleet_submit facade without a live bite runner or DB."""
    from dev_mcp.fleet import bite, queue

    monkeypatch.setattr(bite, "ENABLED", True)
    monkeypatch.setattr(queue, "REQUIRE_GREEN_SUBMIT", False)
    _, store, root, audit = _make_server(tmp_path)
    # The legacy note contract uses the Windows `py -3` launcher. Only this
    # synthetic policy permits it; production policy is never changed.
    policy = Policy.load(tmp_path / "policy.toml")
    policy.exec_allowed_bin.append("py")
    server = build_server(policy, audit)
    tz = "fix:TZ-LEGACY-BITE"
    _seed_task(store, root, tz=tz)
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET kind='fix', acceptance='[]' WHERE project='demo' AND tz=?",
            (tz,), event={"kind": "seed_fix"},
        )
    assert "worker_id: W1" in _tool_fn(server, "fleet_join")("legacy-bite-tab")
    assert f"demo/{tz}" in _tool_fn(server, "fleet_take")("W1")
    (root / "owned.txt").write_text("changed after claim\n", encoding="utf-8")

    started: list[tuple] = []

    def fake_start(store_arg, project, task_tz, work_root, claim_id, argv, record):
        started.append((store_arg, project, task_tz, work_root, claim_id, argv))
        return None

    monkeypatch.setattr(bite, "start", fake_start)
    return server, store, root, tz, started


def _legacy_bite_state(store: Store, tz: str):
    with store.read() as tx:
        task = tx.one("SELECT state FROM tasks WHERE project='demo' AND tz=?", (tz,))
        review = tx.one("SELECT state FROM tasks WHERE project='demo' AND tz=?", (f"review:{tz}",))
        claim = tx.one("SELECT state FROM claims WHERE worker_id='W1' ORDER BY id DESC LIMIT 1")
        events = tx.query(
            "SELECT payload FROM events WHERE kind='bite' AND project='demo' AND tz=? ORDER BY id",
            (tz,),
        )
    return (
        str(task["state"]),
        str(review["state"]) if review else None,
        str(claim["state"]),
        [json.loads(str(row["payload"])) for row in events],
    )


def test_legacy_two_argument_submit_starts_bite_and_records_pending(tmp_path: Path, monkeypatch) -> None:
    server, store, root, tz, started = _legacy_bite_fix_fixture(tmp_path, monkeypatch)
    # Deliberately call the pre-bite_test two-argument tool signature.
    result = _tool_fn(server, "fleet_submit")(
        "W1", "Regression added.\nbite: py -3 -m pytest tests/fleet/test_tools.py -q",
    )
    assert "сдана на приёмку" in result
    assert _legacy_bite_state(store, tz) == (
        "submitted", "ready", "released",
        [{"verdict": "pending", "command": "py -3 -m pytest tests/fleet/test_tools.py -q",
          "reason": "проверка идёт в отдельной копии, результат появится здесь"}],
    )
    assert len(started) == 1
    started_store, project, task_tz, work_root, claim_id, argv = started[0]
    assert str(started_store.path) == str(store.path)
    assert (project, task_tz, work_root) == ("demo", tz, str(root))
    assert isinstance(claim_id, int) and claim_id > 0
    assert argv == ["py", "-3", "-m", "pytest", "tests/fleet/test_tools.py", "-q"]


def test_fix_submit_without_legacy_bite_is_rejected(tmp_path: Path, monkeypatch) -> None:
    server, store, _, tz, started = _legacy_bite_fix_fixture(tmp_path, monkeypatch)
    refused = _tool_fn(server, "fleet_submit")("W1", "Fix complete, no bite line")
    assert "кусающим тестом" in refused
    assert _legacy_bite_state(store, tz) == ("taken", None, "active", [])
    assert started == []


def test_legacy_bite_opt_out_records_skipped_without_running(tmp_path: Path, monkeypatch) -> None:
    server, store, _, tz, started = _legacy_bite_fix_fixture(tmp_path, monkeypatch)
    result = _tool_fn(server, "fleet_submit")("W1", "UI-only.\nbite: n/a — визуальный дефект")
    assert "сдана на приёмку" in result
    assert _legacy_bite_state(store, tz) == (
        "submitted", "ready", "released", [{"verdict": "skipped", "reason": "визуальный дефект"}],
    )
    assert started == []


def test_explicit_bite_test_precedes_legacy_note_command(tmp_path: Path, monkeypatch) -> None:
    server, store, _, tz, started = _legacy_bite_fix_fixture(tmp_path, monkeypatch)
    result = _tool_fn(server, "fleet_submit")(
        "W1", "bite: powershell -NoProfile", "py -3 -m pytest explicit_case.py -q",
    )
    assert "сдана на приёмку" in result
    state, review, claim, events = _legacy_bite_state(store, tz)
    assert (state, review, claim) == ("submitted", "ready", "released")
    assert len(events) == 1 and events[0]["verdict"] == "pending"
    assert events[0]["command"] == "py -3 -m pytest explicit_case.py -q"
    assert len(started) == 1 and started[0][5] == ["py", "-3", "-m", "pytest", "explicit_case.py", "-q"]


@pytest.mark.parametrize("legacy_command", [
    'bite: py -3 -m pytest "unterminated',  # malformed quoting
    "bite: powershell -NoProfile",  # disallowed executable
    "bite: py -3 -m pytest ../outside.py",  # disallowed parent traversal
])
def test_invalid_legacy_bite_never_submits(legacy_command: str, tmp_path: Path, monkeypatch) -> None:
    server, store, _, tz, started = _legacy_bite_fix_fixture(tmp_path, monkeypatch)
    refused = _tool_fn(server, "fleet_submit")("W1", "Fixed.\n" + legacy_command)
    assert refused.startswith("Отказано:")
    assert _legacy_bite_state(store, tz) == ("taken", None, "active", [])
    assert started == []


def test_registered_tools_keep_two_isolated_claims_off_trunk(tmp_path: Path, monkeypatch) -> None:
    """Real FastMCP tools must honor durable take bindings, not just adapter calls."""
    import subprocess
    import venv
    from dev_mcp.fleet import goals
    from dev_mcp.fleet.claim_take_routing import resolve_active_claim_context

    root = tmp_path / "repo"
    root.mkdir()
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "claim@example.invalid")
    git("config", "user.name", "Claim")
    (root / "owned.txt").write_text("trunk\n", encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (root / "runner.py").write_text(
        "from pathlib import Path\n"
        "print('claim-cwd:' + str(Path.cwd()))\n"
        "print('payload:' + Path('owned.txt').read_text().strip())\n", encoding="utf-8",
    )
    git("add", "-A")
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")
    # Shared interpreter exists only in the project root, not the Git claim trees.
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    assert (root / ".venv" / "Scripts" / "python.exe").is_file()

    monkeypatch.setattr(goals, "project_info", lambda name: {
        "commit": True, "claim_trees": True,
    } if name == "demo" else {})
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{tmp_path.as_posix()}']\n"
        f"write_roots = ['{tmp_path.as_posix()}']\n"
        f"exec_roots = ['{tmp_path.as_posix()}']\n"
        "exec_allowed_bin = ['git', 'python']\n"
        "allow_write = true\n"
        "deny_patterns = ['.git', '.env']\n"
        "[tasks.audit]\n"
        f"cwd = '{root.as_posix()}'\n"
        "argv = ['git', 'status', '--short']\n"
        "description = 'isolation test'\n", encoding="utf-8",
    )
    db = tmp_path / "fleet.sqlite"
    store = Store.open(str(db))
    created = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES ('demo',?,1,1)",
                  (str(root),), event={"kind": "seed"})
        for w in ("WA", "WB"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES(?,?,'chatgpt',?,?)", (w, w, created, created),
                      event={"kind": "seed"})
        for tz in ("TZ-A", "TZ-B"):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
                "needs,owns,resources,acceptance,body,created_at,updated_at) "
                "VALUES('demo',?,'t','ready',1,'work','','','[]',?,'[]','[]','',?,?)",
                (tz, json.dumps([str(root / "owned.txt")]), created, created),
                event={"kind": "seed"},
            )
    mcp = build_server(Policy.load(config), Audit(tmp_path / "logs/audit.jsonl"), fleet_db=db)
    take = _tool_fn(mcp, "fleet_take")
    assert "demo/TZ-A" in take("WA")
    assert "demo/TZ-B" in take("WB")
    ca = resolve_active_claim_context(store, "WA")
    cb = resolve_active_claim_context(store, "WB")
    assert ca.tree_path != cb.tree_path
    write = _tool_fn(mcp, "write_file")
    edit = _tool_fn(mcp, "edit_file")
    read = _tool_fn(mcp, "read_file")
    patch = _tool_fn(mcp, "apply_patch")
    assert "перезаписан" in write(str(root / "owned.txt"), "agent A\n", True, "WA")
    assert "перезаписан" in write(str(root / "owned.txt"), "agent B\n", True, "WB")
    assert "заменено" in edit(str(root / "owned.txt"), "agent A", "edited A", False, "WA")
    assert "применено файлов: 1" in patch(
        "*** Begin Patch\n*** Update File: owned.txt\n@@\n-agent B\n+patched B\n*** End Patch",
        str(root), "WB",
    )
    assert "edited A" in read(str(root / "owned.txt"), 1, 10, "WA")
    assert "patched B" in read(str(root / "owned.txt"), 1, 10, "WB")
    ls = _tool_fn(mcp, "list_dir")
    search = _tool_fn(mcp, "search_files")
    assert "owned.txt" in ls(str(root), "*", False, "WA")
    assert "edited A" in search("edited A", str(root), "*.txt", False, 10, "WA")
    assert "Совпадений нет" in search("patched B", str(root), "*.txt", False, 10, "WA")
    assert "Совпадений нет" in search("patched B", "", "*.txt", False, 10, "WA")
    assert (ca.tree_path / "owned.txt").read_text() == "edited A\n"
    assert (cb.tree_path / "owned.txt").read_text() == "patched B\n"
    assert (root / "owned.txt").read_text() == "trunk\n"
    assert git("rev-parse", "HEAD") == baseline
    assert git("status", "--porcelain") == ""
    assert "Отказано" in write(str(cb.tree_path / "owned.txt"), "bad", True, "WA")
    assert "Отказано" in write(str(root.parent / "escape.txt"), "bad", False, "WA")
    assert "Отказано" in write(str(root / ".." / "repo" / "owned.txt"), "bad", True, "WA")
    assert "Отказано" in write(str(root / "owned.txt"), "bad", True, "")

    run = _tool_fn(mcp, "run")
    result = run(str(root), ["git", "status", "--short"], "", 20, False, "WA")
    assert "код возврата: 0" in result and "owned.txt" in result
    assert str(ca.tree_path) in result
    assert "Отказано" in run(str(root), ["git", "-c", "safe.directory=*"], "", 20, False, "WA")
    assert not (ca.tree_path / ".venv").exists()
    assert not (cb.tree_path / ".venv").exists()
    for worker, context, payload in (("WA", ca, "edited A"), ("WB", cb, "patched B")):
        executed = run(str(root), [".venv/Scripts/python.exe", "runner.py"], "", 20, False, worker)
        assert "код возврата: 0" in executed, executed
        assert f"claim-cwd:{context.tree_path}" in executed, executed
        assert f"payload:{payload}" in executed, executed
    # The allowance is exact: do not turn arbitrary checkout paths or parent paths
    # into privileged execution of the shared interpreter.
    assert "Отказано" in run(str(root), ["other/.venv/Scripts/python.exe", "runner.py"], "", 20, False, "WA")
    assert "Отказано" in run(str(root), ["../.venv/Scripts/python.exe", "runner.py"], "", 20, False, "WA")
    named = _tool_fn(mcp, "run_task")("audit", "WA")
    assert "код возврата: 0" in named and "owned.txt" in named
    # Configuration changes after take cannot silently re-enable the trunk.
    monkeypatch.setattr(goals, "project_info", lambda _name: {
        "commit": False, "claim_trees": False,
    })
    assert "edited A" in read(str(root / "owned.txt"), 1, 10, "WA")
    assert (root / "owned.txt").read_text() == "trunk\n"
    with store.read() as tx:
        events = tx.query("SELECT payload FROM events WHERE kind='write' AND worker_id='WA'")
        runs = tx.query("SELECT payload FROM events WHERE kind='run' AND worker_id='WA'")
    assert events and any(json.loads(row["payload"]).get("claim_id") == ca.claim_id
                          for row in events)
    from dev_mcp.fleet.evidence import tree_hash
    assert runs and any(
        (payload := json.loads(row["payload"])).get("claim_id") == ca.claim_id
        and payload.get("tree") == tree_hash([str(ca.tree_path / "owned.txt")])
        and payload.get("tree") != tree_hash([str(root / "owned.txt")])
        for row in runs
    )


def test_isolated_watch_triage_submit_review_and_trusted_commit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real facade: owned decision -> sealed triage -> independent review -> Git."""
    import subprocess
    import venv
    from dev_mcp.fleet import goals
    from dev_mcp.fleet.claim_accept_flow import ClaimAcceptFlow
    from dev_mcp.fleet.claim_review_bite import ClaimReviewError, capture_submission
    from dev_mcp.fleet.claim_take_routing import resolve_active_claim_context

    root = tmp_path / "repo"
    decision = root / "ToDo" / "decisions" / "watchdog-test.md"
    decision.parent.mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "pass.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")

    def git(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Triage test")
    git("config", "user.email", "triage@example.invalid")
    git("add", "-A")
    git("commit", "-m", "initial")
    base = git("rev-parse", "HEAD")
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    monkeypatch.setattr(goals, "project_info", lambda _name: {
        "commit": True, "claim_trees": True,
    })
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{tmp_path.as_posix()}']\n"
        f"write_roots = ['{tmp_path.as_posix()}']\n"
        f"exec_roots = ['{tmp_path.as_posix()}']\n"
        "exec_allowed_bin = ['python', 'git']\n"
        "allow_write = true\n"
        "deny_patterns = ['.git', '.env']\n", encoding="utf-8",
    )
    db = tmp_path / "fleet.sqlite"
    store = Store.open(str(db))
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('demo',?,1,1)",
                  (str(root),), event={"kind": "seed"})
        for who in ("WA", "WB"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES(?,?,'chatgpt',?,?)", (who, who, now, now),
                      event={"kind": "seed"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('demo','watch:fixture','decision','ready',1,'triage','','',"
            "'[]',?,'[]',?,'write decision',?,?)",
            (json.dumps([str(decision)]), json.dumps(["python tests/pass.py"]), now, now),
            event={"kind": "seed"},
        )
    mcp = build_server(Policy.load(config), Audit(tmp_path / "logs/audit.jsonl"), fleet_db=db)
    take = _tool_fn(mcp, "fleet_take")
    submit = _tool_fn(mcp, "fleet_submit")
    write = _tool_fn(mcp, "write_file")
    run = _tool_fn(mcp, "run")
    verdict = _tool_fn(mcp, "fleet_review")
    assert "watch:fixture" in take("WA")
    ctx = resolve_active_claim_context(store, "WA")
    assert ctx and ctx.mode == "isolated"

    # Empty triage refuses BEFORE pinning an orphan submission. A fake author
    # note and a different worker cannot release this claim.
    refused = submit("WA", "decision made without a file edit")
    assert "Итог задачи-суждения" in refused, refused
    assert "Отказано" in submit("WB", "not my claim")
    assert "Отказано" in write(str(root / "foreign.txt"), "bad\n", False, "WA")
    with pytest.raises(ClaimReviewError, match="active author claim"):
        capture_submission(store, ctx.claim_id, "WB")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='claim_submission'") == 0
        assert tx.scalar("SELECT state FROM tasks WHERE project='demo' "
                         "AND tz='watch:fixture'") == "taken"

    assert "создан:" in write(str(decision), "Decision: investigate\n", False, "WA")
    # Even with an owned change, smuggling a foreign file into the private
    # checkout must fail the sealed-diff guard.
    (ctx.tree_path / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    assert "outside accepted owns" in submit("WA", "decision file ready")
    (ctx.tree_path / "foreign.txt").unlink()
    submitted = submit("WA", "Decision recorded in owned file.")
    assert "сдана на приёмку" in submitted, submitted
    assert git("rev-parse", "HEAD") == base
    assert not decision.exists()  # isolated author cannot write directly to trunk
    with store.read() as tx:
        seal = tx.one("SELECT payload FROM events WHERE kind='claim_submission' "
                      "AND project='demo' AND tz='watch:fixture' ORDER BY id DESC")
        assert seal is not None
        payload = json.loads(seal["payload"])
        assert payload["claim_id"] == ctx.claim_id
        assert git("rev-parse", payload["revision"] + "^{tree}") == payload["tree_sha"]
        assert tx.scalar("SELECT state FROM tasks WHERE project='demo' "
                         "AND tz='watch:fixture'") == "submitted"

    assert "review:watch:fixture" in take("WB")
    green = run(str(root), ["python", "tests/pass.py"], "", 30, False, "WB")
    assert "код возврата: 0" in green, green
    accepted = verdict("WB", "accept", "sealed revision and decision verified")
    assert "Вердикт accept" in accepted, accepted
    integrated = ClaimAcceptFlow(
        store, settings=lambda _: {"commit": True, "claim_trees": True},
        gate=lambda _: 0,
    ).on_accept(ctx.claim_id, payload["revision"])
    assert integrated.status == "integrated"
    assert git("rev-parse", "HEAD") != base
    assert decision.read_text(encoding="utf-8") == "Decision: investigate\n"
    store.close()


def test_completed_intent_does_not_report_superseded_template_tasks_as_blockers(tmp_path):
    from dev_mcp.fleet import outcome
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted'",event='test.complete')
    for ident, state in [('old-root','dropped'),('new-root','done')]:
        item=outcome.create_outcome(store,project='demo',title='root',intent_id=ident,
                                    spec={'intent_ref':ident},evidence_spec={'kind':'review'},authority={'test':True})
        outcome.close_outcome(store,item.id,state=state)
        if state=='dropped':
            with store.tx() as tx:
                tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) "
                          "VALUES('demo',?,'frozen','now','now')",(item.id+'-clean_start',),event='test.old_stage')
                stale_id=item.id
    text=_tool_fn(server,'fleet_list')()
    assert 'интенция завершена; очередь пуста' in text, text
    assert stale_id not in text, text
