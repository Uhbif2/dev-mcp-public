"""Ресурсы мира (fleet/world.py): датчик, разморозка по событию, проверка мира не наследуется."""
from __future__ import annotations

import json
import socket
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from dev_mcp.fleet import world
from dev_mcp.fleet.queue import take_next, thaw_frozen
from dev_mcp.fleet.store import Store

CFG = """
[resources."телефон"]
world = true
sensor = "adb"
need = "подключи телефон"

[resources."node"]
world = true
"""


def now(delta_min: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_min)).isoformat(timespec="microseconds")


@pytest.fixture(autouse=True)
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "fleet-projects.toml"
    path.write_text(CFG, encoding="utf-8")
    monkeypatch.setenv("DEV_MCP_FLEET_PROJECTS", str(path))


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    with s.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)", (str(tmp_path / "P"),),
                  event={"kind": "test_setup"})
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
                  "VALUES('W1','w','chatgpt',?,?,'')", (now(), now()), event={"kind": "test_setup"})
    return s


def add(store: Store, tz: str, *, state: str = "ready", needs: list[str] | None = None,
        resources: list[str] | None = None) -> None:
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,resources,"
            "acceptance,body,created_at,updated_at) VALUES('P',?,'t',?,1,'work','','',?,'[]',?,'[]','',?,?)",
            (tz, state, json.dumps(needs or []), json.dumps(resources or []), now(), now()),
            event={"kind": "test_setup"},
        )


def sense(store: Store, state: str) -> list[str]:
    return world.record(store, {"телефон": (state, "test")})


# ---- проверка мира не наследуется ------------------------------------------------------------

def idx(tasks: dict[str, tuple[str, list[str], list[str]]]):
    return world.build_index({"project": "P", "tz": tz, "state": s, "needs": n, "resources": r}
                             for tz, (s, n, r) in tasks.items())


W = frozenset({"телефон", "node"})


def test_evidence_dependency_holds_only_its_inputs() -> None:
    index = idx({
        "CODE": ("accepted", [], []),
        "LIVE": ("ready", ["CODE"], ["телефон"]),          # живая сессия, входы готовы
        "ECON": ("ready", ["LIVE"], []),                    # юнит-экономике телефон не нужен
        "PHONE2": ("ready", ["LIVE"], ["телефон"]),         # вторая телефонная задача — ждёт сессию
    })
    assert world.unmet_needs("P", ["LIVE"], [], index, W) == []
    assert world.relaxed_needs("P", ["LIVE"], [], index, W) == ["LIVE"]
    assert world.unmet_needs("P", ["LIVE"], ["телефон"], index, W) == ["LIVE"]


def test_evidence_relaxation_is_recursive_but_code_still_holds() -> None:
    index = idx({
        "IMPL": ("ready", [], []),                          # код ещё не сделан
        "LIVE": ("ready", ["IMPL"], ["телефон"]),
        "LIVE2": ("ready", ["LIVE"], ["телефон"]),
        "CONS": ("ready", ["LIVE2"], []),
        "CODE_DEP": ("ready", ["IMPL"], []),
    })
    assert world.unmet_needs("P", ["LIVE2"], [], index, W) == ["LIVE2"]  # под цепочкой — непринятый код
    assert world.unmet_needs("P", ["IMPL"], [], index, W) == ["IMPL"]
    index[("P", "IMPL")]["state"] = "accepted"
    assert world.unmet_needs("P", ["LIVE2"], [], index, W) == []


def test_rejected_or_unknown_dependency_still_holds() -> None:
    index = idx({"LIVE": ("rejected", [], ["телефон"])})
    assert world.unmet_needs("P", ["LIVE", "NOPE"], [], index, W) == ["LIVE", "NOPE"]


def test_take_next_hands_out_consumer_of_pending_evidence(store: Store) -> None:
    add(store, "CODE", state="accepted")
    add(store, "LIVE", needs=["CODE"], resources=["телефон"])
    add(store, "ECON", needs=["LIVE"])
    sense(store, "down")  # телефона нет: LIVE не выдаётся, ECON — выдаётся
    assert take_next(store, "W1").tz == "ECON"


