"""Интеграция после приёмки: коммит своих путей, гейт на пачку, одна задача «стык» на красный гейт."""
from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.integrate import (  # noqa: E402
    Batch, Integrator, _base_tz, _porcelain_files, _styk_review_gate,
)
from dev_mcp.fleet.store import Store  # noqa: E402
from dev_mcp.fleet.model import norm_path  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@pytest.fixture(autouse=True)
def _no_inherited_pythonpath(monkeypatch):
    """Урок 53: проверка кандидата у интегратора гонит pytest с PYTHONPATH=<снимок>/src;<снимок>;
    ключ гейта сканирует его — тесты мемо и параллельности краснели только там, и ни одна
    правка Fleet Platform не проходила интеграцию. Нужный тесту PYTHONPATH он задаёт сам."""
    monkeypatch.delenv("PYTHONPATH", raising=False)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


@pytest.fixture()
def env(tmp_path: Path):
    root = tmp_path / "Proj"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    rootn = str(root).replace("\\", "/").casefold()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            ("P", str(root)),
            event={"kind": "test_setup"},
        )
    return store, root, rootn, tmp_path


def add_task(store: Store, tz: str, owns: list[str]) -> None:
    stamp = now()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P", tz, f"title {tz}", "accepted", 5, "work", "", "", "[]", json.dumps(owns), "[]", "[]",
             None, "body", stamp, stamp),
            event={"kind": "test_setup"},
        )


def test_previously_disabled_commit_retries_only_after_permission_restored(env):
    store, root, rootn, temp = env
    with store.tx() as tx:
        tx.record_event(kind='accept',project='P',tz='TZ-old',worker_id='reviewer')
    with store.read() as tx:
        aid = tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'")
    with store.tx() as tx:
        tx.record_event(kind='integrate',project='P',tz='TZ-old',payload={
            'accept_id':aid,'skipped':'коммит не разрешён проектом (fleet-projects.toml: commit = true)'})
    integrator = Integrator(store,temp/'cursor.json',project_info=lambda p: {})
    assert integrator._pending_accepts() == []
    integrator.project_info = lambda p: {'commit':True}
    assert [r['id'] for r in integrator._pending_accepts()] == [aid]
    with store.tx() as tx:
        tx.record_event(kind='integrate',project='P',tz='TZ-old',payload={'accept_id':aid,'commit':'newsha'})
    assert integrator._pending_accepts() == []


def test_pending_retries_rotate_projects_instead_of_one_project_hogging_batch(env):
    store, root, rootn, temp = env
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('Quiet',?,1)",
                  (str(temp/'quiet'),),event='test.seed')
        for project, tz in [('P','TZ-a'),('P','TZ-b'),('P','TZ-c'),('Quiet','TZ-q')]:
            tx.record_event(kind='accept',project=project,tz=tz,worker_id='reviewer')
            aid=int(tx.scalar('SELECT MAX(id) FROM events'))
            tx.record_event(kind='integrate',project=project,tz=tz,payload={
                'accept_id':aid,'deferred':['file.py']})
    integrator=Integrator(store,temp/'cursor.json')
    pending=integrator._pending_accepts()
    assert [r['project'] for r in pending[:2]] == ['P','Quiet']
    with store.tx() as tx:
        tx.record_event(kind='integrate',project='P',tz=pending[0]['tz'],payload={
            'accept_id':pending[0]['id'],'deferred':['file.py']})
    assert integrator._pending_accepts()[0]['project'] == 'Quiet'


def event(store: Store, kind: str, tz: str, **kw) -> None:
    if kind == "accept":
        # Fixture: trusted submit-side seal of the *submitted* bytes. Production
        # legacy accepts without this immutable receipt now defer by design.
        with store.read() as tx:
            root = Path(tx.one("SELECT root FROM projects WHERE name='P'")["root"])
            tasks = tx.query("SELECT tz,owns FROM tasks WHERE project='P'")
            writable = set()
            base = _base_tz(tz)
            for task in tasks:
                if _base_tz(str(task["tz"])) == base:
                    writable.update(str(x).casefold().replace("\\", "/")
                                    for x in json.loads(task["owns"] or "[]"))
            writes = tx.query("SELECT tz,path_norm FROM events WHERE project='P' AND kind='write'")
            writable.update(str(e["path_norm"]) for e in writes
                            if _base_tz(str(e["tz"])) == base)
        status = subprocess.run(
            ["git", "status", "--porcelain", "-z", "-uall"], cwd=root,
            capture_output=True, text=True, check=True,
        ).stdout
        paths = _porcelain_files(status)
        rootn = str(root).replace("\\", "/").casefold().rstrip("/")
        files = {}
        for path in paths:
            absolute = f"{rootn}/{path.casefold().replace(chr(92), '/')}"
            if not any(absolute == own or absolute.startswith(own.rstrip('/') + '/')
                       for own in writable):
                continue
            file = root / path
            files[path] = hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None
        with store.tx() as tx:
            tx.record_event(kind="submit", project="P", tz=tz, worker_id="W1")
            submit_id = int(tx.scalar("SELECT MAX(id) FROM events"))
            tx.record_event(kind="submission_file_seal", project="P", tz=tz,
                            tool="fleet", payload={"submit_id": submit_id, "files": files})
    with store.tx() as tx:
        tx.record_event(kind=kind, project="P", tz=tz, worker_id="W1", **kw)


def events(store: Store, kind: str) -> list[dict]:
    with store.tx() as tx:
        rows = tx.query("SELECT tz,rc,payload FROM events WHERE kind=? ORDER BY id", (kind,))
    return [{"tz": r["tz"], "rc": r["rc"], **json.loads(r["payload"] or "{}")} for r in rows]


def make(store: Store, tmp_path: Path, gate: str = "") -> Integrator:
    integ = Integrator(store, tmp_path / "integrate-state.json",
                       project_info=lambda p: {"commit": True, **({"gate": gate} if gate else {})})
    assert integ.tick() == []  # первый проход только ставит курсор
    return integ


def test_missing_project_gate_disables_memo_instead_of_crashing(env):
    from dev_mcp.fleet.integrate import Batch
    store, root, _, temp = env
    assert make(store, temp)._gate_key(Batch("P", str(root)), "") == ""


def test_candidate_without_project_gate_still_runs_task_acceptance(env):
    store, root, _, temp = env
    failed = root / "failed_acceptance.py"
    failed.write_text("raise SystemExit(7)\n", encoding="utf-8")
    ok, reason = make(store, temp)._candidate(
        str(root), [failed.name], "P", [f'"{sys.executable}" failed_acceptance.py'])
    assert not ok
    assert reason.startswith("acceptance:"), reason


def test_historical_incomplete_seal_opens_one_independent_revalidation(env):
    from dev_mcp.fleet import goals, queue, review
    from unittest.mock import patch

    store, root, _, temp = env
    target = root / "ToDo" / "historical.md"
    own = norm_path(str(target))
    add_task(store, "TZ-historical", [own])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE tz='TZ-historical'", event="setup")
        for worker in ("W-H", "W-review"):
            tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES(?,?,?)",
                      (worker, now(), now()), event="setup")
    integ = make(store, temp)
    with patch.object(goals, "project_info", return_value={"commit": True}):
        queue.take_next(store, "W-H", "P")
        target.parent.mkdir(exist_ok=True)
        target.write_text("unsealed historical report\n", encoding="utf-8")
        queue.submit(store, "W-H", "historically incomplete receipt")
    # Simulate the old server receipt which omitted the report's bytes.
    with store.tx() as tx:
        row = tx.one("SELECT id,payload FROM events WHERE kind='submission_file_seal'")
        data = json.loads(row["payload"])
        data["files"] = {}
        data.pop("scope", None)
        tx.change("UPDATE events SET payload=? WHERE id=?", (json.dumps(data), row["id"]), event="legacy_fixture")
    review.open_review(store, "P", "TZ-historical")
    queue.take_next(store, "W-review", "P")
    review.decide(store, "W-review", "accept", "independent historical approval")
    out = integ.tick()
    assert any(e["payload"].get("reason_code") == "missing_trusted_submission_file_seal" for e in out)
    with store.read() as tx:
        tasks = tx.query("SELECT * FROM tasks WHERE tz LIKE 'revalidate:%'")
        assert len(tasks) == 1 and tasks[0]["state"] == "ready"
        assert own in json.loads(tasks[0]["owns"])
        assert "НОВОЕ независимое ревью" in tasks[0]["body"]
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-historical'") == "accepted"
    integ.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT count(*) FROM tasks WHERE tz LIKE 'revalidate:%'") == 1


