"""Zombie retirement requires independent event, owns and git evidence."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet.store import Store
from dev_mcp.fleet import zombies
from dev_mcp.fleet.zombies import classify, retire


def stamp(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir(parents=True)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)", (str(root),),
                  event={"kind": "test_setup"})
    return store, root


def add(store, tz, *, state="submitted", owns=None, spec="", title="", days=30):
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('P',?,?,?,1,'work',?,'','[]',?,'[]','[]','',?,?)",
            (tz, title, state, spec, json.dumps(owns or []), stamp(days + 1), stamp(days)),
            event={"kind": "test_setup"})


def event(store, tz, kind, days):
    with store.tx() as tx:
        tx.record_event(kind=kind, project="P", tz=tz, ts=stamp(days))


def git(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.org"], check=True)


def commit(root, *files, days=0):
    for file in files:
        path = root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content {days}\\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", *files], check=True)
    env = os.environ.copy()
    env.update(GIT_AUTHOR_DATE=stamp(days), GIT_COMMITTER_DATE=stamp(days))
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], env=env, check=True)


def replacement(tmp_path, old_owns, new_owns, *, files=("file.py",), commit_days=0,
                submit_days=30, accept_days=2, spec=""):
    store, root = setup(tmp_path)
    git(root)
    add(store, "old", owns=[str(root / p) for p in old_owns], spec=spec)
    add(store, "new", state="accepted", owns=[str(root / p) for p in new_owns], days=3)
    event(store, "old", "submit", submit_days)
    event(store, "new", "accept", accept_days)
    commit(root, *files, days=commit_days)
    return store, root


def test_classes_and_apply_preimage_preserves_review_and_unknown(tmp_path):
    store, root = setup(tmp_path)
    owned = str(root / "shared.py")
    add(store, "old", owns=[owned], days=30)
    add(store, "new", state="accepted", owns=[owned], days=3)
    add(store, "missing", spec="spec/deleted.md", days=27)
    add(store, "target", state="accepted", days=4)
    add(store, "blocker", title="Снять проектный блокер финального ревью P/target", days=22)
    add(store, "pending-review", state="frozen", days=20)
    add(store, "unknown", state="frozen", days=20)
    add(store, "submitted-awaiting", days=20)
    classes = {r["id"]: r["class"] for r in classify(store, "P", days=14)}
    assert classes == {"old": "d", "missing": "e", "blocker": "c",
                       "pending-review": "e", "unknown": "e",
                       "submitted-awaiting": "d"}
    result = retire(store, "P", days=14, apply=True, preimage_dir=tmp_path / "retired")
    assert result["applied"] == 1
    pre = json.loads(next((tmp_path / "retired").iterdir()).read_text(encoding="utf-8"))
    assert {r["task"]["tz"] for r in pre} == {"blocker"}
    with store.read() as tx:
        states = {r["tz"]: r["state"] for r in tx.query("SELECT tz,state FROM tasks")}
        events = [dict(r) for r in tx.query(
            "SELECT tz,payload FROM events WHERE kind='task_retired'")]
    assert states["blocker"] == "rejected"
    assert all(states[x] == "submitted" for x in ("old", "missing", "submitted-awaiting"))
    assert states["pending-review"] == states["unknown"] == "frozen"
    assert len(events) == 1
    assert all(json.loads(e["payload"])["evidence"] for e in events)


def test_active_claim_not_retired(tmp_path):
    store, root = setup(tmp_path)
    add(store, "missing", spec="obsolete.md")
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W',?,?)",
                  (stamp(), stamp()), event={"kind": "test_setup"})
        tx.change("INSERT INTO claims(worker_id,project,tz,state,started_at) "
                  "VALUES('W','P','missing','active',?)", (stamp(),),
                  event={"kind": "test_setup"})
    assert classify(store, "P")[0]["class"] == "d"
    assert retire(store, "P", apply=True)["applied"] == 0


def test_git_committed_after_submission_proves_class_a(tmp_path):
    store, root = setup(tmp_path)
    git(root)
    commit(root, "done.py")
    add(store, "finished", owns=[str(root / "done.py")])
    assert classify(store, "P")[0]["class"] == "a"
    (root / "done.py").write_text("uncommitted\\n", encoding="utf-8")
    assert classify(store, "P")[0]["class"] == "d"


def test_valid_replacement_requires_all_three_proofs_and_is_applied(tmp_path):
    store, root = replacement(tmp_path, ["file.py"], ["file.py"])
    row = next(x for x in classify(store, "P") if x["id"] == "old")
    assert row["class"] == "b"
    assert "accepted new" in row["evidence"] and "file.py@" in row["evidence"]
    result = retire(store, "P", apply=True, preimage_dir=tmp_path / "retired")
    assert result["applied"] == 1
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='old'")["state"] == "rejected"


@pytest.mark.parametrize("old_owns,new_owns,files", [
    (["file.py"], ["."], ("file.py",)),  # broad project root is never a witness
    (["file.py"], ["**"], ("file.py",)),  # broad /** root witness also excluded
    (["src"], ["src/module.py"], ("src/module.py",)),  # descendant cannot cover parent
    (["a.py", "b.py"], ["a.py"], ("a.py", "b.py")),  # every old path must be covered
    (["src/module.py"], ["src2"], ("src/module.py", "src2/other.py")),  # segment boundary
])
def test_replacement_rejects_incomplete_or_reverse_owns(tmp_path, old_owns, new_owns, files):
    store, root = replacement(tmp_path, old_owns, new_owns, files=files)
    assert next(x for x in classify(store, "P") if x["id"] == "old")["class"] != "b"


def test_ancestor_directory_witness_covers_stale_file(tmp_path):
    store, root = replacement(tmp_path, ["src/module.py"], ["src"],
                              files=("src/module.py",))
    assert next(x for x in classify(store, "P") if x["id"] == "old")["class"] == "b"


def test_directory_witness_covers_child_only_if_git_has_directory_commit(tmp_path):
    store, root = replacement(tmp_path, ["src"], ["src"], files=("src/real.py",))
    assert next(x for x in classify(store, "P") if x["id"] == "old")["class"] == "b"
    # A later commit elsewhere in the project is not evidence for src.
    store2, root2 = replacement(tmp_path / "second", ["src"], ["src"],
                                files=("elsewhere.py",))
    assert next(x for x in classify(store2, "P") if x["id"] == "old")["class"] != "b"


def test_earlier_git_commit_or_accept_does_not_replace(tmp_path):
    store, root = replacement(tmp_path, ["file.py"], ["file.py"], commit_days=40)
    assert next(x for x in classify(store, "P") if x["id"] == "old")["class"] != "b"
    store2, root2 = replacement(tmp_path / "second", ["file.py"], ["file.py"],
                                accept_days=40)
    assert next(x for x in classify(store2, "P") if x["id"] == "old")["class"] != "b"


def test_no_submit_event_does_not_replace(tmp_path):
    store, root = replacement(tmp_path, ["file.py"], ["file.py"])
    with store.tx() as tx:
        tx.change("DELETE FROM events WHERE tz='old' AND kind='submit'",
                  event={"kind": "test_setup"})
    assert next(x for x in classify(store, "P") if x["id"] == "old")["class"] != "b"


def test_missing_spec_is_orphan_even_when_other_proofs_hold(tmp_path):
    store, root = replacement(tmp_path, ["file.py"], ["file.py"], spec="deleted.md")
    row = next(x for x in classify(store, "P") if x["id"] == "old")
    assert row["class"] == "e" and "orphan" in row["evidence"] and isinstance(row["evidence"], str)


def test_apply_refuses_forged_class_b(tmp_path, monkeypatch):
    store, root = setup(tmp_path)
    add(store, "old", owns=[str(root / "file.py")])
    forged = {"id": "old", "project": "P", "state": "submitted", "class": "b",
              "age_days": 30, "evidence": "forged", "from": "submitted",
              "updated_at": next(x for x in classify(store, "P"))["updated_at"]}
    monkeypatch.setattr(zombies, "classify", lambda *a, **k: [forged])
    with pytest.raises(RuntimeError, match="class b verification failed"):
        zombies.retire(store, "P", apply=True, preimage_dir=tmp_path / "retired")
    assert not (tmp_path / "retired").exists()


def test_dry_does_not_modify_db_or_make_preimage(tmp_path):
    store, root = setup(tmp_path)
    add(store, "stale", spec="deleted.md")
    output = retire(store, "P", preimage_dir=tmp_path / "retired")
    assert output["counts"] == {"e": 1}
    assert not (tmp_path / "retired").exists()
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='P' AND tz='stale'")["state"] == "submitted"


def test_cli_readonly_does_not_make_wal_or_shm_or_touch_database_mtime(tmp_path):
    store, root = setup(tmp_path)
    add(store, "old", spec="deleted.md")
    db = Path(store.path)
    store.close()
    # Even a standalone WAL-mode copy must not grow SQLite sidecars.
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()
    before = db.stat().st_mtime_ns
    cli = Path(__file__).resolve().parents[2] / "tools" / "fleet_zombies.py"
    proc = subprocess.run([sys.executable, str(cli), "P", "--db", str(db), "--json"],
                          capture_output=True, text=True, check=True)
    assert json.loads(proc.stdout)["counts"] == {"e": 1}
    all_projects = subprocess.run(
        [sys.executable, str(cli), "--all", "--db", str(db), "--json"],
        capture_output=True, text=True, check=True)
    assert json.loads(all_projects.stdout)["P"]["counts"] == {"e": 1}
    assert db.stat().st_mtime_ns == before
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-shm").exists()
