"""TZ-272: local worker honors the durable claim checkout, including its Python."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dev_mcp.fleet import evidence, goals, queue


WORKER_FILE = Path(__file__).resolve().parents[1] / "tools" / "worker.py"


def _worker():
    spec = importlib.util.spec_from_file_location("worker_claim_tree", WORKER_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(root), *argv], check=True,
                   capture_output=True, text=True)


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated: bool):
    worker = _worker()
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Fleet test")
    _git(root, "config", "user.email", "fleet@example.invalid")
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (root / "marker.txt").write_text("BEFORE", encoding="utf-8")
    (root / "runner.py").write_text(
        "from pathlib import Path\n"
        "from appflag import FLAG\n"
        "Path('marker.txt').write_text(FLAG, encoding='utf-8')\n",
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "appflag.py").write_text("FLAG = 'CLAIM'\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")

    if isolated:
        # The checkout deliberately lacks this untracked project interpreter.
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
                       check=True, capture_output=True)

    command = (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    argv = [command if isolated else sys.executable, "runner.py"]
    db = tmp_path / "registry.sqlite"
    store = worker._open_store(db)
    worker_id = "W-TZ272"
    stamp = worker._now()
    with worker._connect(db) as conn:
        worker._ensure_project(conn, "P", root)
        conn.execute(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,?,?,?)",
            (worker_id, worker_id, "chatgpt", stamp, stamp),
        )
        conn.execute(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES(?,?,'checkout task','ready',1,'work','','','[]',?,'[]',?,'body',?,?)",
            ("P", "TZ-W", json.dumps([str(root / "marker.txt")]),
             json.dumps([" ".join(argv)]), stamp, stamp),
        )
        conn.commit()
    monkeypatch.setattr(goals, "project_info", lambda _: {
        "commit": True, "claim_trees": isolated,
    })
    task = queue.take_next(store, worker_id, "P")
    assert task is not None and task.tz == "TZ-W"
    # Shared CLI inherits its environment; isolated CLI must prepend claim/src.
    monkeypatch.setenv("PYTHONPATH", str(root / "src"))
    return worker, store, db, root, worker_id, argv, task


@pytest.mark.parametrize("isolated", [True, False])
def test_take_run_and_submit_in_correct_checkout(tmp_path, monkeypatch, capsys, isolated):
    worker, store, db, root, wid, argv, task = _setup(tmp_path, monkeypatch, isolated)
    try:
        context = worker._claim_checkout(db, wid)
        assert context.mode == ("isolated" if isolated else "shared")
        assert context.tree_path != root if isolated else context.tree_path == root
        worker._print_task(task, db)
        assert f"Дерево захвата ({context.mode}): {context.tree_path}" in capsys.readouterr().out

        # This extra trunk edit must not appear inside an isolated claim's imports.
        (root / "src" / "appflag.py").write_text("FLAG = 'TRUNK'\n", encoding="utf-8")
        assert worker._run_evidence(wid, ["--", *argv], db) == 0
        assert (context.tree_path / "marker.txt").read_text(encoding="utf-8") == (
            "CLAIM" if isolated else "TRUNK"
        )
        if isolated:
            assert (root / "marker.txt").read_text(encoding="utf-8") == "BEFORE"
            assert not (context.tree_path / ".venv").exists()

        with worker._connect(db) as conn:
            event = conn.execute(
                "SELECT payload,path_norm,rc FROM events WHERE tool='worker.run' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert event is not None and event["rc"] == 0
        assert Path(event["path_norm"]) == context.tree_path
        recorded = json.loads(event["payload"])
        assert recorded["argv"] == argv
        assert recorded["tree"] == evidence.tree_hash([str(context.tree_path / "marker.txt")])

        # The queue's real green-submit rule must compare the checkout hash,
        # and refuse a write made after the last successful acceptance run.
        monkeypatch.setattr(queue, "REQUIRE_GREEN_SUBMIT", True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-state"))
        monkeypatch.setattr(worker, "_current_state", lambda name=None: {
            "worker_id": wid, "label": wid, "kind": "chatgpt",
        })
        monkeypatch.setattr(sys, "argv", ["worker.cmd", "--db", str(db),
                                           "submit", "acceptance green"])
        (context.tree_path / "marker.txt").write_text("STALE", encoding="utf-8")
        with pytest.raises(ValueError, match="файлы задачи менялись после"):
            worker.main()
        assert worker._run_evidence(wid, ["--", *argv], db) == 0
        assert worker.main() == 0
        output = capsys.readouterr().out
        assert "review=review:TZ-W" in output
        assert f"Дерево сдачи: {context.tree_path}" in output
        with worker._connect(db) as conn:
            state = conn.execute("SELECT state FROM tasks WHERE project='P' AND tz='TZ-W'").fetchone()
        assert state is not None and state["state"] == "submitted"
    finally:
        store.close()


def test_untrusted_checkout_event_is_not_downgraded_to_trunk(tmp_path, monkeypatch):
    worker, store, db, root, wid, argv, task = _setup(tmp_path, monkeypatch, True)
    try:
        with worker._connect(db) as conn:
            row = conn.execute("SELECT id,payload FROM events WHERE kind='take' ORDER BY id DESC LIMIT 1").fetchone()
            payload = json.loads(row["payload"])
            payload["claim_context"]["tree_path"] = str(root)
            conn.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
            conn.commit()
        with pytest.raises(Exception, match="worktree|checkout|mapping|tree"):
            worker._run_evidence(wid, argv, db)
        assert (root / "marker.txt").read_text(encoding="utf-8") == "BEFORE"
    finally:
        store.close()
