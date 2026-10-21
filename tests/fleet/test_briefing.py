from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.briefing import briefing, resume_summary
from dev_mcp.fleet.model import Task
from dev_mcp.fleet.review import open_request
from dev_mcp.fleet.store import Store


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def task(
    tz: str = "TZ-1",
    *,
    state: str = "taken",
    owns: tuple[str, ...] = ("src/dev_mcp/fleet/review.py",),
) -> Task:
    return Task(
        project="P",
        tz=tz,
        state=state,
        priority=2,
        kind="work",
        spec_path=f"ToDo/{tz}.md",
        spec_sha="sha",
        needs=(),
        owns=owns,
        resources=("stand",),
        acceptance=("python -m pytest tests/unit.py -q",),
        title="Test task",
        body="",
    )


def setup_store(tmp_path: Path) -> Store:
    root = tmp_path / "P"
    root.mkdir()
    (root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    stamp = now()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            ("P", str(root)),
            event={"kind": "test_setup"},
        )
        tx.change(
            "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
            "VALUES('W1','worker','chatgpt',?,?,'')",
            (stamp, stamp),
            event={"kind": "test_setup", "worker_id": "W1"},
        )
    return store


def insert_task(store: Store, value: Task, *, author_worker: str | None = None) -> None:
    stamp = now()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks("
            "project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                value.project,
                value.tz,
                value.title,
                value.state,
                value.priority,
                value.kind,
                value.spec_path,
                value.spec_sha,
                json.dumps(list(value.needs)),
                json.dumps(list(value.owns)),
                json.dumps(list(value.resources)),
                json.dumps(list(value.acceptance)),
                author_worker,
                value.body,
                stamp,
                stamp,
            ),
            event={"kind": "test_setup", "project": value.project, "tz": value.tz},
        )


def add_claim(store: Store, value: Task) -> str:
    started = now()
    with store.tx() as tx:
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES(?,?,?,'active',?)",
            (value.project, value.tz, "W1", started),
            event={"kind": "take", "worker_id": "W1", "project": value.project, "tz": value.tz},
        )
    return started


def test_briefing_links_project_agents_and_renders_deterministic_context(tmp_path: Path):
    store = setup_store(tmp_path)
    current = task()
    insert_task(store, current)
    insert_task(
        store,
        task(
            "TZ-2",
            state="ready",
            owns=("src/dev_mcp/fleet/tools.py",),
        ),
    )

    text = briefing(store, "W1", current)

    assert "AGENTS.md" in text
    assert "правила здесь намеренно не пересказываются" in text
    assert "src/dev_mcp/fleet/review.py" in text
    assert "stand" in text
    assert "python -m pytest tests/unit.py -q" in text
    assert "TZ-2" in text
    assert "закрытия по rc" in text
    assert "Пиши только в свою зону" not in text
    assert "Git и телефон не трогай" not in text


def test_briefing_falls_back_to_existing_root_rules_file(tmp_path: Path):
    store = setup_store(tmp_path)
    root = tmp_path / "P"
    (root / "AGENTS.md").unlink()
    (root / "CLAUDE.md").write_text("# project rules\n", encoding="utf-8")
    current = task()
    insert_task(store, current)

    text = briefing(store, "W1", current)

    assert str(root / "CLAUDE.md") in text
    assert "ориентируйся на CLAUDE.md" in text
    assert str(root / "AGENTS.md") not in text


def test_briefing_can_resume_active_task_when_task_argument_is_none(tmp_path: Path):
    store = setup_store(tmp_path)
    current = task()
    insert_task(store, current)
    add_claim(store, current)

    text = briefing(store, "W1", None)

    assert "P/TZ-1" in text
    assert "src/dev_mcp/fleet/review.py" in text


def test_briefing_without_task_is_short_join_instruction(tmp_path: Path):
    store = setup_store(tmp_path)
    text = briefing(store, "W1", None)
    assert "Активной задачи нет" in text
    assert "fleet_take" in text


def test_resume_summary_uses_real_write_run_request_and_unacked_message_rows(tmp_path: Path):
    store = setup_store(tmp_path)
    current = task()
    insert_task(store, current)
    insert_task(store, task("TZ-2", state="ready", owns=("src/dev_mcp/fleet/helper.py",)))
    add_claim(store, current)

    with store.tx() as tx:
        tx.record_event(
            kind="write",
            worker_id="W1",
            project="P",
            tz="TZ-1",
            tool="write_file",
            path_norm="src/dev_mcp/fleet/review.py",
        )
        tx.record_event(
            kind="run",
            worker_id="W1",
            project="P",
            tz="TZ-1",
            tool="run",
            rc=1,
            payload={"argv": ["python", "-m", "pytest", "tests/unit.py", "-q"]},
        )
        tx.record_event(
            kind="run",
            worker_id="W1",
            project="P",
            tz="TZ-1",
            tool="run",
            rc=0,
            payload={"argv": ["python", "-m", "pytest", "tests/unit.py", "-q"]},
        )

    request_tz = open_request(
        store,
        "req1",
        kind="other",
        summary="Нужна правка соседнего helper",
        to="P/TZ-2",
        worker_id="W1",
        source_tz="TZ-1",
    )
    assert request_tz == "request:req1"

    with store.tx() as tx:
        tx.change(
            "INSERT INTO messages("
            "id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,body,"
            "created_at,delivered_at,acked_at"
            ") VALUES(?,?,?,?,0,?,?,?,?,?,NULL,NULL)",
            (
                "m-1",
                "",
                "W1",
                None,
                "system",
                "",
                "note",
                "проверь соседний контракт",
                now(),
            ),
            event={"kind": "test_setup"},
        )

    text = resume_summary(store, "W1", current)

    assert "src/dev_mcp/fleet/review.py (1 write)" in text
    assert "rc=1" in text
    assert "rc=0" in text
    assert "python -m pytest tests/unit.py -q" in text
    assert "request:req1 [ready]" in text
    assert "m-1 [note]" in text
    assert "проверь соседний контракт" in text


