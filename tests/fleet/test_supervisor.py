from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store
from dev_mcp.fleet import supervisor


def _store(tmp_path: Path) -> Store:
    store = Store.open(str(tmp_path / "data" / "fleet" / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    root = tmp_path / "work"
    root.mkdir()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,?,1)",
            ("demo", str(root), 1),
            event={"kind": "seed"},
        )
        tx.change(
            """INSERT INTO tasks(
                   project,tz,title,state,priority,kind,spec_path,spec_sha,
                   needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "demo", "TZ-1", "task", "ready", 1, "work", "ToDo/TZ-1.md", "sha",
                "[]", f'["{norm_path(str(root / "owned.txt"))}"]', "[]", "[]",
                None, "", now, now,
            ),
            event={"kind": "seed"},
        )
    return store


def test_demand_sees_runnable_work_without_any_browser_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    demand = supervisor.fleet_demand(store)
    assert demand.runnable == 1
    assert demand.active_claims == 0
    assert demand.unfinished == 1
    assert demand.needed


def test_extension_lease_is_separate_from_worker_liveness(tmp_path: Path) -> None:
    store = _store(tmp_path)
    supervisor.record_extension_heartbeat(store, version="0.15.0", tabs=4, pending=2)
    supervisor._write_supervisor_status(
        store,
        demand=supervisor.FleetDemand(runnable=1, active_claims=3, unfinished=5),
        ready=True,
        action="healthy",
    )
    line = supervisor.status_line(store)
    assert "browser=up" in line
    assert "extension=alive v0.15.0" in line
    assert "tabs=4" in line
    assert "pending=2" in line
    assert "active_claims=3" in line

    old = datetime.now(timezone.utc) + timedelta(seconds=supervisor.EXTENSION_STALE_S + 5)
    stale = supervisor.status_line(store, now=old)
    assert "extension=stale" in stale


def test_chrome_args_use_dedicated_profile_and_debug_port() -> None:
    args = supervisor.chrome_argv(Path(r"C:\Chrome\chrome.exe"), r"C:\Local")
    assert "--user-data-dir=C:\\Local\\FleetChrome" in args
    assert "--remote-debugging-port=9223" in args
    assert "--restore-last-session" in args
    assert "https://chatgpt.com/" in args


class _Audit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict, dict]] = []

    def record(self, kind: str, args: dict, **meta) -> None:
        self.rows.append((kind, args, meta))


class _Stop:
    def __init__(self) -> None:
        self.waits = 0

    def is_set(self) -> bool:
        return self.waits >= 2

    def wait(self, _seconds: float) -> bool:
        self.waits += 1
        return self.is_set()


def test_supervisor_starts_browser_when_work_exists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = _Audit()
    stop = _Stop()
    probes = iter([False, True, True])

    def probe() -> bool:
        return next(probes, True)

    starts: list[int] = []

    def starter() -> int:
        starts.append(1)
        return 4242

    supervisor.supervise(
        store.path,
        audit,
        stop,  # type: ignore[arg-type]
        interval_s=0,
        start_timeout_s=1,
        probe=probe,
        starter=starter,
        alive_finder=lambda: [],
    )

    assert starts == [1]
    assert any(kind == "fleet_chrome_supervisor" and meta.get("decision") == "allow"
               for kind, _args, meta in audit.rows)
    line = supervisor.status_line(store)
    assert "browser=up" in line
    assert "supervisor=waiting-extension" in line


def test_stop_targets_only_dedicated_devtools_listener() -> None:
    calls: list[list[str]] = []

    class _Done:
        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout
            self.returncode = 0

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[0] == "netstat.exe":
            return _Done("  TCP    127.0.0.1:9223    0.0.0.0:0    LISTENING    4242\n")
        return _Done()

    assert supervisor.listener_pid(run=fake_run) == 4242
    assert supervisor.stop_fleet_chrome(run=fake_run) == 4242
    assert ["taskkill.exe", "/F", "/T", "/PID", "4242"] in calls


def test_supervisor_restarts_browser_when_extension_lease_is_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    supervisor.record_extension_heartbeat(store, version="0.15.0", tabs=3, pending=0)
    status_path = Path(store.path).with_name(supervisor.STATUS_FILE)
    data = json.loads(status_path.read_text(encoding="utf-8"))
    data["extension"]["seen_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=supervisor.EXTENSION_STALE_S + 10)
    ).isoformat()
    status_path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(supervisor, "EXTENSION_BOOT_GRACE_S", 0.0)
    audit = _Audit()
    stop = _Stop()
    stopped: list[int] = []
    started: list[int] = []

    def stopper() -> int:
        stopped.append(4242)
        return 4242

    def starter() -> int:
        started.append(4343)
        return 4343

    supervisor.supervise(
        store.path,
        audit,
        stop,  # type: ignore[arg-type]
        interval_s=0,
        start_timeout_s=1,
        probe=lambda: True,
        starter=starter,
        stopper=stopper,
        tick_age_finder=lambda: None,
        extension_reloader=lambda: False,
    )

    assert stopped == [4242]
    assert started == [4343]
    assert any(
        kind == "fleet_chrome_supervisor"
        and meta.get("decision") == "allow"
        and "extension reload" in meta.get("reason", "")
        for kind, _args, meta in audit.rows
    )
    line = supervisor.status_line(store)
    assert "browser=up" in line
    assert "supervisor=extension-backoff" in line


def test_supervisor_prefers_extension_reload_before_browser_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    supervisor.record_extension_heartbeat(store, version="0.15.1", tabs=3, pending=1)
    status_path = Path(store.path).with_name(supervisor.STATUS_FILE)
    data = json.loads(status_path.read_text(encoding="utf-8"))
    data["extension"]["seen_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=supervisor.EXTENSION_STALE_S + 10)
    ).isoformat()
    status_path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(supervisor, "EXTENSION_BOOT_GRACE_S", 0.0)
    monkeypatch.setattr(supervisor, "EXTENSION_RELOAD_GRACE_S", 60.0)
    audit = _Audit()
    stop = _Stop()
    reloaded: list[bool] = []
    stopped: list[bool] = []
    started: list[bool] = []

    supervisor.supervise(
        store.path,
        audit,
        stop,  # type: ignore[arg-type]
        interval_s=0,
        start_timeout_s=1,
        probe=lambda: True,
        starter=lambda: started.append(True) or 4343,
        stopper=lambda: stopped.append(True) or 4242,
        tick_age_finder=lambda: None,
        extension_reloader=lambda: reloaded.append(True) or True,
    )

    assert reloaded == [True]
    assert stopped == []
    assert started == []
    assert any(
        kind == "fleet_chrome_supervisor"
        and meta.get("decision") == "allow"
        and "extension reload requested through DevTools" in meta.get("reason", "")
        for kind, _args, meta in audit.rows
    )
    line = supervisor.status_line(store)
    assert "browser=up" in line
    assert "supervisor=waiting-extension-reload" in line


def _run_down(tmp_path: Path, *, alive, steps: int, clock):
    store = _store(tmp_path)
    audit = _Audit()

    class Stop:
        def __init__(self) -> None:
            self.n = 0

        def is_set(self) -> bool:
            return self.n >= steps

        def wait(self, _s: float) -> bool:
            self.n += 1
            return self.is_set()

    starts: list[int] = []
    kills: list[list[int]] = []
    import time as _time

    orig = _time.monotonic
    _time.monotonic = clock  # type: ignore[assignment]
    try:
        supervisor.supervise(
            store.path,
            audit,
            Stop(),  # type: ignore[arg-type]
            interval_s=0,
            start_timeout_s=0.01,
            probe=lambda: False,
            starter=lambda: starts.append(1) or 1,
            alive_finder=lambda: list(alive),
            killer=lambda pids: kills.append(list(pids)),
        )
    finally:
        _time.monotonic = orig  # type: ignore[assignment]
    return starts, kills, audit


def test_supervisor_does_not_open_second_window_while_chrome_alive(tmp_path: Path) -> None:
    ticks = iter(range(0, 10_000, 10))
    starts, kills, _audit = _run_down(tmp_path, alive=[777], steps=3, clock=lambda: float(next(ticks)))
    assert starts == []  # живой процесс — второй chrome.exe не запускается
    assert kills == []  # срок молчания DevTools ещё не вышел


def test_supervisor_kills_silent_chrome_after_grace_then_restarts(tmp_path: Path) -> None:
    t = {"v": 0.0}

    def clock() -> float:
        t["v"] += 60.0
        return t["v"]

    starts, kills, audit = _run_down(tmp_path, alive=[777], steps=6, clock=clock)
    assert kills and kills[0] == [777]
    assert any("DevTools молчит" in str(meta.get("reason")) for _k, _a, meta in audit.rows)


def test_fresh_lease_but_stalled_tick_triggers_extension_reload(
    tmp_path: Path, monkeypatch,
) -> None:
    store = _store(tmp_path)
    supervisor.record_extension_heartbeat(store, version="0.18.1", tabs=3, pending=0)
    monkeypatch.setattr(supervisor, "EXTENSION_BOOT_GRACE_S", 0.0)
    audit = _Audit()
    reloaded: list[bool] = []
    restarted: list[bool] = []

    supervisor.supervise(
        store.path,
        audit,
        _Stop(),  # type: ignore[arg-type]
        interval_s=0,
        probe=lambda: True,
        tick_age_finder=lambda: supervisor.EXTENSION_TICK_STALE_S + 120,
        extension_reloader=lambda: reloaded.append(True) or True,
        stopper=lambda: restarted.append(True) or 4242,
    )
    assert reloaded == [True]
    assert restarted == []
    assert "supervisor=waiting-extension-reload" in supervisor.status_line(store)
    assert any("tick stale" in str(meta.get("reason", "")) for _, _, meta in audit.rows)


def test_fresh_tick_keeps_supervisor_healthy(
    tmp_path: Path, monkeypatch,
) -> None:
    store = _store(tmp_path)
    supervisor.record_extension_heartbeat(store, version="0.18.1", tabs=3, pending=0)
    monkeypatch.setattr(supervisor, "EXTENSION_BOOT_GRACE_S", 0.0)
    reloads: list[bool] = []
    supervisor.supervise(
        store.path,
        _Audit(),
        _Stop(),  # type: ignore[arg-type]
        interval_s=0,
        probe=lambda: True,
        tick_age_finder=lambda: 20.0,
        extension_reloader=lambda: reloads.append(True) or True,
    )
    assert reloads == []
    assert "supervisor=healthy" in supervisor.status_line(store)


def test_stale_mcp_lease_but_live_local_recovery_does_not_restart_chrome(tmp_path, monkeypatch):
    store=_store(tmp_path)
    monkeypatch.setattr(supervisor,'EXTENSION_BOOT_GRACE_S',0.0)
    reloads=[]
    supervisor.supervise(store.path,_Audit(),_Stop(),interval_s=0,probe=lambda:True,
                         tick_age_finder=lambda:5.0,
                         extension_reloader=lambda:reloads.append(True) or True,
                         stopper=lambda:reloads.append('browser') or 123)
    assert reloads==[]
    assert 'supervisor=control-unreachable' in supervisor.status_line(store)


def test_partial_extension_receipt_preserves_snapshot_and_completed_tick(tmp_path):
    store=_store(tmp_path)
    supervisor.record_extension_heartbeat(store,version='0.19.2',tabs=3,pending=1,
                                          enabled=True,runnable=2,tick_completed=True)
    path=Path(store.path).with_name(supervisor.STATUS_FILE)
    before=json.loads(path.read_text())['extension']
    supervisor.record_extension_heartbeat(store)
    after=json.loads(path.read_text())['extension']
    assert {k:after[k] for k in before if k!='seen_at'}=={k:v for k,v in before.items() if k!='seen_at'}


def test_missing_extension_tick_probes_as_stalled_without_touching_browser(
    tmp_path: Path, monkeypatch,
) -> None:
    store = _store(tmp_path)
    supervisor.record_extension_heartbeat(store, version="0.18.1", tabs=1, pending=0)
    monkeypatch.setattr(supervisor, "EXTENSION_BOOT_GRACE_S", 0.0)
    reloads: list[bool] = []
    supervisor.supervise(
        store.path,
        _Audit(),
        _Stop(),  # type: ignore[arg-type]
        interval_s=0,
        probe=lambda: True,
        tick_age_finder=lambda: float("inf"),
        extension_reloader=lambda: reloads.append(True) or True,
    )
    assert reloads == [True]


def test_stalled_tick_escalates_to_browser_restart_after_reload_grace(
    tmp_path: Path, monkeypatch,
) -> None:
    store = _store(tmp_path)
    supervisor.record_extension_heartbeat(store, version="0.18.1", tabs=2, pending=0)
    monkeypatch.setattr(supervisor, "EXTENSION_BOOT_GRACE_S", 0.0)
    monkeypatch.setattr(supervisor, "EXTENSION_RELOAD_GRACE_S", 0.1)
    clock = {"t": 100.0}
    def advance() -> float:
        clock["t"] += 0.25
        return clock["t"]
    monkeypatch.setattr(supervisor.time, "monotonic", advance)
    reloads: list[bool] = []
    stops: list[int] = []
    starts: list[int] = []
    supervisor.supervise(
        store.path,
        _Audit(),
        _Stop(),  # type: ignore[arg-type]
        interval_s=0,
        start_timeout_s=1,
        probe=lambda: True,
        tick_age_finder=lambda: supervisor.EXTENSION_TICK_STALE_S + 5,
        extension_reloader=lambda: reloads.append(True) or True,
        stopper=lambda: stops.append(4242) or 4242,
        starter=lambda: starts.append(4343) or 4343,
    )
    assert reloads == [True]
    assert stops == [4242]
    assert starts == [4343]
    assert "supervisor=restarted-for-extension" in supervisor.status_line(store)


def test_unavailable_tick_probe_keeps_legacy_lease_behaviour(
    tmp_path: Path, monkeypatch,
) -> None:
    store = _store(tmp_path)
    supervisor.record_extension_heartbeat(store, version="0.18.1", tabs=1, pending=0)
    monkeypatch.setattr(supervisor, "EXTENSION_BOOT_GRACE_S", 0.0)
    reloads: list[bool] = []
    supervisor.supervise(
        store.path,
        _Audit(),
        _Stop(),  # type: ignore[arg-type]
        interval_s=0,
        probe=lambda: True,
        tick_age_finder=lambda: None,
        extension_reloader=lambda: reloads.append(True) or True,
    )
    assert reloads == []
    assert "supervisor=healthy" in supervisor.status_line(store)