def test_repeated_accepts_of_one_task_open_a_single_revalidation(env):
    """04.11: петля fix:TZ-LEAD:3 — сотни приёмок одной задачи; commit=true открыл
    revalidate на каждую. Ревалидируется последняя приёмка, и только один раз."""
    from dev_mcp.fleet.integrate import Batch

    store, root, _, temp = env
    target = root / "ToDo" / "loop.md"
    add_task(store, "fix:TZ-loop:3", [norm_path(str(target))])
    accepts = []
    with store.tx() as tx:
        for _ in range(3):
            tx.record_event(kind="accept", project="P", tz="fix:TZ-loop:3", worker_id="W-R")
            accepts.append({"id": int(tx.scalar("SELECT MAX(id) FROM events")),
                            "tz": "fix:TZ-loop:3"})
    integ = make(store, temp)
    batch = Batch("P", str(root))
    reason = "missing_trusted_submission_file_seal"
    assert integ._request_revalidation(batch, accepts[0], ["ToDo/loop.md"], reason) is None
    assert integ._request_revalidation(batch, accepts[1], ["ToDo/loop.md"], reason) is None
    latest = integ._request_revalidation(batch, accepts[2], ["ToDo/loop.md"], reason)
    assert latest and latest.startswith("revalidate:")
    assert integ._request_revalidation(
        batch, accepts[2], ["ToDo/loop.md"], "missing_server_submission") == latest
    with store.read() as tx:
        assert tx.scalar("SELECT count(*) FROM tasks WHERE tz LIKE 'revalidate:%'") == 1


@pytest.mark.parametrize(("drift", "foreign", "staged_other", "expanded"), [
    (False, False, False, False), (True, False, False, False),
    (False, True, False, False), (False, False, True, False),
    (False, False, False, True),
])
def test_real_shared_submit_seals_bytes_before_independent_accept(
    env, monkeypatch, drift, foreign, staged_other, expanded,
):
    """No fixture-forged seal: the real submit handler owns its receipt."""
    from dev_mcp.fleet import goals, queue, review

    store, root, _, temp = env
    monkeypatch.setattr(goals, "project_info", lambda _p: {"commit": True, "claim_trees": False})
    target = root / "ToDo" / "TZ-real.md"
    own = norm_path(str(target))
    add_task(store, "TZ-real", [own])
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET state='ready' WHERE project='P' AND tz='TZ-real'",
            (), event={"kind": "test_setup"},
        )
        for worker in ("W-A", "W-R"):
            tx.change(
                "INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                "VALUES (?,?,'chatgpt',?,?)",
                (worker, worker, now(), now()), event={"kind": "test_setup"},
            )

    integrator = make(store, temp)
    assert queue.take_next(store, "W-A", "P").tz == "TZ-real"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("accepted candidate\n", encoding="utf-8")
    expected_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    with store.tx() as tx:
        tx.record_event(kind="write", worker_id="W-A", project="P", tz="TZ-real",
                        path_norm=own, payload={"kind": "test_editor_write"})
    expected_files = {"ToDo/TZ-real.md": expected_sha}
    if expanded:
        report = root / "ToDo" / "report.md"
        report.write_text("server-authorized completion report\n", encoding="utf-8")
        expected_files["ToDo/report.md"] = hashlib.sha256(report.read_bytes()).hexdigest()
        with store.tx() as tx:
            claim = tx.one("SELECT id FROM claims WHERE worker_id='W-A' AND state='active'")
            tx.change("INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
                      (claim["id"], norm_path(str(report))), event="claim_expand")
            tx.record_event(kind="write", worker_id="W-A", project="P", tz="TZ-real",
                            path_norm=norm_path(str(report)))
    if foreign:
        # A write-audit entry outside registered owns cannot authorize extra
        # candidate bytes; the integrator must defer, not absorb this file.
        outsider = root / "outside.txt"
        outsider.write_text("not this task's work\n", encoding="utf-8")
        with store.tx() as tx:
            tx.record_event(kind="write", worker_id="W-A", project="P", tz="TZ-real",
                            path_norm=norm_path(str(outsider)), payload={"kind": "outside_owns"})
    if staged_other:
        staged = root / "staged_other.txt"
        staged.write_text("other task's staging\n", encoding="utf-8")
        git(root, "add", "--", staged.name)
    queue.submit(store, "W-A", "submitted genuine shared bytes")
    with store.read() as tx:
        submit = tx.one("SELECT id FROM events WHERE project='P' AND tz='TZ-real' "
                        "AND kind='submit' ORDER BY id DESC LIMIT 1")
        receipt = tx.one("SELECT id,tool,worker_id,payload FROM events WHERE project='P' "
                         "AND tz='TZ-real' AND kind='submission_file_seal'")
    assert submit is not None and receipt is not None
    assert receipt["id"] > submit["id"]
    assert receipt["tool"] == "fleet" and receipt["worker_id"] is None
    proof = json.loads(receipt["payload"])
    assert {k: v for k, v in proof.items() if k != "scope"} == {
        "submit_id": submit["id"], "files": expected_files}
    assert "ToDo/TZ-real.md".casefold() in proof["scope"]

    review.open_review(store, "P", "TZ-real")
    assert queue.take_next(store, "W-R", "P").tz == "review:TZ-real"
    review.decide(store, "W-R", "accept", "independent approval")
    if drift:
        target.write_text("changed after submit\n", encoding="utf-8")
    integrator.tick()
    integrated = events(store, "integrate")
    assert integrated
    outcome = integrated[-1]
    if drift:
        # 07.11 (1.3, печать = снимок): дерево ушло от печати — коммитятся байты печати из её
        # снимка, а изменённый файл остаётся в дереве (раньше — вечное «байты изменены»).
        assert outcome.get("route") == "snapshot", outcome
        assert git(root, "show", "HEAD:ToDo/TZ-real.md") == "accepted candidate\n"
        assert target.read_text(encoding="utf-8") == "changed after submit\n"
    elif foreign:
        # 07.11 (1.3): запись вне зоны не входит в печать — её байты не этой сдачи. Коммитится
        # запечатанное, чужой файл остаётся в дереве (раньше — вечное «печати нет»).
        assert len(outcome.get("commit", "")) >= 7, outcome
        assert outcome.get("later_work") == ["outside.txt"]
        assert "outside.txt" not in git(root, "show", "--format=", "--name-only", "HEAD")
        with store.read() as tx:
            assert tx.scalar("SELECT count(*) FROM tasks WHERE tz LIKE 'revalidate:%'") == 0
    else:
        assert len(outcome.get("commit", "")) >= 7
        assert git(root, "show", "HEAD:ToDo/TZ-real.md") == "accepted candidate\n"
        if staged_other:
            assert "staged_other.txt" in git(root, "diff", "--cached", "--name-only")
            assert "staged_other.txt" not in git(root, "show", "--format=", "--name-only", "HEAD")


def test_submit_seals_bytes_while_commit_is_off_so_enabling_commit_opens_no_revalidation(
    env, monkeypatch,
):
    """04.11: Nova VPN с commit=false не имел ни одной печати; включение коммита
    поднимало revalidate:* по каждой уже принятой сдаче. Печать — у submit всегда."""
    from dev_mcp.fleet import goals, queue, review

    store, root, _, temp = env
    info = {"commit": False, "claim_trees": False}
    monkeypatch.setattr(goals, "project_info", lambda _p: dict(info))
    target = root / "ToDo" / "TZ-off.md"
    own = norm_path(str(target))
    add_task(store, "TZ-off", [own])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE project='P' AND tz='TZ-off'",
                  (), event={"kind": "test_setup"})
        for worker in ("W-A", "W-R"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES (?,?,'chatgpt',?,?)",
                      (worker, worker, now(), now()), event={"kind": "test_setup"})
    integrator = make(store, temp)
    assert queue.take_next(store, "W-A", "P").tz == "TZ-off"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("accepted while commit was off\n", encoding="utf-8")
    queue.submit(store, "W-A", "submitted with commit=false")
    with store.read() as tx:
        receipt = tx.one("SELECT payload FROM events WHERE project='P' AND tz='TZ-off' "
                         "AND kind='submission_file_seal'")
    assert receipt is not None
    assert json.loads(receipt["payload"])["files"] == {
        "ToDo/TZ-off.md": hashlib.sha256(target.read_bytes()).hexdigest()}

    review.open_review(store, "P", "TZ-off")
    assert queue.take_next(store, "W-R", "P").tz == "review:TZ-off"
    review.decide(store, "W-R", "accept", "independent approval")
    info["commit"] = True
    integrator.tick()
    outcome = events(store, "integrate")[-1]
    assert len(outcome.get("commit", "")) >= 7, outcome
    with store.read() as tx:
        assert tx.scalar("SELECT count(*) FROM tasks WHERE tz LIKE 'revalidate:%'") == 0


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_shared_submit_does_not_seal_file_through_ntfs_junction(env, monkeypatch):
    """An in-owns lexical path must never attest bytes outside project root."""
    from dev_mcp.fleet import goals, queue

    store, root, _, temp = env
    monkeypatch.setattr(goals, "project_info", lambda _p: {"commit": True, "claim_trees": False})
    parent = root / "pkg"
    parent.mkdir()
    source = parent / "a.py"
    source.write_bytes(b"inside source\n")
    git(root, "add", "--", "pkg/a.py")
    git(root, "commit", "-m", "tracked owned source")
    add_task(store, "TZ-J", [norm_path(str(source))])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE project='P' AND tz='TZ-J'",
                  (), event={"kind": "test_setup"})
        tx.change(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen) "
            "VALUES ('W-J','W-J','chatgpt',?,?)",
            (now(), now()), event={"kind": "test_setup"},
        )
    assert queue.take_next(store, "W-J", "P").tz == "TZ-J"

    # A junction requires no elevation on Windows and is NOT a symlink.
    # Replacing only the already-snapshotted parent simulates the contested
    # shared worktree between take and submit.
    outside = temp / "external"
    outside.mkdir()
    (outside / "a.py").write_bytes(b"foreign secret contents\n")
    source.unlink()
    parent.rmdir()
    linked = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(parent), str(outside)],
        capture_output=True,
    )
    if linked.returncode:
        pytest.skip(f"Windows mklink /J unavailable (rc={linked.returncode}): "
                    + repr((linked.stderr or b"")[-160:]))
    assert parent.is_junction() and not parent.is_symlink()
    assert source.is_file() and source.read_bytes() == b"foreign secret contents\n"
    assert "pkg/a.py" in git(root, "status", "--porcelain", "--", "pkg/a.py")

    queue.submit(store, "W-J", "source replaced by an NTFS junction")
    with store.read() as tx:
        assert tx.one("SELECT 1 FROM events WHERE kind='submit' AND tz='TZ-J'") is not None
        assert tx.one("SELECT 1 FROM events WHERE kind='submission_file_seal' "
                      "AND tz='TZ-J'") is None
    assert (outside / "a.py").read_bytes() == b"foreign secret contents\n"


