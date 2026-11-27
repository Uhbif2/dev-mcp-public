"""W10-1: long Windows Git path lists, sealed candidate scripts, bounded retries, status."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from dev_mcp.fleet.integrate import Batch, Integrator
from dev_mcp.fleet.report import uncommitted_accepts
from dev_mcp.fleet.store import Store


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8",
    ).stdout


def setup(tmp_path: Path) -> tuple[Path, Store, Integrator]:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "fleet@example.invalid")
    git(root, "config", "user.name", "Fleet")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    store = Store.open(str(tmp_path / "events.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("P", str(root)), event={"kind": "test_setup"})
    # Do not route synthetic fixture commands via the live resource scheduler.
    integ = Integrator(store, tmp_path / "cursor.json",
                       runner=lambda *args, **kwargs: subprocess.run(*args, **kwargs),
                       project_info=lambda _p: {"commit": True, "retry_deferred": False})
    return root, store, integ


def test_git_700_pathspecs_status_diff_add_commit_without_long_argv(tmp_path):
    root, _, integ = setup(tmp_path)
    paths = [f"case_{index:04d}_" + ("path_segment_" * 5) + ".txt"
             for index in range(700)]
    for name in paths:
        (root / name).write_text("before\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "many paths")
    for name in paths:
        (root / name).write_text("after\n", encoding="utf-8")

    observed: list[list[str]] = []
    original = integ.runner

    def inspect(argv, **kwargs):
        assert len(subprocess.list2cmdline(argv)) < 32767
        observed.append(list(argv))
        if any(v.startswith("--pathspec-from-file=") for v in argv):
            named = next(v.split("=", 1)[1] for v in argv
                         if v.startswith("--pathspec-from-file="))
            assert Path(named).read_bytes().split(b"\0")[:-1] == [
                p.encode("utf-8") for p in paths
            ]
        return original(argv, **kwargs)

    integ.runner = inspect
    status = integ._git(str(root), "status", "--porcelain", "-z", "-uall", "--", *paths)
    assert status.returncode == 0, status.stderr
    assert len([s for s in status.stdout.split("\0") if s]) == 700
    added = integ._git(str(root), "add", "-A", "--", *paths)
    assert added.returncode == 0, added.stderr
    staged = integ._git(str(root), "diff", "--cached", "--name-only", "-z", "--", *paths)
    assert staged.returncode == 0, staged.stderr
    assert len([s for s in staged.stdout.split("\0") if s]) == 700
    committed = integ._git(str(root), "commit", "-q", "--no-verify", "-m", "W10-1 fixture",
                           "--", *paths)
    assert committed.returncode == 0, committed.stderr
    assert not git(root, "status", "--porcelain")
    assert sum(any(v.startswith("--pathspec-from-file=") for v in cmd)
               for cmd in observed) == 2  # add + commit; status/diff are chunked
    assert len(observed) >= 6  # at least two chunks for both status and diff


def test_candidate_missing_script_gets_operator_block_not_endless_retry(tmp_path):
    root, store, integ = setup(tmp_path)
    (root / "tool.py").write_text("print('not accepted')\n", encoding="utf-8")
    (root / "notes.md").write_text("accepted\n", encoding="utf-8")
    ok, reason = integ._candidate(str(root), ["notes.md"], "P",
                                  [f'"{sys.executable}" tool.py'])
    assert not ok and reason.startswith("missing_acceptance_script:"), reason
    assert "только в общем дереве" in reason
    with store.tx() as tx:
        tx.record_event(kind="accept", project="P", tz="TZ-script")
    with store.read() as tx:
        aid = int(tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'"))
    integ._candidate_failed(Batch("P", str(root)), "TZ-script", ["notes.md"],
                            {"accept_id": aid}, reason)
    with store.read() as tx:
        row = tx.one("SELECT payload FROM events WHERE kind='integrate' ORDER BY id DESC LIMIT 1")
    payload = json.loads(row["payload"])
    assert payload["operator_required"]
    assert payload["reason_code"] == "missing_acceptance_script"
    assert integ._pending_accepts() == []


def test_retry_disabled_does_not_suppress_transient_and_caps_attempts(tmp_path, monkeypatch):
    root, store, integ = setup(tmp_path)
    with store.tx() as tx:
        tx.record_event(kind="accept", project="P", tz="TZ-retry")
    with store.read() as tx:
        aid = int(tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'"))
    original = {"accept_id": aid, "deferred": ["base.txt"],
                "reason_code": "validation_timeout", "reason": "validation_timeout"}
    integ._record(kind="integrate", project="P", tz="TZ-retry", payload=dict(original))
    assert integ._pending_accepts() == []  # backoff honored; no busy-loop
    # Event-trigger wakeup works even with retry_deferred=false.
    integ._refresh_latest()
    integ._latest[("P", aid)][2]["retry"]["next_at"] = 0
    assert [row["id"] for row in integ._pending_accepts()] == [aid]
    # Fingerprint-stable repeated timeouts eventually stop and notify operator.
    import dev_mcp.fleet.integrate as integration
    monkeypatch.setattr(integration, "MAX_AUTO_RETRY_ATTEMPTS", 3)
    for _ in range(2):
        integ._refresh_latest()
        event = integ._record(kind="integrate", project="P", tz="TZ-retry",
                              payload=dict(original))
    assert event["payload"]["retry"]["attempt"] == 3
    assert event["payload"]["retry_exhausted"]
    assert event["payload"]["operator_required"]
    assert integ._pending_accepts() == []


def test_uncommitted_report_shows_kind_retry_operator_and_terminal(tmp_path):
    root, store, integ = setup(tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    future = now.timestamp() + 600
    with store.tx() as tx:
        for i, payload in enumerate([
            {"deferred": ["x.py"], "reason_code": "resource_busy",
             "retry": {"next_at": future}, "reason": "resource_busy"},
            {"deferred": ["x.py"], "reason_code": "accepted_candidate_bytes_changed",
             "operator_required": True,
             "reason": "shared worktree differs from accepted submission"},
            {"skipped": "принятые байты не запечатаны; ревалидация невозможна",
             "reason_code": "missing_trusted_submission_file_seal"},
        ]):
            tz = f"TZ-{i}"
            tx.record_event(kind="accept", project="P", tz=tz, ts=old)
            tx.record_event(kind="integrate", project="P", tz=tz, payload=payload, ts=old)
    lines = uncommitted_accepts(store, now=now, commit_projects={"P"})
    assert len(lines) == 1
    assert "busy; повтор в " in lines[0]
    assert "drift; ждёт оператора" in lines[0]
    assert "unsealed; окончательно" in lines[0]
