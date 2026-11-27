"""Read-only night review pilot: real Git, rotation, report and agent budget."""
from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet import nightly_review as nr, project_review, queue, quick, review
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store


def git(root: Path, *words: str) -> str:
    return subprocess.run(["git", "-C", str(root), *words], check=True,
                          text=True, capture_output=True, encoding="utf-8").stdout


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "app").mkdir()
    (path / "app" / "one.py").write_text("x = 1\n" * 20, encoding="utf-8")
    (path / "app" / "two.py").write_text("y = 2\n" * 20, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "Initial")
    return path


@pytest.fixture(autouse=True)
def fixed_review_zone(monkeypatch):
    # These scheduling cases use the previous MSK window regardless of host TZ.
    monkeypatch.setenv("DEV_MCP_NIGHTLY_TZ_OFFSET", "3")


def night() -> datetime:
    return datetime(2026, 11, 17, 2, 30, tzinfo=timezone(timedelta(hours=3)))


def test_nightly_lock_waits_for_the_running_scheduler(tmp_path: Path) -> None:
    path = tmp_path / "_drive.lock"
    attempting = threading.Event()
    arrived = threading.Event()

    def contender() -> None:
        attempting.set()
        try:
            with nr._locked(path):
                pass
        finally:
            arrived.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with nr._locked(path):
            other = pool.submit(contender)
            assert attempting.wait(2)
            assert not arrived.wait(0.2)
        other.result(timeout=3)


def project_fixture(tmp_path: Path) -> tuple[Path, Store]:
    root = repo(tmp_path / "sample")
    (root / "requirements.md").write_text("The command must preserve a user's changes.\n", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    (docs / "old.md").write_text("Replaced command instructions.\n", encoding="utf-8")
    (docs / "guide.md").write_text("Current command instructions.\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Requirements and documentation")
    data = tmp_path / "state"
    data.mkdir()
    store = Store.open(str(data / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("sample", str(root)), event={"kind": "test_setup"})
        stamp = datetime.now(timezone.utc).isoformat()
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,'chatgpt',?,?)",
                  ("W1", "worker", stamp, stamp), event={"kind": "test_setup"})
    return root, store


def review_finding() -> dict:
    return {
        "severity": "high", "kind": "issue", "key": "preserve-edits",
        "location": "app/one.py:1", "source": "requirements.md:1",
        "summary": "Preserve user edits on the normal command path",
        "evidence": "The implementation drops the edits promised in the requirement.",
        "verification": "Run the normal command on an edited file and compare its content.",
        "owns": ["app/one.py"],
    }


def test_review_reads_queue_while_a_worker_is_writing(tmp_path: Path) -> None:
    root, store = project_fixture(tmp_path)
    with store.tx() as tx:
        tx.record_event(kind="test_write_in_progress", project=root.name)
        assert "Живая очередь" in nr._project_context(tmp_path / "state", root.name)


def test_nightly_gap_registers_once_and_uses_normal_work_and_review(tmp_path: Path) -> None:
    root, store = project_fixture(tmp_path)
    data = tmp_path / "state"
    reports = tmp_path / "reports"
    kwargs = dict(now=night().replace(hour=3), data=data, reports=reports, mode="gap",
                  publish_tasks=True)
    initial = nr.drive(["sample"], **kwargs)["sample"]
    assert initial == {"status": "running", "launched": 1, "total": 1}
    book = quick.Book(data)
    state_path = nr.state_path(data, "sample", "gap")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for entry in state["jobs"]:
        chunk = state["chunks"][entry["index"]]
        checked = [f"{i['path']}:L{i['start']}-L{i['end']}" for i in chunk["files"]]
        agent = book.jobs()[entry["id"]]["tag"]
        quick.exchange(book, agent, json.dumps({"findings": [review_finding()], "checked": checked}), hold_s=0)
    nr.drive(["sample"], **kwargs)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    candidate = state["proposals"][0]
    final = book.jobs()[state["summary_job"]]
    assert candidate["source_quick"] != final["id"]
    quick.exchange(book, final["tag"], json.dumps(
        {"findings": [], "approved": [candidate["id"], "invented-id"], "dismissed": {}}), hold_s=0)
    result = nr.drive(["sample"], **kwargs)["sample"]
    assert result["status"] == "complete"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    tz = state["actions"][0]["result"]
    assert tz in Path(result["report"]).read_text(encoding="utf-8")
    assert project_review.publish(store, "sample", root, "gap", [candidate],
                                  [candidate["id"]])[0]["status"] == "already_registered"
    with store.read() as tx:
        work = tx.one("SELECT * FROM tasks WHERE project='sample'")
        assert work["tz"] == tz and work["state"] == "ready"
        assert "Run the normal command" in review._review_body(work)
        assert json.loads(work["acceptance"]) == []
        assert json.loads(work["owns"]) == [norm_path(str(root / "app/one.py"))]
    assert queue.take_next(store, "W1", "sample").tz == tz
    assert not git(root, "status", "--porcelain").strip()


