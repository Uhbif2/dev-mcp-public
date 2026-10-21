from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.queue import submit, take_next
from dev_mcp.fleet.review import (
    _matches_acceptance_run, _norm_command, decide, open_request, open_review,
)
from dev_mcp.fleet.store import Store


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def test_acceptance_command_normalizes_safe_direct_batch_like_cmd_wrapper() -> None:
    assert _norm_command(r"cmd.exe /d /c check.cmd --fast") == _norm_command(r"check.cmd --fast")
    assert _norm_command(r"cmd /s /c call tools\verify.bat") == _norm_command(r"tools\verify.bat")


def setup_store(tmp_path: Path) -> Store:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            ("P", str(tmp_path / "P")),
            event={"kind": "test_setup"},
        )
        for worker in ("W1", "W2", "W3"):
            stamp = now()
            tx.change(
                "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
                "VALUES(?,?,?,?,?,'')",
                (worker, worker, "chatgpt", stamp, stamp),
                event={"kind": "test_setup", "worker_id": worker},
            )
    return store


def add_task(
    store: Store,
    tz: str,
    *,
    state: str = "ready",
    kind: str = "work",
    author_worker: str | None = None,
    owns: tuple[str, ...] = ("src/work.py",),
    resources: tuple[str, ...] = (),
    acceptance: tuple[str, ...] = ("python -m pytest tests/unit.py -q",),
) -> None:
    stamp = now()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks("
            "project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "P",
                tz,
                tz,
                state,
                5,
                kind,
                f"ToDo/{tz}.md",
                "sha",
                "[]",
                json.dumps(list(owns)),
                json.dumps(list(resources)),
                json.dumps(list(acceptance)),
                author_worker,
                "",
                stamp,
                stamp,
            ),
            event={"kind": "test_setup", "project": "P", "tz": tz},
        )


def add_original_claim(store: Store, tz: str, worker: str = "W1") -> None:
    with store.tx() as tx:
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES(?,?,?,'active',?)",
            ("P", tz, worker, now()),
            event={"kind": "test_setup", "worker_id": worker, "project": "P", "tz": tz},
        )


def record_run(store: Store, worker: str, argv: list[str], rc: int, *, cwd: str = "") -> None:
    with store.tx() as tx:
        tx.record_event(
            kind="run",
            worker_id=worker,
            tool="run",
            path_norm=cwd,
            rc=rc,
            payload={"argv": argv},
        )


def task_row(store: Store, tz: str):
    with store.tx() as tx:
        return tx.one("SELECT * FROM tasks WHERE project='P' AND tz=?", (tz,))


def test_open_review_is_ready_owned_by_nobody_and_not_given_to_author(tmp_path: Path):
    store = setup_store(tmp_path)
    add_task(store, "TZ-1", state="submitted", author_worker="W1")
    review_tz = open_review(store, "P", "TZ-1")

    row = task_row(store, review_tz)
    assert row["kind"] == "review"
    assert row["state"] == "ready"
    assert json.loads(row["owns"]) == []
    assert json.loads(row["resources"]) == []
    assert row["author_worker"] == "W1"
    assert "Ревью своей работы запрещено" in row["body"]
    assert "python -m pytest tests/unit.py -q" in row["body"]

    assert take_next(store, "W1", "P") is None
    taken = take_next(store, "W2", "P")
    assert taken is not None
    assert taken.tz == review_tz


def test_open_review_is_idempotent_while_review_is_unfinished(tmp_path: Path):
    store = setup_store(tmp_path)
    add_task(store, "TZ-1", state="submitted", author_worker="W1")
    first = open_review(store, "P", "TZ-1")
    second = open_review(store, "P", "TZ-1")
    assert first == second


def test_accept_without_reviewers_own_successful_acceptance_run_is_rejected(tmp_path: Path):
    store = setup_store(tmp_path)
    command = "python -m pytest tests/unit.py -q"
    add_task(store, "TZ-1", state="submitted", author_worker="W1", acceptance=(command,))
    add_original_claim(store, "TZ-1")
    review_tz = open_review(store, "P", "TZ-1")
    assert take_next(store, "W2", "P").tz == review_tz

    with pytest.raises(ValueError, match="reviewer's own successful run"):
        decide(store, "W2", "accept", "looks fine")

    assert task_row(store, "TZ-1")["state"] == "submitted"
    assert task_row(store, review_tz)["state"] == "taken"