# ---- датчик ---------------------------------------------------------------------------------

def test_missing_resource_is_not_handed_out_until_it_appears(store: Store) -> None:
    add(store, "LIVE", resources=["телефон"])
    sense(store, "down")
    assert take_next(store, "W1") is None
    assert sense(store, "up") == ["телефон"]
    assert take_next(store, "W1").tz == "LIVE"


def test_unknown_sensor_state_keeps_old_behaviour(store: Store) -> None:
    add(store, "LIVE", resources=["телефон"])
    assert take_next(store, "W1").tz == "LIVE"


def test_transitions_only_are_recorded(store: Store) -> None:
    assert sense(store, "down") == []
    assert sense(store, "down") == []
    assert sense(store, "up") == ["телефон"]
    with store.tx() as tx:
        n = tx.scalar("SELECT COUNT(*) FROM events WHERE kind='resource'")
        assert world.last_state(tx, "телефон")[0] == "up"
    assert n == 2


def block(store: Store, tz: str, reason: str, ts: str) -> None:
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='frozen' WHERE tz=?", (tz,), event={"kind": "test_setup"})
        tx.record_event(kind="block", project="P", tz=tz, ts=ts, payload={"reason": reason})


def test_frozen_task_thaws_when_resource_appears_not_by_timer(store: Store) -> None:
    add(store, "LIVE", resources=["телефон"])
    sense(store, "down")
    block(store, "LIVE", "blocked: adb devices пуст, нет usb устройства", now(-600))
    assert thaw_frozen(store) == []               # таймер не трогает: ресурса нет
    sense(store, "up")
    thawed = thaw_frozen(store)
    assert len(thawed) == 1 and "появился ресурс: телефон" in thawed[0]


def test_block_while_resource_up_follows_timer(store: Store) -> None:
    add(store, "LIVE", resources=["телефон"])
    sense(store, "up")
    block(store, "LIVE", "blocked: cellular underlay red", now(1))
    assert thaw_frozen(store) == []               # телефон был и есть — фактов не прибавилось


def test_named_unaccepted_dependency_wins_over_resource(store: Store) -> None:
    # TZ-05: a dependency must be structured; prose alone cannot hold a task.
    add(store, "LIVE", resources=["телефон"], needs=["TZ-9"])
    add(store, "TZ-9")
    sense(store, "down")
    block(store, "LIVE", "ждём TZ-9", now(-1))
    sense(store, "up")
    assert thaw_frozen(store) == []


class FakeAdb:
    def __init__(self, body: bytes, status: bytes = b"OKAY") -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.got = b""

        def serve() -> None:
            conn, _ = self.sock.accept()
            with conn:
                self.got = conn.recv(64)
                conn.sendall(status + (b"%04x" % len(body) + body if status == b"OKAY" else b""))
        threading.Thread(target=serve, daemon=True).start()


def test_adb_sensor_speaks_server_protocol() -> None:
    fake = FakeAdb(b"085562527M002215\tdevice\nemulator-5554\toffline\n")
    state, detail = world._adb_devices(port=fake.port)
    assert fake.got == b"000chost:devices"
    assert state == "up" and "085562527M002215" in detail


def test_adb_sensor_down_and_unknown() -> None:
    fake = FakeAdb(b"X\tunauthorized\n")
    assert world._adb_devices(port=fake.port)[0] == "down"
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert world._adb_devices(port=port, timeout=0.5)[0] == "unknown"


def test_console_line_counts_waiting_work(store: Store) -> None:
    import fleet as console  # tools/fleet.py
    add(store, "CODE")
    add(store, "LIVE", resources=["телефон"])
    add(store, "AFTER", needs=["LIVE"], resources=["телефон"])
    add(store, "ECON", needs=["LIVE"])
    sense(store, "down")
    conn = console._open_existing(Path(store.path))
    try:
        lines = console.world_lines(conn)
    finally:
        conn.close()
    assert len(lines) == 1
    assert "«телефон»" in lines[0] and "ждут 2" in lines[0] and "ещё 0" in lines[0]
    assert "подключи телефон" in lines[0]


