"""TZ-52: deterministic lane grouping and opt-in shadow decisions."""
from __future__ import annotations

import itertools
import random

import pytest

from dev_mcp.fleet.routing import (
    Lane, LaneState, lanes, pick_for, shadow_pick, spine_blockers,
)


def task(tz, owns=(), needs=(), *, project="P", kind="work", subsystem=None):
    out = {"project": project, "tz": tz, "owns": list(owns),
           "needs": list(needs), "kind": kind}
    if subsystem is not None:
        out["subsystem"] = subsystem
    return out


def ids(groups):
    return [group.tzs for group in groups]


def test_sorted_prefix_groups_match_pairwise_overlap_graph():
    from dev_mcp.fleet import routing
    from dev_mcp.fleet.model import paths_overlap
    rng = random.Random(20261014)
    paths = ['C:/P/src/fleet/a.py', 'c:\\p\\SRC\\fleet\\a.py',
             'C:/P/src/fleet/**', 'C://P/src/fleet/b.py',
             'C:/P/src/fleet_extra/a.py', 'C:/P/app/android/a.kt',
             'C:/P/app/android/b.kt', 'C:/P/docs/a.md']
    for _ in range(40):
        source = [task(f'TZ-{i}', rng.sample(paths, rng.randrange(3)),
                       [f'TZ-{rng.randrange(18)}'], project=rng.choice(['P', 'Q']))
                  for i in range(18)]
        components = [{(t['project'], t['tz'])} for t in source]
        for i, left in enumerate(source):
            for right in source[i+1:]:
                if left['project'] != right['project']:
                    continue
                overlap = any(paths_overlap(a,b) for a in left['owns'] for b in right['owns'])
                need = left['tz'] in right['needs'] or right['tz'] in left['needs']
                if overlap or (need and routing._domains(left) & routing._domains(right)):
                    a = next(c for c in components if (left['project'],left['tz']) in c)
                    b = next(c for c in components if (right['project'],right['tz']) in c)
                    if a is not b:
                        a.update(b); components.remove(b)
        actual = {frozenset((g.project,tz) for tz in g.tzs) for g in lanes(source)}
        assert actual == {frozenset(c) for c in components}


def test_dense_historical_lane_has_bounded_overlap_work(monkeypatch):
    from dev_mcp.fleet import routing
    original = routing.paths_overlap
    calls = 0
    def bounded(a,b):
        nonlocal calls
        calls += 1
        assert calls < 10000, 'quadratic historical scan blocks real dispatch'
        return original(a,b)
    monkeypatch.setattr(routing,'paths_overlap',bounded)
    source = [task(f'TZ-{i}', ['C:/P/src/fleet/shared.py']) for i in range(4000)]
    groups = lanes(source)
    assert len(groups)==1 and len(groups[0].tzs)==4000


def test_same_owned_file_joins_tasks_and_name_uses_minimum_tz():
    a = task("TZ-20", ["C:/P/src/dev_mcp/fleet/a.py"])
    b = task("TZ-03", ["c:\\p\\SRC\\dev_mcp\\fleet\\a.py"])
    assert lanes([a, b]) == [Lane("P/TZ-03", "P", ("TZ-03", "TZ-20"))]


def test_numeric_tz_name_not_lexical_tz_ten_before_tz_two():
    a = task("TZ-10", ["src/dev_mcp/fleet/shared.py"])
    b = task("TZ-2", ["src/dev_mcp/fleet/shared.py"])
    assert lanes([a, b]) == [Lane("P/TZ-2", "P", ("TZ-2", "TZ-10"))]


def test_trailing_subtree_glob_connects_child_but_not_similar_prefix():
    base = task("TZ-1", ["C:/P/src/dev_mcp/fleet/**"])
    child = task("TZ-2", ["C:/P/src/dev_mcp/fleet/queue.py"])
    other = task("TZ-3", ["C:/P/src/dev_mcp/fleet_extra/queue.py"])
    assert ids(lanes([base, child, other])) == [("TZ-1", "TZ-2"), ("TZ-3",)]


def test_direct_needs_same_subsystem_joins_even_disjoint_files():
    first = task("TZ-10", ["C:/P/src/dev_mcp/fleet/a.py"])
    second = task("TZ-11", ["C:/P/src/dev_mcp/fleet/b.py"], ["TZ-10"])
    assert ids(lanes([second, first])) == [("TZ-10", "TZ-11")]