def test_accept_requires_matching_command_rc_zero_and_releases_claims(tmp_path: Path):
    store = setup_store(tmp_path)
    command = "python -m pytest tests/unit.py -q"
    add_task(store, "TZ-1", state="submitted", author_worker="W1", acceptance=(command,))
    add_original_claim(store, "TZ-1")
    review_tz = open_review(store, "P", "TZ-1")
    assert take_next(store, "W2", "P").tz == review_tz

    record_run(store, "W2", ["python", "-m", "pytest", "tests/unit.py", "-q"], 1)
    with pytest.raises(ValueError):
        decide(store, "W2", "accept", "red")

    record_run(store, "W2", ["python", "-m", "pytest", "tests/unit.py", "-q"], 0)
    decide(store, "W2", "accept", "green")

    assert task_row(store, "TZ-1")["state"] == "accepted"
    assert task_row(store, review_tz)["state"] == "accepted"
    with store.tx() as tx:
        active = tx.scalar(
            "SELECT COUNT(*) FROM claims WHERE project='P' "
            "AND tz IN (?,?) AND state='active'",
            ("TZ-1", review_tz),
        )
        event = tx.one(
            "SELECT payload FROM events WHERE kind='accept' AND project='P' AND tz='TZ-1' "
            "ORDER BY id DESC LIMIT 1"
        )
    assert active == 0
    assert json.loads(event["payload"])["note"] == "green"


def test_review_accepts_absolute_script_paths_via_verified_relative_run(tmp_path: Path):
    store = setup_store(tmp_path)
    root = "C:/hub/_todo/hub-fleet"
    command = f"py -3 -X utf8 {root}/design/check_answer.py {root}/design/q5-v1.md"
    relative = ["py", "-3", "-X", "utf8", "design/check_answer.py", "design/q5-v1.md"]
    add_task(store, "TZ-abs", state="submitted", author_worker="W1", acceptance=(command,))
    add_original_claim(store, "TZ-abs")
    review_tz = open_review(store, "P", "TZ-abs")
    assert take_next(store, "W2", "P").tz == review_tz

    # A green command elsewhere, or a red command in the right directory,
    # must not count as evidence for the acceptance script.
    record_run(store, "W2", relative, 0, cwd=root + "-other")
    record_run(store, "W2", relative, 1, cwd=root)
    with pytest.raises(ValueError, match="own successful run"):
        decide(store, "W2", "accept", "no trusted green event")

    record_run(store, "W2", relative, 0, cwd=root)
    decide(store, "W2", "accept", "green safe relative equivalent")
    assert task_row(store, "TZ-abs")["state"] == "accepted"


def test_submit_uses_same_safe_path_equivalence(tmp_path: Path, monkeypatch):
    from dev_mcp.fleet import queue

    monkeypatch.setattr(queue, "REQUIRE_GREEN_SUBMIT", True)
    store = setup_store(tmp_path)
    root = "C:/hub/_todo/hub-fleet"
    command = f"py -3 -X utf8 {root}/design/check_answer.py {root}/design/q7-v1.md"
    add_task(store, "TZ-submit", acceptance=(command,))
    add_original_claim(store, "TZ-submit")

    def submit_guard() -> str:
        with store.read() as tx:
            claim = tx.one("SELECT * FROM claims WHERE worker_id='W1' AND state='active'")
            return queue._unproven_submit_reason(store, tx, "W1", claim, "done")

    assert "без зелёной приёмки" in submit_guard()
    record_run(store, "W1", ["py", "-3", "-X", "utf8", "design/check_answer.py", "design/q7-v1.md"], 0, cwd=root)
    assert submit_guard() == ""


@pytest.mark.parametrize("cwd, actual", [
    ("", ["py", "-3", "-X", "utf8", "design/check_answer.py", "design/q.md"]),
    ("C:/hub/_todo/hub-fleet-evil", ["py", "-3", "-X", "utf8", "design/check_answer.py", "design/q.md"]),
    ("C:/hub/_todo/hub-fleet", ["py", "-3", "-X", "utf8", "../design/check_answer.py", "design/q.md"]),
    ("C:/hub/_todo/hub-fleet", ["py", "-3", "-X", "utf8", "design/other.py", "design/q.md"]),
    ("C:/hub/_todo/hub-fleet", ["py", "-3", "-X", "utf8", "design/check_answer.py", "design/other.md"]),
    ("C:/hub/_todo/hub-fleet", ["py", "-3", "-X", "utf8", "D:design/check_answer.py", "design/q.md"]),
])
def test_absolute_acceptance_equivalence_refuses_untrusted_variants(cwd: str, actual: list[str]) -> None:
    root = "C:/hub/_todo/hub-fleet"
    required = f"py -3 -X utf8 {root}/design/check_answer.py {root}/design/q.md"
    assert not _matches_acceptance_run(required, " ".join(actual), cwd)


