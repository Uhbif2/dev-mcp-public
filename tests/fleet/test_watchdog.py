"""Synthetic replays of fleet stalls; no browser, guardian or external process."""
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from dev_mcp.fleet.store import Store
from dev_mcp.fleet.watchdog import Signals, detect, watchdog_tick, local_signals, run_service
from dev_mcp.fleet.interventions import InterventionLedger
from dev_mcp.fleet import watchdog


NOW = datetime(2026, 10, 11, 9, tzinfo=timezone.utc)


def setup(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "work"
    root.mkdir()
    with store.tx() as tx:
        for name in ("Fleet Platform", "Example"):
            tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                      (name, str(root),), event={"kind": "seed"})
    return store


def task(store, tz, *, project="Example", state="ready", needs=(), age=40):
    created = (NOW - timedelta(minutes=age)).isoformat()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, tz, tz, state, 1, "work", "", "", json.dumps(needs),
             "[]", "[]", "[]", None, "", created, created), event="seed")


def event(store, kind, *, project="Example", tz=None, minutes=40, rc=None, payload=None):
    with store.tx() as tx:
        tx.record_event(kind=kind, project=project, tz=tz,
                        ts=(NOW - timedelta(minutes=minutes)).isoformat(),
                        rc=rc, payload=payload or {})


def found(store, signals=Signals()):
    return {finding.kind: finding for finding in detect(store, now=NOW, signals=signals)}


def count(store, kind):
    with store.read() as tx:
        return int(tx.scalar("SELECT COUNT(*) FROM events WHERE kind=?", (kind,)))


def state(store, tz, project="Example"):
    with store.read() as tx:
        return tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))["state"]


def tickets(store):
    with store.read() as tx:
        return list(tx.query("SELECT tz,body FROM tasks WHERE author_worker='watchdog'"))


def test_inactive_or_removed_projects_do_not_keep_historical_red_gate_alarm(tmp_path):
    store = setup(tmp_path)
    event(store,"gate",project="Fleet Platform",rc=1)
    event(store,"gate",project="Removed project",rc=1)
    with store.tx() as tx:
        tx.change("UPDATE projects SET active=0 WHERE name='Fleet Platform'",event="seed")
    assert "red_trunk" not in found(store)
    assert "no_agents_red" not in found(store)
    event(store,"gate",project="Example",rc=1)
    assert found(store)["red_trunk"].project == "Example"


def test_retired_automatic_guard_is_not_a_live_sensor_in_manual_mode(tmp_path,monkeypatch):
    store = setup(tmp_path)
    monkeypatch.setattr("dev_mcp.fleet.platform_manual.enabled",lambda:True)
    monkeypatch.setattr(watchdog,"_recent_guard_events",lambda *a:pytest.fail("retired guard must not be read"))
    signals = local_signals(store,now=NOW)
    assert signals.guard_heartbeat is None
    assert signals.guard_rejections == ()


def test_healthy_flow_no_false_alarm(tmp_path):
    store = setup(tmp_path)
    task(store, "TZ-1")
    event(store, "gate", rc=0, minutes=2)
    event(store, "take", minutes=1)
    event(store, "controller_tick", minutes=1)
    sig = Signals(runnable=2, extension_runnable=2, extension_enabled=True,
                  extension_last_tick=NOW - timedelta(seconds=25),
                  chatgpt_up=True, guard_heartbeat=NOW - timedelta(minutes=1))
    assert watchdog_tick(store, now=NOW, signals=sig) == []
    assert not tickets(store)
    assert count(store, "watchdog_open") == 0
    assert count(store, "watchdog_daemon_tick") == 0


def test_cursor_migrates_legacy_history_without_appending_full_state_each_tick(tmp_path, monkeypatch):
    store = setup(tmp_path)
    history = {f"old-{i}": [{"id": i + 1, "ts": NOW.isoformat(),
                            "kind": "watchdog_recovered"}] for i in range(1000)}
    event(store, "watchdog_cursor", payload={"through": 0, "history": history})
    monkeypatch.setattr(watchdog, "detect", lambda *a, **k: [])
    for i in range(5):
        watchdog_tick(store, now=NOW + timedelta(seconds=i))
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM watchdog_state") == 1
        row = tx.one("SELECT history FROM watchdog_state WHERE id=1")
        assert json.loads(row["history"]) == history
        markers = tx.query("SELECT payload FROM events WHERE kind='watchdog_cursor' ORDER BY id")
        assert len(markers) == 6
        assert all(len(r["payload"]) < 128 for r in markers[1:])
    reopened = Store.open(str(store.path))
    restored, _, _, _ = watchdog._watchdog_history(reopened)
    assert restored == history


