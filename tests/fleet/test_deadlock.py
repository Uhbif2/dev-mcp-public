"""Fail-closed replay of the 11 October fleet review / trunk / styk cycle."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.deadlock import cycles, exempt_reviews, recovery_plan, wait_graph
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.shadow import ShadowRules
from test_queue import StubStore, add_project, add_task, now

P = "Fleet Platform"


def _store(tmp_path):
    store = StubStore.open(tmp_path / "deadlock.sqlite")
    add_project(store, P)
    return store


def _task(store, tz, state="ready", owns=(), kind="work"):
    add_task(store, tz, project=P, state=state, owns=owns, kind=kind)
    if kind == "review":
        source = tz.removeprefix("review:")
        with store.tx() as tx:
            tx.execute("UPDATE tasks SET body=? WHERE project=? AND tz=?",
                       (f'[fleet-review] {json.dumps({"project": P, "tz": source})}', P, tz))


def _event(store, kind, tz="", rc=None, payload=None):
    with store.tx() as tx:
        tx.execute(
            "INSERT INTO events(ts,kind,project,tz,rc,payload) VALUES(?,?,?,?,?,?)",
            (now(), kind, P, tz, rc, json.dumps(payload or {})),
        )


def _cycle(store):
    root = "/" + P
    fail = norm_path("tests/broken.py", project_root=root)
    other = norm_path("docs/other.md", project_root=root)
    _task(store, "TZ-resolver", "submitted", (fail,))
    _task(store, "review:TZ-resolver", "ready", kind="review")
    _task(store, "TZ-unrelated", "submitted", (fail,))
    _task(store, "review:TZ-unrelated", "ready", kind="review")
    _task(store, "TZ-clean", "submitted", (norm_path("docs/**", project_root=root),))
    _task(store, "review:TZ-clean", "ready", kind="review")
    _task(store, "styk:1", "frozen", (fail,))
    _event(store, "gate", "TZ-source", rc=1, payload={"failed": ["tests/broken.py"], "commits": ["old"]})
    _event(store, "styk_open", "styk:1", payload={"after": ["TZ-source"]})
    _event(store, "integrate", "TZ-source", payload={
        "deferred": ["tests/broken.py"], "waiting_for": ["TZ-resolver"]})
    _event(store, "gate", "TZ-source", rc=1, payload={"failed": ["tests/broken.py"], "commits": ["old"]})
    return fail, other


def test_detects_11_october_cycle_and_unblocks_resolver_review(tmp_path):
    store = _store(tmp_path)
    _cycle(store)
    with store.read() as tx:
        graph = wait_graph(tx)
        loops = cycles(graph)
        exempt = exempt_reviews(tx)
    assert loops
    assert any({"review:Fleet Platform/review:TZ-resolver", "trunk:Fleet Platform",
                "styk:Fleet Platform/styk:1", "integrate:Fleet Platform/TZ-source",
                "work:Fleet Platform/TZ-resolver"}.issubset(set(c)) for c in loops)
    assert (P, "review:TZ-resolver") in exempt
    assert (P, "review:TZ-unrelated") not in exempt


def test_actual_diff_outside_failing_files_exempts_broad_owns(tmp_path):
    store = _store(tmp_path)
    fail, other = _cycle(store)
    _event(store, "write", "TZ-unrelated", payload={"path": other})
    with store.tx() as tx:
        tx.execute("UPDATE events SET path_norm=? WHERE kind='write' AND tz='TZ-unrelated'", (other,))
    with store.read() as tx:
        exempt = exempt_reviews(tx)
    assert (P, "review:TZ-unrelated") in exempt
    assert (P, "review:TZ-resolver") in exempt


def test_failing_diff_neither_resolver_nor_exempt(tmp_path):
    store = _store(tmp_path)
    fail, _ = _cycle(store)
    _event(store, "write", "TZ-unrelated")
    with store.tx() as tx:
        tx.execute("UPDATE events SET path_norm=? WHERE kind='write' AND tz='TZ-unrelated'", (fail,))
    with store.read() as tx:
        assert (P, "review:TZ-unrelated") not in exempt_reviews(tx)


def test_stale_styk_after_green_gate_is_recoverable(tmp_path):
    store = _store(tmp_path)
    _cycle(store)
    _event(store, "gate", "TZ-green", rc=0, payload={"failed": [], "commits": ["new"]})
    with store.read() as tx:
        plan = recovery_plan(tx)
    assert plan.stale_styks == ((P, "styk:1"),)
    assert plan.repair_projects == ()


def test_changed_revision_with_unrelated_failures_makes_styk_obsolete(tmp_path):
    store = _store(tmp_path)
    _cycle(store)
    _event(store, "gate", "TZ-other", rc=1, payload={"failed": ["tests/other.py"], "commits": ["new"]})
    with store.read() as tx:
        plan = recovery_plan(tx)
    assert plan.stale_styks == ((P, "styk:1"),)


def test_same_revision_or_same_failures_never_obsolete_styk(tmp_path):
    store = _store(tmp_path)
    _cycle(store)
    _event(store, "gate", "TZ-other", rc=1, payload={"failed": ["tests/other.py"], "commits": ["old"]})
    with store.read() as tx:
        assert not recovery_plan(tx).stale_styks
    _event(store, "gate", "TZ-other", rc=1, payload={"failed": ["tests/broken.py"], "commits": ["new"]})
    with store.read() as tx:
        assert not recovery_plan(tx).stale_styks


def test_without_resolver_creates_one_repair_not_duplicate(tmp_path):
    store = _store(tmp_path)
    fail = norm_path("tests/broken.py", project_root="/" + P)
    _task(store, "styk:1", "frozen", (fail,))
    _task(store, "styk:2", "frozen", (fail,))
    _event(store, "styk_open", "styk:1")
    _event(store, "styk_open", "styk:2")
    _event(store, "gate", "TZ-source", rc=1, payload={"failed": ["tests/broken.py"]})
    with store.read() as tx:
        assert recovery_plan(tx).repair_projects == (P,)
    _task(store, "TZ-53", "ready", (fail,))
    with store.read() as tx:
        assert recovery_plan(tx).repair_projects == ()


def test_acyclic_and_self_cycles_deterministic():
    graph = {"b": {"a"}, "a": {"b"}, "c": {"d"}, "d": set(), "x": {"x"}}
    assert cycles(graph) == [("a", "b"), ("x",)]


def test_shadow_observation_logs_without_changing_review_or_styk(tmp_path):
    store = _store(tmp_path)
    _cycle(store)
    with store.read() as tx:
        before = [(r["tz"], r["state"]) for r in tx.query(
            "SELECT tz,state FROM tasks ORDER BY tz")]
    report = ShadowRules(store).observe_deadlocks()
    assert report["mode"] == "shadow"
    assert report["minimum_observation_hours"] == 24
    assert [P, "review:TZ-resolver"] in report["exempt_reviews"]
    assert report["cycles"]
    with store.read() as tx:
        after = [(r["tz"], r["state"]) for r in tx.query(
            "SELECT tz,state FROM tasks ORDER BY tz")]
        row = tx.one("SELECT payload FROM events WHERE kind='deadlock_shadow'")
    assert before == after
    assert json.loads(row["payload"]) == report