def test_needs_chain_transitive_in_same_subsystem():
    a = task("TZ-1", ["C:/P/app/android/a.kt"])
    b = task("TZ-2", ["C:/P/app/android/b.kt"], ["TZ-1"])
    c = task("TZ-3", ["C:/P/app/android/c.kt"], ["TZ-2"])
    assert ids(lanes([c, a, b])) == [("TZ-1", "TZ-2", "TZ-3")]


def test_need_across_different_domains_does_not_join():
    first = task("TZ-1", ["C:/P/src/dev_mcp/fleet/a.py"])
    second = task("TZ-2", ["C:/P/src/dev_mcp/owner/b.py"], ["TZ-1"])
    assert ids(lanes([first, second])) == [("TZ-1",), ("TZ-2",)]


def test_generic_docs_and_tests_do_not_link_unrelated_needs():
    first = task("TZ-1", ["C:/P/tests/fleet/a.py", "C:/P/docs/a.md"])
    second = task("TZ-2", ["C:/P/tests/fleet/b.py", "C:/P/docs/b.md"], ["TZ-1"])
    assert ids(lanes([first, second])) == [("TZ-1",), ("TZ-2",)]


def test_explicit_subsystem_can_group_nonstandard_code_layout():
    a = task("TZ-1", ["C:/P/lib/one.py"], subsystem="runtime")
    b = task("TZ-2", ["C:/P/plugins/two.py"], ["TZ-1"], subsystem="runtime")
    assert ids(lanes([a, b])) == [("TZ-1", "TZ-2")]


def test_different_projects_do_not_merge_even_same_absolute_owned_file():
    a = task("TZ-1", ["/shared/file.py"], project="P")
    b = task("TZ-2", ["/shared/file.py"], project="Q", needs=["TZ-1"])
    assert len(lanes([a, b])) == 2


def test_shuffled_input_has_stable_connected_components():
    source = [task("TZ-3", ["C:/P/src/dev_mcp/fleet/c.py"], ["TZ-2"]),
              task("TZ-1", ["C:/P/src/dev_mcp/fleet/a.py"]),
              task("TZ-2", ["C:/P/src/dev_mcp/fleet/b.py"], ["TZ-1"]),
              task("TZ-4", ["C:/P/app/android/x.kt"])]
    expected = lanes(source)
    for order in itertools.permutations(source):
        assert lanes(order) == expected


def test_duplicate_task_fails_loudly():
    with pytest.raises(ValueError, match="duplicate task"):
        lanes([task("TZ-1"), task("TZ-1")])


def test_reviews_and_triage_excluded_from_work_lanes():
    source = [task("TZ-1", ["C:/P/src/dev_mcp/fleet/a.py"]),
              task("review:TZ-1", ["C:/P/src/dev_mcp/fleet/a.py"], kind="review"),
              task("triage:1", ["C:/P/src/dev_mcp/fleet/a.py"], kind="triage")]
    assert ids(lanes(source)) == [("TZ-1",)]


def test_warm_leader_prefers_own_lane_over_unclaimed():
    free = LaneState(Lane("P/TZ-1", "P", ("TZ-1",)), ready=("TZ-1",))
    warm = LaneState(Lane("P/TZ-2", "P", ("TZ-2",)), ready=("TZ-2",),
                     leader="W1")
    assert pick_for("W1", [free, warm]) == "TZ-2"
    assert pick_for("W2", [free, warm]) == "TZ-1"


def test_foreign_leader_blocks_selection_of_its_lane():
    taken = LaneState(Lane("P/TZ-1", "P", ("TZ-1",)), ready=("TZ-1",),
                      leader="W1")
    assert pick_for("W2", [taken]) is None
    assert pick_for("W1", [taken]) == "TZ-1"


def test_dead_leader_releases_lane_for_reassignment():
    state = LaneState(Lane("P/TZ-1", "P", ("TZ-1",)), ready=("TZ-1",),
                      leader="W1", leader_alive=False)
    assert pick_for("W2", state) == "TZ-1"


def test_picker_preserves_ready_order_and_never_takes_reviews():
    lane = Lane("P/TZ-1", "P", ("TZ-1", "TZ-2"))
    state = LaneState(lane, ready=({"tz": "review:TZ-1", "kind": "review"},
                                   {"tz": "TZ-2", "kind": "work"},
                                   {"tz": "TZ-1", "kind": "work"}))
    assert pick_for("W1", state) == "TZ-2"