def test_document_action_reserves_archive_and_respects_active_work_and_preimage(tmp_path: Path) -> None:
    root, store = project_fixture(tmp_path)
    sha = git(root, "rev-parse", "HEAD").strip()
    finding = {**review_finding(), "kind": "archive", "key": "old-instructions",
               "location": "docs/old.md:1", "source": "docs/guide.md:1",
               "owns": ["docs/old.md"], "revision": sha, "source_quick": "q1", "id": "checked"}

    def publish(item: dict) -> dict:
        return project_review.publish(store, "sample", root, "docs", [item], ["checked"])[0]

    with store.tx() as tx:
        stamp = datetime.now(timezone.utc).isoformat()
        tx.change("INSERT INTO tasks(project,tz,title,state,kind,owns,created_at,updated_at)"
                  " VALUES(?,'TZ-existing','other','frozen','work',?,?,?)",
                  ("sample", json.dumps([norm_path(str(root / "docs"))]), stamp, stamp),
                  event={"kind": "test_setup"})
    assert publish(finding)["status"] == "overlap"
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='TZ-existing'", (),
                  event={"kind": "test_setup"})
    old = root / "docs/old.md"
    original = old.read_bytes()
    old.write_bytes(b"changed during review")
    assert publish(finding)["status"] == "needs_evidence"
    old.write_bytes(original)
    assert publish({**finding, "owns": ["../outside.md"]})["status"] == "needs_evidence"
    result = publish(finding)
    assert result["status"] == "registered"
    with store.read() as tx:
        task = tx.one("SELECT * FROM tasks WHERE tz=?", (result["result"],))
        assert norm_path(str(root / "docs/archive/review/docs/old.md")) in json.loads(task["owns"])
    assert old.read_bytes() == original and not (root / "docs/archive").exists()