def test_resume_summary_marks_acknowledged_messages_as_done(tmp_path: Path):
    store = setup_store(tmp_path)
    current = task()
    insert_task(store, current)
    add_claim(store, current)
    with store.tx() as tx:
        tx.change(
            "INSERT INTO messages("
            "id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,kind,body,"
            "created_at,delivered_at,acked_at"
            ") VALUES(?,?,?,?,0,?,?,?,?,?,?,?)",
            (
                "m-ack",
                "",
                "W1",
                None,
                "owner",
                "",
                "note",
                "уже прочитано",
                now(),
                now(),
                now(),
            ),
            event={"kind": "test_setup"},
        )

    text = resume_summary(store, "W1", current)
    assert "m-ack" not in text
    assert "Сообщения без ack:" in text


def test_existing_request_review_receives_original_contract_and_submit_note(tmp_path: Path):
    from dev_mcp.fleet.review import open_review

    store = setup_store(tmp_path)
    source = replace(task("request:abc12345", state="submitted"), kind="request",
                     spec_path="", acceptance=(),
                     body="Что нужно: обновить статус по фактам\nНе заявлять live PASS.")
    insert_task(store, source, author_worker="W1")
    with store.tx() as tx:
        tx.record_event(kind="submit", project="P", tz=source.tz, worker_id="W1",
                        payload={"note": "Обновлён STATUS.md; live-ворота оставлены красными."})
    review_tz = open_review(store, "P", source.tz, final_note="старое ревью без контекста")
    # A previously-created review has only its routing marker and no source text.
    # The live briefing must repair context without rewriting the task or verdict.
    with store.read() as tx:
        from dev_mcp.fleet.briefing import _task_from_row
        review = _task_from_row(tx.one("SELECT * FROM tasks WHERE project='P' AND tz=?", (review_tz,)))
    text = briefing(store, "W2", review)
    assert source.body in text
    assert source.owns[0] in text
    assert "Обновлён STATUS.md; live-ворота оставлены красными." in text
    assert "исходной задачи (только чтение)" in text
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz=?", (source.tz,)) == "submitted"


def test_review_context_does_not_disclose_blind_source(tmp_path: Path):
    from dev_mcp.fleet.briefing import _review_source_lines

    store = setup_store(tmp_path)
    with store.tx() as tx:
        tx.change("UPDATE projects SET name='Mill Works' WHERE name='P'", event={"kind": "test_setup"})
    source = replace(task("TZ-MR-batch-abc123", state="submitted"), project="Mill Works",
                     body='[fleet-blind] {"batch_id":"batch-abc123"}\nSECRET-CANDIDATE')
    insert_task(store, source, author_worker="W1")
    review = replace(source, tz="review:" + source.tz, kind="review",
                     body='[fleet-review] {"project":"Mill Works","tz":"TZ-MR-batch-abc123"}')
    assert "SECRET-CANDIDATE" not in "\n".join(_review_source_lines(store, review))


def test_dependency_receipt_is_visible_even_if_task_note_says_pending(tmp_path):
    store = setup_store(tmp_path)
    supplier = replace(task("TZ-supplier", state="accepted"), body="old note: review pending")
    insert_task(store, supplier, author_worker="W1")
    consumer = replace(task("TZ-consumer"), needs=("TZ-supplier",))
    insert_task(store, consumer)
    with store.tx() as tx:
        tx.record_event(kind="accept", project="P", tz="TZ-supplier", worker_id="W-independent",
                        payload={"review_task": "review:TZ-supplier"})
    result = briefing(store, "W2", consumer)
    assert "TZ-supplier: accepted; accept event" in result
    assert "reviewer W-independent" in result
    assert '"fleet.cmd", "result"' in result


def test_briefing_shows_owner_grants_from_registry_as_formal(tmp_path: Path):
    store = setup_store(tmp_path)
    current = task()
    insert_task(store, current)
    with store.tx() as tx:
        tx.record_event(kind="owner_grant", project="P", tz="TZ-1",
                        payload={"text": "полигон на второй ПК разрешаю", "tzs": ["TZ-1"]})
        tx.record_event(kind="owner_grant", project="P", tz="TZ-7",
                        payload={"text": "чужое разрешение", "tzs": ["TZ-7"]})
    text = briefing(store, "W1", current)
    assert "полигон на второй ПК разрешаю" in text
    assert "действительны" in text
    assert "чужое разрешение" not in text


def test_briefing_trusts_owner_decisions_written_in_project_files(tmp_path: Path):
    store = setup_store(tmp_path)
    current = task()
    insert_task(store, current)
    with store.read() as tx:
        root = Path(str(tx.one("SELECT root FROM projects WHERE name='P'")["root"]))
    (root / "ToDo" / "fleet").mkdir(parents=True, exist_ok=True)
    (root / "ToDo" / "fleet" / "OWNER-BLOCKERS.md").write_text(
        "# Блокеры\n\nстарое\n\n## Решения владельца 18.10.2026\n\n- перенос полигона разрешаю\n\n"
        "### Owner-only gates\n\n- телефон\n\n## Другое\n\nне показывать\n", encoding="utf-8")
    text = briefing(store, "W1", current)
    assert "перенос полигона разрешаю" in text
    assert "действительны" in text
    assert "не показывать" not in text