def test_shadow_records_difference_without_mutating_normal_dispatch():
    state = LaneState(Lane("P/TZ-1", "P", ("TZ-1",)), ready=("TZ-1",),
                      leader="W1")
    baseline = "TZ-9"
    observation = shadow_pick("W1", [state], baseline)
    assert (observation.actual, observation.proposed, observation.would_change) == (
        baseline, "TZ-1", True)
    assert state.ready == ("TZ-1",)
    assert pick_for("W2", state) is None


def test_admission_spine_has_one_writer_until_first_accepted():
    first = {**task("TZ-2", ["src/dev_mcp/fleet/controller.py"]), "state": "taken"}
    later = {**task("TZ-10", ["src/dev_mcp/fleet/controller.py"]), "state": "ready"}
    assert spine_blockers(later, [later, first]) == ("TZ-2",)
    assert spine_blockers(first, [first, later]) == ()
    assert spine_blockers(later, [{**first, "state": "submitted"}]) == ("TZ-2",)
    assert spine_blockers(later, [{**first, "state": "accepted"}]) == ()


def test_admission_uses_creation_order_and_actual_hot_overlap():
    earlier = {**task("TZ-50", ["C:/p/src/dev_mcp/fleet/**"]),
               "created_at": "2026-10-11T10:00:00"}
    later = {**task("TZ-1", ["c:/p/src/dev_mcp/fleet/queue.py"]),
             "created_at": "2026-10-11T11:00:00"}
    assert spine_blockers(later, [earlier]) == ("TZ-50",)
    assert spine_blockers(later, [task("TZ-0", ["src/dev_mcp/fleet/helper.py"])]) == ()
    assert spine_blockers(later, [task("TZ-0", ["src/dev_mcp/owner/queue.py"])]) == ()
    assert spine_blockers(later, [task("review:TZ-0", ["c:/p/src/dev_mcp/fleet/queue.py"],
                                     kind="review")]) == ()
    assert spine_blockers(later, [task("TZ-0", ["c:/p/src/dev_mcp/fleet/queue.py"],
                                     project="Else")]) == ()


def test_queue_take_records_shadow_without_changing_priority_or_warm_leader(tmp_path):
    import json
    from datetime import datetime, timezone

    from dev_mcp.fleet import queue
    from dev_mcp.fleet.store import Store

    db = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    with db.tx() as tx:
        tx.changes([
            ("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
             ("Unit", str(tmp_path))),
            ("INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at) "
             "VALUES(?,?,'chatgpt',?,?,NULL)", ("W1", "W1", now, now)),
        ], event={"kind": "setup"})
        for tz, state, owns, priority in (
            ("TZ-1", "accepted", ["src/dev_mcp/fleet/a.py"], 5),
            ("TZ-2", "ready", ["src/dev_mcp/fleet/a.py"], 5),
            ("TZ-3", "ready", ["src/dev_mcp/owner/b.py"], 1),
        ):
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
                "spec_sha,needs,owns,resources,acceptance,body,created_at,updated_at)"
                "VALUES(?,?,?,?,?,'work','','',?,?,?,?,?,?,?)",
                ("Unit", tz, tz, state, priority, "[]", json.dumps(owns),
                 "[]", "[]", "", now, now),
                event={"kind": "setup"},
            )
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
            "VALUES('Unit','TZ-1','W1','released',?,?)", (now, now),
            event={"kind": "setup"},
        )
    taken = queue.take_next(db, "W1", "Unit")
    assert taken is not None and taken.tz == "TZ-3"  # actual unchanged
    with db.read() as tx:
        observations = tx.query(
            "SELECT payload FROM events WHERE kind='routing_would_pick'"
        )
    assert len(observations) == 1
    payload = json.loads(observations[0]["payload"])
    assert payload["actual"] == "TZ-3"
    assert payload["proposed"] == "TZ-2"
    assert payload["would_change"] is True
    assert payload["lane"] == "Unit/TZ-1"
    assert payload["leader"] == "W1"
    db.close()


