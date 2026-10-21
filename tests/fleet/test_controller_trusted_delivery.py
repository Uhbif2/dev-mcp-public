"""Accepted fleet event -> protected Operator route -> durable failure receipt.

No production key, address, process or deployment is accessed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dev_mcp.fleet import controller
from dev_mcp.fleet.store import Store
from dev_mcp.operator_cli import OperatorIPCClient


def _signed_observation(path: Path, key: bytes, *, artifact: str,
                        workspace: Path, env: str) -> None:
    body = {
        "project": "Demo", "workspace": str(workspace), "target": "staging",
        "artifact_sha256": artifact, "env_version": env,
        "observed_ts": int(time.time()),
    }
    message = json.dumps(body, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    body["mac"] = hmac.new(key, b"operator-delivery-observation-v1\x00" + message,
                           hashlib.sha256).hexdigest()
    path.write_text(json.dumps(body), encoding="utf-8")


def _setup(tmp_path: Path, address: str):
    root = tmp_path / "product"
    root.mkdir()
    artifact = tmp_path / "accepted-artifact.bin"
    artifact.write_bytes(b"synthetic accepted bytes")
    subject = hashlib.sha256(artifact.read_bytes()).hexdigest()
    original_env = "b" * 64
    good = "d" * 64
    (root / "deliver.toml").write_text(
        f'[delivery]\nversion = 1\ntarget = "staging"\n'
        f'workspace = {json.dumps(str(root))}\ntimeout_seconds = 5\n'
        f'known_good_revision = "{good}"\n'
        f'[apply]\noperation = "cloud.deploy"\n[apply.params]\n'
        f'workspace = {json.dumps(str(root))}\ntarget = "staging"\n'
        f'artifact_sha256 = "{subject}"\nrelease_id = "new"\n'
        f'[verify]\noperation = "ci.run"\n[verify.params]\n'
        f'workspace = {json.dumps(str(root))}\ntarget = "staging"\n'
        f'commit_sha = "{subject}"\nworkflow = "postdeploy"\n'
        f'[rollback]\noperation = "cloud.deploy"\n[rollback.params]\n'
        f'workspace = {json.dumps(str(root))}\ntarget = "staging"\n'
        f'artifact_sha256 = "{good}"\nrelease_id = "old"\n',
        encoding="utf-8",
    )
    key_file = tmp_path / "ipc-transport.key"
    key_file.write_bytes(b"k" * 48)  # Synthetic-only transport secret.
    ipc = tmp_path / "ipc.toml"
    ipc.write_text(
        f'[operator_ipc]\naddress = {json.dumps(address)}\n'
        f'key_file = {json.dumps(str(key_file))}\ntimeout_s = 0.12\n',
        encoding="utf-8",
    )
    attestor = tmp_path / "attestation.key"
    attestor.write_bytes(b"a" * 48)
    observation = tmp_path / "observation.json"
    _signed_observation(observation, attestor.read_bytes(), artifact=subject,
                        workspace=root, env=original_env)
    config = tmp_path / "delivery.toml"
    config.write_text(
        '[operator_delivery]\n'
        f'ipc_config = {json.dumps(str(ipc))}\n'
        f'state_db = {json.dumps(str(tmp_path / "delivery.sqlite"))}\n'
        f'attestation_key = {json.dumps(str(attestor))}\n'
        '[[projects]]\nname = "Demo"\n'
        f'root = {json.dumps(str(root))}\n'
        f'workspace = {json.dumps(str(root))}\ntarget = "staging"\n'
        f'artifact = {json.dumps(str(artifact))}\n'
        f'observation = {json.dumps(str(observation))}\n'
        f'allowed_roots = [{json.dumps(str(root))}]\n',
        encoding="utf-8",
    )
    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    store = Store.open(str(db))
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Demo", str(root)), event="seed")
        for tz, kind, checks in (("TZ-X", "work", '["synthetic test"]'),
                                 ("review:TZ-X", "review", "[]")):
            tx.change("INSERT INTO tasks(project,tz,title,state,kind,acceptance,"
                      "created_at,updated_at) VALUES('Demo',?,?,'accepted',?,?,?,?)",
                      (tz, tz, kind, checks, stamp, stamp), event="seed")
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES('W-r','reviewer','chatgpt',?,?)", (stamp, stamp), event="seed")
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
                  "VALUES('Demo','review:TZ-X','W-r','released',?,?)",
                  (stamp, stamp), event="seed")
        tx.record_event(kind="accept", worker_id="W-r", project="Demo", tz="TZ-X",
                        payload={"review_task": "review:TZ-X"})
    return db, store, config


def test_trusted_route_hung_operator_has_hard_deadline_and_durable_receipt(
    tmp_path, monkeypatch,
):
    # A stalled *external* IPC peer must not stall the Controller event cursor.
    # A mocked Python callback cannot furnish a hard timeout; the production
    # route is required to bind the real, deadline-limited OperatorIPCClient.
    if __import__("os").name == "nt":
        from multiprocessing.connection import Listener
        address = "\\\\.\\pipe\\fleet-test-" + uuid.uuid4().hex
        def stalled():
            with Listener(address, family="AF_PIPE") as listener:
                ready.set()
                with listener.accept():
                    time.sleep(0.7)
    else:
        address = str(tmp_path / "operator.sock")
        def stalled():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(address)
                listener.listen(1)
                ready.set()
                with listener.accept()[0]:
                    time.sleep(0.7)
    ready = threading.Event()
    db, store, config = _setup(tmp_path, address)
    provisioning = controller.load_trusted_delivery_provisioning(config, db)
    runtime = provisioning.delivery_route("Demo", "TZ-X")
    assert type(runtime.fleet_op) is OperatorIPCClient
    assert runtime.operator_timeout_s == runtime.fleet_op.timeout_s == 0.12
    thread = threading.Thread(target=stalled, daemon=True)
    thread.start()
    assert ready.wait(3)

    monkeypatch.setattr(controller, "run_daily_drill_scheduler", lambda _stop: None)
    monkeypatch.setattr(controller, "run_cross_review_worker", lambda _store, _stop: None)
    monkeypatch.setattr(controller, "run_regater", lambda *_: None)
    stop = threading.Event()
    original_tick = controller.Controller.tick
    def once(self):
        try:
            return original_tick(self)
        finally:
            stop.set()
    monkeypatch.setattr(controller.Controller, "tick", once)
    started = time.monotonic()
    controller.run_controller(db, stop, interval_s=0.001, provisioning=provisioning)
    elapsed = time.monotonic() - started
    with store.read() as tx:
        row = tx.one("SELECT receipt FROM controller_delivery_receipts WHERE project='Demo'")
        cursor = tx.scalar("SELECT last_event FROM controller_cursor WHERE name='delivery_accept'")
        provisioned = tx.one("SELECT payload FROM events WHERE kind='controller_provisioning'")
    assert row is not None, "accepted review must produce a durable receipt"
    result = json.loads(row["receipt"])
    assert result["status"] != "delivered"
    assert result["status"] in {"rollback_failed", "failed", "repair_pending"}
    assert cursor is not None and cursor > 0
    assert json.loads(provisioned["payload"])["routes"]["delivery"] == "provisioned"
    assert elapsed < 3.0, "hung IPC blocked the controller reconciliation cycle"
    thread.join(3)
    assert not thread.is_alive()


def test_provisioning_rejects_tampered_independent_observation(tmp_path):
    db, _store, config = _setup(tmp_path, str(tmp_path / "nonexistent.sock"))
    provision = controller.load_trusted_delivery_provisioning(config, db)
    assert provision.delivery_route("Demo", "TZ-X").subject_version
    observation = tmp_path / "observation.json"
    body = json.loads(observation.read_text(encoding="utf-8"))
    body["env_version"] = "f" * 64  # Unsigned attempt to spoof environment.
    observation.write_text(json.dumps(body), encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="trusted_delivery_observation_invalid"):
        provision.delivery_route("Demo", "TZ-X")