def test_identical_relative_acceptance_from_foreign_cwd_is_not_review_proof(tmp_path):
    store=setup_store(tmp_path)
    command='python -m pytest tests/unit.py -q'
    add_task(store,'TZ-cwd',state='submitted',author_worker='W1',acceptance=(command,))
    add_original_claim(store,'TZ-cwd')
    open_review(store,'P','TZ-cwd')
    take_next(store,'W2','P')
    root=str(tmp_path/'P')
    record_run(store,'W2',command.split(),0,cwd=root+'-foreign')
    with pytest.raises(ValueError,match='own successful run'):
        decide(store,'W2','accept','identical argv, wrong directory')
    record_run(store,'W2',command.split(),0,cwd=root)
    decide(store,'W2','accept','trusted project directory')
    assert task_row(store,'TZ-cwd')['state']=='accepted'


def test_identical_relative_acceptance_from_foreign_cwd_is_not_submit_proof(tmp_path,monkeypatch):
    from dev_mcp.fleet import queue
    monkeypatch.setattr(queue,'REQUIRE_GREEN_SUBMIT',True)
    store=setup_store(tmp_path)
    command='python -m pytest tests/unit.py -q'
    add_task(store,'TZ-cwd-submit',acceptance=(command,))
    add_original_claim(store,'TZ-cwd-submit')
    record_run(store,'W1',command.split(),0,cwd=str(tmp_path/'P-foreign'))
    with store.read() as tx:
        claim=dict(tx.one("SELECT * FROM claims WHERE worker_id='W1' AND state='active'"))
        assert 'без зелёной приёмки' in queue._unproven_submit_reason(store,tx,'W1',claim,'done')
    record_run(store,'W1',command.split(),0,cwd=str(tmp_path/'P'))
    with store.read() as tx:
        assert queue._unproven_submit_reason(store,tx,'W1',claim,'done')==''


def test_reject_returns_source_to_ready_and_creates_fix_task(tmp_path: Path):
    store = setup_store(tmp_path)
    add_task(store, "TZ-1", state="submitted", author_worker="W1")
    add_original_claim(store, "TZ-1")
    review_tz = open_review(store, "P", "TZ-1")
    assert take_next(store, "W2", "P").tz == review_tz

    decide(store, "W2", "reject", "стык сломан")

    assert task_row(store, "TZ-1")["state"] == "submitted"
    assert task_row(store, review_tz)["state"] == "accepted"
    fix = task_row(store, "fix:TZ-1")
    assert fix is not None
    assert fix["kind"] == "fix"
    assert fix["state"] == "ready"
    assert "стык сломан" in fix["body"]
    assert json.loads(fix["owns"]) == ["src/work.py"]


def test_accepting_fix_closes_original_source(tmp_path: Path):
    store = setup_store(tmp_path)
    command = "python -m pytest tests/unit.py -q"
    add_task(store, "TZ-1", state="submitted", author_worker="W1", acceptance=(command,))
    add_original_claim(store, "TZ-1")
    review_tz = open_review(store, "P", "TZ-1")
    assert take_next(store, "W2", "P").tz == review_tz
    decide(store, "W2", "reject", "стык сломан")

    fix = take_next(store, "W3", "P")
    assert fix is not None
    assert fix.tz == "fix:TZ-1"
    submit(store, "W3", "fixed")
    fix_review_tz = open_review(store, "P", fix.tz)

    fix_review = take_next(store, "W2", "P")
    assert fix_review is not None
    assert fix_review.tz == fix_review_tz
    record_run(store, "W2", ["python", "-m", "pytest", "tests/unit.py", "-q"], 0)
    decide(store, "W2", "accept", "fix verified")

    assert task_row(store, "TZ-1")["state"] == "accepted"
    assert task_row(store, "fix:TZ-1")["state"] == "accepted"
    assert task_row(store, fix_review_tz)["state"] == "accepted"