def test_base_tz_and_porcelain_parsing() -> None:
    assert _base_tz("review:fix:TZ-7:2") == "TZ-7"
    assert _base_tz("TZ-7") == "TZ-7"
    assert _porcelain_files(" M a.txt\0R  new.txt\0old.txt\0?? d/x y.txt\0") == [
        "a.txt", "new.txt", "old.txt", "d/x y.txt",
    ]


def test_head_gate_styk_acceptance_is_rewritten_only_for_identical_trusted_pair(tmp_path):
    live, work = tmp_path / "dev-mcp", tmp_path / "dev-mcp-work"
    for root in (live, work):
        (root / "tools").mkdir(parents=True)
        (root / "tools/gate_head.py").write_bytes(b"# detached HEAD gate\n")
    command = f'py -3 "{live / "tools/gate_head.py"}"'

    rewritten, proof = _styk_review_gate(command, str(work))
    assert rewritten == "py -3 tools/gate_head.py"
    assert proof == hashlib.sha256(b"# detached HEAD gate\n").hexdigest()

    # Never convert a different program, an altered command or a drifted copy.
    assert _styk_review_gate(command + " --skip", str(work)) == (command + " --skip", "")
    other = f'py -3 "{tmp_path / "foreign/gate_head.py"}"'
    assert _styk_review_gate(other, str(work)) == (other, "")
    (work / "tools/gate_head.py").write_bytes(b"# different program\n")
    assert _styk_review_gate(command, str(work)) == (command, "")
    (work / "tools/gate_head.py").unlink()
    assert _styk_review_gate(command, str(work)) == (command, "")


def test_styk_registers_verified_reviewable_gate_without_replacing_original(tmp_path):
    live, work = tmp_path / "dev-mcp", tmp_path / "dev-mcp-work"
    for root in (live, work):
        (root / "tools").mkdir(parents=True)
        (root / "tools/gate_head.py").write_bytes(b"# cwd driven HEAD gate\n")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("P", str(work)), event={"kind": "seed"})
    cmd = f'py -3 "{live / "tools/gate_head.py"}"'
    integ = Integrator(store, tmp_path / "state.json")
    integ._open_styk(Batch("P", str(work)), cmd, "rc=1", "failed", ["TZ-X"])
    with store.read() as tx:
        task = tx.one("SELECT acceptance,body FROM tasks WHERE project=? AND tz=?", ("P", "styk:1"))
        event = tx.one("SELECT payload FROM events WHERE project=? AND tz=? AND kind='styk_open'",
                       ("P", "styk:1"))
    assert json.loads(task["acceptance"]) == ["py -3 tools/gate_head.py"]
    assert "sha256=" in task["body"]
    payload = json.loads(event["payload"])
    assert payload["gate_original"] == cmd
    assert payload["gate_acceptance"] == "py -3 tools/gate_head.py"
    assert payload["gate_script_sha256"] == hashlib.sha256(b"# cwd driven HEAD gate\n").hexdigest()