def test_metric_comparison_rejects_skew_missing_or_stale_browser_tick(tmp_path):
    store = setup(tmp_path)
    status_path = tmp_path / "launcher-health.json"

    def observe(tick):
        status_path.write_text(json.dumps({
            "supervisor": {"checked_at": NOW.isoformat(), "demand": {"runnable": 3}},
            "extension": {"enabled": True, "runnable": 1, **tick},
        }), encoding="utf-8")
        return replace(local_signals(store, now=NOW), boot_at=None)

    for tick in ({}, {"tick_at": (NOW - timedelta(seconds=20)).isoformat()},
                 {"tick_at": (NOW - timedelta(minutes=6)).isoformat()},
                 {"tick_at": (NOW + timedelta(seconds=2)).isoformat()}):
        signals = observe(tick)
        assert signals.runnable == 3 and signals.extension_runnable == 1
        assert signals.metrics_comparable is False
        assert signals.metric_sample_id == ""
        assert "metrics_mismatch" not in found(store, signals)
        assert "metrics_mismatch" not in {
            item.kind for item in watchdog_tick(store, now=NOW, signals=signals)
        }
    assert not tickets(store)


def test_metrics_require_two_distinct_comparable_ticks_and_stable_fingerprint(tmp_path):
    store = setup(tmp_path)
    status_path = tmp_path / "launcher-health.json"

    def observe(when, supervisor_count, browser_count, *, skew=2):
        status_path.write_text(json.dumps({
            "supervisor": {
                "checked_at": (when - timedelta(seconds=skew)).isoformat(),
                "demand": {"runnable": supervisor_count},
            },
            "extension": {
                "enabled": True, "runnable": browser_count,
                "tick_at": when.isoformat(),
            },
        }), encoding="utf-8")
        return replace(local_signals(store, now=when), boot_at=None)

    first = observe(NOW, 4, 2)
    assert first.metrics_comparable is True and first.metric_sample_id
    assert "supervisor_at=" in found(store, first)["metrics_mismatch"].detail
    assert "metrics_mismatch" not in {
        item.kind for item in watchdog_tick(store, now=NOW, signals=first)
    }
    assert not tickets(store)
    # Rereading the same browser tick must neither confirm nor create a ticket.
    watchdog_tick(store, now=NOW + timedelta(seconds=1), signals=first)
    assert not tickets(store)

    later = NOW + timedelta(seconds=30)
    second = observe(later, 5, 1)
    first_fingerprint = found(store, first)["metrics_mismatch"].fingerprint
    second_fingerprint = found(store, second)["metrics_mismatch"].fingerprint
    assert first_fingerprint == second_fingerprint
    detected = watchdog_tick(store, now=later, signals=second)
    assert "metrics_mismatch" in {item.kind for item in detected}
    assert len(tickets(store)) == 1
    assert count(store, "watchdog_open") == 1
    watchdog_tick(store, now=later + timedelta(seconds=1), signals=second)
    assert count(store, "watchdog_recovered") == 0
    assert len(tickets(store)) == 1

    good = NOW + timedelta(seconds=60)
    matching = observe(good, 2, 2)
    watchdog_tick(store, now=good, signals=matching)
    assert count(store, "watchdog_recovered") == 1
    assert len(tickets(store)) == 1

    # After a successful comparison, a new first mismatch must not reopen.
    after = NOW + timedelta(seconds=90)
    new_first = observe(after, 3, 0)
    assert "metrics_mismatch" not in {
        item.kind for item in watchdog_tick(store, now=after, signals=new_first)
    }
    assert len(tickets(store)) == 1


