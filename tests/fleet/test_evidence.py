from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import briefing, evidence, report
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.queue import finish, submit, take_next
from dev_mcp.fleet.review import decide, open_review
from dev_mcp.fleet.spec import spec_sha
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.tools import FleetContext

COMMAND = "python -m pytest test -q"
ARGV = ["python", "-m", "pytest", "test", "-q"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def build(tmp_path: Path) -> tuple[Store, Path]:
    root = tmp_path / "P"
    (root / "test").mkdir(parents=True)
    (root / "ToDo" / "tz").mkdir(parents=True)
    (root / "app.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    (root / "test" / "test_app.py").write_text(
        "def test_f():\n    assert f(1) == 1\n    assert f(2) == 2\n", encoding="utf-8"
    )
    spec = root / "ToDo" / "tz" / "TZ-1.md"
    spec.write_text("```yaml\ntz: TZ-1\nowns:\n  - app.py\n```\n\n# TZ-1\n", encoding="utf-8")

    store = Store.open(str(tmp_path / "fleet" / "fleet.sqlite"))
    stamp = now()
    owns = [norm_path(str(root / "app.py")), norm_path(str(root / "test" / "test_app.py"))]
    with store.tx() as tx:
        tx.change(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
            ("P", str(root)),
            event={"kind": "test_setup"},
        )
        for worker in ("W1", "W2"):
            tx.change(
                "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoke_reason) "
                "VALUES(?,?,?,?,?,'')",
                (worker, worker, "chatgpt", stamp, stamp),
                event={"kind": "test_setup", "worker_id": worker},
            )
        tx.change(
            "INSERT INTO tasks("
            "project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("P", "TZ-1", "TZ-1", "ready", 5, "work", "ToDo/tz/TZ-1.md", spec_sha(str(spec)),
             "[]", json.dumps(owns), "[]", json.dumps([COMMAND]), None, "", stamp, stamp),
            event={"kind": "test_setup", "project": "P", "tz": "TZ-1"},
        )
    return store, root


def test_expand_bare_directory_tracks_files_like_recursive_owns(tmp_path: Path):
    folder = tmp_path / "owned"
    folder.mkdir()
    (folder / "a.py").write_text("x=1\n", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "b.py").write_text("y=2\n", encoding="utf-8")

    bare, bare_truncated = evidence.expand([norm_path(str(folder))])
    globbed, glob_truncated = evidence.expand([norm_path(str(folder)) + "/**"])
    assert bare == globbed
    assert len(bare) == 2
    assert not bare_truncated and not glob_truncated


def test_extend_capture_preserves_old_baseline_and_adds_new_path(tmp_path: Path):
    store, root = build(tmp_path)
    assert take_next(store, "W1").tz == "TZ-1"
    with store.tx() as tx:
        claim_id = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W1' AND state='active'"))
    assert evidence.capture(store, claim_id)

    original = root / "app.py"
    original.write_text("changed after baseline\n", encoding="utf-8")
    extra = root / "extra.py"
    extra.write_text("before auto-expanded edit\n", encoding="utf-8")
    extra_norm = norm_path(str(extra))
    with store.tx() as tx:
        tx.change(
            "INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
            (claim_id, extra_norm),
            event={"kind": "claim_expand", "worker_id": "W1", "project": "P", "tz": "TZ-1"},
        )

    assert evidence.extend_capture(store, claim_id, [extra_norm])
    manifest = json.loads(
        (evidence.snapshot_root(store) / f"claim-{claim_id}.json").read_text(encoding="utf-8")
    )
    assert extra_norm in manifest["files"]
    # Старый baseline не должен переписаться текущим содержимым original.
    assert manifest["files"][norm_path(str(original))] != evidence._sha(str(original))


def author_does_work(store: Store, root: Path, *, weaken: bool) -> None:
    assert take_next(store, "W1").tz == "TZ-1"
    with store.tx() as tx:
        claim_id = int(tx.scalar("SELECT id FROM claims WHERE worker_id='W1' AND state='active'"))
    assert evidence.capture(store, claim_id)
    (root / "app.py").write_text("def f(x):\n    return x * 1\n", encoding="utf-8")
    if weaken:
        (root / "test" / "test_app.py").write_text(
            "def test_f():\n    assert f(1) == 1\n", encoding="utf-8"
        )
    submit(store, "W1", "done")
    finish(store, "W1", "done")


def take_review(store: Store):
    review_tz = open_review(store, "P", "TZ-1")
    task = take_next(store, "W2")
    assert task is not None and task.tz == review_tz
    return task


def test_reviewer_sees_diff_and_weakened_test(tmp_path: Path):
    store, root = build(tmp_path)
    author_does_work(store, root, weaken=True)
    task = take_review(store)

    text = briefing.briefing(store, "W2", task)

    assert "изменён:" in text and "app.py" in text
    assert "+    return x * 1" in text
    assert "проверок стало меньше (2 → 1)" in text
    assert "escalate" in text