def test_accept_commits_only_the_task_paths(env) -> None:
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / "Mine.py").write_text("x = 1\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "extra.md").write_text("written outside owns\n", encoding="utf-8")
    (root / "other.txt").write_text("someone else's\n", encoding="utf-8")
    add_task(store, "TZ-1", [f"{rootn}/mine.py"])  # регистр на диске другой
    event(store, "write", "TZ-1", path_norm=f"{rootn}/sub/extra.md")
    event(store, "accept", "TZ-1")

    out = integ.tick()

    [integ_event] = [e for e in out if e["kind"] == "integrate"]
    assert integ_event["payload"]["commit"]
    files = git(root, "show", "--name-only", "--format=%B", "HEAD")
    assert "Mine.py" in files and "sub/extra.md" in files and "other.txt" not in files
    assert "Fleet-Task: P/TZ-1" in files
    assert "?? other.txt" in git(root, "status", "--porcelain")
    assert integ.tick() == []  # курсор сдвинулся — повторно не коммитим


def test_late_foreign_bytes_without_write_event_are_not_committed(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    mine = root / "Mine.py"
    mine.write_text("accepted = 1\n", encoding="utf-8")
    add_task(store, "TZ-77", [f"{rootn}/mine.py"])
    event(store, "accept", "TZ-77")  # sealed accepted = 1
    mine.write_text("accepted = 1\nforeign = 2\n", encoding="utf-8")
    outcome = integ.tick()
    [integrated] = [x for x in outcome if x["kind"] == "integrate"]
    assert integrated["payload"]["reason_code"] == "accepted_candidate_bytes_changed"
    assert integrated["payload"]["deferred_paths"] == ["Mine.py"]
    assert "Mine.py" not in git(root, "ls-tree", "-r", "--name-only", "HEAD")
    assert mine.read_text(encoding="utf-8").endswith("foreign = 2\n")
    assert "?? Mine.py" in git(root, "status", "--porcelain")


def test_missing_submitted_seal_must_defer_despite_owns_and_write_event(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / "Mine.py").write_text("x = 1\n", encoding="utf-8")
    add_task(store, "TZ-78", [f"{rootn}/mine.py"])
    with store.tx() as tx:
        # Урок 54: эпоха печатей объявлена — до неё действует прежнее правило.
        tx.record_event(kind="submission_seal_epoch", project="P", tz="-")
        tx.record_event(kind="accept", project="P", tz="TZ-78", worker_id="W1")
    [integrated] = [x for x in integ.tick() if x["kind"] == "integrate"]
    assert integrated["payload"]["reason_code"] == "missing_server_submission"
    assert "Mine.py" not in git(root, "ls-tree", "-r", "--name-only", "HEAD")


def test_unrelated_staged_file_preserved_but_staged_candidate_deferred(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / "Mine.py").write_text("mine\n", encoding="utf-8")
    other = root / "other.txt"
    other.write_text("foreign staged\n", encoding="utf-8")
    git(root, "add", "other.txt")
    add_task(store, "TZ-79", [f"{rootn}/mine.py"])
    event(store, "accept", "TZ-79")
    outcome = integ.tick()
    assert [x for x in outcome if x["kind"] == "integrate"][0]["payload"]["commit"]
    assert "other.txt" not in git(root, "show", "--pretty=format:", "--name-only", "HEAD")
    assert "other.txt" in git(root, "diff", "--cached", "--name-only")
    # Next accepted candidate is staged by an unrelated actor: must not touch it.
    another = root / "Another.py"
    another.write_text("candidate\n", encoding="utf-8")
    add_task(store, "TZ-80", [f"{rootn}/another.py"])
    event(store, "accept", "TZ-80")
    git(root, "add", "Another.py")
    deferred = [x for x in integ.tick() if x["kind"] == "integrate"]
    assert deferred[0]["payload"]["reason_code"] == "candidate_paths_already_staged"
    assert "Another.py" in git(root, "diff", "--cached", "--name-only")
    assert another.read_text(encoding="utf-8") == "candidate\n"


def test_no_changes_and_no_git_are_recorded_not_raised(env) -> None:
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    add_task(store, "TZ-2", [f"{rootn}/base.txt"])
    event(store, "accept", "TZ-2")
    integ.tick()
    assert events(store, "integrate")[-1]["skipped"].startswith("изменений нет")


def test_red_gate_opens_one_styk_and_green_gate_does_not(env) -> None:
    store, root, rootn, tmp = env
    integ = make(store, tmp, gate=f'"{sys.executable}" -c "import sys; print(\'FAILED test_x\'); sys.exit(1)"')
    add_task(store, "TZ-3", [f"{rootn}/a.txt"])
    add_task(store, "TZ-4", [f"{rootn}/b.txt"])
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    event(store, "accept", "TZ-3")
    event(store, "accept", "fix:TZ-4")

    integ.tick()

    [gate] = events(store, "gate")
    assert gate["rc"] == 1 and gate["after"] == ["TZ-3", "fix:TZ-4"] and "FAILED test_x" in gate["summary"]
    with store.tx() as tx:
        styk = tx.query("SELECT tz,owns,acceptance,state FROM tasks WHERE tz LIKE 'styk:%'")
    assert len(styk) == 1 and styk[0]["state"] == "ready"
    assert sorted(json.loads(styk[0]["owns"])) == [f"{rootn}/a.txt", f"{rootn}/b.txt"]

    # вторая красная пачка не плодит второй «стык», пока первый открыт
    add_task(store, "TZ-5", [f"{rootn}/c.txt"])
    (root / "c.txt").write_text("c\n", encoding="utf-8")
    event(store, "accept", "TZ-5")
    integ.tick()
    with store.tx() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'styk:%'") == 1

    green = Integrator(store, tmp / "integrate-state.json", project_info=lambda p: {"gate": f'"{sys.executable}" -c "pass"'})
    add_task(store, "TZ-6", [f"{rootn}/d.txt"])
    event(store, "accept", "TZ-6")
    green.tick()
    assert events(store, "gate")[-1]["rc"] == 0 and events(store, "gate")[-1]["summary"] == "зелёный"


@pytest.mark.parametrize('phase', ['frozen', 'fix', 'review'])
def test_red_gate_reuses_frozen_repair_and_its_pending_family(env, phase):
    store, root, rootn, tmp = env
    integ=make(store,tmp,gate='python -c "pass"')
    batch=Batch('P',str(root),owns=[f'{rootn}/base.txt'])
    integ._open_styk(batch,'python -c "pass"','failure','log',['TZ-1'])
    with store.tx() as tx:
        if phase == 'frozen':
            tx.change("UPDATE tasks SET state='frozen' WHERE tz='styk:1'",event='test')
        else:
            tx.change("UPDATE tasks SET state='rejected' WHERE tz='styk:1'",event='test')
    if phase != 'frozen':
        add_task(store,f'{phase}:styk:1:2',[f'{rootn}/base.txt'])
        with store.tx() as tx:
            tx.change('UPDATE tasks SET state=? WHERE tz=?',('ready',f'{phase}:styk:1:2'),event='test')
    integ._open_styk(batch,'python -c "pass"','same failure','new log',['TZ-2'])
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'styk:%'")==1
        record=tx.one("SELECT tz,payload FROM events WHERE kind='styk_coalesced'")
        assert record['tz']=='styk:1' and json.loads(record['payload'])['after']==['TZ-2']


def test_commit_needs_project_permission(env) -> None:
    store, root, rootn, tmp = env
    integ = Integrator(store, tmp / "integrate-state.json", project_info=lambda p: {})
    integ.tick()
    (root / "x.txt").write_text("x\n", encoding="utf-8")
    add_task(store, "TZ-9", [f"{rootn}/x.txt"])
    event(store, "accept", "TZ-9")
    integ.tick()
    assert events(store, "integrate")[-1]["skipped"].startswith("коммит не разрешён")
    assert "?? x.txt" in git(root, "status", "--porcelain")


def test_gates_of_projects_run_in_parallel_and_get_changed_paths(env, tmp_path: Path) -> None:
    """Гейт одного проекта не держит другой; гейт получает пути пачки в DEV_MCP_FLEET_CHANGED."""
    import threading
    import time as _time

    store, root, rootn, _ = env
    root2 = tmp_path / "Proj2"
    root2.mkdir()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)", ("Q", str(root2)),
                  event={"kind": "test_setup"})
    seen: dict[str, str] = {}
    active, peak = [0], [0]
    lock = threading.Lock()
    rendezvous = threading.Barrier(2)

    def runner(cmd, **kw):
        if cmd == ["fleet-test-gate"]:  # stub must not resolve the host's check.cmd
            assert kw["shell"] is False
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            rendezvous.wait(timeout=10)
            _time.sleep(0.05)
            with lock:
                active[0] -= 1
            seen[str(kw["cwd"])] = kw["env"]["DEV_MCP_FLEET_CHANGED"]
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        return subprocess.run(cmd, **kw)

    integ = Integrator(store, tmp_path / "integrate-state.json", runner=runner,
                       project_info=lambda p: {"gate": "fleet-test-gate"})
    assert integ.tick() == []
    add_task(store, "TZ-1", [f"{rootn}/src/a.py"])
    event(store, "accept", "TZ-1")
    with store.tx() as tx:
        stamp = now()
        tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,resources,"
                  "acceptance,author_worker,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("Q", "TZ-9", "t", "accepted", 5, "work", "", "", "[]", "[]", "[]", "[]", None, "b", stamp, stamp),
                  event={"kind": "test_setup"})
        tx.record_event(kind="accept", project="Q", tz="TZ-9", worker_id="W2")
    gates = [e for e in integ.tick() if e.get("kind") == "gate"]
    assert len(gates) == 2
    assert peak[0] == 2, "гейты разных проектов идут одновременно"
    assert "src/a.py" in seen.values()


def test_windows_relative_venv_gate_runs_without_cmd_shell(env) -> None:
    """Fleet Platform's configured gate executable was split by cmd.exe at '/'."""
    store, _root, _rootn, tmp = env
    project_root = Path(__file__).resolve().parents[2]
    executable = project_root / ".venv" / "Scripts" / "python.exe"
    if not executable.is_file():
        pytest.skip("Windows project .venv is unavailable")
    integ = Integrator(store, tmp / "cursor.json",
                       project_info=lambda _p: {"gate": ".venv/Scripts/python.exe -V"})
    batch = Batch(project="P", root=str(project_root), accepts=[{"tz": "TZ-TEST"}])
    result = integ._gate(batch)
    assert result is not None and result["rc"] == 0, result
    assert "Python " in result["payload"]["log_tail"]


def test_styk_zone_is_failed_tests_and_written_files_never_project_root(env) -> None:
    store, root, rootn, tmp = env
    (root / "test").mkdir()
    (root / "test" / "test_x.py").write_text("x\n", encoding="utf-8")
    (root / "backend" / "tests").mkdir(parents=True)
    (root / "backend" / "tests" / "test_y.py").write_text("y\n", encoding="utf-8")
    script = tmp / "gate.py"
    script.write_text(
        "import sys\nprint('noise')\nprint('FAILED test/test_x.py::T::t - boom')\n"
        "print('FAILED tests/test_y.py::u')\nprint('k, ' * 2000)\nsys.exit(1)\n",
        encoding="utf-8",
    )
    integ = make(store, tmp, gate=f'"{sys.executable}" "{script}"')
    add_task(store, "TZ-3", [rootn])  # задача с зоной «весь проект»
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    event(store, "write", "TZ-3", path_norm=f"{rootn}/a.txt")
    event(store, "accept", "TZ-3")

    integ.tick()

    gate = events(store, "gate")[-1]
    assert gate["failed"] == ["test/test_x.py", "backend/tests/test_y.py"]
    assert gate["log_tail"].startswith("FAILED test/test_x.py")  # имена не съедает шум в конце лога
    assert "FAILED test/test_x.py" in gate["summary"]
    with store.tx() as tx:
        owns = json.loads(tx.scalar("SELECT owns FROM tasks WHERE tz='styk:1'"))
    assert sorted(owns) == sorted([f"{rootn}/a.txt", f"{rootn}/test/test_x.py", f"{rootn}/backend/tests/test_y.py"])
    assert rootn not in owns


def test_green_gate_closes_open_styk_family(env) -> None:
    from dev_mcp.fleet import queue

    store, root, rootn, tmp = env
    red = make(store, tmp, gate=f'"{sys.executable}" -c "import sys; sys.exit(1)"')
    add_task(store, "TZ-3", [f"{rootn}/a.txt"])
    event(store, "accept", "TZ-3")
    red.tick()
    stamp = now()
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE tz='styk:1'", (), event={"kind": "test_setup"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P", "fix:styk:1", "Fix styk:1", "ready", 0, "fix", "", "", "[]", "[]", "[]", "[]", None,
             "[fleet-fix] source=P/styk:1", stamp, stamp), event={"kind": "test_setup"},
        )
    assert queue.close_resolved_styks(store) == []  # гейт ещё не зеленел — стык нужен

    green = Integrator(store, tmp / "integrate-state.json", project_info=lambda p: {"gate": f'"{sys.executable}" -c "pass"'})
    add_task(store, "TZ-4", [f"{rootn}/b.txt"])
    event(store, "accept", "TZ-4")
    green.tick()

    assert queue.close_resolved_styks(store) == ["P/styk:1"]
    with store.tx() as tx:
        states = {r["tz"]: r["state"] for r in tx.query("SELECT tz,state FROM tasks WHERE tz LIKE '%styk%'")}
    assert states == {"styk:1": "accepted", "fix:styk:1": "rejected"}
    assert events(store, "auto_obsolete")[-1]["closed"] == ["fix:styk:1", "styk:1"] or \
        sorted(events(store, "auto_obsolete")[-1]["closed"]) == ["fix:styk:1", "styk:1"]