def test_two_browser_ticks_with_one_supervisor_snapshot_do_not_confirm(tmp_path):
    store = setup(tmp_path)
    status_path = tmp_path / "launcher-health.json"
    supervisor_at = NOW - timedelta(seconds=2)

    def observe(browser_at, supervisor_count):
        status_path.write_text(json.dumps({
            "supervisor": {
                "checked_at": supervisor_at.isoformat(),
                "demand": {"runnable": supervisor_count},
            },
            "extension": {
                "enabled": True, "runnable": 0, "tick_at": browser_at.isoformat(),
            },
        }), encoding="utf-8")
        return replace(local_signals(store, now=browser_at), boot_at=None)

    first = observe(NOW, 2)
    second_at = NOW + timedelta(seconds=5)
    second = observe(second_at, 2)
    assert first.metrics_comparable is True and second.metrics_comparable is True
    watchdog_tick(store, now=NOW, signals=first)
    findings = watchdog_tick(store, now=second_at, signals=second)
    assert "metrics_mismatch" not in {item.kind for item in findings}
    assert not tickets(store)


def test_metric_counters_with_different_global_and_per_project_semantics_are_unknown(tmp_path):
    from dev_mcp.fleet import queue
    store = setup(tmp_path)
    (tmp_path / "launcher-health.json").write_text(json.dumps({
        "supervisor": {"checked_at": NOW.isoformat(), "demand": {"runnable": 1}},
        "extension": {"enabled": True, "runnable": 0, "tick_at": NOW.isoformat()},
    }), encoding="utf-8")
    # The extension sums project-local candidate counts, which can differ
    # from the global reservation-aware peek_next even at the same instant.
    with patch.object(queue, "peek_next", return_value=(None, 1)), patch.object(
        queue, "peek_projects", return_value=[("A", None, 1), ("B", None, 1)]
    ):
        signals = replace(local_signals(store, now=NOW), boot_at=None)
    assert signals.metrics_comparable is False
    assert "metrics_mismatch" not in found(store, signals)
    assert not tickets(store)


def test_optional_intervention_sqlite_error_does_not_disable_watchdog(tmp_path):
    """An unavailable TZ-48 ledger cannot suppress other critical watchdog facts."""
    store = setup(tmp_path)
    status = {
        "supervisor": {"checked_at": NOW.isoformat(), "demand": {"runnable": 2}},
        "extension": {"enabled": False, "runnable": 0},
    }
    (tmp_path / "launcher-health.json").write_text(json.dumps(status), encoding="utf-8")
    with patch.object(watchdog, "_intervention_metrics",
                      side_effect=sqlite3.OperationalError("database is locked")):
        signals = local_signals(store, now=NOW)
    assert signals.intervention_metrics is None
    assert signals.runnable == 2
    assert "extension_off" in found(store, signals)


def test_verified_intervention_rise_owner_signal_is_per_episode(tmp_path):
    """Actual TZ-48 metrics feed a deduplicated owner signal, never guessed counts."""
    store = setup(tmp_path)
    assert local_signals(store, now=NOW).intervention_metrics is None
    ledger = InterventionLedger(store, "Fleet Platform", tmp_path)

    def measured():
        # The synthetic replay clock is independent of the host's actual boot
        # time; avoid counting unrelated boot_no_take owner notifications.
        return replace(local_signals(store, now=NOW), boot_at=None)

    def insert(identifier, when):
        with store.tx() as tx:
            tx.change(
                "INSERT INTO intervention_log VALUES (?,?,?,?,?,?,?,?)",
                ("owner-action", identifier, "Fleet Platform", "manual-owner-action",
                 "operator", "reactivate", when.isoformat(), "synthetic:" + identifier),
                event="test.intervention",
            )

    yesterday = NOW - timedelta(days=1)
    insert("yesterday", yesterday)
    signals = measured()
    assert signals.intervention_metrics == ledger.metrics(now=NOW)
    assert signals.intervention_metrics["rising"] is False
    assert "interventions_rising" not in found(store, signals)

    insert("today-1", NOW)
    insert("today-2", NOW)
    signals = measured()
    issue = found(store, signals)["interventions_rising"]
    assert issue.repair == "owner"
    assert "сегодня 2" in issue.detail and "вчера 1" in issue.detail
    watchdog_tick(store, now=NOW, signals=signals)
    assert count(store, "watchdog_owner_signal") == 1

    insert("today-3", NOW)
    newer = measured()
    assert newer.intervention_metrics["today"] == 3
    assert found(store, newer)["interventions_rising"].fingerprint == issue.fingerprint
    watchdog_tick(store, now=NOW, signals=newer)
    assert count(store, "watchdog_owner_signal") == 1

    with store.tx() as tx:
        tx.change("UPDATE intervention_log SET ts=? WHERE identifier LIKE 'today-%'",
                  (yesterday.isoformat(),), event="test.intervention")
    declining = measured()
    assert declining.intervention_metrics["rising"] is False
    watchdog_tick(store, now=NOW, signals=declining)
    assert count(store, "watchdog_recovered") == 1

    for i in range(5):
        insert("second-rise-" + str(i), NOW)
    watchdog_tick(store, now=NOW, signals=measured())
    assert count(store, "watchdog_owner_signal") == 2


