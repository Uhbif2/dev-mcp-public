"""Гейт проекта в приёмке задачи не держит вердикт, пока ствол красный (Beta App 14–17.10)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import goals, review
from dev_mcp.fleet.model import norm_path
from test_queue import StubStore, add_project, add_task, now

P = "Gated"
GATE = "py -3 tools/check_all.py"
DOC = "py -3 tools/tzcheck.py doc docs/a.md"


def _setup(tmp_path, monkeypatch, *, owns, gate_rc=1, tz="request:1"):
    monkeypatch.setattr(goals, "project_info", lambda project: {"gate": GATE})
    store = StubStore.open(tmp_path / "g.sqlite")
    add_project(store, P)
    root = "/" + P
    add_task(store, tz, project=P, state="submitted",
             owns=tuple(norm_path(o, project_root=root) for o in owns))
    add_task(store, f"review:{tz}", project=P, state="taken", kind="review")
    started = now(-5)
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET body=?, acceptance=? WHERE project=? AND tz=?",
                   (f'[fleet-review] {json.dumps({"project": P, "tz": tz})}', "[]", P, f"review:{tz}"))
        tx.execute("UPDATE tasks SET acceptance=? WHERE project=? AND tz=?",
                   (json.dumps([DOC, GATE]), P, tz))
        tx.execute("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES(?,?,?,?,?)",
                   (P, f"review:{tz}", "W1", "active", started))
        tx.execute("INSERT INTO events(ts,kind,project,tz,rc,payload) VALUES(?,?,?,?,?,?)",
                   (now(-60), "gate", P, "TZ-x", gate_rc,
                    json.dumps({"failed": ["test/test_broken.py"] if gate_rc else []})))
    return store, started


def _deferred(store, started):
    with store.read() as tx:
        return review.deferred_gate_commands(tx, "W1", started, [DOC, GATE])


def test_red_trunk_outside_zone_defers_gate_only(tmp_path, monkeypatch):
    store, started = _setup(tmp_path, monkeypatch, owns=("docs/a.md",))
    assert _deferred(store, started) == {GATE}
    with store.read() as tx:
        assert review._missing_runs(tx, "W1", started, [DOC, GATE]) == [DOC]


def test_green_trunk_keeps_gate(tmp_path, monkeypatch):
    store, started = _setup(tmp_path, monkeypatch, owns=("docs/a.md",), gate_rc=0)
    assert _deferred(store, started) == set()


def test_task_touching_red_zone_keeps_gate(tmp_path, monkeypatch):
    store, started = _setup(tmp_path, monkeypatch, owns=("test/test_broken.py",))
    assert _deferred(store, started) == set()


def test_styk_keeps_gate(tmp_path, monkeypatch):
    store, started = _setup(tmp_path, monkeypatch, owns=("docs/a.md",), tz="styk:7")
    assert _deferred(store, started) == set()