def test_non_owner_request_becomes_ready_and_is_taken_without_owner_approval(tmp_path: Path):
    store = setup_store(tmp_path)
    add_task(
        store,
        "TZ-9",
        state="submitted",
        owns=("src/target.py",),
        resources=("stand",),
        acceptance=("python -m pytest tests/target.py -q",),
    )

    request_tz = open_request(
        store,
        "r1",
        kind="other",
        summary="Нужен новый helper",
        detail="без него дублируется логика",
        to="P/TZ-9",
        worker_id="W1",
        source_tz="TZ-1",
    )
    assert request_tz == "request:r1"
    row = task_row(store, request_tz)
    assert row["kind"] == "request"
    assert row["state"] == "ready"
    assert "P/TZ-9" in row["body"]
    assert json.loads(row["owns"]) == ["src/target.py"]
    assert json.loads(row["resources"]) == ["stand"]
    assert json.loads(row["acceptance"]) == ["python -m pytest tests/target.py -q"]
    taken = take_next(store, "W2", "P")
    assert taken is not None
    assert taken.tz == request_tz


def test_missing_explicit_task_cannot_silently_drop_resource_and_acceptance(tmp_path: Path):
    store = setup_store(tmp_path)
    with pytest.raises(ValueError, match="не зарегистрирована"):
        open_request(store, "missing-target", kind="other", summary="rebuild installer",
                     to="P/TZ-504", path="packaging/build.py", worker_id="W1")
    with store.read() as tx:
        assert tx.scalar("SELECT count(*) FROM tasks WHERE kind='request'") == 0


@pytest.mark.parametrize("kind", ["access", "bin", "root", "network"])
def test_access_request_stays_on_legacy_channel(tmp_path: Path, kind: str):
    store = setup_store(tmp_path)
    result = open_request(
        store,
        "r-access",
        kind=kind,
        summary="Нужен доступ",
        to="queue",
        project="P",
        worker_id="W1",
    )
    assert result is None
    with store.tx() as tx:
        count = tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='request'")
    assert count == 0


def test_owner_request_stays_on_legacy_channel(tmp_path: Path):
    store = setup_store(tmp_path)
    assert (
        open_request(
            store,
            "r-owner",
            kind="other",
            summary="Вопрос владельцу",
            to="owner",
            worker_id="W1",
        )
        is None
    )


def test_sixth_reject_creates_ready_end_to_end_repair_instead_of_freezing(tmp_path: Path):
    store = setup_store(tmp_path)
    add_task(store, "TZ-1", state="submitted", author_worker="W1")
    for index in range(1, 6):
        tz = "fix:TZ-1" if index == 1 else f"fix:TZ-1:{index}"
        add_task(
            store,
            tz,
            state="submitted" if index == 5 else "rejected",
            kind="fix",
            author_worker="W3",
        )
        with store.tx() as tx:
            tx.change(
                "UPDATE tasks SET body=? WHERE project='P' AND tz=?",
                ("[fleet-fix] source=P/TZ-1\nprevious repair", tz),
                event={"kind": "test_setup", "project": "P", "tz": tz},
            )

    review_tz = open_review(store, "P", "fix:TZ-1:5")
    assert take_next(store, "W2", "P").tz == review_tz
    decide(store, "W2", "reject", "пятый локальный патч не устранил общий корень")

    repair = task_row(store, "fix:TZ-1:6")
    assert repair["state"] == "ready"
    assert "diagnostic/end-to-end repair" in repair["body"]
    assert "не дроби найденные дефекты" in repair["body"]
    with store.tx() as tx:
        event = tx.one(
            "SELECT payload FROM events WHERE project='P' AND tz='fix:TZ-1:5' "
            "AND kind='reject' ORDER BY id DESC LIMIT 1"
        )
    payload = json.loads(event["payload"])
    assert payload["fix_round"] == 6
    assert payload["diagnostic_repair"] is True