# ---- ресурс на время процесса ----------------------------------------------------------------

RUN_CFG = CFG + """
[resources."heavy-build"]
scope = "run"
run_match = '(build|bundle)'
"""


@pytest.fixture()
def run_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "run.toml"
    path.write_text(RUN_CFG, encoding="utf-8")
    monkeypatch.setenv("DEV_MCP_FLEET_PROJECTS", str(path))


def second_worker(store: Store) -> None:
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
                  "VALUES('W2','w','chatgpt',?,?,'')", (now(), now()), event={"kind": "test_setup"})


def test_run_scoped_resource_does_not_block_take(store: Store, run_cfg: None) -> None:
    second_worker(store)
    add(store, "A", resources=["heavy-build"])
    add(store, "B", resources=["heavy-build"])
    assert take_next(store, "W1").tz == "A"
    assert take_next(store, "W2").tz == "B"      # раньше B ждал весь захват A


def test_only_matching_commands_serialize(store: Store, run_cfg: None) -> None:
    second_worker(store)
    add(store, "A", resources=["heavy-build"])
    add(store, "B", resources=["heavy-build"])
    take_next(store, "W1"), take_next(store, "W2")
    rel_a, why = world.hold_for_run(store, "W1", ["py", "-3", "app/build_local.py"], "W1 build")
    assert rel_a is not None and why == ""
    light, _ = world.hold_for_run(store, "W2", ["git", "diff"], "W2 diff", wait_s=0.1)
    assert light is not None                      # лёгкая команда не ждёт чужую сборку
    light()
    busy, why = world.hold_for_run(store, "W2", ["py", "bundle.py"], "W2 bundle", wait_s=0.1)
    assert busy is None and "W1 build" in why and "не повод для fleet_block" in why
    rel_a()
    rel_b, _ = world.hold_for_run(store, "W2", ["py", "bundle.py"], "W2 bundle", wait_s=0.1)
    assert rel_b is not None
    rel_b()


def test_worker_without_resource_is_not_affected(store: Store, run_cfg: None) -> None:
    add(store, "A")
    take_next(store, "W1")
    release, why = world.hold_for_run(store, "W1", ["py", "build.py"], "W1", wait_s=0.1)
    assert release is not None and why == ""
    release()


def test_global_run_resource_covers_undeclared_pytest_and_keeps_reads_free(store: Store, monkeypatch) -> None:
    monkeypatch.setattr(world, 'config', lambda: {
        'host-test': {'scope':'run','global':True,'run_match':'pytest'}})
    first, _ = world.hold_for_run(store, '', ['py','-3','-m','pytest'], 'first test', wait_s=0)
    assert first is not None
    try:
        light, _ = world.hold_for_run(store, '', ['git','diff'], 'read', wait_s=0)
        assert light is not None
        light()
        second, why = world.hold_for_run(store, '', ['pytest.exe','-q'], 'second test', wait_s=0)
        assert second is None and 'НЕ запущена' in why
    finally:
        first()
    next_run, _ = world.hold_for_run(store, '', ['pytest.exe'], 'next', wait_s=0)
    assert next_run is not None
    next_run()


# ---- уровни готовности ------------------------------------------------------------------------

def test_level_dependency_satisfied_by_provide_submit_or_accept() -> None:
    index = idx({"API": ("taken", [], [])})
    assert world.unmet_needs("P", ["API#contract"], [], index, W) == ["API#contract"]
    assert world.unmet_needs("P", ["API"], [], index, W) == ["API"]
    index[("P", "API")]["provided"] = {"contract"}
    assert world.unmet_needs("P", ["API#contract"], [], index, W) == []
    assert world.unmet_needs("P", ["API"], [], index, W) == ["API"]          # без уровня — как раньше
    assert world.unmet_needs("P", ["API#impl"], [], index, W) == ["API#impl"]
    index[("P", "API")].update(state="submitted", provided=set())
    assert world.unmet_needs("P", ["API#contract"], [], index, W) == []