@pytest.mark.parametrize("age,expected", [(9, False), (10, True)])
def test_runnable_without_take_boundary_and_repair_class(tmp_path, age, expected):
    store = setup(tmp_path)
    task(store, "TZ-1", age=age)
    issues = found(store, Signals(runnable=3, stalled_reason="сеть недоступна"))
    assert ("no_take" in issues) == expected
    if expected:
        assert issues["no_take"].repair == "restore_agents"
        watchdog_tick(store, now=NOW, signals=Signals(runnable=3))
        assert any("restore_agents" in row["body"] for row in tickets(store))


def test_red_trunk_after_30_without_styk_and_agents_first(tmp_path):
    store = setup(tmp_path)
    event(store, "gate", minutes=30, rc=1)
    issues = found(store)
    assert issues["red_trunk"].repair == "reopen_styk"
    assert issues["no_agents_red"].repair == "restore_agents"
    watchdog_tick(store, now=NOW)
    assert len(tickets(store)) == 2
    task(store, "styk:4", state="taken")
    assert "red_trunk" not in found(store)


def test_green_trunk_old_accepted_and_same_guard_code(tmp_path):
    store = setup(tmp_path)
    task(store, "TZ-1", state="accepted")
    event(store, "gate", rc=0, minutes=1)
    event(store, "accept", tz="TZ-1", minutes=31)
    sig = Signals(guard_rejections=("GUARD_LOCK", "GUARD_LOCK"))
    assert found(store, sig)["guard_denial"].repair == "guard_diagnosis"
    watchdog_tick(store, now=NOW, signals=sig)
    assert any("GUARD_LOCK" in t["body"] for t in tickets(store))
    # A verified integrate commit removes the predicate, even if the guard
    # continues to log the same denial for some other job.
    with store.read() as tx:
        accept_id = tx.one("SELECT id FROM events WHERE kind='accept'")["id"]
    event(store, "integrate", payload={"accept_id": accept_id, "commit": "a" * 40})
    assert "guard_denial" not in found(store, sig)


def test_guard_denial_is_one_project_incident_not_per_old_accept_or_log_tick(tmp_path):
    store = setup(tmp_path)
    event(store, "gate", project="Example", rc=0, minutes=1)
    for i in range(16):
        name = f"TZ-{i}"
        task(store, name, state="accepted")
        event(store, "accept", tz=name, minutes=40)
    first = Signals(guard_rejections=("unreviewed_paths",) * 2,
                    guard_events=("2026-10-11T09:00:00Z unreviewed_paths",))
    issues = [item for item in detect(store, now=NOW, signals=first)
              if item.kind == "guard_denial"]
    assert len(issues) == 1
    assert issues[0].subject == "unknown_accept"
    assert "unintegrated_accepted=16" in issues[0].detail
    assert len(issues[0].detail) <= 600
    watchdog_tick(store, now=NOW, signals=first)
    assert len(tickets(store)) == 1

    second = replace(first, guard_events=("2026-10-11T09:01:00Z unreviewed_paths",))
    issue2 = found(store, second)["guard_denial"]
    assert issues[0].detail != issue2.detail
    assert issues[0].fingerprint == issue2.fingerprint
    watchdog_tick(store, now=NOW + timedelta(minutes=1), signals=second)
    assert len(tickets(store)) == 1
    assert count(store, "watchdog_open") == 1

    # A superseded/historic accept is not a present unintegrated workload.
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE project='Example'",
                  event="test.supersede")
    assert "guard_denial" not in found(store, second)