def test_third_reject_enters_automatic_final_fix_round_without_owner(tmp_path: Path):
    store = setup_store(tmp_path)
    add_task(store, "TZ-1", state="submitted", author_worker="W1")
    add_original_claim(store, "TZ-1")
    review_tz = open_review(store, "P", "TZ-1")
    assert take_next(store, "W2", "P").tz == review_tz
    decide(store, "W2", "reject", "первое замечание")

    for expected_fix, remark in (("fix:TZ-1", "второе замечание"), ("fix:TZ-1:2", "третье замечание")):
        fix = take_next(store, "W3", "P")
        assert fix is not None and fix.tz == expected_fix
        submit(store, "W3", "fixed")
        fix_review_tz = open_review(store, "P", fix.tz)
        assert take_next(store, "W2", "P").tz == fix_review_tz
        decide(store, "W2", "reject", remark)

    assert task_row(store, "fix:TZ-1:2")["state"] == "rejected"
    final_fix = task_row(store, "fix:TZ-1:3")
    assert final_fix["state"] == "ready"
    assert "3-й отказ подряд" in final_fix["body"]
    assert "[fleet-final]" in final_fix["body"]
    taken = take_next(store, "W3", "P")
    assert taken is not None and taken.tz == "fix:TZ-1:3"
    with store.tx() as tx:
        rejection = tx.one(
            "SELECT kind,payload FROM events WHERE project='P' AND tz='fix:TZ-1:2' "
            "AND kind='reject' ORDER BY id DESC LIMIT 1"
        )
    assert rejection is not None


def test_project_gate_run_on_the_same_tree_counts_for_reviewer(tmp_path: Path, monkeypatch):
    """Гейт проекта (~880 с) не гоняется второй раз по тому же дереву: rc пишет сервер."""
    from dev_mcp.fleet import goals

    gate = "py -3 tools/check_all.py"
    monkeypatch.setattr(goals, "project_info", lambda p: {"gate": gate} if p == "P" else {})
    store = setup_store(tmp_path)
    unit = "python -m pytest tests/unit.py -q"
    add_task(store, "styk:1", state="submitted", author_worker="W1", acceptance=(gate, unit))
    add_original_claim(store, "styk:1")
    review_tz = open_review(store, "P", "styk:1")
    assert take_next(store, "W2", "P").tz == review_tz
    with store.tx() as tx:  # прогон автора: проект и дерево зоны пишет сервер
        tx.record_event(kind="run", worker_id="W1", project="P", tz="styk:1", rc=0,
                        payload={"argv": ["py", "-3", "tools/check_all.py"], "tree": "T1"})
        tx.record_event(kind="run", worker_id="W2", project="P", tz=review_tz, rc=0,
                        payload={"argv": ["python", "-m", "pytest", "tests/unit.py", "-q"], "tree": "T1"})

    with pytest.raises(ValueError, match="reviewer's own successful run.*check_all"):
        decide(store, "W2", "accept", "гейт автора без дерева не в счёт", tree=None)
    with pytest.raises(ValueError, match="check_all"):
        decide(store, "W2", "accept", "дерево другое", tree="T2")
    decide(store, "W2", "accept", "гейт автора по этому же дереву", tree="T1")
    assert task_row(store, "styk:1")["state"] == "accepted"


def test_shared_gate_proof_does_not_cover_short_commands(tmp_path: Path, monkeypatch):
    from dev_mcp.fleet import goals

    monkeypatch.setattr(goals, "project_info", lambda p: {"gate": "py -3 tools/check_all.py"})
    store = setup_store(tmp_path)
    unit = "python -m pytest tests/unit.py -q"
    add_task(store, "TZ-1", state="submitted", author_worker="W1", acceptance=(unit,))
    add_original_claim(store, "TZ-1")
    review_tz = open_review(store, "P", "TZ-1")
    assert take_next(store, "W2", "P").tz == review_tz
    with store.tx() as tx:
        tx.record_event(kind="run", worker_id="W1", project="P", tz="TZ-1", rc=0,
                        payload={"argv": ["python", "-m", "pytest", "tests/unit.py", "-q"], "tree": "T1"})

    with pytest.raises(ValueError, match="reviewer's own successful run"):
        decide(store, "W2", "accept", "чужой прогон", tree="T1")