def test_pre_seal_epoch_accept_integrates_by_previous_rule(env) -> None:
    """Урок 54: приёмка, сделанная до появления печатей сдачи, не откладывается навсегда."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / "Old.py").write_text("old = 1\n", encoding="utf-8")
    add_task(store, "TZ-OLD", [f"{rootn}/old.py"])
    with store.tx() as tx:
        tx.record_event(kind="submit", project="P", tz="TZ-OLD", worker_id="W1")
        tx.record_event(kind="accept", project="P", tz="TZ-OLD", worker_id="W2")
    [integrated] = [x for x in integ.tick() if x["kind"] == "integrate"]
    assert integrated["payload"].get("commit"), integrated
    assert integrated["payload"].get("legacy_pre_seal") is True


def test_file_rewritten_by_a_later_accepted_task_does_not_block_the_earlier(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "TZ.md").write_text("v1\n", encoding="utf-8")
    add_task(store, "TZ-A", [f"{rootn}/a.py", f"{rootn}/tz.md"])
    event(store, "accept", "TZ-A")  # sealed: a.py, TZ.md v1
    (root / "TZ.md").write_text("v2\n", encoding="utf-8")
    add_task(store, "TZ-B", [f"{rootn}/tz.md"])
    event(store, "accept", "TZ-B")  # sealed: TZ.md v2
    out = [x for x in integ.tick() if x["kind"] == "integrate"]
    first = next(x for x in out if x["tz"] == "TZ-A")["payload"]
    assert first["superseded"] == ["TZ.md"] and first.get("commit")
    tracked = git(root, "ls-tree", "-r", "--name-only", "HEAD")
    assert "a.py" in tracked and "TZ.md" in tracked
    assert git(root, "show", "HEAD:TZ.md").strip() == "v2"


def test_unexplained_drift_is_reported_to_the_operator_once(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    mine = root / "Mine.py"
    mine.write_text("accepted = 1\n", encoding="utf-8")
    add_task(store, "TZ-79", [f"{rootn}/mine.py"])
    event(store, "accept", "TZ-79")
    mine.write_text("hand edit\n", encoding="utf-8")
    integ.tick()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1


def test_reopened_lead_keeps_accepted_byte_provenance(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / 'shared.py').write_text('v = 1\n', encoding='utf-8')
    add_task(store, 'TZ-A', [f'{rootn}/shared.py'])
    event(store, 'accept', 'TZ-A')
    with store.read() as tx:
        older = tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'")
    add_task(store, 'TZ-LEAD', [f'{rootn}/shared.py'])
    (root / 'shared.py').write_text('v = 2\n', encoding='utf-8')
    event(store, 'accept', 'TZ-LEAD')
    v2 = hashlib.sha256((root / 'shared.py').read_bytes()).hexdigest()
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='taken' WHERE tz='TZ-LEAD'", (), event='test_setup')
    assert integ._superseded_by_later_accept('P', older, {'shared.py': v2}) == {'shared.py'}
    # A later rejected seal is never blessed by the next milestone's acceptance.
    v3 = hashlib.sha256(b'v = 3\n').hexdigest()
    with store.tx() as tx:
        tx.record_event(kind='submit', project='P', tz='TZ-LEAD')
        sid = tx.scalar('SELECT MAX(id) FROM events')
        tx.record_event(kind='submission_file_seal', project='P', tz='TZ-LEAD', tool='fleet',
                        payload={'submit_id': sid, 'files': {'shared.py': v3}})
        tx.record_event(kind='reject', project='P', tz='TZ-LEAD')
    (root / 'shared.py').write_text('v = 4\n', encoding='utf-8')
    event(store, 'accept', 'TZ-LEAD')
    assert integ._superseded_by_later_accept('P', older, {'shared.py': v3}) == set()
    # Accepted v2 remains historical proof even after a newer submit/reject.
    assert integ._superseded_by_later_accept('P', older, {'shared.py': v2}) == {'shared.py'}


def test_old_deferred_accept_is_not_forgotten_behind_many_retries(env):
    """20.10.2026: only the newest 3000 integrate events were read; 406 works were lost."""
    store, root, rootn, temp = env
    with store.tx() as tx:
        tx.record_event(kind='accept', project='P', tz='TZ-old', worker_id='reviewer')
        aid = int(tx.scalar('SELECT MAX(id) FROM events'))
        tx.record_event(kind='integrate', project='P', tz='TZ-old',
                        payload={'accept_id': aid, 'deferred': ['old.py']})
        tx.record_event(kind='accept', project='P', tz='TZ-busy', worker_id='reviewer')
        busy = int(tx.scalar('SELECT MAX(id) FROM events'))
        for _ in range(3100):
            tx.record_event(kind='integrate', project='P', tz='TZ-busy',
                            payload={'accept_id': busy, 'deferred': ['busy.py']})
    integrator = Integrator(store, temp/'cursor.json')
    assert sorted(r['id'] for r in integrator._pending_accepts()) == [aid, busy]
    with store.tx() as tx:  # incremental pass sees the later decision
        tx.record_event(kind='integrate', project='P', tz='TZ-old',
                        payload={'accept_id': aid, 'commit': 'abc'})
    assert [r['id'] for r in integrator._pending_accepts()] == [busy]


def test_stale_deferred_work_replaced_by_later_accepted_work_is_closed(env):
    store, root, rootn, temp = env
    old_ts = '2026-10-10T08:00:00+00:00'
    with store.tx() as tx:
        tx.record_event(kind='accept', project='P', tz='TZ-a', worker_id='reviewer')
        aid = int(tx.scalar('SELECT MAX(id) FROM events'))
        tx.change('UPDATE events SET ts=? WHERE id=?', (old_ts, aid), event='test.seed')
        tx.record_event(kind='integrate', project='P', tz='TZ-a',
                        payload={'accept_id': aid, 'deferred': ['a.py', 'b.py']})
        tx.record_event(kind='accept', project='P', tz='TZ-c', worker_id='reviewer')
        keep = int(tx.scalar('SELECT MAX(id) FROM events'))
        tx.change('UPDATE events SET ts=? WHERE id=?', (old_ts, keep), event='test.seed')
        tx.record_event(kind='integrate', project='P', tz='TZ-c',
                        payload={'accept_id': keep, 'deferred': ['c.py']})
        tx.change("INSERT INTO tasks(project,tz,title,state,created_at,updated_at) "
                  "VALUES('P','TZ-b','b','accepted',?,?)", (old_ts, old_ts), event='test.seed')
        tx.record_event(kind='submission_file_seal', project='P', tz='TZ-b', tool='fleet',
                        payload={'files': {'a.py': 'x', 'b.py': 'y'}})
    integrator = Integrator(store, temp/'cursor.json')
    assert [r['id'] for r in integrator._pending_accepts()] == [keep]
    with store.read() as tx:
        closed = json.loads(tx.scalar("SELECT payload FROM events WHERE kind='integrate' "
                                      "AND json_extract(payload,'$.superseded')=1"))
        assert closed['accept_id'] == aid and closed['superseded_by'] == ['TZ-b']
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1
    assert [r['id'] for r in integrator._pending_accepts()] == [keep]


def _request(store: Store, tz: str, state: str, kind: str = "request") -> None:
    stamp = now()
    body = "[fleet-request] id=x\nТип: integration\nЧто нужно: Исправить неприменимую acceptance-версию P/TZ-src"
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P", tz, tz, state, 5, kind, "", "", "[]", "[]", "[]", "[]", None, body, stamp, stamp),
            event={"kind": "test_setup"},
        )


def test_integration_request_closed_once_its_source_is_integrated(env):
    """21.10.2026: заявка «исправить неприменимую версию X» и её ревью висели после того, как X
    ушла в историю; теперь семья закрывается, взятая агентом — нет."""
    from dev_mcp.fleet import integrate as mod
    store, root, rootn, temp = env
    integ = make(store, temp)
    with store.tx() as tx:
        tx.record_event(kind="accept", project="P", tz="TZ-src", worker_id="r")
        tx.record_event(kind="accept", project="P", tz="TZ-wip", worker_id="r")
    with store.read() as tx:
        src, wip = [int(r["id"]) for r in tx.query("SELECT id FROM events WHERE kind='accept' ORDER BY id")]
    for name, source, aid, payload in (
            ("aaaa1111", "TZ-src", src, {"skipped": "изменений нет (уже в истории)"}),
            ("bbbb2222", "TZ-wip", wip, {"deferred": ["a.txt"], "reason": "x"})):
        _request(store, f"request:{name}:3", "submitted")
        _request(store, f"review:request:{name}:3:2", "frozen", kind="review")
        with store.tx() as tx:
            tx.record_event(kind="request_draft", project="P", tz=f"request:{name}:3", worker_id="w",
                            payload=json.dumps({"source_tz": source}))
            tx.record_event(kind="integrate", project="P", tz=source, worker_id=None,
                            payload=json.dumps({"accept_id": aid, **payload}))
    integ._refresh_latest()
    integ._moot_at = 0.0
    assert integ._close_moot_requests() == ["P/request:aaaa1111"]
    with store.read() as tx:
        states = {r["tz"]: r["state"] for r in tx.query("SELECT tz,state FROM tasks WHERE kind<>'work'")}
    assert states["request:aaaa1111:3"] == "accepted"
    assert states["review:request:aaaa1111:3:2"] == "accepted"
    assert states["request:bbbb2222:3"] == "submitted"  # источник ещё отложен — заявка нужна
    assert mod.Integrator._final_integration({"skipped": "коммит не разрешён проектом (x)"}) is False


def test_late_bytes_of_unfinished_fleet_task_wait_instead_of_operator(env):
    """27.10: bytes of an unfinished fleet task are an observable wait, not an operator front."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    mine = root / "Mine.py"
    mine.write_text("accepted = 1\n", encoding="utf-8")
    add_task(store, "TZ-78", [f"{rootn}/mine.py"])
    event(store, "accept", "TZ-78")
    add_task(store, "TZ-79", [f"{rootn}/mine.py"])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE project='P' AND tz='TZ-79'", event="seed")
    event(store, "write", "TZ-79", path_norm=f"{rootn}/mine.py")
    mine.write_text("accepted = 1\nlater = 2\n", encoding="utf-8")
    [integrated] = [x for x in integ.tick() if x["kind"] == "integrate"]
    payload = integrated["payload"]
    assert payload["reason_code"] == "accepted_candidate_bytes_changed_by_unfinished"
    assert payload["waiting_for"] == ["TZ-79"]
    assert payload["obstacle"] == {"type": "needs_result", "resolver_work": ["TZ-79"]}
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 0
    assert "Mine.py" not in git(root, "ls-tree", "-r", "--name-only", "HEAD")