def test_spec_header_edit_by_author_is_flagged(tmp_path: Path):
    store, root = build(tmp_path)
    author_does_work(store, root, weaken=False)
    spec = root / "ToDo" / "tz" / "TZ-1.md"
    spec.write_text(spec.read_text(encoding="utf-8").replace("  - app.py", "  - app.py\n  - other.py"),
                    encoding="utf-8")
    task = take_review(store)

    assert "шапка файла ТЗ изменена" in briefing.briefing(store, "W2", task)


def test_spec_body_edit_by_author_is_flagged(tmp_path: Path):
    store, root = build(tmp_path)
    author_does_work(store, root, weaken=False)
    spec = root / "ToDo" / "tz" / "TZ-1.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("# TZ-1", "# TZ-1\n\nНовое требование"),
        encoding="utf-8",
    )
    task = take_review(store)

    assert "тело файла ТЗ изменено" in briefing.briefing(store, "W2", task)


def test_run_events_carry_task_and_file_state(tmp_path: Path):
    store, root = build(tmp_path)
    author_does_work(store, root, weaken=False)
    take_review(store)
    ctx = FleetContext(store=store)

    ctx.record_event("run", "W2", tool="run", rc=0, payload={"argv": ARGV})

    with store.tx() as tx:
        row = tx.one("SELECT project,tz,payload FROM events WHERE kind='run' ORDER BY id DESC LIMIT 1")
    assert row["project"] == "P" and row["tz"].startswith("review:")
    assert json.loads(row["payload"])["tree"] == evidence.subject_tree(store, "W2")


def test_accept_counts_only_runs_on_the_current_file_state(tmp_path: Path):
    store, root = build(tmp_path)
    author_does_work(store, root, weaken=False)
    take_review(store)
    ctx = FleetContext(store=store)
    ctx.record_event("run", "W2", tool="run", rc=0, payload={"argv": ARGV})

    # Ревьюер «подправил» код после своего прогона — прогон больше не доказывает принимаемое.
    (root / "app.py").write_text("def f(x):\n    return x + 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="менялись после"):
        decide(store, "W2", "accept", "ok", tree=evidence.subject_tree(store, "W2"))

    ctx.record_event("run", "W2", tool="run", rc=0, payload={"argv": ARGV})
    decide(store, "W2", "accept", "ok", tree=evidence.subject_tree(store, "W2"))
    with store.tx() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-1'") == "accepted"


def test_escalate_immediately_opens_final_review_without_owner_gate(tmp_path: Path):
    store, root = build(tmp_path)
    author_does_work(store, root, weaken=False)
    task = take_review(store)

    with pytest.raises(ValueError, match="note"):
        decide(store, "W2", "escalate", "  ")
    decide(store, "W2", "escalate", "нужна живая проверка трея после рестарта")

    with store.tx() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz=?", (task.tz,)) == "accepted"
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-1'") == "submitted"
        assert tx.scalar("SELECT COUNT(*) FROM claims WHERE worker_id='W2' AND state='active'") == 0
        final = tx.one(
            "SELECT tz,state,body FROM tasks WHERE project='P' AND kind='review' AND state='ready' ORDER BY created_at DESC LIMIT 1"
        )
        assert final is not None and final["tz"] != task.tz
        assert "[fleet-final]" in final["body"]
    assert not any("НУЖЕН ВЛАДЕЛЕЦ" in line for line in report.exceptions(store))


def test_review_without_snapshot_says_so(tmp_path: Path):
    store, root = build(tmp_path)
    assert take_next(store, "W1").tz == "TZ-1"  # взята до включения снимков: capture не вызван
    submit(store, "W1", "done")
    finish(store, "W1", "done")
    task = take_review(store)

    assert "снимка на момент взятия нет" in briefing.briefing(store, "W2", task)


@pytest.mark.parametrize(
    ("before", "after", "deleted", "expected"),
    [
        ("def test_a():\n    assert 1\n", None, True, "удалён тестовый файл"),
        ("test('a', () => { assert.equal(1, 1); });\n", "test.skip('a', () => { assert.equal(1, 1); });\n",
         False, "пропусков"),
        ("def test_a():\n    assert 1\ndef test_b():\n    assert 2\n", "def test_a():\n    assert 1\n",
         False, "тестов стало меньше"),
    ],
)
def test_test_signals(before: str, after: str | None, deleted: bool, expected: str):
    signals = evidence.test_signals("c:/p/test/test_x.py", before, after, deleted)
    assert any(expected in s for s in signals)


def test_non_test_file_gives_no_signals():
    assert evidence.test_signals("c:/p/app.py", "assert 1\nassert 2\n", "assert 1\n", False) == []