def test_docs_rotation_survives_archive_without_skipping_next_folder(tmp_path: Path) -> None:
    root = repo(tmp_path / "sample")
    for folder in ("aaa", "bbb", "ccc", "ddd"):
        (root / folder).mkdir()
        (root / folder / "note.md").write_text("notes\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "project docs")
    first, cursor = nr.plan(root, mode="docs")
    assert [chunk["directory"] for chunk in first] == ["aaa", "bbb", "ccc"]
    git(root, "rm", "bbb/note.md")
    git(root, "commit", "-qm", "Archive bbb")
    second, _ = nr.plan(root, mode="docs", cursor=cursor)
    assert second[0]["directory"] == "ddd"


def test_git_numstat_and_rotation_are_bounded(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    (root / "app" / "one.py").write_text("x = 3\n" * 1100, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "big edit")
    chunks, cursor = nr.plan(root, cursor=0)
    changed = [x for x in chunks if x["kind"] == "changed"]
    assert changed and sum(x["changed_lines"] for x in changed) >= 1100
    assert all(0 < x["changed_lines"] <= nr.MAX_LINES for x in changed)
    assert all(x["directory"] == "app" for x in changed)
    assert len([x for x in chunks if x["kind"] == "rotation"]) == 1
    rotations = chunks[-1]["files"]
    assert sum(item["weight"] for item in rotations) <= 800
    assert all(item.get("start") and item.get("end") for item in rotations)
    next_chunks, next_cursor = nr.plan(root, cursor=cursor)
    assert next_chunks[-1]["files"] != rotations
    assert cursor != next_cursor


def test_dry_has_no_state_no_jobs_and_no_project_writes(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    data = tmp_path / "data"
    before = git(root, "status", "--porcelain")
    result = nr.drive(["demo"], now=night(), dry=True, roots={"demo": root},
                      data=data, reports=tmp_path / "reports")
    assert result["demo"]["chunks"]
    assert not data.exists()
    assert git(root, "status", "--porcelain") == before


def test_scope_and_six_agent_global_budget(tmp_path: Path) -> None:
    one = repo(tmp_path / "first")
    two = repo(tmp_path / "second")
    # Enough changed chunks in both projects to force scheduler admission.
    for root in (one, two):
        for i in range(4):
            path = root / f"dir-{i}"
            path.mkdir()
            (path / "module.py").write_text(f"n = {i}\n" * 30, encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "Many dirs")
    data = tmp_path / "state"
    roots = {"A": one, "B": two}
    result = nr.drive(["A", "B"], now=night(), roots=roots, data=data,
                      reports=tmp_path / "reports")
    jobs = quick.Book(data).jobs()
    active = [j for j in jobs.values() if j["state"] != "closed"]
    assert result["A"]["status"] == result["B"]["status"] == "running"
    assert len(active) == 6
    assert all(j["owner"].startswith("nightly:") for j in active)
    for job in active:
        root = roots[job["project"]]
        assert quick.scope_denial(job, str(root / "app" / "one.py").replace("\\", "/"))
        assert str(root) not in job["scope"]
        assert "НЕ ПРАВЬ" in job["prompt"]
    nr.drive(["A", "B"], now=night(), roots=roots, data=data,
             reports=tmp_path / "reports")
    assert len(quick.Book(data).jobs()) == 6


def test_complete_chain_closes_jobs_and_writes_only_outside_repo(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    data = tmp_path / "data"
    reports = tmp_path / "reports"
    project = "sample"
    kwargs = dict(now=night(), roots={project: root}, data=data, reports=reports)
    baseline = git(root, "status", "--porcelain")
    state = nr.drive([project], **kwargs)
    assert state[project]["status"] == "running"
    book = quick.Book(data)
    answer = json.dumps({"findings": [{"severity": "high", "kind": "bug",
                  "location": "app/one.py:1", "summary": "broken precondition",
                  "evidence": "the function returns None"}]})
    for job in list(book.jobs().values()):
        assert job["owner"] == "nightly:" + project
        quick.exchange(book, job["tag"], answer + job["id"], hold_s=0)
    nr.drive([project], **kwargs)
    state = json.loads(nr.state_path(data, project).read_text(encoding="utf-8"))
    assert all(book.jobs()[j["id"]]["state"] == "closed" for j in state["jobs"])
    assert state["summary_job"]
    final = book.jobs()[state["summary_job"]]
    assert final["after"] == [state["jobs"][-1]["id"]]
    assert "source_agents" in final["prompt"]
    summary = json.dumps({"findings": [{"severity": "high", "kind": "bug",
                  "location": "app/one.py:1", "summary": "broken precondition",
                  "evidence": "the function returns None"}],
                  "duplicates": ["one.py:1 repeated"], "disputed": ["unknown source"]})
    quick.exchange(book, final["tag"], summary, hold_s=0)
    result = nr.drive([project], **kwargs)
    assert result[project]["status"] == "complete"
    report_path = Path(result[project]["report"])
    report = report_path.read_text(encoding="utf-8")
    assert "Топ-10" in report and "app/one.py:1" in report
    assert "Дубли" in report and "Спорное" in report
    assert len(report) < 6200
    assert quick.Book(data).jobs()[final["id"]]["state"] == "closed"
    assert not list(root.rglob("*nightly*"))
    assert git(root, "status", "--porcelain") == baseline


def test_summary_requires_evidence_and_line() -> None:
    raw = json.dumps({"findings": [
        {"severity": "critical", "location": "x.py:45", "summary": "valid", "evidence": "code shows issue"},
        {"severity": "critical", "location": "x.py", "summary": "bad", "evidence": "not precise"},
        {"severity": "high", "location": "x.py:3", "summary": "no evidence", "evidence": ""}]})
    result = nr.render_summary("P", "2026-11-17", raw)
    assert "x.py:45" in result and "x.py:3" not in result
    assert "x.py**" not in result
    assert nr.render_summary("P", "2026-11-17", "bad answer").count("невалиден") == 1


def test_nightly_middleware_enforces_read_only_and_root(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    job = {"owner": "nightly:demo", "tag": "Q1-NIGHT", "root": str(root)}
    allow = nr.allows_tool
    assert allow(job, "read_file", {"path": str(root / "app" / "one.py")})
    assert allow(job, "list_dir", {"path": str(root)})
    assert allow(job, "git_log", {"repo": str(root)})
    assert allow(job, "fleet_quick", {"agent": "Q1-NIGHT", "command": "report"})
    assert not allow(job, "fleet_quick", {"agent": "Q1-NIGHT", "command": "new"})
    assert not allow(job, "run", {"root": str(root), "argv": ["python", "-c", "pass"]})
    assert not allow(job, "write_file", {"path": str(root / "app" / "one.py")})
    assert not allow(job, "read_file", {"path": str(tmp_path / "other.py")})
    assert not allow(job, "search_files", {"query": "anything"})
    assert not allow(job, "read_file", {"path": str(root / ".env")})
    assert not allow(job, "read_file", {"path": str(root / "cfg" / "api_token.txt")})
    assert not allow(job, "read_file", {"path": str(root / "id.pem")})
    assert not allow(job, "read_images", {"paths": [str(root / "app" / "one.py"),
                                                  str(tmp_path / "other.py")]})


def test_outside_window_does_not_start_new_run(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    data = tmp_path / "data"
    at_noon = datetime(2026, 11, 17, 12, tzinfo=timezone(timedelta(hours=3)))
    result = nr.drive(["demo"], now=at_noon, roots={"demo": root},
                      data=data, reports=tmp_path / "reports")
    assert result["demo"]["status"] == "outside_window"
    assert not quick.Book(data).jobs()


def test_unfinished_night_does_not_launch_new_chunks_after_five(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    data = tmp_path / "data"
    kwargs = dict(roots={"demo": root}, data=data, reports=tmp_path / "reports")
    nr.drive(["demo"], now=night(), max_agents=1, **kwargs)
    first = len(quick.Book(data).jobs())
    at_six = datetime(2026, 11, 17, 6, tzinfo=timezone(timedelta(hours=3)))
    nr.drive(["demo"], now=at_six, **kwargs)
    assert len(quick.Book(data).jobs()) == first


def test_explicit_manual_pulse_can_start_outside_window(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    data = tmp_path / "data"
    at_noon = datetime(2026, 11, 17, 12, tzinfo=timezone(timedelta(hours=3)))
    result = nr.drive(["demo"], now=at_noon, roots={"demo": root},
                      data=data, reports=tmp_path / "reports", start_anytime=True)
    assert result["demo"]["status"] == "running"
    assert quick.Book(data).jobs()


def _findings(start: int, count: int) -> list[dict]:
    return [{"severity": "high", "kind": "bug", "location": f"app/one.py:{i}",
             "summary": f"issue {i}", "evidence": f"reproduced {i}"}
            for i in range(start, start + count)]


def test_summary_context_keeps_five_and_prioritizes_old_tail() -> None:
    class FakeBook:
        def __init__(self, findings: list[dict], url: str):
            self._jobs = {"q-first": {"last_answer": json.dumps({"findings": findings}),
                                      "url": url}}

        def jobs(self):
            return self._jobs

    first = FakeBook(_findings(1, 15), "https://chatgpt.com/c/original")
    context, pending = nr._summary_context(first, ["q-first"], day="2026-11-17")
    payload = json.loads(context)
    assert len(payload["candidates"]) == 10
    assert len(pending) == 5
    assert payload["omitted_due_to_limit"] == 5
    assert all(x["source_quick"] == "q-first" for x in pending)
    assert all(x["source_url"] == "https://chatgpt.com/c/original" for x in pending)
    assert all(x["id"] and x["location"] for x in pending)
    assert nr._pending_age(pending, "2026-11-18") == 1

    second = FakeBook(_findings(16, 12), "https://chatgpt.com/c/next")
    next_context, next_pending = nr._summary_context(
        second, ["q-first"], day="2026-11-18", carryover=pending)
    chosen = json.loads(next_context)["candidates"]
    assert [x["id"] for x in chosen[:5]] == [x["id"] for x in pending]
    assert len(chosen) == 10 and len(next_pending) == 7
    again, repeated = nr._summary_context(
        second, ["q-first"], day="2026-11-18", carryover=pending + pending)
    assert again == next_context and repeated == next_pending
    assert len({x["id"] for x in chosen + next_pending}) == 17


def test_legacy_night_recovers_unreported_answers() -> None:
    issues = _findings(1, 15)

    class FakeBook:
        def jobs(self):
            return {"q-old": {"last_answer": json.dumps({"findings": issues}),
                              "url": "https://chatgpt.com/c/old"},
                    "q-summary": {"last_answer": json.dumps({"findings": issues[:10]})}}

    legacy = nr._legacy_pending(FakeBook(), {
        "date": "2026-11-17", "jobs": [{"id": "q-old"}],
        "summary_job": "q-summary"})
    assert len(legacy) == 5
    assert {item["location"] for item in legacy} == {
        f"app/one.py:{i}" for i in range(11, 16)}
    assert all(item["source_url"] == "https://chatgpt.com/c/old" for item in legacy)


def test_pending_file_survives_night_and_repeat_does_not_duplicate(tmp_path: Path) -> None:
    root = repo(tmp_path / "project")
    data, reports, project = tmp_path / "data", tmp_path / "reports", "demo"
    kwargs = dict(roots={project: root}, data=data, reports=reports)
    book = quick.Book(data)
    nr.drive([project], now=night(), **kwargs)
    state = nr._load(nr.state_path(data, project))
    assert len(state["jobs"]) == 2
    for index, entry in enumerate(state["jobs"]):
        job = book.jobs()[entry["id"]]
        findings = _findings(1, 8) if index == 0 else _findings(9, 7)
        quick.exchange(book, job["tag"], json.dumps({"findings": findings}), hold_s=0)
    nr.drive([project], now=night(), **kwargs)
    state = nr._load(nr.state_path(data, project))
    pending_path = nr._pending_file(reports, project, "2026-11-17")
    queue = nr._load(pending_path)["candidates"]
    assert len(queue) == 5
    assert all(item["source_quick"] in [x["id"] for x in state["jobs"]] for item in queue)
    summary = book.jobs()[state["summary_job"]]
    prompt_candidates = json.loads(summary["prompt"].split("Исходные данные:\n", 1)[1])
    assert len(prompt_candidates["candidates"]) == 10
    jobs_before = len(book.jobs())
    nr.drive([project], now=night(), **kwargs)
    assert nr._load(pending_path)["candidates"] == queue
    assert len(book.jobs()) == jobs_before

    quick.exchange(book, summary["tag"], json.dumps({"findings": []}), hold_s=0)
    complete = nr.drive([project], now=night(), **kwargs)
    report = Path(complete[project]["report"]).read_text(encoding="utf-8")
    assert "Осталось 5" in report and pending_path.name in report

    tomorrow = night() + timedelta(days=1)
    nr.drive([project], now=tomorrow, **kwargs)
    state2 = nr._load(nr.state_path(data, project))
    assert len(state2["carryover"]) == 5
    for entry in state2["jobs"]:
        job = book.jobs()[entry["id"]]
        quick.exchange(book, job["tag"], json.dumps({"findings": []}), hold_s=0)
    nr.drive([project], now=tomorrow, **kwargs)
    state2 = nr._load(nr.state_path(data, project))
    second_summary = book.jobs()[state2["summary_job"]]
    second_input = json.loads(second_summary["prompt"].split("Исходные данные:\n", 1)[1])
    assert [x["id"] for x in second_input["candidates"]] == [x["id"] for x in queue]
    assert nr._load(nr._pending_file(reports, project, "2026-11-18"))["candidates"] == []
