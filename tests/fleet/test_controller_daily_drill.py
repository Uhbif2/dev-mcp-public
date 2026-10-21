"""Server registration and actual UTC-day synthetic drill (never production state)."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dev_mcp.fleet import controller, drills
from dev_mcp.fleet.store import Store


def test_controller_registers_background_daily_worker(monkeypatch, tmp_path: Path) -> None:
    stopped = threading.Event()
    scheduled = threading.Event()
    db = tmp_path / "separate-fleet.sqlite"
    class FakeController:
        def __init__(self, store, *, policy=None, intent_project=None,
                     delivery_route=None, handoff_route=None, draft_runner=None,
                     register_drafts_inline=True):
            assert isinstance(store, Store)
            assert draft_runner is None, "foreign test Store must not gain pytest authority"
        def route_status(self):
            return {"intents": "unprovisioned", "delivery": "unprovisioned",
                    "handoff": "unprovisioned"}
        def tick(self):
            assert scheduled.wait(3), "daily runner was not registered at server startup"
            stopped.set()

    def scheduler(stop_event):
        assert stop_event is stopped
        scheduled.set()

    monkeypatch.setattr(controller, "Controller", FakeController)
    monkeypatch.setattr(controller, "run_daily_drill_scheduler", scheduler)
    controller.run_controller(db, stopped, interval_s=0.01)
    assert scheduled.is_set()
    with Store.open(str(db)).read() as tx:
        startup = tx.one("SELECT payload FROM events WHERE kind='controller_provisioning'")
    assert startup and json.loads(startup["payload"])["status"] == "fail_closed"
    assert not (tmp_path / "reports").exists(), "daily drill must never use production DB root"


def test_server_daily_tick_creates_independent_isolated_report(tmp_path: Path) -> None:
    """Run the real daily entrypoint via the actual scheduler, not a mocked green verdict."""
    sandbox = tmp_path / "fleet-drill-sandbox"
    fixed = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    stop = threading.Event()
    result: list[dict] = []
    finished = threading.Event()

    def instrumented_run(root, *, now):
        report = drills.run_daily(root, now=now)
        result.append(report)
        finished.set()
        return report

    worker = threading.Thread(
        target=controller.run_daily_drill_scheduler,
        args=(stop,),
        kwargs={"run_daily": instrumented_run, "now": lambda: fixed,
                "sandbox_root": sandbox},
        daemon=True,
    )
    worker.start()
    try:
        assert finished.wait(60), "production daily scheduler must execute at least one drill"
    finally:
        stop.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(result) == 1, "no second daily tick on the same UTC day"
    report = result[0]
    assert report["synthetic"] is True
    assert report["date"] == "2026-10-09"
    assert report["status"] == "recovered", report.get("capability_gaps")
    assert len(report["scenarios"]) == 5
    path = sandbox / "reports" / "2026-10-09.json"
    assert path.is_file(), "independent on-disk report is the proof, not a log message"
    recorded = json.loads(path.read_text(encoding="utf-8"))
    assert recorded["report_digest"] == report["report_digest"]
    assert drills.run_daily(sandbox, now=fixed)["report_digest"] == report["report_digest"]
    assert not list(sandbox.glob("case-*")), "no leftover synthetic project"


def test_real_server_controller_fails_closed_then_delivers_with_trusted_route(
    tmp_path: Path, monkeypatch,
) -> None:
    """Actual run_controller entrypoint, SQLite acceptance and a synthetic Operator.

    No route must not consume an accepted event. After trusted provisioning it
    must reach the real delivery.on_accept and record an Operator-backed receipt.
    """
    monkeypatch.setattr(controller, "run_daily_drill_scheduler", lambda stop: None)
    monkeypatch.setattr(controller, "run_cross_review_worker", lambda store, stop: None)
    db = tmp_path / "fleet.sqlite"
    store = Store.open(str(db))
    old = "a" * 64
    new = "b" * 64
    subject = "c" * 64
    good = "d" * 64
    policy = f'''
[delivery]
version = 1
target = "staging"
workspace = {json.dumps(str(tmp_path))}
timeout_seconds = 30
known_good_revision = "{good}"

[apply]
operation = "cloud.deploy"
[apply.params]
workspace = {json.dumps(str(tmp_path))}
target = "staging"
artifact_sha256 = "{subject}"
release_id = "r1"

[verify]
operation = "ci.run"
[verify.params]
workspace = {json.dumps(str(tmp_path))}
target = "staging"
commit_sha = "{subject}"
workflow = "postdeploy"

[rollback]
operation = "cloud.deploy"
[rollback.params]
workspace = {json.dumps(str(tmp_path))}
target = "staging"
artifact_sha256 = "{good}"
release_id = "known_good"
'''
    (tmp_path / "deliver.toml").write_text(policy, encoding="utf-8")
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Demo", str(tmp_path)), event="seed")
        for tz, kind, acceptance in (
            ("TZ-X", "work", '["python -m pytest"]'),
            ("review:TZ-X", "review", "[]"),
        ):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,kind,acceptance,created_at,updated_at) "
                "VALUES('Demo',?,?,'accepted',?,?,?,?)",
                (tz, tz, kind, acceptance, stamp, stamp), event="seed",
            )
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES('W-reviewer','reviewer','chatgpt',?,?)", (stamp, stamp), event="seed")
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
                  "VALUES('Demo','review:TZ-X','W-reviewer','released',?,?)",
                  (stamp, stamp), event="seed")
        tx.record_event(kind="accept", worker_id="W-reviewer", project="Demo", tz="TZ-X",
                        payload={"review_task": "review:TZ-X"})

    ops: list[str] = []
    environment = [old]

    def operator(name, params):
        assert name in {"cloud.deploy", "ci.run"}
        ops.append(name)
        if name == "cloud.deploy":
            environment[0] = new
        return {"ok": True, "evidence": {
            "kind": "delivery" if name == "cloud.deploy" else "run",
            "ref": "opref:" + "e" * 64, "subject_version": subject,
            "env_version": environment[0], "verdict": "pass",
            "producer": "operator", "operation": name,
            "request_fingerprint": "f" * 64, "observed_ts": 1,
            "metrics": {"steps": 1},
        }}

    def launch(provisioning, condition):
        stopped = threading.Event()
        thread = threading.Thread(
            target=controller.run_controller, args=(db, stopped),
            kwargs={"interval_s": 0.05, "provisioning": provisioning}, daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 12
        try:
            while not condition() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert condition(), "startup route did not reach its durable evidence"
        finally:
            stopped.set()
            thread.join(timeout=5)
        assert not thread.is_alive()

    def fail_closed_seen():
        with store.read() as tx:
            return bool(tx.scalar("SELECT COUNT(*) FROM events "
                "WHERE kind='controller_provisioning' AND payload LIKE '%fail_closed%'")) and bool(
                tx.scalar("SELECT COUNT(*) FROM events WHERE kind='controller_tick'"))

    launch(None, fail_closed_seen)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM controller_delivery_receipts") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='controller_tick'") >= 1
    assert not ops, "unprovisioned production route may not publish"

    route = controller.DeliveryRuntime(
        allowed_roots=(tmp_path,), state_db=tmp_path / "delivery.sqlite",
        subject_version=subject, env_version=old, fleet_op=operator,
        probe_environment=lambda root, target: environment[0],
    )
    trusted = controller.ControllerProvisioning(delivery_route=lambda project, tz: route)

    def delivered():
        with store.read() as tx:
            receipt = tx.scalar("SELECT receipt FROM controller_delivery_receipts LIMIT 1")
            return bool(receipt and json.loads(receipt).get("status") == "delivered")

    launch(trusted, delivered)
    assert ops == ["cloud.deploy", "ci.run"]
    with store.read() as tx:
        statuses = [json.loads(e["payload"])["routes"] for e in tx.query(
            "SELECT payload FROM events WHERE kind='controller_provisioning' ORDER BY id")]
        assert statuses[0] == dict.fromkeys(("intents", "delivery", "handoff"), "unprovisioned")
        assert statuses[-1]["delivery"] == "provisioned"
        assert statuses[-1]["intents"] == "unprovisioned"
