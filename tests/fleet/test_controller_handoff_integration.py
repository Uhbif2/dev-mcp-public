"""Post-accept → production controller → TZ-28 handoff → owner-only dashboard.

The production entrypoint is exercised, not a hand-constructed Controller.
Operator and GUI are deterministic substitutes; no HKCU or native toast writes.
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import controller, outcome
from dev_mcp.fleet.handoff import HandoffService
from dev_mcp.fleet.store import Store
from dev_mcp.owner.overview import snapshot


SUBJECT = "c" * 64
BEFORE = "a" * 64
AFTER = "b" * 64
GOOD = "d" * 64


def _delivery_config(root: Path) -> None:
    (root / "deliver.toml").write_text(f"""
[delivery]
version = 1
target = "staging"
workspace = {json.dumps(str(root))}
timeout_seconds = 30
known_good_revision = "{GOOD}"
[apply]
operation = "cloud.deploy"
[apply.params]
workspace = {json.dumps(str(root))}
target = "staging"
artifact_sha256 = "{SUBJECT}"
release_id = "new"
[verify]
operation = "ci.run"
[verify.params]
workspace = {json.dumps(str(root))}
target = "staging"
commit_sha = "{SUBJECT}"
workflow = "postdeploy"
[rollback]
operation = "cloud.deploy"
[rollback.params]
workspace = {json.dumps(str(root))}
target = "staging"
artifact_sha256 = "{GOOD}"
release_id = "old"
""", encoding="utf-8")


def _installed_dashboard(root: Path, profile: Path) -> str:
    source = Path(__file__).resolve().parents[2] / "extension" / "fleet-tabs"
    extension = root / "extension" / "fleet-tabs"
    (extension / "dashboard").mkdir(parents=True)
    shutil.copyfile(source / "manifest.json", extension / "manifest.json")
    for name in ("index.html", "app.js"):
        shutil.copyfile(source / "dashboard" / name, extension / "dashboard" / name)
    import base64
    import hashlib
    key = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))["key"]
    ext_id = "".join(chr(97 + n) for byte in hashlib.sha256(
        base64.b64decode(key)
    ).digest()[:16] for n in (byte >> 4, byte & 15))
    profile.mkdir(parents=True)
    (profile / "Secure Preferences").write_text(json.dumps({
        "extensions": {"settings": {ext_id: {
            "location": 4, "state": 1, "path": str(extension),
        }}},
    }), encoding="utf-8")
    return f"chrome-extension://{ext_id}/dashboard/index.html"


def _accepted_result(store: Store, project_root: Path) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Demo", str(project_root)), event="seed")
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
                  "VALUES('W-reviewer','reviewer','chatgpt',?,?)",
                  (stamp, stamp), event="seed")
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
                  "VALUES('Demo','review:TZ-X','W-reviewer','released',?,?)",
                  (stamp, stamp), event="seed")
        tx.record_event(kind="accept", worker_id="W-reviewer", project="Demo", tz="TZ-X",
                        payload={"review_task": "review:TZ-X"})
    outcome.create_outcome(
        store, project="Demo", title="Public release", intent_id="release-intent",
        spec={"goal": "release", "dashboard_visibility": "public"},
        evidence_spec={"kind": "run", "subject_version": SUBJECT},
        authority={"source": "owner"}, outcome_id="release",
    )
    outcome.link_work(store, outcome_id="release", project="Demo", tz="TZ-X")
    outcome.add_evidence(
        store, writer=outcome.server_evidence_writer(store), outcome_id="release",
        kind="run", ref="opref:" + "e" * 64, subject_version=SUBJECT,
        env_version=AFTER, verdict="pass", producer="operator",
    )
    outcome.close_outcome(store, "release")


def test_production_route_rejects_foreign_db_and_requires_actual_dashboard(tmp_path):
    profile = tmp_path / "profile"
    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    assert controller._production_handoff_route(db, repository_root=tmp_path) is None
    expected_url = _installed_dashboard(tmp_path, profile)
    probe = [False]
    route = controller._production_handoff_route(
        db, repository_root=tmp_path, profile_root=profile,
        chrome_probe=lambda: probe[0],
    )
    assert route is not None
    assert controller._production_handoff_route(
        tmp_path / "arbitrary.sqlite", repository_root=tmp_path, profile_root=profile,
    ) is None
    runtime = route("Demo", "TZ-X")
    assert runtime.overview_url == expected_url
    assert runtime.notifier is None  # No fabricated registered AUMID/toast.
    assert not runtime.overview_available(expected_url)
    assert not runtime.overview_available("http://localhost:8766/owner/overview")
    probe[0] = True
    assert runtime.overview_available(expected_url)
    # Unpacked and correctly located is insufficient: state=0 means disabled
    # even when disable_reasons is absent. Missing/malformed also fail closed.
    settings = profile / "Secure Preferences"
    data = json.loads(settings.read_text(encoding="utf-8"))
    ext_id = expected_url.split("/")[2]
    entry = data["extensions"]["settings"][ext_id]
    for invalid_state in (0, -1, 2, "1", True, None):
        if invalid_state is None:
            entry.pop("state", None)
        else:
            entry["state"] = invalid_state
        settings.write_text(json.dumps(data), encoding="utf-8")
        assert not runtime.overview_available(expected_url), invalid_state
    entry["state"] = 1
    settings.write_text(json.dumps(data), encoding="utf-8")
    assert runtime.overview_available(expected_url)
    # An installed path replaced by an unrelated extension is not a valid route.
    data["extensions"]["settings"][ext_id]["path"] = str(tmp_path / "other")
    settings.write_text(json.dumps(data), encoding="utf-8")
    assert not runtime.overview_available(expected_url)


def test_real_entrypoint_accept_handoff_owner_dashboard_and_offline_retry(
    tmp_path, monkeypatch,
):
    profile = tmp_path / "chrome-profile"
    expected_url = _installed_dashboard(tmp_path, profile)
    project_root = tmp_path / "product"
    project_root.mkdir()
    _delivery_config(project_root)
    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    store = Store.open(str(db))
    _accepted_result(store, project_root)
    desktop = [False]
    chrome_running = [False]
    notifications: list[str] = []
    ops: list[str] = []
    environment = [BEFORE]

    def operator(name, _params):
        ops.append(name)
        if name == "cloud.deploy":
            environment[0] = AFTER
        return {"ok": True, "evidence": {
            "kind": "delivery" if name == "cloud.deploy" else "run",
            "ref": "opref:" + "e" * 64, "subject_version": SUBJECT,
            "env_version": environment[0], "verdict": "pass",
            "producer": "operator", "operation": name,
            "request_fingerprint": "f" * 64, "observed_ts": 1,
            "metrics": {"steps": 1},
        }}

    delivery = controller.DeliveryRuntime(
        allowed_roots=(project_root,), state_db=tmp_path / "delivery.sqlite",
        subject_version=SUBJECT, env_version=BEFORE, fleet_op=operator,
        probe_environment=lambda _root, _target: environment[0],
    )
    # Only delivery authority is supplied by the trusted launcher. The actual
    # run_controller entrypoint MUST attach the server-owned handoff route.
    trusted = controller.ControllerProvisioning(
        delivery_route=lambda _project, _tz: delivery,
    )
    original = controller._production_handoff_route

    def production_route(path):
        underlying = original(
            path, repository_root=tmp_path, profile_root=profile,
            chrome_probe=lambda: chrome_running[0],
        )
        assert underlying is not None

        def route(project, tz):
            return replace(
                underlying(project, tz),
                # A test double only for the already-registered native notifier.
                notifier=lambda url: notifications.append(url) or True,
                session_ready=lambda: desktop[0],
            )
        return route

    monkeypatch.setattr(controller, "_production_handoff_route", production_route)
    monkeypatch.setattr(controller, "run_daily_drill_scheduler", lambda _stop: None)
    monkeypatch.setattr(controller, "run_cross_review_worker",
                        lambda _store, _stop: None)

    # Leave the dashboard unavailable: route must persist proof, never toast.
    stop = threading.Event()
    original_tick = controller.Controller.tick

    def once(self):
        result = original_tick(self)
        stop.set()
        return result

    monkeypatch.setattr(controller.Controller, "tick", once)
    controller.run_controller(db, stop, interval_s=0.001, provisioning=trusted)
    with store.read() as tx:
        accepted = tx.one("SELECT receipt FROM controller_delivery_receipts")
        published = tx.one("SELECT notice_state,attempts FROM controller_handoffs")
        startup = json.loads(tx.one(
            "SELECT payload FROM events WHERE kind='controller_provisioning' ORDER BY id DESC LIMIT 1"
        )["payload"])
    assert json.loads(accepted["receipt"])["status"] == "delivered"
    assert startup["routes"]["handoff"] == "provisioned"
    assert published["notice_state"] == "pending" and published["attempts"] == 0
    assert notifications == []
    handoff = HandoffService(db.with_name("handoff.sqlite"))
    receipt = handoff.get(json.loads(accepted["receipt"])["event_id"])
    assert receipt.packet["overview_url"] == expected_url
    assert receipt.packet["result"]["version"] == SUBJECT
    item = snapshot(store)["intents"][0]["results"][0]
    assert item["state"] == "done" and item["stage"]["delivered"] is True
    assert ops == ["cloud.deploy", "ci.run"]

    # Chrome alive is insufficient when the Windows desktop/toasts are off.
    chrome_running[0] = True
    stop.clear()
    controller.run_controller(db, stop, interval_s=0.001, provisioning=trusted)
    assert handoff.get(receipt.packet["event_id"]).attempts == 0
    assert notifications == []

    # Owner logs back in and allows GUI; only the pending record may retry.
    desktop[0] = True
    stop.clear()
    controller.run_controller(db, stop, interval_s=0.001, provisioning=trusted)
    assert handoff.get(receipt.packet["event_id"]).notice_state == "submitted"
    stop.clear()
    controller.run_controller(db, stop, interval_s=0.001, provisioning=trusted)
    assert notifications == [expected_url]
    assert ops == ["cloud.deploy", "ci.run"], "no duplicate Operator apply"