def test_global_guard_denial_has_one_unknown_accept_incident_across_projects(tmp_path):
    store = setup(tmp_path)
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Another", str(tmp_path),), event="seed")
    for project in ("Example", "Another"):
        event(store, "gate", project=project, rc=0, minutes=1)
        for i in range(50):
            tz = f"TZ-{i}"
            task(store, tz, project=project, state="accepted")
            event(store, "accept", project=project, tz=tz, minutes=40)

    signal = Signals(guard_rejections=("unreviewed_paths",) * 2,
                     guard_events=("15:14:11 unreviewed_paths",))
    findings = [f for f in detect(store, now=NOW, signals=signal)
                if f.kind == "guard_denial"]
    assert len(findings) == 1
    item = findings[0]
    assert item.project == "Fleet Platform"  # global deny, no project attribution
    assert item.subject == "unknown_accept"
    assert "correlation=unknown_accept" in item.detail
    assert "unintegrated_accepted=100" in item.detail
    assert "Another" in item.detail and "Example" in item.detail
    watchdog_tick(store, now=NOW, signals=signal)
    assert len(tickets(store)) == 1

    later = replace(signal, guard_events=("15:16:11 unreviewed_paths",))
    assert [f for f in detect(store, now=NOW, signals=later)
            if f.kind == "guard_denial"][0].fingerprint == item.fingerprint
    watchdog_tick(store, now=NOW + timedelta(minutes=1), signals=later)
    assert len(tickets(store)) == 1
    assert count(store, "watchdog_open") == 1

    other = replace(signal, guard_rejections=("protected_path",) * 2)
    other_item = [f for f in detect(store, now=NOW, signals=other)
                  if f.kind == "guard_denial"][0]
    assert other_item.fingerprint != item.fingerprint


def test_heartbeat_stale_and_not_missing_without_a_baseline(tmp_path):
    store = setup(tmp_path)
    assert "controller_stale" not in found(store)
    event(store, "controller_tick", minutes=6)
    sig = Signals(guard_heartbeat=NOW - timedelta(minutes=6))
    issues = found(store, sig)
    assert issues["guard_stale"].repair == "restart_trusted"
    assert issues["controller_stale"].repair == "restart_trusted"
    assert count(store, "watchdog_open") == 0  # detect is read-only


def test_wait_cycle_thaws_only_one_publisher_with_evidence(tmp_path):
    store = setup(tmp_path)
    task(store, "TZ-A", state="frozen", needs=["TZ-B"])
    task(store, "TZ-B", state="frozen", needs=["TZ-A"])
    event(store, "block", tz="TZ-A", payload={"reason": "жду TZ-B"})
    event(store, "block", tz="TZ-B", payload={"reason": "жду TZ-A"})
    issues = found(store)
    assert issues["wait_cycle"].subject == "TZ-A"
    assert "контракт первым" in issues["wait_cycle"].detail
    watchdog_tick(store, now=NOW)
    assert state(store, "TZ-A") == "ready"
    assert state(store, "TZ-B") == "frozen"
    with store.read() as tx:
        thaw = tx.one("SELECT payload FROM events WHERE kind='thaw'")
    assert json.loads(thaw["payload"])["predicate"] == "wait_cycle"


def test_frozen_verified_predicate_thaws_and_documents_policy_conflict(tmp_path):
    store = setup(tmp_path)
    task(store, "TZ-1", state="frozen")
    task(store, "TZ-2", state="frozen")
    event(store, "block", tz="TZ-1", payload={"reason": "путь закрыт"})
    event(store, "block", tz="TZ-2", payload={"reason": "документ утверждает denied"})
    sig = Signals(
        satisfied_blockers={("Example", "TZ-1"): "policy_info: путь разрешён"},
        policy_conflicts={("Example", "TZ-2"): "policy_info: путь уже разрешён"})
    issues = found(store, sig)
    assert issues["satisfied_frozen"].repair == "thaw"
    assert issues["policy_conflict"].repair == "thaw_and_document"
    watchdog_tick(store, now=NOW, signals=sig)
    assert state(store, "TZ-1") == state(store, "TZ-2") == "ready"
    assert len(tickets(store)) == 1
    assert count(store, "thaw") == 2


def test_manual_freeze_and_unverified_deployment_never_thaw(tmp_path):
    store = setup(tmp_path)
    task(store, "TZ-1", state="frozen")
    task(store, "TZ-2", state="frozen")
    event(store, "freeze", tz="TZ-1", payload={"reason": "owner froze"})
    event(store, "block", tz="TZ-2", payload={"reason": "until: platform_deploy after=2026-10-11"})
    sig = Signals(satisfied_blockers={("Example", "TZ-1"): "yes", ("Example", "TZ-2"): "yes"})
    assert not any(f.repair.startswith("thaw") for f in detect(store, now=NOW, signals=sig))
    watchdog_tick(store, now=NOW, signals=sig)
    assert state(store, "TZ-1") == state(store, "TZ-2") == "frozen"