def test_acceptance_run_of_active_task_is_recognised(tmp_path: Path, monkeypatch):
    """run команды приёмки своей задачи живёт до потолка политики, а не до умолчания 300 с."""
    from dev_mcp.fleet import goals
    from dev_mcp.fleet.tools import FleetContext

    monkeypatch.setattr(goals, "project_info", lambda p: {"gate": "py -3 tools/check_all.py"})
    store = setup_store(tmp_path)
    add_task(store, "TZ-1", acceptance=("cmd.exe /d /c check.cmd",))
    assert take_next(store, "W1", "P").tz == "TZ-1"
    ctx = FleetContext(store=store)

    assert ctx.is_acceptance_run("W1", ["check.cmd"])
    assert ctx.is_acceptance_run("W1", ["py", "-3", "tools/check_all.py"])  # гейт проекта
    assert not ctx.is_acceptance_run("W1", ["python", "-c", "print(1)"])
    assert not ctx.is_acceptance_run("W2", ["check.cmd"])  # у W2 нет задачи


def _red_gate(store: Store, tmp_path: Path, failed: list[str], rc: int = 1) -> None:
    with store.tx() as tx:
        tx.record_event(kind="gate", project="P", tz=None, rc=rc, payload={"failed": failed})


def test_red_trunk_does_not_hold_review_issue(tmp_path: Path):
    """План консорциума 11.10, п.3: ревью судит снимок кандидата; красный ствол держит
    интеграцию, не выдачу ревью (было: ревью в зоне поломки ждало стыка — круг урока 34)."""
    root = str(tmp_path / "P").replace("\\", "/").casefold()
    store = setup_store(tmp_path)
    command = "python -m pytest tests/unit.py -q"
    add_task(store, "TZ-1", state="submitted", author_worker="W1", owns=(f"{root}/test/test_x.py",),
             acceptance=(command,))
    add_task(store, "TZ-2", state="submitted", author_worker="W1", owns=(f"{root}/src/other.py",),
             acceptance=(command,))
    add_original_claim(store, "TZ-1")
    add_original_claim(store, "TZ-2")
    r1, r2 = open_review(store, "P", "TZ-1"), open_review(store, "P", "TZ-2")
    _red_gate(store, tmp_path, ["test/test_x.py"])

    assert {take_next(store, "W2", "P").tz, take_next(store, "W3", "P").tz} == {r1, r2}


def test_accept_is_not_refused_by_red_trunk(tmp_path: Path):
    root = str(tmp_path / "P").replace("\\", "/").casefold()
    store = setup_store(tmp_path)
    command = "python -m pytest tests/unit.py -q"
    add_task(store, "TZ-1", state="submitted", author_worker="W1", owns=(f"{root}/test",), acceptance=(command,))
    add_original_claim(store, "TZ-1")
    review_tz = open_review(store, "P", "TZ-1")
    assert take_next(store, "W2", "P").tz == review_tz
    record_run(store, "W2", ["python", "-m", "pytest", "tests/unit.py", "-q"], 0)
    _red_gate(store, tmp_path, ["test/test_x.py"])  # покраснел во время ревью

    decide(store, "W2", "accept", "green")  # цвет ствола вердикт не держит
    assert task_row(store, "TZ-1")["state"] == "accepted"


def test_styk_family_is_accepted_while_trunk_is_red(tmp_path: Path):
    root = str(tmp_path / "P").replace("\\", "/").casefold()
    store = setup_store(tmp_path)
    command = "python -m pytest tests/unit.py -q"
    add_task(store, "styk:1", state="submitted", author_worker="W1", owns=(f"{root}/test/test_x.py",),
             acceptance=(command,))
    add_original_claim(store, "styk:1")
    review_tz = open_review(store, "P", "styk:1")
    _red_gate(store, tmp_path, ["test/test_x.py"])
    assert take_next(store, "W2", "P").tz == review_tz
    record_run(store, "W2", ["python", "-m", "pytest", "tests/unit.py", "-q"], 0)

    decide(store, "W2", "accept", "стык починен")
    assert task_row(store, "styk:1")["state"] == "accepted"


def test_old_styk_with_whole_project_zone_does_not_stop_reviews(tmp_path: Path):
    root = str(tmp_path / "P").replace("\\", "/").casefold()
    store = setup_store(tmp_path)
    add_task(store, "styk:1", state="submitted", owns=(root,))
    add_task(store, "TZ-1", state="submitted", author_worker="W1", owns=(f"{root}/src/a.py",))
    add_original_claim(store, "TZ-1")
    review_tz = open_review(store, "P", "TZ-1")
    _red_gate(store, tmp_path, [])
    assert take_next(store, "W2", "P").tz == review_tz