def test_unfinished_writer_on_unchanged_path_is_not_blamed_for_drift(env):
    """A foreign writer of sealed B cannot explain dirty bytes of A."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    mine = root / "Mine.py"
    other = root / "Other.py"
    mine.write_text("accepted = 1\n", encoding="utf-8")
    other.write_text("other = 1\n", encoding="utf-8")
    add_task(store, "TZ-78", [f"{rootn}/mine.py", f"{rootn}/other.py"])
    event(store, "accept", "TZ-78")
    add_task(store, "TZ-79", [f"{rootn}/other.py"])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE project='P' AND tz='TZ-79'", event="seed")
    event(store, "write", "TZ-79", path_norm=f"{rootn}/other.py")
    mine.write_text("accepted = 1\nunattributed = 2\n", encoding="utf-8")
    [integrated] = [x for x in integ.tick() if x["kind"] == "integrate"]
    payload = integrated["payload"]
    assert payload["reason_code"] == "accepted_candidate_bytes_changed"
    assert payload["reason"] == "shared worktree differs from accepted submission"
    assert payload["waiting_for"] == []


def test_integration_repair_chain_stops_after_two_attempts(env):
    """27.10: до 36 заявок «почини приёмку» на один источник; две — и смена подхода."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / "Mine.py").write_text("x = 1\n", encoding="utf-8")
    batch = Batch("P", str(root))

    def requests():
        with store.read() as tx:
            return [(r["tz"], r["title"]) for r in tx.query(
                "SELECT tz,title FROM tasks WHERE kind='request' ORDER BY rowid")]

    for attempt in range(5):
        integ._preflight_repair_request(batch, "TZ-9", ["Mine.py"], f"red {attempt}")
        with store.tx() as tx:  # каждую заявку взяли и приняли — интеграция снова красная
            tx.change("UPDATE tasks SET state='accepted' WHERE kind='request'", event={"kind": "seed"})
    titles = [t for _, t in requests()]
    assert len(titles) == 3, titles
    assert sum("неприменимую acceptance-версию P/TZ-9" in t for t in titles) == 2
    assert "Сменить подход: 2 ремонта" in titles[2]


def test_foreign_write_absorbed_by_later_commit_does_not_hold_candidate(env):
    # 05.11.2026 Nova VPN: веху ведущего ночь держали TZ-170 (frozen, запись 27.09) и TZ-262
    # (submitted с 19.10), хотя их файлы с тех пор коммитились. Свежая чужая запись — держит.
    store, root, _, temp = env
    integrator = make(store, temp)
    shared = root / "shared.py"
    shared.write_text("v1\n", encoding="utf-8")
    for tz in ("TZ-old", "TZ-new"):
        add_task(store, tz, [])
        with store.tx() as tx:
            tx.change("UPDATE tasks SET state='frozen' WHERE tz=?", (tz,), event="test_setup")
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="TZ-old", path_norm=norm_path(str(shared)))
        tx.change("UPDATE events SET ts='2026-09-27T18:02:01+00:00' WHERE tz='TZ-old' AND kind='write'",
                  (), event="test_setup")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "absorb old write")
    assert integrator._foreign_writers("P", "TZ-LEAD", str(root), ["shared.py"]) == []
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="TZ-new", path_norm=norm_path(str(shared)))
        tx.change("UPDATE events SET ts='2999-01-01T00:00:00+00:00' WHERE tz='TZ-new' AND kind='write'",
                  (), event="test_setup")
    assert integrator._foreign_writers("P", "TZ-LEAD", str(root), ["shared.py"]) == ["TZ-new"]


def _lead_setup(store, workers=("W-L", "W-R")):
    with store.tx() as tx:
        for worker in workers:
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES (?,?,'chatgpt',?,?)",
                      (worker, worker, now(), now()), event="setup")


def test_later_write_in_seal_scope_does_not_void_the_seal(env, monkeypatch):
    """05.11: ведущий пишет в своей зоне дальше — файл, ставший грязным после сдачи,
    давал missing_trusted_submission_file_seal (4231 за сутки). Теперь коммитится запечатанное,
    позднее остаётся следующей сдаче."""
    from dev_mcp.fleet import goals, queue, review

    store, root, _, temp = env
    monkeypatch.setattr(goals, "project_info", lambda _p: {"commit": True, "claim_trees": False})
    sealed, later = root / "ToDo" / "a.md", root / "ToDo" / "b.md"
    add_task(store, "TZ-work", [norm_path(str(root / "ToDo"))])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE tz='TZ-work'", event="setup")
    _lead_setup(store)
    integ = make(store, temp)
    assert queue.take_next(store, "W-L", "P").tz == "TZ-work"
    sealed.parent.mkdir(exist_ok=True)
    sealed.write_text("milestone\n", encoding="utf-8")
    queue.submit(store, "W-L", "milestone")
    review.open_review(store, "P", "TZ-work")
    queue.take_next(store, "W-R", "P")
    review.decide(store, "W-R", "accept", "independent approval")
    later.write_text("next window, not reviewed\n", encoding="utf-8")
    integ.tick()
    outcome = events(store, "integrate")[-1]
    assert len(outcome.get("commit", "")) >= 7, outcome
    assert outcome.get("later_work") == ["ToDo/b.md"]
    committed = git(root, "show", "--format=", "--name-only", "HEAD")
    assert "ToDo/a.md" in committed and "ToDo/b.md" not in committed
    assert "ToDo/b.md" in git(root, "status", "--porcelain")
    with store.read() as tx:
        assert tx.scalar("SELECT count(*) FROM tasks WHERE tz LIKE 'revalidate:%'") == 0