def test_extension_network_metrics_and_boot_signals(tmp_path):
    store = setup(tmp_path)
    sig = Signals(runnable=4, extension_runnable=0, extension_enabled=False,
                  extension_last_tick=NOW - timedelta(minutes=6), chatgpt_up=False,
                  boot_at=NOW - timedelta(minutes=11))
    issues = found(store, sig)
    for kind, repair in (
        ("extension_off", "owner"), ("extension_stale", "owner"),
        ("chatgpt_down", "owner"), ("boot_no_take", "restore_agents"),
        ("metrics_mismatch", "observability"),
    ):
        assert issues[kind].repair == repair
    watchdog_tick(store, now=NOW, signals=sig)
    assert len(tickets(store)) == 1  # observability work, not fake owner jobs
    assert count(store, "watchdog_owner_signal") == 4  # immediate durable owner outbox


def test_idempotence_retry_twice_owner_outbox_once_and_rearm(tmp_path):
    store = setup(tmp_path)
    event(store, "gate", minutes=32, rc=1)
    watchdog_tick(store, now=NOW)
    watchdog_tick(store, now=NOW)
    assert len(tickets(store)) == 2
    assert count(store, "watchdog_attempt") == 2
    watchdog_tick(store, now=NOW + timedelta(minutes=11))
    assert count(store, "watchdog_attempt") == 3  # reopened styk ends red_trunk predicate
    watchdog_tick(store, now=NOW + timedelta(minutes=22))
    watchdog_tick(store, now=NOW + timedelta(minutes=33))
    assert count(store, "watchdog_owner_signal") == 1  # only agents still absent
    assert len(tickets(store)) == 2
    event(store, "gate", rc=0, minutes=-34)
    from dev_mcp.fleet.queue import close_resolved_styks
    close_resolved_styks(store)
    watchdog_tick(store, now=NOW + timedelta(minutes=35))
    assert count(store, "watchdog_recovered") == 2
    event(store, "gate", rc=1, minutes=-36)
    watchdog_tick(store, now=NOW + timedelta(minutes=67))
    assert len(tickets(store)) == 4  # new styk and no-agents episode


def test_probes_must_pass_not_merely_exist(tmp_path):
    store = setup(tmp_path)
    task(store, "TZ-1", state="frozen")
    event(store, "block", tz="TZ-1", payload={"reason": "until: py -3 tools/ping.py"})
    event(store, "block_probe", tz="TZ-1", minutes=39,
          payload={"argv": ["py", "-3", "tools/ping.py"]})
    event(store, "probe_run", tz="TZ-1", minutes=1, payload={"rc": 1})
    assert "satisfied_frozen" not in found(store)
    event(store, "probe_run", tz="TZ-1", minutes=0, payload={"rc": 0})
    assert found(store)["satisfied_frozen"].repair == "thaw"


def test_local_signals_does_not_confuse_extension_lease_with_real_tick(tmp_path):
    store = setup(tmp_path)
    status = {
        "supervisor": {"checked_at": NOW.isoformat(), "demand": {"runnable": 4}},
        "extension": {"seen_at": NOW.isoformat(), "runnable": 3, "enabled": False},
    }
    (tmp_path / "launcher-health.json").write_text(json.dumps(status), encoding="utf-8")
    signals = local_signals(store, now=NOW)
    assert signals.runnable == 4 and signals.extension_runnable == 3
    assert signals.extension_enabled is False
    assert signals.extension_last_tick is None  # lease does not prove completed tick
    old = local_signals(store, now=NOW + timedelta(minutes=6))
    assert old.runnable is None  # stale supervisor snapshot is not a fresh fact