def test_checkpoint_provide_unlocks_consumer(store: Store) -> None:
    from dev_mcp.fleet.tools import FleetContext, _record_provides
    second_worker(store)
    add(store, "API")
    add(store, "UI", needs=["API#contract"])
    assert take_next(store, "W1").tz == "API"
    assert take_next(store, "W2") is None                                   # контракта ещё нет
    ctx = FleetContext(store=store, policy=None)
    note = _record_provides(ctx, "W1", "P/API", "схема зафиксирована, provide:contract")
    assert "contract" in note and "ждавших его задач: 1" in note
    assert take_next(store, "W2").tz == "UI"
    assert _record_provides(ctx, "W1", "P/API", "ещё раз provide:contract")  # повтор — без дубля
    with store.tx() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='provide'") == 1


def test_producer_brief_names_waiting_consumers(store: Store) -> None:
    from dev_mcp.fleet.model import Task
    from dev_mcp.fleet.tools import FleetContext, _relaxed_needs_note
    add(store, "API")
    add(store, "UI", needs=["API#contract"])
    task = Task(project="P", tz="API", state="taken", priority=1, kind="work", spec_path="", spec_sha="",
                needs=(), owns=(), resources=(), acceptance=(), title="t", body="")
    lines = _relaxed_needs_note(FleetContext(store=store, policy=None), task)
    assert any("От тебя ждут уровня «contract»: UI" in line for line in lines)
    assert any("provide:contract" in line for line in lines)


def test_deeper_remaining_chain_goes_first(store: Store) -> None:
    add(store, "WIDE")
    add(store, "DEEP")
    for i in range(3):
        add(store, f"W{i}", needs=["WIDE"])         # широко, но мелко: цепочка 1
    add(store, "D1", needs=["DEEP"])
    add(store, "D2", needs=["D1"])
    add(store, "D3", needs=["D2"])                  # глубина 3 — критический путь
    assert take_next(store, "W1").tz == "DEEP"


# ---- проба препятствия (fleet/probe.py) -----------------------------------------------------

class StubPolicy:
    exec_allowed_bin = ["py", "python", "adb"]

    def guard_exec_args(self, args, bin_name=""):
        for a in args:
            if a.startswith(("C:", "/")):
                raise ValueError(f"абсолютный путь: {a}")


def test_until_line_is_parsed_from_block_reason() -> None:
    from dev_mcp.fleet import probe
    assert probe.from_reason("blocked: нода молчит\nuntil: py -3 tools/ping.py — когда ответит") == "py -3 tools/ping.py"
    assert probe.from_reason("**until:** `adb devices`") == "adb devices"
    assert probe.from_reason("blocked: без пробы") == ""
    assert probe.from_reason("blocked: rc=1. Подменять нельзя. until: py -3 tools/e2e.py --spawn") == "py -3 tools/e2e.py --spawn"
    assert probe.from_reason("blocked: wait until: later") == ""


def frozen_with_probe(store: Store, tz: str, cmd: str) -> str:
    from dev_mcp.fleet import probe
    add(store, tz)
    block(store, tz, f"blocked\nuntil: {cmd}", now(-30))
    return probe.register(store, StubPolicy(), "W1", "P", tz, f"until: {cmd}")


def test_probe_thaws_task_when_it_passes_and_holds_it_while_failing(store: Store) -> None:
    from dev_mcp.fleet import probe
    assert "Проба принята" in frozen_with_probe(store, "B", "py -3 check.py")
    rcs = iter([1, 0])
    calls = []

    def runner(argv, cwd):
        calls.append(argv)
        return next(rcs)

    with store.tx() as tx:
        tx.change("UPDATE projects SET root=?", (str(Path(store.path).parent),), event={"kind": "test_setup"})
    assert probe.run_due(store, StubPolicy(), runner) == ["P/B rc=1"]
    assert thaw_frozen(store) == []                    # проба падает — таймер (30 мин) не трогает
    assert probe.run_due(store, StubPolicy(), runner) == []   # пауза 2 мин ещё не прошла
    with store.tx() as tx:
        tx.change("UPDATE events SET ts=? WHERE kind='probe_run'", (now(-3),), event={"kind": "test_setup"})
    assert probe.run_due(store, StubPolicy(), runner) == ["P/B rc=0"]
    thawed = thaw_frozen(store)
    assert len(thawed) == 1 and "проба препятствия прошла: py -3 check.py" in thawed[0]
    assert calls == [["py", "-3", "check.py"]] * 2


