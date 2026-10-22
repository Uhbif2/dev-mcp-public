"""TZ-266: acceptance trains run on HEAD+all sealed bytes, not one-at-a-time."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet.integrate import Integrator
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          encoding="utf-8", text=True).stdout.strip()


class Runner:
    """Real Git; only the expensive project pytest gate is substituted."""

    def __init__(self) -> None:
        self.candidates: list[tuple[str, ...]] = []

    def __call__(self, argv, **kwargs):
        args = tuple(str(a) for a in argv)
        if "pytest" in args and "--collect-only" in args:
            # This fixture contains only text files. Do not depend on pytest
            # being installed in the machine's base Python outside this venv.
            return subprocess.CompletedProcess(args, 5, "no tests collected\n", "")
        if (("-m" in args and "pytest" in args and "--collect-only" not in args)
                or any(part.endswith("verify.py") for part in args)):
            work = Path(kwargs["cwd"])
            present = tuple(sorted(f.name for f in work.glob("*.txt")
                                   if f.read_text(encoding="utf-8") != "baseline\n"))
            self.candidates.append(present)
            broken = any((work / name).read_text(encoding="utf-8") == "BROKEN\n"
                         for name in present)
            return subprocess.CompletedProcess(
                args, 1 if broken else 0,
                "FAILED tests/test_sample.py::test_regression\n" if broken else "3 passed\n",
                "",
            )
        return subprocess.run(argv, **kwargs)


@pytest.fixture()
def fleet(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fleet Test")
    git(root, "config", "user.email", "fleet@example.invalid")
    for i in range(3):
        (root / f"part{i}.txt").write_text("baseline\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    base = git(root, "rev-parse", "HEAD")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("P", str(root)), event={"kind": "setup"})
    runner = Runner()
    integrator = Integrator(
        store, tmp_path / "cursor.json", runner=runner,
        project_info=lambda _: {"commit": True, "gate": "py -3 -m pytest"},
    )
    assert integrator.tick() == []
    # The separate HEAD gate is not the candidate gate; assert it is invoked
    # only once after all accepted changes have landed on trunk.
    heads: list[str] = []
    integrator._gate = lambda batch: heads.append(git(root, "rev-parse", "HEAD")) or None
    return root, store, integrator, runner, heads, base


def accepted(store: Store, root: Path, files: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for i, name in enumerate(files):
        tz = f"TZ-{i}"
        with store.tx() as tx:
            tx.change(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
                "spec_sha,needs,owns,resources,acceptance,author_worker,body,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("P", tz, tz, "accepted", 5, "work", "", "", "[]",
                 json.dumps([norm_path(str(root / name))]), "[]", "[]",
                 "W-author", "test", now, now), event={"kind": "setup"},
            )
            tx.record_event(kind="submit", project="P", tz=tz, worker_id="W-author")
            submit_id = int(tx.scalar("SELECT MAX(id) FROM events"))
            digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
            tx.record_event(
                kind="submission_file_seal", project="P", tz=tz, tool="fleet",
                payload={"submit_id": submit_id, "files": {name: digest}},
            )
            tx.record_event(kind="accept", project="P", tz=tz, worker_id="W-reviewer")


def test_three_green_acceptances_one_candidate_three_attributed_commits(fleet):
    root, store, integ, runner, heads, base = fleet
    for i in range(3):
        (root / f"part{i}.txt").write_text(f"approved-{i}\n", encoding="utf-8")
    accepted(store, root, [f"part{i}.txt" for i in range(3)])
    results = integ.tick()

    assert len(runner.candidates) == 1, json.dumps(results, ensure_ascii=False)
    assert runner.candidates == [("part0.txt", "part1.txt", "part2.txt")]
    commits = [e["payload"]["commit"] for e in results
               if e["kind"] == "integrate" and e["payload"].get("commit")]
    assert len(commits) == 3
    assert git(root, "rev-list", "--count", f"{base}..HEAD") == "3"
    assert len(heads) == 1 and heads[0] == git(root, "rev-parse", "HEAD")
    for i in range(3):
        assert f"Fleet-Task: P/TZ-{i}" in git(root, "log", "-1", "--format=%B", f"HEAD~{2-i}")


def test_one_bad_is_deferred_with_failure_and_two_good_commits(fleet):
    root, store, integ, runner, heads, base = fleet
    for i in range(3):
        (root / f"part{i}.txt").write_text(
            "BROKEN\n" if i == 1 else f"approved-{i}\n", encoding="utf-8")
    accepted(store, root, [f"part{i}.txt" for i in range(3)])
    results = integ.tick()

    assert len(runner.candidates) <= 4, runner.candidates
    by_tz = {e["tz"]: e["payload"] for e in results if e["kind"] == "integrate"}
    assert by_tz["TZ-0"].get("commit"), by_tz["TZ-0"]
    assert by_tz["TZ-2"].get("commit")
    assert by_tz["TZ-1"]["reason_code"] == "batch_candidate_failed"
    assert "test_regression" in by_tz["TZ-1"]["reason"]
    assert git(root, "rev-list", "--count", f"{base}..HEAD") == "2"
    assert git(root, "show", "HEAD:part1.txt") == "baseline"
    assert (root / "part1.txt").read_text(encoding="utf-8") == "BROKEN\n"
    assert len(heads) == 1


def test_unfinished_foreign_write_is_not_part_of_shared_train(fleet):
    root, store, integ, runner, heads, base = fleet
    for i in range(3):
        (root / f"part{i}.txt").write_text(f"approved-{i}\n", encoding="utf-8")
    accepted(store, root, [f"part{i}.txt" for i in range(3)])
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
            "spec_sha,needs,owns,resources,acceptance,author_worker,body,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P", "TZ-WIP", "unfinished dependency", "taken", 5, "work",
             "", "", "[]", json.dumps([norm_path(str(root / "part1.txt"))]),
             "[]", "[]", "W-other", "test", now, now),
            event={"kind": "setup"},
        )
        tx.record_event(kind="write", project="P", tz="TZ-WIP",
                        worker_id="W-other",
                        path_norm=norm_path(str(root / "part1.txt")))
    output = integ.tick()
    by_tz = {e["tz"]: e["payload"] for e in output if e["kind"] == "integrate"}

    assert runner.candidates == [("part0.txt", "part2.txt")], output
    assert by_tz["TZ-0"].get("commit") and by_tz["TZ-2"].get("commit")
    assert by_tz["TZ-1"]["waiting_for"] == ["TZ-WIP"]
    assert git(root, "rev-list", "--count", f"{base}..HEAD") == "2"
    assert git(root, "show", "HEAD:part1.txt") == "baseline"
    assert len(heads) == 1


def test_non_pytest_project_gate_also_validates_a_single_shared_train(fleet):
    root, store, integ, runner, heads, base = fleet
    (root / "verify.py").write_text("print('project check')\n", encoding="utf-8")
    git(root, "add", "verify.py")
    git(root, "commit", "-q", "-m", "project validator")
    integ.project_info = lambda _: {"commit": True, "gate": "py -3 verify.py"}
    for i in range(3):
        (root / f"part{i}.txt").write_text(f"approved-{i}\n", encoding="utf-8")
    accepted(store, root, [f"part{i}.txt" for i in range(3)])
    events = integ.tick()

    assert runner.candidates == [("part0.txt", "part1.txt", "part2.txt")]
    assert sum(bool(e.get("payload", {}).get("commit")) for e in events) == 3
    assert git(root, "rev-list", "--count", f"{base}..HEAD") == "4"
    assert len(heads) == 1