def test_accepted_revalidation_ends_retries_of_unsealed_accept(env):
    """05.11: 74 из 78 отложенных приёмок ждали уже принятую ревалидацию и повторялись вечно."""
    from dev_mcp.fleet import goals, queue, review
    from unittest.mock import patch

    store, root, _, temp = env
    target = root / "ToDo" / "historical.md"
    add_task(store, "TZ-historical", [norm_path(str(target))])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE tz='TZ-historical'", event="setup")
        for worker in ("W-H", "W-review"):
            tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES(?,?,?)",
                      (worker, now(), now()), event="setup")
    integ = make(store, temp)
    with patch.object(goals, "project_info", return_value={"commit": True}):
        queue.take_next(store, "W-H", "P")
        target.parent.mkdir(exist_ok=True)
        target.write_text("unsealed historical report\n", encoding="utf-8")
        queue.submit(store, "W-H", "historically incomplete receipt")
    with store.tx() as tx:
        row = tx.one("SELECT id,payload FROM events WHERE kind='submission_file_seal'")
        data = json.loads(row["payload"])
        data["files"] = {}
        data.pop("scope", None)
        tx.change("UPDATE events SET payload=? WHERE id=?", (json.dumps(data), row["id"]), event="legacy_fixture")
    review.open_review(store, "P", "TZ-historical")
    queue.take_next(store, "W-review", "P")
    review.decide(store, "W-review", "accept", "independent historical approval")
    integ.tick()
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz LIKE 'revalidate:%'", event="setup")
    integ.tick()
    last = [e for e in events(store, "integrate") if e["tz"] == "TZ-historical"][-1]
    assert last.get("superseded") is True and "ревалидация" in last["reason"], last
    before = len(events(store, "integrate"))
    integ.tick()
    assert len([e for e in events(store, "integrate")[before:] if e["tz"] == "TZ-historical"]) == 0


def _snapshot(store: Store, root: Path, tz: str, rel: str) -> None:
    """Тень снимка сдачи: blob текущих байтов файла (как snapshot.submission_shadow)."""
    blob = git(root, "hash-object", "-w", "--", rel).strip()
    with store.tx() as tx:
        tx.record_event(kind="submission_snapshot", project="P", tz=tz, worker_id="W1",
                        payload={"blobs": {rel: blob}, "mode": "shadow"})


def test_lead_milestone_commits_snapshot_while_next_round_edits_same_file(env):
    """06.11: ведущий правит файл через 20 с после приёмки — веха коммитится по снимку."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    shared = root / "shared.txt"
    shared.write_text("milestone\n", encoding="utf-8")
    add_task(store, "TZ-LEAD", [f"{rootn}/shared.txt"])
    _snapshot(store, root, "TZ-LEAD", "shared.txt")
    event(store, "accept", "TZ-LEAD")
    accepted = shared.read_bytes()
    shared.write_text("milestone\nnext round\n", encoding="utf-8")
    [done] = [x for x in integ.tick() if x["kind"] == "integrate"]
    payload = done["payload"]
    assert payload.get("route") == "snapshot" and payload.get("commit"), payload
    assert "Fleet-Route: snapshot" in git(root, "log", "-1", "--format=%B")
    committed = subprocess.run(["git", "show", "HEAD:shared.txt"], cwd=root,
                               capture_output=True, check=True).stdout
    assert committed.replace(b"\r\n", b"\n") == accepted.replace(b"\r\n", b"\n")
    assert git(root, "diff", "--cached", "--name-only") == ""
    assert git(root, "status", "--porcelain").strip() == "M shared.txt"
    assert "next round" in shared.read_text(encoding="utf-8")


def _stalled_foreign(store: Store, rootn: str, review_state: str) -> None:
    from datetime import timedelta
    add_task(store, "TZ-X", [f"{rootn}/shared.txt"])
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE project='P' AND tz='TZ-X'", event="seed")
    add_task(store, "review:TZ-X:2", [])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state=?,updated_at=? WHERE project='P' AND tz='review:TZ-X:2'",
                  (review_state, old), event="seed")
    event(store, "write", "TZ-X", path_norm=f"{rootn}/shared.txt")


def test_frozen_foreign_writer_no_longer_holds_accepted_milestone(env):
    """05–06.11 Nova: TZ-266/TZ-333 с замороженным ревью всю ночь держали вехи ведущего."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    shared = root / "shared.txt"
    shared.write_text("foreign\n", encoding="utf-8")
    _stalled_foreign(store, rootn, "frozen")
    shared.write_text("foreign\nlead\n", encoding="utf-8")
    add_task(store, "TZ-LEAD", [f"{rootn}/shared.txt"])
    event(store, "accept", "TZ-LEAD")
    [done] = [x for x in integ.tick() if x["kind"] == "integrate" and x["tz"] == "TZ-LEAD"]
    payload = done["payload"]
    assert payload.get("commit") and payload.get("absorbed_stalled") == ["TZ-X"], payload
    assert "Fleet-Absorbed: TZ-X" in git(root, "log", "-1", "--format=%B")
    assert git(root, "status", "--porcelain").strip() == ""