def test_probe_outside_policy_is_refused_and_block_stays_plain(store: Store) -> None:
    note = frozen_with_probe(store, "B", "curl http://x")
    assert "не принята" in note
    with store.tx() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='block_probe'") == 0


def test_new_block_without_probe_forgets_old_probe(store: Store) -> None:
    from dev_mcp.fleet import probe
    frozen_with_probe(store, "B", "py -3 check.py")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE tz='B'", (), event={"kind": "thaw", "project": "P", "tz": "B"})
    block(store, "B", "blocked: другое", now())
    with store.tx() as tx:
        assert probe.passed(tx, "P", "B") == (False, None)


def test_structured_process_probe_uses_server_observer(store: Store, monkeypatch) -> None:
    from dev_mcp.fleet import probe, process_idle
    note = frozen_with_probe(store, "B", "process-idle tools/check.py --mode beta")
    assert "Проба принята" in note
    with store.tx() as tx:
        tx.change("UPDATE projects SET root=?", (str(Path(store.path).parent),),
                  event={"kind": "test_setup"})
    monkeypatch.setattr(process_idle, "run", lambda argv, cwd: 0)
    assert probe.run_due(store, StubPolicy(), lambda *a: pytest.fail("must not spawn")) == ["P/B rc=0"]
    assert len(thaw_frozen(store)) == 1


def test_disk_admission_probe_observes_capacity_without_running_acceptance(store: Store, monkeypatch) -> None:
    from types import SimpleNamespace
    from dev_mcp.fleet import probe
    note = frozen_with_probe(store, "DISK", "disk-admission py -3 build.py")
    assert "Проба принята" in note
    with store.tx() as tx:
        tx.change("UPDATE projects SET root=?", (str(Path(store.path).parent),), event="seed")
    cfg = {"disk": {"scope": "run", "global": True, "sensor": "disk", "path": ".",
                    "min_free_bytes": 100, "run_match": "build"}}
    monkeypatch.setattr(world, "config", lambda: cfg)
    monkeypatch.setattr("shutil.disk_usage", lambda p: SimpleNamespace(free=99))
    never_run = lambda *a: pytest.fail("admission must never execute embedded command")
    assert probe.run_due(store, StubPolicy(), never_run) == ["P/DISK rc=1"]
    assert thaw_frozen(store) == []
    with store.tx() as tx:
        tx.change("UPDATE events SET ts=? WHERE kind='probe_run'", (now(-3),), event="seed")
    monkeypatch.setattr("shutil.disk_usage", lambda p: SimpleNamespace(free=101))
    assert probe.run_due(store, StubPolicy(), never_run) == ["P/DISK rc=0"]
    assert len(thaw_frozen(store)) == 1


def test_obsolete_probe_success_cannot_pass_replacement_block(store: Store) -> None:
    from dev_mcp.fleet import probe
    frozen_with_probe(store, "B", "py -3 old.py")
    with store.tx() as tx:
        tx.change("UPDATE projects SET root=?", (str(Path(store.path).parent),),
                  event={"kind": "test_setup"})

    def runner(argv, cwd):
        probe.register(store, StubPolicy(), "W1", "P", "B", "until: py -3 new.py")
        return 0

    assert probe.run_due(store, StubPolicy(), runner) == []
    with store.read() as tx:
        assert probe.passed(tx, "P", "B") == (False, ["py", "-3", "new.py"])
    assert thaw_frozen(store) == []


