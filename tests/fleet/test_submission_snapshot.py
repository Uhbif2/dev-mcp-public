"""04.11.2026: снимок сдачи — неизменяемый git-объект сданных байтов, в тени (§7 шаг 2)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from dev_mcp.fleet import snapshot
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store


def _repo(tmp_path: Path) -> tuple[Path, callable]:
    root = tmp_path / "Repo"
    (root / "Src").mkdir(parents=True)

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              check=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (root / "Src" / "App.py").write_text("old\n", encoding="utf-8")
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    git("add", "-A"); git("commit", "-q", "-m", "init")
    return root, git


def _store(tmp_path: Path, root: Path) -> Store:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,0,1)", (str(root),),
                  event="test_setup")
    return store


def _shadow_events(store: Store) -> list[dict]:
    with store.read() as tx:
        return [json.loads(r["payload"]) for r in tx.query(
            "SELECT payload FROM events WHERE kind='submission_snapshot' ORDER BY id")]


def test_submission_bytes_become_git_object_without_touching_checkout(tmp_path):
    root, git = _repo(tmp_path)
    store = _store(tmp_path, root)
    with store.tx() as tx:
        tx.record_event(kind="write", worker_id="W1", project="P", tz="TZ-1",
                        path_norm=norm_path(str(root / "src" / "app.py")))  # до take — не его
        tx.record_event(kind="take", worker_id="W1", project="P", tz="TZ-1", payload={"claim_id": 7})
        tx.record_event(kind="write", worker_id="W1", project="P", tz="TZ-1",
                        path_norm=norm_path(str(root / "Src" / "App.py")))
        tx.record_event(kind="write", worker_id="W2", project="P", tz="TZ-2",
                        path_norm=norm_path(str(root / "keep.txt")))       # чужая задача
    (root / "Src" / "App.py").write_text("new\n", encoding="utf-8")
    (root / "keep.txt").write_text("foreign edit\n", encoding="utf-8")
    head, status = git("rev-parse", "HEAD"), git("status", "--porcelain")

    result = snapshot.submission_shadow(store, "P", "TZ-1", "W1", 7)

    assert result["commit"] and result["paths"] == ["Src/App.py"] and result["mode"] == "shadow"
    assert git("show", result["commit"] + ":Src/App.py") == "new"
    assert git("show", result["commit"] + ":keep.txt") == "keep"          # чужое — из HEAD
    assert result["blobs"]["Src/App.py"] == git("hash-object", "Src/App.py")
    assert git("rev-parse", "HEAD") == head and git("status", "--porcelain") == status
    assert git("for-each-ref") .count("refs/") == 1                       # в тени ссылок нет
    assert _shadow_events(store) == [result]


def test_submission_without_writes_is_recorded_as_skipped(tmp_path):
    root, _git = _repo(tmp_path)
    store = _store(tmp_path, root)
    with store.tx() as tx:
        tx.record_event(kind="take", worker_id="W1", project="P", tz="TZ-1", payload={"claim_id": 3})
    assert snapshot.submission_shadow(store, "P", "TZ-1", "W1", 3)["skipped"] == "записей нет"


def test_submit_starts_shadow_off_the_response_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("DEV_MCP_FLEET_SUBMIT_SNAPSHOT", "1")
    monkeypatch.setattr(snapshot, "submission_shadow", lambda *a: calls.append(a))
    assert snapshot.start_submission_shadow(object(), "P", "TZ-1", "W1", 1) is True
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls and calls[0][1:] == ("P", "TZ-1", "W1", 1)
    monkeypatch.setenv("DEV_MCP_FLEET_SUBMIT_SNAPSHOT", "0")
    assert snapshot.start_submission_shadow(object(), "P", "TZ-1", "W1", 1) is False


def test_shadow_report_compares_snapshot_with_integration(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "snapshot_shadow", Path(__file__).resolve().parents[2] / "tools" / "snapshot_shadow.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    root, git = _repo(tmp_path)
    store = _store(tmp_path, root)
    with store.tx() as tx:
        tx.record_event(kind="take", worker_id="W1", project="P", tz="TZ-1", payload={"claim_id": 7})
        tx.record_event(kind="write", worker_id="W1", project="P", tz="TZ-1",
                        path_norm=norm_path(str(root / "Src" / "App.py")))
        tx.record_event(kind="take", worker_id="W2", project="P", tz="TZ-2", payload={"claim_id": 8})
        tx.record_event(kind="write", worker_id="W2", project="P", tz="TZ-2",
                        path_norm=norm_path(str(root / "keep.txt")))
    (root / "Src" / "App.py").write_text("new\n", encoding="utf-8")
    (root / "keep.txt").write_text("mine\n", encoding="utf-8")
    snapshot.submission_shadow(store, "P", "TZ-1", "W1", 7)
    snapshot.submission_shadow(store, "P", "TZ-2", "W2", 8)
    git("add", "Src/App.py"); git("commit", "-q", "-m", "integrated")
    trunk = git("rev-parse", "HEAD")
    with store.tx() as tx:
        for tz, payload in (("TZ-1", {"commit": trunk}),
                            ("TZ-2", {"reason_code": "accepted_candidate_bytes_changed_by_unfinished"})):
            tx.record_event(kind="accept", project="P", tz=tz, payload={})
            tx.record_event(kind="integrate", project="P", tz=tz, payload=payload)
    store.close()
    out = tool.report(tmp_path / "fleet.sqlite")
    assert out["by_class"] == {"snapshot / committed / True": 1, "snapshot / drift / None": 1}


def test_project_root_inside_repository_subfolder(tmp_path):
    # 04.11 вживую: Main Hub — C:\hub\_todo\hub-fleet, подпапка репозитория пульта.
    root, git = _repo(tmp_path)
    sub = root / "Src"
    store = _store(tmp_path, sub)
    with store.tx() as tx:
        tx.record_event(kind="take", worker_id="W1", project="P", tz="TZ-1", payload={"claim_id": 5})
        tx.record_event(kind="write", worker_id="W1", project="P", tz="TZ-1",
                        path_norm=norm_path(str(sub / "App.py")))
    (sub / "App.py").write_text("sub\n", encoding="utf-8")
    result = snapshot.submission_shadow(store, "P", "TZ-1", "W1", 5)
    assert result["paths"] == ["Src/App.py"] and git("show", result["commit"] + ":Src/App.py") == "sub"


def test_project_outside_git_is_skipped(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    store = _store(tmp_path, plain)
    with store.tx() as tx:
        tx.record_event(kind="take", worker_id="W1", project="P", tz="TZ-1", payload={"claim_id": 5})
    assert snapshot.submission_shadow(store, "P", "TZ-1", "W1", 5)["skipped"] == "проект не в git"