def test_live_foreign_writer_still_holds_accepted_milestone(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    shared = root / "shared.txt"
    shared.write_text("foreign\n", encoding="utf-8")
    _stalled_foreign(store, rootn, "ready")
    add_task(store, "TZ-LEAD", [f"{rootn}/shared.txt"])
    event(store, "accept", "TZ-LEAD")
    [done] = [x for x in integ.tick() if x["kind"] == "integrate" and x["tz"] == "TZ-LEAD"]
    payload = done["payload"]
    assert not payload.get("commit") and payload.get("waiting_for") == ["TZ-X"], payload


def test_drift_without_provable_snapshot_keeps_legacy_deferral(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    shared = root / "shared.txt"
    shared.write_text("milestone\n", encoding="utf-8")
    add_task(store, "TZ-LEAD", [f"{rootn}/shared.txt"])
    event(store, "accept", "TZ-LEAD")
    shared.write_text("changed by hand\n", encoding="utf-8")
    [done] = [x for x in integ.tick() if x["kind"] == "integrate"]
    payload = done["payload"]
    assert not payload.get("commit") and payload.get("reason_code") == "accepted_candidate_bytes_changed"


def test_old_lead_round_leaves_file_rewritten_by_later_accepted_round(env):
    """06.11 Nova: 35 старых раундов ждали байтов файла, который несут следующие принятые."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    shared, other = root / "shared.txt", root / "other.txt"
    add_task(store, "TZ-LEAD", [f"{rootn}/shared.txt", f"{rootn}/other.txt"])
    shared.write_text("round 1\n", encoding="utf-8")
    other.write_text("only round 1\n", encoding="utf-8")
    _snapshot(store, root, "TZ-LEAD", "other.txt")  # снимок старого раунда без shared.txt
    event(store, "accept", "TZ-LEAD")
    shared.write_text("round 2\n", encoding="utf-8")
    _snapshot(store, root, "TZ-LEAD", "shared.txt")
    event(store, "accept", "TZ-LEAD")
    shared.write_text("round 3 in progress\n", encoding="utf-8")
    done = [x["payload"] for x in integ.tick() if x["kind"] == "integrate"]
    first, second = done[0], done[1]
    assert first.get("carried_by_later_accept") == ["shared.txt"] and first.get("commit"), first
    assert second.get("commit") and second.get("route") == "snapshot", second
    assert git(root, "show", "HEAD:shared.txt").strip() == "round 2"
    assert git(root, "show", "HEAD:other.txt").strip() == "only round 1"


def test_snapshot_validation_failure_falls_back_without_new_tasks(env):
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    broken = root / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")
    add_task(store, "TZ-LEAD", [f"{rootn}/broken.py"])
    _snapshot(store, root, "TZ-LEAD", "broken.py")
    event(store, "accept", "TZ-LEAD")
    broken.write_text("x = 1\n", encoding="utf-8")
    with store.read() as tx:
        before = tx.scalar("SELECT COUNT(*) FROM tasks")
    [done] = [x["payload"] for x in integ.tick() if x["kind"] == "integrate"]
    assert not done.get("commit") and str(done.get("snapshot_route", "")).startswith("validation:"), done
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks") == before


def test_sweep_commits_all_accepted_bytes_and_leaves_unaccepted(env):
    """06.11 Nova: 316 из 336 грязных файлов — ровно принятые байты, коммитов не было."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    (root / "a.txt").write_text("accepted a\n", encoding="utf-8")
    (root / "b.txt").write_text("accepted b\n", encoding="utf-8")
    add_task(store, "TZ-A", [f"{rootn}/a.txt"])
    add_task(store, "TZ-LEAD", [f"{rootn}/b.txt"])
    event(store, "accept", "TZ-A")
    event(store, "accept", "TZ-LEAD")
    (root / "wip.txt").write_text("never accepted\n", encoding="utf-8")
    [done] = integ.sweep_accepted(now=10**10)
    assert done["ready"] and done["commit"] and done["files"] == 2, done
    assert git(root, "show", "HEAD:a.txt").strip() == "accepted a"
    assert git(root, "status", "--porcelain").strip() == "?? wip.txt"
    assert integ.sweep_accepted(now=10**10 + 1) == []  # не чаще SWEEP_EVERY_S


def test_candidate_keeps_clean_file_bytes_of_the_live_tree(env):
    """06.11 Nova: autocrlf писал канон с CRLF — sha256 ledger_check расходился в кандидате."""
    store, root, rootn, tmp = env
    integ = make(store, tmp)
    canon = root / "canon.md"
    canon.write_bytes(b"line one\nline two\n")
    git(root, "-c", "core.autocrlf=true", "add", "canon.md")
    git(root, "commit", "-q", "-m", "canon")
    want = hashlib.sha256(canon.read_bytes()).hexdigest()
    check = root / "check.py"
    check.write_text(
        "import hashlib, pathlib, sys\n"
        f"sys.exit(0 if hashlib.sha256(pathlib.Path('canon.md').read_bytes()).hexdigest() == '{want}' else 3)\n",
        encoding="utf-8")
    ok, reason = integ._candidate(str(root), ["check.py"], "P", [f'"{sys.executable}" check.py'])
    assert ok, reason


def _seal_round(env, monkeypatch, files: dict[str, str], *, owns: str = "ToDo"):
    """Ведущий сдаёт files в зоне owns, независимый ревьюер принимает. → (store, root, integ)."""
    from dev_mcp.fleet import goals, queue, review

    store, root, _, temp = env
    monkeypatch.setattr(goals, "project_info", lambda _p: {"commit": True, "claim_trees": False})
    add_task(store, "TZ-work", [norm_path(str(root / owns))])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE tz='TZ-work'", event="setup")
    _lead_setup(store)
    integ = make(store, temp)
    assert queue.take_next(store, "W-L", "P").tz == "TZ-work"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    queue.submit(store, "W-L", "milestone")
    return store, root, integ


def _accept(store):
    from dev_mcp.fleet import queue, review
    review.open_review(store, "P", "TZ-work")
    queue.take_next(store, "W-R", "P")
    review.decide(store, "W-R", "accept", "independent approval")


def test_seal_writes_blobs_and_ref_at_submit_time(env, monkeypatch):
    """LEAD-SYSTEM-DESIGN 1.3: печать = снимок. Байты сдачи — git-объекты под ref с момента
    сдачи; переписанный через секунду файл не подменяет принятое, коммит несёт байты печати,
    а `refs/fleet/accepted/<accept>` указывает на снимок до интеграции."""
    store, root, integ = _seal_round(env, monkeypatch, {"ToDo/a.md": "milestone\n"})
    snaps = [e for e in events(store, "submission_snapshot") if e.get("mode") == "seal"]
    assert len(snaps) == 1, events(store, "submission_snapshot")
    snap = snaps[0]
    assert snap["ref"] == f"refs/fleet/sealed/{snap['submit_id']}"
    assert git(root, "rev-parse", snap["ref"]).strip() == snap["commit"]
    assert git(root, "show", f"{snap['commit']}:ToDo/a.md") == "milestone\n"
    # Ведущий сразу правит тот же файл — дерево ушло от печати.
    (root / "ToDo" / "a.md").write_text("next window, not reviewed\n", encoding="utf-8")
    _accept(store)
    integ.tick()
    outcome = events(store, "integrate")[-1]
    assert outcome.get("route") == "snapshot", outcome
    assert git(root, "show", "HEAD:ToDo/a.md") == "milestone\n"
    ref = outcome["accepted_ref"]
    assert ref == f"refs/fleet/accepted/{outcome['accept_id']}"
    assert git(root, "rev-parse", ref).strip() == snap["commit"]
    assert (root / "ToDo" / "a.md").read_text(encoding="utf-8") == "next window, not reviewed\n"


def test_lead_state_md_is_outside_the_seal(env, monkeypatch):
    """1.3: STATE.md ведущего — проекция; его байты не входят в печать и не коммитятся с вехой."""
    store, root, integ = _seal_round(env, monkeypatch, {
        "ToDo/lead/LEDGER.json": "{}\n", "ToDo/lead/STATE.md": "state\n"})
    seal = events(store, "submission_file_seal")[-1]
    assert set(seal["files"]) == {"ToDo/lead/LEDGER.json"}
    assert seal["excluded"] == ["ToDo/lead/STATE.md"]
    _accept(store)
    integ.tick()
    committed = git(root, "show", "--format=", "--name-only", "HEAD")
    assert "ToDo/lead/LEDGER.json" in committed and "STATE.md" not in committed


def test_refused_seal_is_typed_and_terminal(env, monkeypatch):
    """1.3: печать невозможна — событие с причиной; интегратор пишет её в reason_code и не
    повторяет приёмку вечно (раньше — missing_trusted_submission_file_seal каждые ~20 мин)."""
    from dev_mcp.fleet import queue

    real = queue._legacy_submission_files

    def refuse(tx, claim, worker_id, scope_out=None, meta=None):
        if meta is not None:
            meta["refused"] = "path_is_link"
        return None

    monkeypatch.setattr(queue, "_legacy_submission_files", refuse)
    store, root, integ = _seal_round(env, monkeypatch, {"ToDo/a.md": "milestone\n"})
    monkeypatch.setattr(queue, "_legacy_submission_files", real)
    refusal = events(store, "submission_seal_refused")
    assert refusal and refusal[-1]["reason"] == "path_is_link"
    _accept(store)
    integ.tick()
    outcome = events(store, "integrate")[-1]
    assert outcome.get("reason_code") == "seal_refused:path_is_link", outcome
    assert not outcome.get("deferred") and "повтор прекращён" in outcome.get("skipped", "")
    before = len(events(store, "integrate"))
    integ.tick()
    assert len(events(store, "integrate")) == before


def test_truncated_seal_is_a_typed_refusal(env, monkeypatch):
    """1.3: зона шире MAX_COMMIT_PATHS — печать с флагом усечения, интегратор — seal_truncated."""
    from dev_mcp.fleet import integrate as integrate_mod

    monkeypatch.setattr(integrate_mod, "MAX_COMMIT_PATHS", 2)
    store, root, integ = _seal_round(env, monkeypatch, {
        "ToDo/a.md": "a\n", "ToDo/b.md": "b\n", "ToDo/c.md": "c\n"})
    seal = events(store, "submission_file_seal")[-1]
    assert seal["truncated"] == 3 and len(seal["files"]) == 2
    _accept(store)
    with store.read() as tx:
        accept_id = int(tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'"))
    sealed, reason = integ._accepted_file_seal("P", "TZ-work", accept_id, ["ToDo/a.md"])
    assert sealed is None and reason == "seal_truncated"


def test_seal_of_subfolder_project_reads_bytes_under_project_root(tmp_path, monkeypatch):
    # 08.11: проект-подпапка (Main Hub = `_todo/hub-fleet` в репозитории пульта). Пути porcelain —
    # от верха репозитория, а байты читались как `root / путь` — печать видела файл «удалённым».
    from dev_mcp.fleet import goals, queue
    repo = tmp_path / "Repo"
    root = repo / "Sub" / "hub"
    (root / "ToDo").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "top.txt").write_text("top\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    monkeypatch.setattr(goals, "project_info", lambda _p: {"commit": True, "claim_trees": False})
    target = root / "ToDo" / "TZ-sub.md"
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)", ("P", str(root)),
                  event={"kind": "test_setup"})
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES ('W-A','W-A','chatgpt',?,?)",
                  (now(), now()), event={"kind": "test_setup"})
    add_task(store, "TZ-sub", [norm_path(str(target))])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE project='P' AND tz='TZ-sub'", (),
                  event={"kind": "test_setup"})
    assert queue.take_next(store, "W-A", "P").tz == "TZ-sub"
    target.write_text("sub candidate\n", encoding="utf-8")
    queue.submit(store, "W-A", "submitted from a subfolder project")
    with store.read() as tx:
        seal = tx.one("SELECT payload FROM events WHERE kind='submission_file_seal'")
        snap = tx.one("SELECT payload FROM events WHERE kind='submission_snapshot'")
    files = json.loads(seal["payload"])["files"]
    assert files == {"Sub/hub/ToDo/TZ-sub.md": hashlib.sha256(target.read_bytes()).hexdigest()}
    snapshot = json.loads(snap["payload"])
    assert snapshot.get("written") == 1 and "refused" not in snapshot


def test_candidate_result_is_reused_for_same_head_bytes_and_commands(env, monkeypatch):
    # 08.11: отложенная приёмка заново строила worktree и гоняла приёмку на тех же байтах.
    store, root, _, temp = env
    integ = make(store, temp)
    (root / "base.txt").write_text("changed\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(integ, "_candidate_run", lambda *a, **k: calls.append(1) or (True, ""))
    assert integ._candidate(str(root), ["base.txt"], "P", ["cmd"]) == (True, "")
    assert integ._candidate(str(root), ["base.txt"], "P", ["cmd"]) == (True, "")
    assert len(calls) == 1
    (root / "base.txt").write_text("changed again\n", encoding="utf-8")
    integ._candidate(str(root), ["base.txt"], "P", ["cmd"])
    assert len(calls) == 2  # другие байты — новая проверка
    monkeypatch.setattr(integ, "_candidate_run", lambda *a, **k: calls.append(1) or (False, "validation_timeout"))
    (root / "base.txt").write_text("third\n", encoding="utf-8")
    integ._candidate(str(root), ["base.txt"], "P", ["cmd"])
    integ._candidate(str(root), ["base.txt"], "P", ["cmd"])
    assert len(calls) == 4  # сбой среды не запоминается