def test_new_obstacle_is_not_starved_by_old_higher_priority_probes(store: Store):
    from dev_mcp.fleet import probe
    for i in range(probe.MAX_PER_TICK + 1):
        frozen_with_probe(store, f"OLD-{i}", "py -3 check.py")
        with store.tx() as tx:
            tx.change("UPDATE tasks SET priority=0 WHERE tz=?", (f"OLD-{i}",), event="seed")
            tx.record_event(kind="probe_run", project="P", tz=f"OLD-{i}",
                            ts=now(-60), payload={"rc": 1})
    frozen_with_probe(store, "FRESH", "py -3 check.py")
    assert probe.due_probes(store)[0][1] == "FRESH"


def test_tcp_and_http_sensors() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert world._tcp({"host": "127.0.0.1", "port": port})[0] == "up"
    srv.close()
    assert world._tcp({"host": "127.0.0.1", "port": port, "timeout": 0.5})[0] == "down"
    assert world._tcp({})[0] == "unknown"
    assert world._http({"url": f"http://127.0.0.1:{port}/", "timeout": 0.5})[0] == "down"


def test_task_frozen_only_on_world_evidence_inherits_its_world(store: Store) -> None:
    add(store, "LIVE", resources=["телефон"])                  # живая сессия на телефоне
    add(store, "CODE", needs=["LIVE"])                        # код зелёный, «живой цикл ждёт LIVE»
    add(store, "NEXT", needs=["CODE"])                        # потребителю телефон не нужен
    add(store, "PHONE", needs=["CODE"], resources=["телефон"])
    add(store, "MIXED", needs=["CODE2"])
    add(store, "IMPL")
    add(store, "CODE2")
    block(store, "CODE", "blocked: код зелёный, но живой цикл ждёт TZ LIVE: LIVE не accepted", now())
    block(store, "CODE2", "blocked: ждём LIVE и IMPL", now())
    with store.tx() as tx:
        refs = world.block_refs(tx)
        index = world.build_index(tx.query("SELECT project,tz,state,needs,resources FROM tasks"), None, refs)
    # имена LIVE/IMPL не похожи на TZ-ид — подставим разбор вручную, как его дал бы TZ-регекс
    index[("P", "CODE")]["blocked_on"] = ["LIVE"]
    index[("P", "CODE2")]["blocked_on"] = ["LIVE", "IMPL"]
    assert world.unmet_needs("P", ["CODE"], [], index, W) == []
    assert world.unmet_needs("P", ["CODE"], ["телефон"], index, W) == ["CODE"]
    assert world.unmet_needs("P", ["CODE2"], [], index, W) == ["CODE2"]   # держит и код IMPL


def test_block_refs_reads_task_ids_from_reason(store: Store) -> None:
    add(store, "TZ-61")
    block(store, "TZ-61", "blocked: TZ-61 зелёный, но live цикл в TZ-78; TZ-78 не accepted", now())
    with store.tx() as tx:
        assert world.block_refs(tx) == {("P", "TZ-61"): ["TZ-78"]}


def test_shared_evidence_input_reached_by_two_branches() -> None:
    index = idx({
        "LIVE": ("ready", [], ["телефон"]),
        "LIVE2": ("ready", ["LIVE"], ["телефон"]),
        "CODE": ("frozen", ["LIVE2", "LIVE"], []),           # LIVE — и напрямую, и через LIVE2
    })
    index[("P", "CODE")]["blocked_on"] = ["LIVE"]
    assert world.unmet_needs("P", ["CODE"], [], index, W) == []


def test_console_why_names_holder_of_ready_work(store: Store) -> None:
    import fleet as console  # tools/fleet.py
    second_worker(store)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET owns='[]'", (), event={"kind": "test_setup"})
    add(store, "HOLD")
    add(store, "WAIT")
    add(store, "LATER", needs=["HOLD"])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET owns=? WHERE tz IN ('HOLD','WAIT')", (json.dumps(["c:/p/hot.py"]),),
                  event={"kind": "test_setup"})
    assert take_next(store, "W1").tz == "HOLD"
    conn = console._open_existing(Path(store.path))
    try:
        lines = console.stuck_lines(conn)
    finally:
        conn.close()
    assert lines[0].startswith("готовых 2: можно выдать 0;") and "ждёт задачи 1" in lines[0]
    assert any("P/WAIT: зона у HOLD (hot.py)" in line for line in lines)
    assert lines[-1] == "больше всех держит готовую работу: HOLD — 1"


