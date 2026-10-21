"""Controller→TZ-48 integration: trusted worker consumes real git and emits TZ-38 draft."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dev_mcp.fleet.controller import (
    InterventionRuntime, _intervention_cycle, _trusted_intervention_runtime,
    run_intervention_worker,
)
from dev_mcp.fleet.store import Store


def test_controller_intervention_worker_real_git_draft_and_cursor(tmp_path: Path) -> None:
    root = tmp_path / "platform"
    root.mkdir()
    def git(*args: str) -> str:
        response = subprocess.run(
            ["git", "-C", str(root), *args], text=True,
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        assert response.returncode == 0, response.stderr
        return response.stdout.strip()

    git("init", "-b", "master")
    git("config", "user.email", "operator@example.invalid")
    git("config", "user.name", "Operator")
    (root / "owned.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "initial baseline")
    integrator_sha = git("rev-parse", "HEAD")
    git("branch", "platform/work")
    git("switch", "platform/work")
    (root / "owned.txt").write_text("manual correction\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "secret-data-NEVER-COPY")
    manual_sha = git("rev-parse", "HEAD")

    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES(?,?,1)",
                  ("Fleet Platform", str(root)), event="seed")
        tx.record_event(kind="integrate", payload={"commit": integrator_sha})

    (tmp_path / "audit.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "guard-approved-commits.txt").write_text("", encoding="utf-8")
    (tmp_path / "platform-guard.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "LESSONS.md").write_text("# empty fixture\n", encoding="utf-8")
    runtime = InterventionRuntime(
        draft_root=root, repositories=(root,),
        audit_path=tmp_path / "audit.jsonl",
        approvals_path=tmp_path / "guard-approved-commits.txt",
        guard_log=tmp_path / "platform-guard.jsonl",
        lessons_path=tmp_path / "LESSONS.md",
    )
    assert _trusted_intervention_runtime(store.path) is None

    class SingleTick:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, _seconds):
            self.stopped = True

    # This is the actual entrypoint invoked by the production daemon, not an
    # isolated call to InterventionLedger.ingest or a mocked git subprocess.
    run_intervention_worker(store, SingleTick(), runtime)
    with store.read() as tx:
        rows = tx.query("SELECT * FROM intervention_log WHERE project='Fleet Platform'")
        signals = tx.query("SELECT kind,payload FROM events "
                           "WHERE kind IN ('intervention','intervention_scan')")
    assert len(rows) == 1
    assert rows[0]["source"] == "git"
    assert rows[0]["identifier"] == manual_sha
    assert rows[0]["class"] == "manual-commit"
    assert any(event["kind"] == "intervention_scan"
               and json.loads(event["payload"])["new"] == 1 for event in signals)
    drafts = list((root / "ToDo" / "tz-drafts").glob("TZ-*-intervention-manual-commit.md"))
    assert len(drafts) == 1
    body = drafts[0].read_text(encoding="utf-8")
    assert "Убрать необходимость в ручном шаге" in body
    assert "acceptance:" in body
    assert "secret-data-NEVER-COPY" not in body
    assert integrator_sha not in body and manual_sha not in body

    again = _intervention_cycle(store, runtime)
    assert again["new"] == 0
    assert len(list((root / "ToDo" / "tz-drafts").glob("TZ-*.md"))) == 1
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM intervention_log") == 1
        scan = tx.one("SELECT payload FROM events WHERE kind='intervention_scan' "
                      "ORDER BY id DESC LIMIT 1")
    since = datetime.fromisoformat(json.loads(scan["payload"])["since"])
    assert since.tzinfo is not None and since <= datetime.now(timezone.utc)


def test_server_launcher_wires_intervention_worker_only_with_trusted_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    import threading
    from dev_mcp.fleet import controller as controller_module

    store = Store.open(str(tmp_path / "fleet.sqlite"))
    runtime = InterventionRuntime(
        draft_root=tmp_path, repositories=(tmp_path,),
        audit_path=tmp_path / "audit.jsonl",
        approvals_path=tmp_path / "approved.txt",
        guard_log=tmp_path / "guard.jsonl",
        lessons_path=tmp_path / "lessons.md",
    )
    started: list[tuple[str, object]] = []

    class StubThread:
        def __init__(self, *, target, args, daemon, name):
            assert daemon is True
            self.target = target
            self.name = name
        def start(self):
            started.append((self.name, self.target))

    stopped = threading.Event()
    stopped.set()  # Exercise startup without running the main maintenance loop.
    monkeypatch.setattr(controller_module.threading, "Thread", StubThread)
    monkeypatch.setattr(controller_module, "_trusted_intervention_runtime",
                        lambda _db: runtime)
    controller_module.run_controller(store.path, stopped)
    assert ("dev-mcp-intervention-ledger", controller_module.run_intervention_worker) in started

    started.clear()
    monkeypatch.setattr(controller_module, "_trusted_intervention_runtime",
                        lambda _db: None)
    controller_module.run_controller(store.path, stopped)
    assert not any(name == "dev-mcp-intervention-ledger" for name, _ in started)