def test_run_service_lifecycle_and_cross_process_lease(tmp_path):
    import threading
    from dev_mcp.fleet.watchdog import _lease

    store = setup(tmp_path)
    stop = threading.Event()
    checked = []

    def sample(current, *, now):
        assert current is store
        checked.append(now)
        stop.set()
        return Signals()

    assert run_service(store, stop, interval_s=0.01, read_signals=sample) == "stopped"
    assert len(checked) == 1
    assert count(store, "watchdog_daemon_tick") == 1
    with store.read() as tx:
        tick = tx.one("SELECT project,payload FROM events WHERE kind='watchdog_daemon_tick'")
    assert tick["project"] == "Fleet Platform"
    assert json.loads(tick["payload"])["source"] == "run_service"
    lock = tmp_path / "watchdog.lock"
    with _lease(lock) as first:
        assert first
        with _lease(lock) as second:
            assert not second  # a duplicate launcher never owns the lease


def test_daemon_does_not_report_fresh_tick_when_sensor_failed(tmp_path):
    import threading

    store = setup(tmp_path)
    stop = threading.Event()

    def broken(current, *, now):
        stop.set()
        raise RuntimeError("broken sensor")

    assert run_service(store, stop, interval_s=0.01, read_signals=broken) == "stopped"
    assert count(store, "watchdog_daemon_tick") == 0
    assert count(store, "watchdog_tick_error") == 1


def test_ambiguous_obstacle_is_only_gemini_hypothesis_not_automatic_thaw(tmp_path):
    store = setup(tmp_path)
    task(store, "TZ-1", state="frozen")
    event(store, "block", tz="TZ-1", payload={"reason": "unknown"})
    sig = Signals(ambiguities={"question-1": "страж или сеть?"})
    assert found(store, sig)["ambiguous"].repair == "gemini_hypothesis"
    watchdog_tick(store, now=NOW, signals=sig)
    assert state(store, "TZ-1") == "frozen"
    assert count(store, "watchdog_hypothesis_needed") == 1
    assert count(store, "thaw") == 0


def test_chatgpt_network_sensor_transition_flows_into_controller_watchdog(tmp_path):
    """The TZ-45 world event, not a new speculative network probe, drives TZ-47."""
    from dev_mcp.fleet import world
    from dev_mcp.fleet.controller import _controller_flow_watchdog_step

    store = setup(tmp_path)
    status = {"supervisor": {"checked_at": NOW.isoformat(),
                             "demand": {"runnable": 2}}}
    (tmp_path / "launcher-health.json").write_text(json.dumps(status), encoding="utf-8")
    assert local_signals(store, now=NOW).chatgpt_up is None
    with store.tx() as tx:
        tx.record_event(kind=world.EVENT_KIND, path_norm=world.path_key("chatgpt-reachable"),
                        ts=NOW.isoformat(), payload={"state": world.DOWN,
                                                     "detail": "proxy_not_listening"})
    observation = local_signals(store, now=NOW)
    assert observation.chatgpt_up is False
    assert observation.stalled_reason.startswith("ChatGPT недоступен")
    issues = _controller_flow_watchdog_step(store, now=NOW)
    assert "chatgpt_down" in {item.kind for item in issues}
    def net_alerts():
        with store.read() as tx:
            return [row for row in tx.query(
                "SELECT payload FROM events WHERE kind='watchdog_owner_signal'")
                if json.loads(row["payload"]).get("predicate") == "chatgpt_down"]

    assert len(net_alerts()) == 1
    # Re-running the controller must not duplicate owner pushes.
    _controller_flow_watchdog_step(store, now=NOW)
    assert len(net_alerts()) == 1
    with store.tx() as tx:
        tx.record_event(kind=world.EVENT_KIND, path_norm=world.path_key("chatgpt-reachable"),
                        ts=(NOW + timedelta(minutes=1)).isoformat(),
                        payload={"state": world.UP, "detail": "recovered"})
    assert local_signals(store, now=NOW + timedelta(minutes=1)).chatgpt_up is True