def test_effective_blockers_replace_evidence_task_with_its_holding_inputs() -> None:
    index = idx({
        "IMPL": ("ready", [], []),
        "DONE": ("accepted", [], []),
        "LIVE": ("ready", ["IMPL", "DONE"], ["телефон"]),
        "CODE": ("ready", [], []),
    })
    assert world.effective_blockers("P", ["LIVE", "CODE"], [], index, W) == ["IMPL", "CODE"]
    assert world.effective_blockers("P", ["LIVE"], ["телефон"], index, W) == ["LIVE"]


def _heavy_cfg(monkeypatch) -> None:
    monkeypatch.setattr(world, 'config', lambda: {
        'heavy-build': {'scope': 'run', 'global': True, 'scan_processes': True,
                        'run_match': r'\b(check_all|product_release_check)'}})
    monkeypatch.setenv('DEV_MCP_FLEET_GATE_LOCK', '1')


def test_orphan_heavy_process_outside_dev_mcp_blocks_run(store: Store, monkeypatch) -> None:
    # 17.10: после рестарта dev-mcp пять check_all шли параллельно — замок жил в памяти.
    _heavy_cfg(monkeypatch)
    monkeypatch.setattr(world, '_foreign_runs', lambda pattern: ['pid 15932 с 14:57: py tools/check_all.py'])
    release, why = world.hold_for_run(store, 'W1', ['py', '-3', 'tools/check_all.py'], 'W1', wait_s=0)
    assert release is None and 'pid 15932' in why
    monkeypatch.setattr(world, '_foreign_runs', lambda pattern: [])
    release, why = world.hold_for_run(store, 'W1', ['py', '-3', 'tools/check_all.py'], 'W1', wait_s=0)
    assert release is not None and not why
    release()


def test_gate_and_agent_run_share_one_lock(store: Store, monkeypatch) -> None:
    _heavy_cfg(monkeypatch)
    monkeypatch.setattr(world, '_foreign_runs', lambda pattern: [])
    agent, _ = world.hold_for_run(store, 'W1', ['py', 'tools/product_release_check.py', '--mode', 'rc'], 'W1')
    assert agent is not None
    gate, why = world.hold_blocking(['py', '-3', 'tools/check_all.py'], 'gate', wait_s=0.2)
    assert gate is None and 'W1' in why
    agent()
    gate, why = world.hold_blocking(['py', '-3', 'tools/check_all.py'], 'gate', wait_s=0.2)
    assert gate is not None and not why
    gate()
    light, _ = world.hold_blocking(['git', 'status'], 'gate', wait_s=0)
    assert light is not None


def test_foreign_scan_ignores_own_process_tree() -> None:
    assert world._foreign_runs(r'\bpytest\b') == [] or all(
        f'pid {__import__("os").getpid()} ' not in line for line in world._foreign_runs(r'\bpytest\b'))


def test_disk_sensor_needs_a_margin_to_come_back_up(store: Store, monkeypatch) -> None:
    """20.10.2026: build-disk flipped every minute at the threshold."""
    from types import SimpleNamespace
    cfg = {"build-disk": {"sensor": "disk", "path": ".", "min_free_bytes": 100,
                          "recover_margin_bytes": 50}}
    monkeypatch.setattr(world, "config", lambda: cfg)
    world.record(store, {"build-disk": (world.DOWN, "low")})
    monkeypatch.setattr("shutil.disk_usage", lambda p: SimpleNamespace(free=120))
    assert world.record(store, {"build-disk": (world.UP, "just above")}) == []
    monkeypatch.setattr("shutil.disk_usage", lambda p: SimpleNamespace(free=160))
    assert world.record(store, {"build-disk": (world.UP, "clear")}) == ["build-disk"]