def test_submit_sealed_candidate_records_tz57_findings_in_shadow_only(tmp_path, monkeypatch):
    """Real Git candidate: weakened assertion is journalled, never a submit veto."""
    import json
    import subprocess
    from datetime import datetime, timezone
    from pathlib import Path
    from types import SimpleNamespace

    from dev_mcp.fleet import queue, claim_review_bite
    from dev_mcp.fleet.store import Store

    root, checkout = tmp_path / "source", tmp_path / "candidate"
    root.mkdir()

    def git(path, *argv):
        result = subprocess.run(["git", "-C", str(path), *argv], check=True,
                                capture_output=True, text=True)
        return result.stdout.strip()

    git(root, "init")
    git(root, "config", "user.name", "Routing Tester")
    git(root, "config", "user.email", "routing@example.invalid")
    spec = root / "ToDo/tz/TZ-shadow.md"
    spec.parent.mkdir(parents=True)
    command = "python -m pytest -q tests/test_contract.py"
    spec.write_text(
        "```yaml\n"
        "tz: TZ-shadow\n"
        "owns: [ToDo/tz/TZ-shadow.md, tests/test_contract.py]\n"
        "acceptance:\n"
        f"  - {command}\n"
        "```\n\n# Test\n", encoding="utf-8",
    )
    test_file = root / "tests/test_contract.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_contract():\n    assert 1 == 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    base = git(root, "rev-parse", "HEAD")
    git(root, "worktree", "add", "-b", "candidate-review", str(checkout), base)
    (checkout / "tests/test_contract.py").write_text(
        "def test_contract():\n    assert True\n", encoding="utf-8",
    )

    db = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    owns = [str(spec), str(test_file)]
    with db.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Unit", str(root)), event={"kind": "setup"})
        tx.change(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen) "
            "VALUES(?,?,'chatgpt',?,?)",
            ("worker", "worker", now, now), event={"kind": "setup"},
        )
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
            "spec_sha,needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES(?,?,?,'taken',1,'request',?,'','[]',?,'[]',?,'',?,?)",
            ("Unit", "TZ-shadow", "shadow", str(spec),
             json.dumps(owns), json.dumps([command]), now, now),
            event={"kind": "setup"},
        )
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('Unit','TZ-shadow','worker','active',?)",
            (now,), event={"kind": "setup"},
        )
        claim_id = int(tx.scalar("SELECT id FROM claims WHERE worker_id='worker'"))
        tx.record_event(kind="claim_submission", project="Unit", tz="TZ-shadow",
                        worker_id="worker",
                        payload={"claim_id": claim_id, "submission_id": "f" * 32})

    def sealed(store, cid):
        assert store is db and cid == claim_id
        return (
            SimpleNamespace(submission_id="f" * 32, revision="b" * 40),
            SimpleNamespace(mode="isolated", tree_path=checkout,
                            base_sha=base, project_root=root),
            {},
        )

    # Sealing and its Git evidence are proven by the claim-review component.
    # The seam here checks queue's trusted registration/checkout handoff.
    monkeypatch.setattr(claim_review_bite, "_submission", sealed)
    queue.submit(db, "worker", "candidate completed")
    with db.read() as tx:
        task = tx.one("SELECT state FROM tasks WHERE project='Unit' AND tz='TZ-shadow'")
        audit = tx.one("SELECT payload FROM events WHERE kind='acceptance_guard_shadow'")
        would = tx.query("SELECT payload FROM events WHERE kind='would_block'")
        rule = tx.one("SELECT payload FROM events WHERE kind='shadow_rule_registered'")
        submitted = tx.scalar("SELECT COUNT(*) FROM events WHERE kind='submit'")
    assert task["state"] == "submitted" and submitted == 1
    report = json.loads(audit["payload"])
    assert report["claim_id"] == claim_id
    assert report["submission_id"] == "f" * 32
    assert report["revision"] == "b" * 40
    assert report["mode"] == "shadow"
    assert report["minimum_observation_hours"] == 24
    assert any("assert removed or weakened" in issue for issue in report["findings"])
    assert would and all(json.loads(row["payload"])["rule_id"] ==
                         "acceptance_guard:submission" for row in would)
    assert json.loads(rule["payload"])["mode"] == "shadow"
    db.close()
    git(root, "worktree", "remove", "--force", str(checkout))


def test_unsealed_claim_is_not_sent_to_acceptance_guard(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from dev_mcp.fleet import queue, acceptance_guard
    from dev_mcp.fleet.store import Store

    db = Store.open(str(tmp_path / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    with db.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Unit", str(tmp_path)), event={"kind": "setup"})
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                  "VALUES(?,?,'chatgpt',?,?)",
                  ("worker", "worker", now, now), event={"kind": "setup"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
            "spec_sha,needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('Unit','TZ-1','unsealed','taken',1,'request','','','[]','[]',"
            "'[]','[]','',?,?)", (now, now), event={"kind": "setup"},
        )
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('Unit','TZ-1','worker','active',?)",
                  (now,), event={"kind": "setup"})
    monkeypatch.setattr(acceptance_guard, "check",
                        lambda *_a: pytest.fail("unsealed candidate was inspected"))
    queue.submit(db, "worker", "legacy shared request")
    with db.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='submit'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='acceptance_guard_shadow'") == 0
    db.close()