def test_controller_consumes_real_guard_log_and_extension_report_schema(tmp_path):
    """Bite: actual producers -> persisted files -> controller, not injected Signals."""
    from dev_mcp.fleet import supervisor
    from dev_mcp.fleet.controller import _controller_flow_watchdog_step

    host = tmp_path / "host"
    registry = host / "data" / "fleet"
    registry.mkdir(parents=True)
    store = setup(registry)
    task(store, "TZ-1", state="accepted")
    event(store, "gate", rc=0, minutes=1)
    event(store, "accept", tz="TZ-1", minutes=35)
    status_path = registry / "launcher-health.json"
    status_path.write_text(json.dumps({
        "supervisor": {"checked_at": NOW.isoformat(), "demand": {"runnable": 3}}
    }), encoding="utf-8")

    guard_log = host / "logs" / "platform-guard.jsonl"
    guard_log.parent.mkdir()
    guard_log.write_text("".join(json.dumps({
        "ts": (NOW - timedelta(minutes=minutes)).isoformat(),
        "code": code, "candidate": "abc",
    }) + "\n" for minutes, code in [(3, "protected_path"),
                                      (2, "start"), (1, "protected_path")]),
        encoding="utf-8")

    old_tick = (NOW - timedelta(minutes=6)).isoformat()
    with patch.object(supervisor, "_utcnow", return_value=old_tick):
        supervisor.record_extension_heartbeat(store, enabled=True, tick_completed=True,
                                              version="0.20", tabs=2, pending=0, runnable=2)
    with patch.object(supervisor, "_utcnow", return_value=NOW.isoformat()):
        supervisor.record_extension_heartbeat(store, enabled=True, tick_completed=False,
                                              version="0.20", tabs=2, pending=0, runnable=2)
    persisted = json.loads(status_path.read_text(encoding="utf-8"))["extension"]
    assert persisted["enabled"] is True
    assert persisted["tick_at"] == old_tick
    assert persisted["seen_at"] == NOW.isoformat()

    observed = local_signals(store, now=NOW)
    assert observed.guard_rejections[-2:] == ("protected_path", "protected_path")
    assert any("protected_path" in item for item in observed.guard_events)
    assert observed.extension_enabled is True
    assert observed.extension_runnable == 2
    assert observed.extension_last_tick == NOW - timedelta(minutes=6)
    findings = _controller_flow_watchdog_step(store, now=NOW)
    kinds = {finding.kind for finding in findings}
    assert {"guard_denial", "extension_stale"} <= kinds
    assert "metrics_mismatch" not in kinds  # stale browser tick is not comparable
    assert count(store, "watchdog_owner_signal") >= 1
    assert any("protected_path" in ticket["body"] for ticket in tickets(store))

    with patch.object(supervisor, "_utcnow", return_value=NOW.isoformat()):
        supervisor.record_extension_heartbeat(store, enabled=False, version="0.20",
                                              tabs=2, pending=0)
    assert "extension_off" in {
        finding.kind for finding in _controller_flow_watchdog_step(store, now=NOW)
    }


def test_controller_rechecks_typed_policy_info_conflict_against_real_policy(tmp_path):
    """Policy conflict is a verified local allow, not an agent's prose assertion."""
    from dev_mcp.fleet.controller import _controller_flow_watchdog_step

    host = tmp_path / "host"
    registry = host / "data" / "fleet"
    registry.mkdir(parents=True)
    store = setup(registry)
    allowed = registry / "work" / "owned.md"
    denied = registry / "work" / "other.md"
    (host / "policy.toml").write_text(
        f"roots = ['{host.as_posix()}']\n"
        f"write_roots = ['{(registry / 'work').as_posix()}']\n"
        "allow_write = false\n", encoding="utf-8")

    def claim(path):
        # Exact free-form reason supported by today's real fleet_block tool.
        return {"reason": "документ объявляет путь закрытым\n"
                f"policy_info denied write: {path}"}

    task(store, "TZ-1", state="frozen")
    task(store, "TZ-2", state="frozen")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET owns=? WHERE project=? AND tz=?",
                  (json.dumps([str(allowed)]), "Example", "TZ-1"), event="seed.owns")
        tx.change("UPDATE tasks SET owns=? WHERE project=? AND tz=?",
                  (json.dumps([str(allowed)]), "Example", "TZ-2"), event="seed.owns")
    event(store, "block", tz="TZ-1", payload=claim(allowed))
    event(store, "block", tz="TZ-2", payload=claim(denied))
    signals = local_signals(store, now=NOW)
    assert signals.policy_conflicts == {
        ("Example", "TZ-1"): f"policy_info: confirmed writable own path {allowed}"
    }
    findings = _controller_flow_watchdog_step(store, now=NOW)
    assert any(f.kind == "policy_conflict" and f.subject == "TZ-1" for f in findings)
    assert state(store, "TZ-1") == "ready"
    assert state(store, "TZ-2") == "frozen"  # unowned path is not authority
    assert count(store, "thaw") == 1
