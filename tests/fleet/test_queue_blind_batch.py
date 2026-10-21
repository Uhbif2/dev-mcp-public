"""A blind Mill Works reviewer must not have seen its miner's candidate."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.briefing import _blind_brief, _blind_is_sealed
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.queue import blind_read_denial, finish, peek_next, requeue_stale, submit, take_next
from dev_mcp.fleet.review import _blind_submission_sealed
from test_queue import StubStore, add_project, add_task, add_worker, now


BATCH = "f5b38bb9eaeb"
MA = f"TZ-MA-batch-{BATCH}"
MR = f"TZ-MR-batch-{BATCH}"
PROJECT = "Mill Works"


def _store(tmp_path: Path, *, frozen: bool = False) -> StubStore:
    store = StubStore.open(tmp_path / "queue.sqlite")
    add_project(store, PROJECT)
    add_task(store, MR, project=PROJECT,
             state="frozen" if frozen else "ready", kind="work")
    add_worker(store, "worker")
    return store


def _historical_claim(store: StubStore, tz: str) -> None:
    with store.tx() as tx:
        tx.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES(?,?,?,?,?)",
            (PROJECT, tz, "worker", "released", now(-10)),
        )


def _recorded_read(store: StubStore, path: str) -> None:
    with store.tx() as tx:
        tx.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,path_norm,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (now(-2), "read", "worker", PROJECT, MR, path, "{}"),
        )


def test_clean_worker_can_take_blind_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = take_next(store, "worker")
    assert task is not None and task.tz == MR


@pytest.mark.parametrize("exposure", [
    MA,
    f"fix:{MA}",
    f"review:{MA}",
    f"review:fix:{MA}",
    f"review:fix:{MA}:1",
])
def test_miner_author_or_reviewer_exposure_denied(tmp_path: Path, exposure: str) -> None:
    store = _store(tmp_path)
    _historical_claim(store, exposure)
    assert take_next(store, "worker") is None


def test_author_worker_fallback_without_a_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    add_task(store, MA, project=PROJECT, state="accepted",
             author_worker="worker")
    assert take_next(store, "worker") is None


def test_unrelated_batch_does_not_contaminate_blind_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _historical_claim(store, "review:TZ-MA-batch-aabbccddeeff")
    task = take_next(store, "worker")
    assert task is not None and task.tz == MR


@pytest.mark.parametrize("path", [
    rf"C:\hub\mill-works\data\batches\out\batch-{BATCH}\MANIFEST.md",
    f"C:/hub/mill-works/data/reviews/batch-{BATCH}/case-01/verdict.json",
])
def test_registered_read_of_candidate_blocks_blind_assignment(
    tmp_path: Path, path: str,
) -> None:
    store = _store(tmp_path)
    _recorded_read(store, path)
    assert take_next(store, "worker") is None


def test_unrelated_manifest_read_remains_eligible(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _recorded_read(
        store,
        "C:/hub/mill-works/data/batches/out/batch-aabbccddeeff/MANIFEST.md",
    )
    task = take_next(store, "worker")
    assert task is not None and task.tz == MR


def test_thawed_task_still_excludes_exposed_worker_and_allows_clean_one(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, frozen=True)
    _historical_claim(store, f"review:{MA}")
    add_worker(store, "clean")
    # The same readiness transition used after reactivation/thaw: the guard
    # must depend on worker history, not the frozen task's old state.
    with store.tx() as tx:
        tx.execute("UPDATE tasks SET state='ready' WHERE project=? AND tz=?",
                   (PROJECT, MR))
    assert take_next(store, "worker") is None
    task = take_next(store, "clean")
    assert task is not None and task.tz == MR


def _submitted_ma_with_ready_mr(tmp_path: Path) -> StubStore:
    """Real take -> submit events, followed by synthetic MA acceptance."""
    store = _store(tmp_path, frozen=True)
    add_task(store, MA, project=PROJECT, kind="work")
    add_worker(store, "clean")
    add_worker(store, "clean2")
    task = take_next(store, "worker")
    assert task is not None and task.tz == MA
    submit(store, "worker", "Synthetic miner manifest submitted")
    with store.tx() as tx:
        assert tx.one(
            "SELECT kind FROM events WHERE worker_id='worker' AND tz=? "
            "AND kind='submit'", (MA,)
        ) is not None
        tx.execute(
            "UPDATE tasks SET state='accepted' WHERE project=? AND tz=?",
            (PROJECT, MA),
        )
        tx.execute(
            "UPDATE tasks SET state='ready' WHERE project=? AND tz=?",
            (PROJECT, MR),
        )
    return store


def test_e2e_miner_take_submit_denies_own_mr_but_allows_clean_worker(
    tmp_path: Path,
) -> None:
    store = _submitted_ma_with_ready_mr(tmp_path)
    assert take_next(store, "worker") is None
    task = take_next(store, "clean")
    assert task is not None and task.tz == MR


def test_e2e_finish_does_not_restore_miner_eligibility(tmp_path: Path) -> None:
    store = _submitted_ma_with_ready_mr(tmp_path)
    assert take_next(store, "clean").tz == MR
    assert finish(store, "clean", "handoff: retry with another reviewer") == "ready"
    assert take_next(store, "worker") is None
    assert take_next(store, "clean2").tz == MR


def test_e2e_stale_requeue_does_not_restore_miner_eligibility(
    tmp_path: Path,
) -> None:
    store = _submitted_ma_with_ready_mr(tmp_path)
    assert take_next(store, "clean").tz == MR
    with store.tx() as tx:
        tx.execute(
            "UPDATE workers SET last_seen=? WHERE id='clean'",
            (now(-120),),
        )
    returned = requeue_stale(store, silence_min=30)
    assert [(task.project, task.tz) for task in returned] == [(PROJECT, MR)]
    assert take_next(store, "worker") is None
    assert take_next(store, "clean2").tz == MR


def test_e2e_submit_event_survives_compacted_claims_and_author(
    tmp_path: Path,
) -> None:
    store = _submitted_ma_with_ready_mr(tmp_path)
    with store.tx() as tx:
        tx.execute("DELETE FROM claims WHERE project=? AND tz=?", (PROJECT, MA))
        tx.execute(
            "UPDATE tasks SET author_worker=NULL WHERE project=? AND tz=?",
            (PROJECT, MA),
        )
    assert take_next(store, "worker") is None
    assert take_next(store, "clean").tz == MR


def test_e2e_prior_ma_review_claim_alone_blocks_blind_mr(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, frozen=True)
    add_worker(store, "ma-reviewer")
    add_worker(store, "fresh")
    add_task(store, f"review:{MA}", project=PROJECT, kind="review")
    assert take_next(store, "ma-reviewer").tz == f"review:{MA}"
    assert finish(store, "ma-reviewer", "handoff: review released") == "ready"
    with store.tx() as tx:
        tx.execute(
            "UPDATE tasks SET state='accepted' WHERE project=? AND tz=?",
            (PROJECT, f"review:{MA}"),
        )
        tx.execute(
            "UPDATE tasks SET state='ready' WHERE project=? AND tz=?",
            (PROJECT, MR),
        )
    assert take_next(store, "ma-reviewer") is None
    assert take_next(store, "fresh").tz == MR


@pytest.mark.parametrize('through_run', [False, True])
def test_sealed_blind_byte_replacement_revokes_candidate_read_and_exposure_prevents_reseal(
    tmp_path: Path, through_run: bool,
) -> None:
    """The server SHA is a commitment to bytes, not a reusable phase flag.

    Use the public seal tool with a real temporary fleet Store, rather than
    manufacturing a plausible seal event: a mutated blind must invalidate both
    the briefing and the read policy, and exposure must forbid a second seal.
    """
    from test_tools import _make_server, _tool_fn

    server, store, root, _audit = _make_server(tmp_path)
    batch = f"batch-{BATCH}"
    manifest = root / "data" / "batches" / "out" / batch / "MANIFEST.md"
    blind = root / "data" / "reviews" / batch / "case-01" / "blind.json"
    manifest.parent.mkdir(parents=True)
    blind.parent.mkdir(parents=True)
    manifest.write_text(
        "<!-- MILL_MANIFEST_JSON\n"
        + json.dumps({"entries": {"case-01": {"outcome": "candidate"}}})
        + "\nMILL_MANIFEST_JSON -->\n", encoding="utf-8",
    )
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            (PROJECT, str(root)), event={"kind": "seed"},
        )
        tx.change(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,?,?,?)",
            ("worker", "worker", "chatgpt", now(), now()), event={"kind": "seed"},
        )
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES(?,?,?,'ready',1,'work','','','[]',?,'[]','[]','',?,?)",
            (PROJECT, MR, "blind reviewer", json.dumps([norm_path(str(blind.parent.parent))]),
             now(), now()), event={"kind": "seed"},
        )
    task = take_next(store, "worker")
    assert task is not None and task.tz == MR
    with store.read() as tx:
        claim_id = int(tx.scalar(
            "SELECT id FROM claims WHERE worker_id='worker' AND state='active'"
        ))

    original = {
        "case_id": "case-01",
        "independent_finding": "Independent finding before candidate exposure",
        "alternative": "Alternative hypothesis",
        "hidden_queries": [
            {"query": "Positive replay one", "negative": False},
            {"query": "Negative replay", "negative": True},
            {"query": "Positive replay two", "negative": False},
        ],
    }
    blind.write_text(json.dumps(original), encoding="utf-8")
    with store.tx() as tx:
        tx.record_event(kind="write", worker_id="worker", project=PROJECT,
                        tz=MR, path_norm=norm_path(str(blind)), payload={})

    seal = _tool_fn(server, "fleet_blind_seal")
    if through_run:
        seal = lambda *, agent: _tool_fn(server, 'run')(
            root=str(root), argv=['fleet.cmd', 'blind-seal'], agent=agent)
    first = seal(agent="worker")
    assert "blind_seal:" in first and batch in first, first
    with store.read() as tx:
        seals = tx.query(
            "SELECT payload FROM events WHERE kind='blind_seal' AND worker_id='worker'"
        )
    assert len(seals) == 1
    assert json.loads(seals[0]["payload"])["claim_id"] == claim_id
    assert _blind_is_sealed(store, "worker", task, batch)
    assert blind_read_denial(store, "worker", str(manifest)) is None
    # A legitimately sealed original MR must still be readable via the public
    # tool; the global pre-seal guard must not become a permanent blanket deny.
    read_file = _tool_fn(server, "read_file")
    sealed_read = read_file(path=str(manifest), agent="worker")
    assert "MILL_MANIFEST_JSON" in sealed_read, sealed_read

    # Attacker changes hidden replay after server approval, without changing
    # its JSON schema or the stored signed SHA. The old implementation merely
    # checked that the seal digest LOOKED like a 64-character hex string.
    altered = {**original, "independent_finding": "Candidate-derived replacement"}
    blind.write_text(json.dumps(altered), encoding="utf-8")
    assert not _blind_is_sealed(store, "worker", task, batch)
    assert "FLEET_BLIND_SEAL_REQUIRED" in (
        blind_read_denial(store, "worker", str(manifest)) or ""
    )

    # Candidate access is already recorded; re-stamping altered bytes cannot
    # erase that exposure or advance the reviewer to the candidate phase.
    with store.tx() as tx:
        tx.record_event(kind="read", worker_id="worker", project=PROJECT,
                        tz=MR, path_norm=norm_path(str(manifest)), payload={})
    second = seal(agent="worker")
    assert "FLEET_BLIND" in second and "blind_seal:" not in second, second
    with store.read() as tx:
        assert tx.scalar(
            "SELECT COUNT(*) FROM events WHERE kind='blind_seal' AND worker_id='worker'"
        ) == 1
    assert not _blind_is_sealed(store, "worker", task, batch)


def test_unsealed_sanitized_fix_request_denies_anonymous_and_spoofed_manifest_reads(
    tmp_path: Path,
) -> None:
    """A fix-request with one owned batch is case-only, even without an agent.

    The real read_file tool must inspect *all active blind claims*. Trusting
    the user-supplied `agent` would disclose the miner's MANIFEST to a fix
    reviewer with prior candidate hints in its rejected task envelope.
    """
    from test_tools import _make_server, _tool_fn

    server, store, root, _audit = _make_server(tmp_path)
    batch = f"batch-{BATCH}"
    manifest = root / "data" / "batches" / "out" / batch / "MANIFEST.md"
    review_dir = root / "data" / "reviews" / batch
    manifest.parent.mkdir(parents=True)
    review_dir.mkdir(parents=True)
    manifest.write_text(
        "<!-- MILL_MANIFEST_JSON\n"
        + json.dumps({"entries": {"case-01": {"outcome": "candidate"}}})
        + "\nMILL_MANIFEST_JSON -->\n", encoding="utf-8",
    )
    fix_tz = "fix:request:blind-example"
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            (PROJECT, str(root)), event={"kind": "seed"},
        )
        for worker in ("fixer", "spoofed"):
            tx.change(
                "INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES(?,?,?,?,?)",
                (worker, worker, "chatgpt", now(), now()), event={"kind": "seed"},
            )
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES(?,?,?,'ready',1,'fix','','','[]',?,'[]','[]',?, ?, ?)",
            (PROJECT, fix_tz, "blind fix reviewer",
             json.dumps([norm_path(str(review_dir))]),
             "Rejected hint: candidate's MANIFEST.md contains private answers",
             now(), now()), event={"kind": "seed"},
        )
    task = take_next(store, "fixer")
    assert task is not None and task.tz == fix_tz
    with store.read() as tx:
        current = tx.one(
            "SELECT body FROM tasks WHERE project=? AND tz=?", (PROJECT, fix_tz)
        )
    assert current is not None
    assert str(current["body"]).startswith('[fleet-blind] {"batch_id":')
    assert "Rejected hint" not in str(current["body"])

    read_file = _tool_fn(server, "read_file")
    for supplied_agent in ("", "spoofed", "fixer"):
        response = read_file(path=str(manifest), agent=supplied_agent)
        assert "FLEET_BLIND_SEAL_REQUIRED" in response, (supplied_agent, response)
        assert "case-01" not in response, (supplied_agent, response)


def test_fresh_mr_fix_two_workers_seal_distinct_attempt_without_old_verdict_leak(
    tmp_path: Path,
) -> None:
    """Rejected candidate never reaches a fresh worker; prior evidence survives."""
    from test_tools import _make_server, _tool_fn

    server, store, root, _audit = _make_server(tmp_path)
    batch = f"batch-{BATCH}"
    review = root / "data" / "reviews" / batch
    manifest = root / "data" / "batches" / "out" / batch / "MANIFEST.md"
    old_blind = review / "case-01" / "blind.json"
    old_verdict = review / "case-01" / "verdict.json"
    manifest.parent.mkdir(parents=True)
    old_blind.parent.mkdir(parents=True)
    manifest.write_text(
        "<!-- MILL_MANIFEST_JSON\n"
        + json.dumps({"entries": {"case-01": {"outcome": "candidate", "body": "private"}}})
        + "\nMILL_MANIFEST_JSON -->\n", encoding="utf-8",
    )
    old_blind.write_text('{"prior":"sealed old independent replay"}', encoding="utf-8")
    old_verdict.write_text('{"reason":"rejected candidate secret"}', encoding="utf-8")
    before = old_blind.read_bytes(), old_verdict.read_bytes()
    fix_tz = f"fix:{MR}"
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  (PROJECT, str(root)), event={"kind": "seed"})
        for worker in ("exposed", "fresh"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES(?,?,?,?,?)", (worker, worker, "chatgpt", now(), now()),
                      event={"kind": "seed"})
        for tz, kind, state, body in (
            (MR, "work", "submitted", ""),
            (fix_tz, "fix", "ready", "[fleet-fix] source=" + PROJECT + "/" + MR
             + "\nWhat to fix: rejected candidate secret and false positive probe"),
        ):
            tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,"
                      "spec_sha,needs,owns,resources,acceptance,body,created_at,updated_at) "
                      "VALUES(?,?,?,?,1,?,'','','[]',?,'[]','[]',?,?,?)",
                      (PROJECT, tz, tz, state, kind, json.dumps([norm_path(str(review))]),
                       body, now(), now()), event={"kind": "seed"})
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES(?,?,'exposed','released',?)", (PROJECT, MR, now(-30)),
                  event={"kind": "seed"})
        tx.record_event(kind="checkpoint", worker_id="exposed", project=PROJECT,
                        tz=MR, payload={"done": "candidate from batch-" + BATCH})

    preview, _ = peek_next(store)
    assert preview is not None and preview.tz == fix_tz
    assert "rejected candidate secret" not in preview.body
    assert preview.body.startswith('[fleet-blind] {"batch_id":')
    assert take_next(store, "exposed") is None
    task = take_next(store, "fresh")
    assert task is not None and task.tz == fix_tz
    assert "rejected candidate secret" not in task.body
    with store.read() as tx:
        claim = tx.one("SELECT id FROM claims WHERE worker_id='fresh' AND state='active'")
        persisted = tx.scalar("SELECT body FROM tasks WHERE project=? AND tz=?",
                              (PROJECT, fix_tz))
    assert claim is not None and persisted == task.body
    attempt_id = f"claim-{int(claim['id'])}"
    attempt_blind = review / "attempts" / attempt_id / "case-01" / "blind.json"
    assert str(attempt_blind.parent.parent) in _blind_brief(store, "fresh", task, batch)
    read_file = _tool_fn(server, "read_file")
    assert "FLEET_BLIND_SEAL_REQUIRED" in read_file(path=str(manifest), agent="fresh")
    assert "FLEET_BLIND_SEAL_REQUIRED" in read_file(path=str(old_verdict), agent="fresh")
    seal = _tool_fn(server, "fleet_blind_seal")
    assert "FLEET_BLIND" in seal(agent="fresh")  # cannot reuse old blind as new proof

    attempt_blind.parent.mkdir(parents=True)
    attempt_blind.write_text(json.dumps({
        "case_id": "case-01", "independent_finding": "independent source finding",
        "alternative": "plausible contrary interpretation",
        "hidden_queries": [
            {"query": "case-specific positive one", "negative": False},
            {"query": "case-specific negative", "negative": True},
            {"query": "case-specific positive two", "negative": False},
        ],
    }), encoding="utf-8")
    with store.tx() as tx:
        tx.record_event(kind="write", worker_id="fresh", project=PROJECT,
                        tz=fix_tz, path_norm=norm_path(str(attempt_blind)), payload={})
    success = seal(agent="fresh")
    assert "blind_seal:" in success and attempt_id in success, success
    assert _blind_is_sealed(store, "fresh", task, batch)
    assert "MILL_MANIFEST_JSON" in read_file(path=str(manifest), agent="fresh")
    assert (old_blind.read_bytes(), old_verdict.read_bytes()) == before
    with store.read() as tx:
        event = tx.one("SELECT payload FROM events WHERE worker_id='fresh' "
                       "AND kind='blind_seal' ORDER BY id DESC LIMIT 1")
    assert json.loads(event["payload"])["attempt_id"] == attempt_id

    # A later meta-review must refuse mechanical acceptance if the independent
    # source blind is absent/changed, even with a green review-check command.
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted',author_worker='fresh' "
                  "WHERE project=? AND tz=?", (PROJECT, fix_tz), event={"kind": "seed"})
    with store.read() as tx:
        original_task = tx.one("SELECT * FROM tasks WHERE project=? AND tz=?",
                               (PROJECT, fix_tz))
        assert _blind_submission_sealed(tx, original_task)
    changed = json.loads(attempt_blind.read_text(encoding="utf-8"))
    changed["independent_finding"] = "tampered after seal"
    attempt_blind.write_text(json.dumps(changed), encoding="utf-8")
    with store.read() as tx:
        original_task = tx.one("SELECT * FROM tasks WHERE project=? AND tz=?",
                               (PROJECT, fix_tz))
        assert not _blind_submission_sealed(tx, original_task)
