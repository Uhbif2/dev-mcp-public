"""Regression coverage for a stale gate cluster vs. direct current spec edges.

The decisions dashboard may report transitive or historical blockers. The
judgment miner must not turn that report into permission for a no-op unlink.
All mutations below are confined to pytest's temporary project and Store.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import judgment
from dev_mcp.fleet.store import Store


@pytest.fixture()
def scenario(tmp_path: Path) -> tuple[Store, Path]:
    root = tmp_path / "P"
    (root / "ToDo" / "tz").mkdir(parents=True)
    (root / "ToDo" / "decisions").mkdir(parents=True)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)",
            (str(root),), event={"kind": "test_setup"},
        )
    return store, root


def _task(store: Store, root: Path, tz: str, needs: list[str], *,
          state: str = "ready") -> Path:
    """Persist an authentic YAML spec and the current (possibly stale later) registry edge."""
    path = root / "ToDo" / "tz" / f"{tz}.md"
    path.write_text(
        f"```yaml\ntz: {tz}\nneeds: {json.dumps(needs)}\n"
        f"owns: [src/{tz}.py]\nacceptance: []\n```\n# {tz}\n",
        encoding="utf-8",
    )
    timestamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,body,created_at,updated_at) "
            "VALUES('P',?,?,?,1,'work',?,'',?,'[]','[]','[]','',?,?)",
            (tz, tz, state, f"ToDo/tz/{tz}.md", json.dumps(needs), timestamp, timestamp),
            event={"kind": "test_setup"},
        )
    return path


def _cluster(members: list[str], gates: list[str] | None = None) -> dict:
    return {
        "clusters": [{"project": "P", "gates": gates or ["TZ-90"], "tasks": members}],
        "items": [],
    }


def _count(store: Store, kind: str) -> int:
    with store.read() as tx:
        return int(tx.scalar("SELECT COUNT(*) FROM events WHERE kind=? AND project='P'", (kind,)) or 0)


def test_unlink_in_yaml_invalidates_stale_dashboard_cluster_and_is_idempotent(
    scenario: tuple[Store, Path],
) -> None:
    store, root = scenario
    members = [f"TZ-{n}" for n in range(1, 7)]
    paths = [_task(store, root, tz, ["TZ-90"]) for tz in members]
    _task(store, root, "TZ-90", [], state="frozen")
    dashboard = _cluster(members)  # stale snapshot: six tasks used to need TZ-90

    decision = root / "ToDo" / "decisions" / "gates-TZ-90.md"
    decision.write_text(
        "```yaml\nunlink:\n  TZ-1: [TZ-90]\n  TZ-2: [TZ-90]\n"
        "why: independently executable\n```\n", encoding="utf-8",
    )
    assert judgment._apply_unlink(store, "P", str(root), decision, set()) == (
        ["TZ-1", "TZ-2"], [], [],
    )
    with store.read() as tx:
        # Emulate a lagging registry/decisions refresh after server-side YAML unlink.
        assert json.loads(tx.one("SELECT needs FROM tasks WHERE tz='TZ-1'")["needs"]) == ["TZ-90"]
    saved = [path.read_bytes() for path in paths]

    assert judgment.open_triages(store, lambda: dashboard) == []
    assert _count(store, "triage_open") == 0
    assert _count(store, "triage_cluster_stale") == 1
    with store.read() as tx:
        row = tx.one("SELECT payload FROM events WHERE kind='triage_cluster_stale'")
        payload = json.loads(row["payload"])
        assert set(payload["direct"]) == set(members[2:])
        assert set(payload["reported"]) == set(members)

    # Repeating a poll and an already-applied unlink cannot create new work,
    # repeat diagnostics, or modify any specification bytes.
    assert judgment._apply_unlink(store, "P", str(root), decision, set()) == ([], [], [])
    assert judgment.open_triages(store, lambda: dashboard) == []
    assert _count(store, "triage_cluster_stale") == 1
    assert [path.read_bytes() for path in paths] == saved


def test_transitive_members_do_not_leak_into_a_real_direct_edge_group(
    scenario: tuple[Store, Path],
) -> None:
    store, root = scenario
    direct = [f"TZ-{n}" for n in range(1, 6)]
    for tz in direct:
        _task(store, root, tz, ["TZ-90"])
    _task(store, root, "TZ-6", ["TZ-1"])  # blocked by TZ-90 transitively, no direct edge
    _task(store, root, "TZ-90", [], state="frozen")
    report = _cluster([*direct, "TZ-6"])

    assert judgment.open_triages(store, lambda: report) == ["P/triage:1"]
    with store.read() as tx:
        opened = tx.one("SELECT body FROM tasks WHERE tz='triage:1'")
        audit = tx.one("SELECT payload FROM events WHERE kind='triage_open' AND tz='triage:1'")
    assert "5 задач ПРЯМО" in opened["body"]
    assert "TZ-6" not in opened["body"]
    assert set(json.loads(audit["payload"])["edges"]) == set(direct)
    assert _count(store, "triage_cluster_stale") == 1


def test_same_direct_edge_topology_never_reopens_without_a_new_edge(
    scenario: tuple[Store, Path],
) -> None:
    store, root = scenario
    direct = [f"TZ-{n}" for n in range(1, 6)]
    for tz in direct:
        _task(store, root, tz, ["TZ-90"])
    _task(store, root, "TZ-90", [], state="frozen")
    clock = datetime.now(timezone.utc)
    report = _cluster(direct)

    assert judgment.open_triages(store, lambda: report, now=clock) == ["P/triage:1"]
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='triage:1'", (),
                  event={"kind": "test_setup"})
        tx.record_event(kind="triage_applied", project="P", tz="triage:1",
                        payload={"unlinked": []})  # accepted no-op
    later = clock + timedelta(hours=judgment.GATE_COOLDOWN_H + 2)
    assert judgment.open_triages(store, lambda: report, now=later) == []
    assert _count(store, "triage_open") == 1

    # A genuine new direct edge, unlike another dashboard poll, is new work.
    _task(store, root, "TZ-6", ["TZ-90"])
    changed = _cluster([*direct, "TZ-6"])
    assert judgment.open_triages(store, lambda: changed, now=later) == ["P/triage:2"]
    assert _count(store, "triage_open") == 2
    assert judgment.open_triages(store, lambda: changed, now=later) == []


def test_file_preflight_releases_writer_and_changed_task_is_not_adjudicated(scenario, monkeypatch):
    import threading
    store, root = scenario
    members = [f'TZ-{n}' for n in range(1, 6)]
    for tz in members:
        _task(store, root, tz, ['TZ-90'])
    _task(store, root, 'TZ-90', [], state='frozen')
    original = judgment._verified_cluster_edges
    def changed(*args):
        acquired = threading.Event()
        def agent_write():
            with store.tx() as tx:
                tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz='TZ-1'",
                          event='test.concurrent_progress')
            acquired.set()
        thread = threading.Thread(target=agent_write)
        thread.start()
        assert acquired.wait(2), 'filesystem preflight held the global writer gate'
        thread.join()
        return original(*args)
    monkeypatch.setattr(judgment, '_verified_cluster_edges', changed)
    assert judgment.open_triages(store, lambda: _cluster(members)) == []
    assert _count(store, 'triage_open') == 0
