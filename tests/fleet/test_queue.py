from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.queue import block, close_obsolete_requests, reopen_orphan_reviews, finish, promote_legacy_drafts, reactivate_stale_generated_fixes, recover_stale_with_work, requeue_stale, submit, take_next, thaw_frozen
from dev_mcp.fleet.review import decide, open_review


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE projects(
    name TEXT PRIMARY KEY,
    root TEXT,
    priority INTEGER DEFAULT 5,
    active INTEGER DEFAULT 1
);
CREATE TABLE tasks(
    project TEXT NOT NULL,
    tz TEXT NOT NULL,
    title TEXT DEFAULT '',
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    kind TEXT NOT NULL DEFAULT 'work',
    spec_path TEXT NOT NULL DEFAULT '',
    spec_sha TEXT NOT NULL DEFAULT '',
    needs TEXT NOT NULL DEFAULT '[]',
    owns TEXT NOT NULL DEFAULT '[]',
    resources TEXT NOT NULL DEFAULT '[]',
    acceptance TEXT NOT NULL DEFAULT '[]',
    author_worker TEXT,
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project,tz)
);
CREATE TABLE workers(
    id TEXT PRIMARY KEY,
    label TEXT DEFAULT '',
    kind TEXT DEFAULT 'chatgpt',
    first_seen TEXT,
    last_seen TEXT,
    revoked_at TEXT,
    revoke_reason TEXT DEFAULT ''
);
CREATE TABLE claims(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT,
    tz TEXT,
    worker_id TEXT,
    state TEXT,
    started_at TEXT,
    released_at TEXT
);
CREATE TABLE claim_paths(claim_id INTEGER,path_norm TEXT);
CREATE TABLE claim_resources(claim_id INTEGER,resource TEXT);
CREATE TABLE events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    worker_id TEXT,
    project TEXT,
    tz TEXT,
    tool TEXT DEFAULT '',
    path_norm TEXT DEFAULT '',
    rc INTEGER,
    payload TEXT DEFAULT '{}'
);
"""


def now(delta_min: int = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=delta_min)
    ).isoformat(timespec="microseconds")


class StubTxn:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql, params=()):
        row = self.one(sql, params)
        return None if row is None else row[0]

    def _record_event(self, event):
        if isinstance(event, str):
            event = {"kind": event}
        payload = event.get("payload", {})
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self.conn.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                now(),
                event.get("kind"),
                event.get("worker_id"),
                event.get("project"),
                event.get("tz"),
                event.get("tool", ""),
                event.get("path_norm", ""),
                event.get("rc"),
                payload,
            ),
        )

    def record_event(self, **event):
        self._record_event(event)

    def change(self, sql, params=(), *, event):
        cursor = self.conn.execute(sql, params)
        self._record_event(event)
        return cursor

    def changes(self, statements, *, event):
        cursors = [self.conn.execute(sql, params) for sql, params in statements]
        self._record_event(event)
        return cursors


class StubStore:
    def __init__(self, path: Path):
        self.path = str(path)

    @classmethod
    def open(cls, path: Path):
        store = cls(path)
        conn = sqlite3.connect(store.path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        return store

    @contextlib.contextmanager
    def tx(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield StubTxn(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read(self):
        return self.tx()

    def touch_worker(self, worker_id: str):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "UPDATE workers SET last_seen=? WHERE id=?",
            (now(), worker_id),
        )
        conn.commit()
        conn.close()


def add_project(store: StubStore, name: str, priority: int = 5):
    with store.tx() as db:
        db.execute(
            "INSERT OR IGNORE INTO projects(name,root,priority,active) VALUES(?,?,?,1)",
            (name, "/" + name, priority),
        )


def add_worker(store: StubStore, worker_id: str, *, last_seen: str | None = None):
    seen = last_seen or now()
    with store.tx() as db:
        db.execute(
            "INSERT OR REPLACE INTO workers("
            "id,label,kind,first_seen,last_seen,revoke_reason"
            ") VALUES(?,?,?,?,?,'')",
            (worker_id, worker_id, "chatgpt", seen, seen),
        )


def add_task(
    store: StubStore,
    tz: str,
    *,
    project: str = "P",
    state: str = "ready",
    needs=(),
    owns=(),
    resources=(),
    kind: str = "work",
    author_worker=None,
    priority: int = 5,
):
    with store.tx() as db:
        db.execute(
            "INSERT INTO tasks("
            "project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                project,
                tz,
                tz,
                state,
                priority,
                kind,
                f"ToDo/tz/{tz}.md",
                tz,
                json.dumps(list(needs)),
                json.dumps(list(owns)),
                json.dumps(list(resources)),
                json.dumps(["check"]),
                author_worker,
                "",
                now(),
                now(),
            ),
        )


def test_legacy_manual_drafts_are_promoted_without_owner_approval(tmp_path: Path):
    store = StubStore.open(tmp_path / "legacy.sqlite")
    add_project(store, "P", 1)
    add_task(store, "request:old", state="draft", kind="request", owns=("src/request.py",))
    add_task(store, "fix:old", state="draft", kind="fix")
    add_task(store, "TZ-source", state="submitted", kind="work")
    add_task(store, "review:TZ-source", state="draft", kind="review")
    marker = '[fleet-review] {"project":"P","tz":"TZ-source"}\nlegacy review'
    with store.tx() as db:
        db.execute("UPDATE tasks SET body=? WHERE project='P' AND tz='review:TZ-source'", (marker,))

    promoted = promote_legacy_drafts(store)
    assert set(promoted) == {"P/request:old", "P/fix:old", "P/review:TZ-source"}
    with store.tx() as db:
        states = {
            row["tz"]: row["state"]
            for row in db.query("SELECT tz,state FROM tasks WHERE project='P'")
        }
    assert states["request:old"] == "ready"
    assert states["fix:old"] == "ready"
    assert states["review:TZ-source"] == "ready"


def test_legacy_request_without_owns_is_rejected_instead_of_wasting_a_worker(tmp_path: Path):
    store = StubStore.open(tmp_path / "legacy-empty-request.sqlite")
    add_project(store, "P", 1)
    add_task(store, "request:empty", state="draft", kind="request", owns=())
    assert promote_legacy_drafts(store) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='request:empty'")["state"] == "rejected"
        event = db.one(
            "SELECT kind,payload FROM events WHERE project='P' AND tz='request:empty' ORDER BY id DESC LIMIT 1"
        )
    assert event["kind"] == "auto_reject"


def test_legacy_review_is_not_promoted_when_source_is_no_longer_submitted(tmp_path: Path):
    store = StubStore.open(tmp_path / "legacy-review.sqlite")
    add_project(store, "P", 1)
    add_task(store, "TZ-source", state="accepted", kind="work")
    add_task(store, "review:TZ-source", state="draft", kind="review")
    marker = '[fleet-review] {"project":"P","tz":"TZ-source"}\nobsolete review'
    with store.tx() as db:
        db.execute("UPDATE tasks SET body=? WHERE project='P' AND tz='review:TZ-source'", (marker,))
    assert promote_legacy_drafts(store) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='review:TZ-source'")["state"] == "draft"


def test_project_priority_beats_foreign_review(tmp_path: Path):
    # 14.10: приоритет проекта — первым; «доводи начатое» — внутри проекта (см. ниже).
    store = StubStore.open(tmp_path / "review-first.sqlite")
    add_project(store, "HighPriorityProject", 1)
    add_project(store, "LowPriorityProject", 9)
    add_worker(store, "W-review")
    add_task(store, "TZ-new", project="HighPriorityProject", kind="work", owns=("high/new.py",))
    add_task(
        store,
        "review:TZ-old",
        project="LowPriorityProject",
        kind="review",
        author_worker="W-author",
    )

    task = take_next(store, "W-review")

    assert task is not None
    assert (task.project, task.tz, task.kind) == ("HighPriorityProject", "TZ-new", "work")


def test_take_next_is_atomic_under_real_parallel_race(tmp_path: Path):
    path = tmp_path / "queue.sqlite"
    setup = StubStore.open(path)
    add_project(setup, "P", 1)

    for i in range(50):
        tz = f"TZ-{i:02d}"
        w1, w2 = f"W{i:02d}a", f"W{i:02d}b"
        add_worker(setup, w1)
        add_worker(setup, w2)
        add_task(setup, tz, owns=(f"/repo/file-{i}.py",))

        barrier = threading.Barrier(3)
        results = {}
        errors = []

        def run(worker):
            try:
                local = StubStore(path)
                barrier.wait()
                results[worker] = take_next(local, worker, project="P")
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run, args=(w1,))
        t2 = threading.Thread(target=run, args=(w2,))
        t1.start()
        t2.start()
        barrier.wait()
        t1.join()
        t2.join()

        assert not errors
        winners = [value for value in results.values() if value is not None]
        losers = [value for value in results.values() if value is None]
        assert len(winners) == 1
        assert len(losers) == 1
        assert winners[0].tz == tz


def test_needs_gate(tmp_path: Path):
    store = StubStore.open(tmp_path / "needs.sqlite")
    add_project(store, "P")
    add_worker(store, "W")
    add_task(store, "TZ-A", state="submitted")
    add_task(store, "TZ-B", needs=("TZ-A",))

    assert take_next(store, "W") is None

    with store.tx() as db:
        db.execute(
            "UPDATE tasks SET state='accepted' "
            "WHERE project='P' AND tz='TZ-A'"
        )

    assert take_next(store, "W").tz == "TZ-B"


def test_resource_is_global_across_projects(tmp_path: Path):
    store = StubStore.open(tmp_path / "resource.sqlite")
    add_project(store, "A")
    add_project(store, "B")
    add_worker(store, "W1")
    add_worker(store, "W2")
    add_task(store, "TZ-A", project="A", resources=("stand",))
    add_task(store, "TZ-B", project="B", resources=("stand",))

    assert take_next(store, "W1", "A").tz == "TZ-A"
    assert take_next(store, "W2", "B") is None


def test_overlapping_claim_paths_block(tmp_path: Path):
    store = StubStore.open(tmp_path / "paths.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_worker(store, "W2")
    add_task(store, "TZ-A", owns=("/repo/ops/probe",))
    add_task(
        store,
        "TZ-B",
        owns=("/repo/ops/probe/runner.py",),
        priority=6,
    )

    assert take_next(store, "W1").tz == "TZ-A"
    assert take_next(store, "W2") is None


def test_review_is_not_given_to_its_author(tmp_path: Path):
    store = StubStore.open(tmp_path / "review.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(
        store,
        "REV-1",
        kind="review",
        author_worker="W1",
    )

    assert take_next(store, "W1") is None


def test_submit_then_finish_keeps_submitted_and_releases_claim(tmp_path: Path):
    store = StubStore.open(tmp_path / "finish.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-1")

    assert take_next(store, "W1").tz == "TZ-1"
    submit(store, "W1", "done")
    finish(store, "W1", "summary")

    with store.tx() as db:
        task_state = db.execute(
            "SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'"
        ).fetchone()[0]
        claim_state = db.execute(
            "SELECT state FROM claims WHERE project='P' AND tz='TZ-1'"
        ).fetchone()[0]

    assert task_state == "submitted"
    assert claim_state == "released"


def test_finish_without_submit_returns_task_to_ready(tmp_path: Path):
    store = StubStore.open(tmp_path / "finish-ready.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-1")

    assert take_next(store, "W1").tz == "TZ-1"
    finish(store, "W1", "stopped")

    with store.tx() as db:
        task_state = db.execute(
            "SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'"
        ).fetchone()[0]
        claim_state = db.execute(
            "SELECT state FROM claims WHERE project='P' AND tz='TZ-1'"
        ).fetchone()[0]

    assert task_state == "ready"
    assert claim_state == "released"


def test_chat_handoffs_do_not_accumulate_task_freeze_score(tmp_path: Path):
    store = StubStore.open(tmp_path / "handoff-ready.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_worker(store, "W2")
    add_task(store, "TZ-1")

    assert take_next(store, "W1").tz == "TZ-1"
    assert finish(store, "W1", "handoff: chat UI stalled") == "ready"
    assert take_next(store, "W2").tz == "TZ-1"
    assert finish(store, "W2", "handoff: context exhausted") == "ready"

    with store.tx() as db:
        assert db.execute("SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'").fetchone()[0] == "ready"
        rows = db.execute(
            "SELECT payload FROM events WHERE project='P' AND tz='TZ-1' AND kind='finish' ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert all(json.loads(row[0])["automatic_handoff"] is True for row in rows)


def test_explicit_blocked_finish_freezes_on_first_attempt(tmp_path: Path):
    store = StubStore.open(tmp_path / "finish-blocked.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-1")

    assert take_next(store, "W1").tz == "TZ-1"
    state = finish(store, "W1", "blocked: внешний стенд пока недоступен")
    assert state == "frozen"
    with store.tx() as db:
        assert db.execute("SELECT state FROM tasks WHERE project='P' AND tz='TZ-1'").fetchone()[0] == "frozen"
        event = db.execute(
            "SELECT kind,payload FROM events WHERE project='P' AND tz='TZ-1' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event[0] == "block"
    assert "внешний стенд" in json.loads(event[1])["reason"]


def test_block_releases_claim_and_freezes_exact_task(tmp_path: Path):
    store = StubStore.open(tmp_path / "block.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "fix:TZ-1", kind="fix")

    assert take_next(store, "W1").tz == "fix:TZ-1"
    block(store, "W1", "external owner decision")

    with store.tx() as db:
        task_state = db.execute(
            "SELECT state FROM tasks WHERE project='P' AND tz='fix:TZ-1'"
        ).fetchone()[0]
        claim_state = db.execute(
            "SELECT state FROM claims WHERE project='P' AND tz='fix:TZ-1'"
        ).fetchone()[0]
        event = db.execute(
            "SELECT payload FROM events WHERE project='P' AND tz='fix:TZ-1' AND kind='block' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert task_state == "frozen"
    assert claim_state == "released"
    assert json.loads(event[0])["reason"] == "external owner decision"
    assert take_next(store, "W1") is None


def test_recover_stale_with_work_revokes_worker_and_preserves_task(tmp_path: Path):
    store = StubStore.open(tmp_path / "stale-work.sqlite")
    add_project(store, "P")
    add_worker(store, "W1", last_seen=now(-240))
    add_task(store, "TZ-WORK", state="taken")
    with store.tx() as db:
        cur = db.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P','TZ-WORK','W1','active',?)",
            (now(-230),),
        )
        claim_id = cur.lastrowid
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload) "
            "VALUES (?,'write','W1','P','TZ-WORK','edit_file','owned.py',NULL,'{}')",
            (now(-220),),
        )

    returned = recover_stale_with_work(store, silence_min=180)
    assert [task.tz for task in returned] == ["TZ-WORK"]
    with store.tx() as db:
        assert db.execute("SELECT state FROM tasks WHERE project='P' AND tz='TZ-WORK'").fetchone()[0] == "ready"
        assert db.execute("SELECT state FROM claims WHERE id=?", (claim_id,)).fetchone()[0] == "requeued"
        worker = db.execute("SELECT revoked_at,revoke_reason FROM workers WHERE id='W1'").fetchone()
        assert worker[0]
        assert "preserved worktree" in worker[1]
        payload = json.loads(db.execute(
            "SELECT payload FROM events WHERE project='P' AND tz='TZ-WORK' AND kind='requeue' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
    assert payload["automatic_recovery"] is True
    assert payload["preserved_work"] is True


def test_recover_stale_with_work_keeps_recent_partial_claim(tmp_path: Path):
    store = StubStore.open(tmp_path / "recent-work.sqlite")
    add_project(store, "P")
    add_worker(store, "W1", last_seen=now(-120))
    add_task(store, "TZ-WORK", state="taken")
    with store.tx() as db:
        db.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P','TZ-WORK','W1','active',?)",
            (now(-150),),
        )
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload) "
            "VALUES (?,'write','W1','P','TZ-WORK','edit_file','owned.py',NULL,'{}')",
            (now(-140),),
        )
    assert recover_stale_with_work(store, silence_min=180) == []
    with store.tx() as db:
        assert db.execute("SELECT state FROM tasks WHERE project='P' AND tz='TZ-WORK'").fetchone()[0] == "taken"


def test_requeue_stale_requires_all_three_conditions(tmp_path: Path):
    store = StubStore.open(tmp_path / "stale.sqlite")
    add_project(store, "P")

    old = now(-120)
    recent = now(-1)
    for worker, seen in (
        ("W-write", old),
        ("W-live", recent),
        ("W-stale", old),
        ("W-accept", old),
    ):
        add_worker(store, worker, last_seen=seen)

    for tz, worker in (
        ("TZ-WRITE", "W-write"),
        ("TZ-LIVE", "W-live"),
        ("TZ-STALE", "W-stale"),
        ("TZ-ACCEPT", "W-accept"),
    ):
        add_task(store, tz, state="taken")
        with store.tx() as db:
            cur = db.execute(
                "INSERT INTO claims(project,tz,worker_id,state,started_at) "
                "VALUES('P',?,?,'active',?)",
                (tz, worker, old),
            )
            db.execute(
                "INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
                (cur.lastrowid, f"/repo/{tz}"),
            )

    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
            "VALUES(?,?,?,?,?,'{}')",
            (now(-60), "write", "W-write", "P", "TZ-WRITE"),
        )
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
            "VALUES(?,?,?,?,?,'{}')",
            (now(-60), "accept", "W-accept", "P", "TZ-ACCEPT"),
        )

    returned = requeue_stale(store, silence_min=30)
    assert [task.tz for task in returned] == ["TZ-STALE"]

    with store.tx() as db:
        states = dict(db.execute("SELECT tz,state FROM tasks").fetchall())
        claims = dict(db.execute("SELECT tz,state FROM claims").fetchall())

    assert states["TZ-STALE"] == "ready"
    assert claims["TZ-STALE"] == "requeued"
    assert states["TZ-WRITE"] == "taken"
    assert states["TZ-LIVE"] == "taken"
    assert states["TZ-ACCEPT"] == "taken"


def test_requeue_stale_skips_imported_legacy_claim(tmp_path: Path):
    store = StubStore.open(tmp_path / "imported-stale.sqlite")
    add_project(store, "P")
    old = now(-180)
    add_worker(store, "W-imported", last_seen=old)
    add_task(store, "TZ-OLD", state="taken")

    with store.tx() as db:
        cur = db.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P','TZ-OLD','W-imported','active',?)",
            (old,),
        )
        db.execute(
            "INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
            (cur.lastrowid, "/repo/legacy"),
        )
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) "
            "VALUES(?,?,?,?,?,?)",
            (
                old,
                "take",
                "W-imported",
                "P",
                "TZ-OLD",
                json.dumps({"imported": True, "legacy_claim_fingerprint": "abc"}),
            ),
        )

    assert requeue_stale(store, silence_min=30) == []
    with store.tx() as db:
        assert db.one(
            "SELECT state FROM tasks WHERE project='P' AND tz='TZ-OLD'"
        )["state"] == "taken"
        assert db.one(
            "SELECT state FROM claims WHERE project='P' AND tz='TZ-OLD'"
        )["state"] == "active"


def _claim_paths(store: StubStore, tz: str) -> list[str]:
    with store.tx() as db:
        rows = db.execute(
            "SELECT cp.path_norm FROM claim_paths cp JOIN claims c ON c.id=cp.claim_id "
            "WHERE c.tz=? AND c.state='active' ORDER BY cp.path_norm",
            (tz,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def test_writing_task_also_holds_its_own_spec_file(tmp_path: Path):
    # Протокол велит писать строку расхождения в свой файл ТЗ — это не «вне зоны».
    store = StubStore.open(tmp_path / "spec-owned.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-A", owns=("/repo/app.py",))

    assert take_next(store, "W1").tz == "TZ-A"
    paths = _claim_paths(store, "TZ-A")
    assert "/repo/app.py" in paths
    assert any(path.endswith("/p/todo/tz/tz-a.md") for path in paths)  # norm_path приводит регистр


def test_review_does_not_hold_spec_file(tmp_path: Path):
    store = StubStore.open(tmp_path / "spec-review.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "REV-1", kind="review", author_worker="W2")

    assert take_next(store, "W1").tz == "REV-1"
    assert _claim_paths(store, "REV-1") == []


def test_peek_next_predicts_what_a_new_worker_gets_without_claiming(tmp_path: Path):
    from dev_mcp.fleet.queue import peek_next

    store = StubStore.open(tmp_path / "peek.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-A", owns=("/repo/a.py",), priority=2)
    add_task(store, "TZ-B", owns=("/repo/b.py",), priority=5)

    task, count = peek_next(store)
    assert (task.tz, count) == ("TZ-A", 2)
    assert take_next(store, "W1").tz == "TZ-A"  # прогноз ничего не занял
    task, count = peek_next(store)
    assert (task.tz, count) == ("TZ-B", 1)


def _take_and_submit(store: StubStore, worker_id: str, project: str | None = None):
    task = take_next(store, worker_id, project)
    submit(store, worker_id, "done")
    finish(store, worker_id, "done")
    return task


def test_chat_takes_tasks_in_a_row_up_to_its_budget(tmp_path: Path):
    from dev_mcp.fleet.queue import CHAT_TASK_BUDGET, budget_left

    store = StubStore.open(tmp_path / "budget.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    for i in range(CHAT_TASK_BUDGET + 1):
        add_task(store, f"TZ-{i:02d}", owns=(f"/repo/{i}.py",))

    assert budget_left(store, "W1") == CHAT_TASK_BUDGET
    for _ in range(CHAT_TASK_BUDGET):
        assert take_next(store, "W1") is not None
        finish(store, "W1", "partial")  # вернул в очередь — чат всё равно потратил задачу
    assert budget_left(store, "W1") == 0
    assert take_next(store, "W1") is None


def test_cli_worker_has_no_chat_budget(tmp_path: Path):
    from dev_mcp.fleet.queue import CHAT_TASK_BUDGET, budget_left

    store = StubStore.open(tmp_path / "cli.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    with store.tx() as db:
        db.execute("UPDATE workers SET kind='claude-code' WHERE id='W1'")
    for i in range(CHAT_TASK_BUDGET + 2):
        add_task(store, f"TZ-{i:02d}")
    assert budget_left(store, "W1") is None
    for _ in range(CHAT_TASK_BUDGET + 2):
        assert take_next(store, "W1") is not None
        finish(store, "W1", "partial")


def test_review_of_a_fix_is_not_given_to_the_author_of_the_source(tmp_path: Path):
    store = StubStore.open(tmp_path / "family.sqlite")
    add_project(store, "P")
    for worker in ("W1", "W2", "W3"):
        add_worker(store, worker)
    add_task(store, "TZ-09", owns=("/repo/a.py",))
    assert _take_and_submit(store, "W1").tz == "TZ-09"
    # fix сделал другой чат; ревью фикса — снова в семье TZ-09
    add_task(store, "fix:TZ-09", kind="fix", owns=("/repo/a.py",))
    assert _take_and_submit(store, "W2").tz == "fix:TZ-09"
    add_task(store, "review:fix:TZ-09", kind="review", author_worker="W2")

    assert take_next(store, "W1") is None  # писал исходную
    assert take_next(store, "W2") is None  # писал fix
    assert take_next(store, "W3").tz == "review:fix:TZ-09"


def test_obsolete_generated_request_closes_when_source_is_already_accepted(tmp_path: Path):
    store = StubStore.open(tmp_path / "obsolete-request.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-1", state="accepted")
    add_task(store, "request:deadbeef", state="ready", kind="request", owns=("/P",))
    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) VALUES(?,?,?,?,?,?)",
            (
                now(-30),
                "request_draft",
                "W1",
                "P",
                "request:deadbeef",
                json.dumps({"source_tz": "TZ-1", "request_id": "deadbeef"}),
            ),
        )

    assert close_obsolete_requests(store) == ["P/request:deadbeef"]
    with store.tx() as db:
        row = db.one("SELECT state FROM tasks WHERE project='P' AND tz='request:deadbeef'")
        event = db.one(
            "SELECT kind,payload FROM events WHERE project='P' AND tz='request:deadbeef' ORDER BY id DESC LIMIT 1"
        )
    assert row["state"] == "accepted"
    assert event["kind"] == "auto_obsolete"
    assert json.loads(event["payload"])["source_tz"] == "TZ-1"


def test_legacy_frozen_req_without_any_consumer_is_closed_as_obsolete(tmp_path: Path):
    store = StubStore.open(tmp_path / "legacy-orphan-request.sqlite")
    add_project(store, "P")
    add_task(store, "REQ-ABCDEF123456", state="frozen", kind="request", owns=("/P/x",))

    assert close_obsolete_requests(store) == ["P/REQ-ABCDEF123456"]
    with store.tx() as db:
        task = db.one("SELECT state FROM tasks WHERE project='P' AND tz='REQ-ABCDEF123456'")
        event = db.one(
            "SELECT kind,payload FROM events WHERE project='P' AND tz='REQ-ABCDEF123456' ORDER BY id DESC LIMIT 1"
        )
    assert task["state"] == "rejected"
    payload = json.loads(event["payload"])
    assert event["kind"] == "auto_obsolete"
    assert payload["legacy_orphan"] is True


def test_legacy_frozen_req_is_kept_when_structurally_or_implicitly_referenced(tmp_path: Path):
    store = StubStore.open(tmp_path / "legacy-live-request.sqlite")
    add_project(store, "P")
    add_task(store, "REQ-STRUCT123456", state="frozen", kind="request", owns=("/P/a",))
    add_task(store, "REQ-BLOCK123456", state="frozen", kind="request", owns=("/P/b",))
    add_task(store, "TZ-STRUCT", state="ready", needs=("REQ-STRUCT123456",))
    add_task(store, "TZ-BLOCK", state="frozen")
    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) VALUES(?,?,?,?,?,?)",
            (
                now(-10),
                "block",
                "W1",
                "P",
                "TZ-BLOCK",
                json.dumps({"reason": "blocked: ждём REQ-BLOCK123456"}),
            ),
        )

    assert close_obsolete_requests(store) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='REQ-STRUCT123456'")["state"] == "frozen"
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='REQ-BLOCK123456'")["state"] == "frozen"


def test_generated_request_stays_runnable_while_source_is_not_accepted(tmp_path: Path):
    store = StubStore.open(tmp_path / "live-request.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-1", state="submitted")
    add_task(store, "request:deadbeef", state="ready", kind="request", owns=("/P",))
    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) VALUES(?,?,?,?,?,?)",
            (
                now(-30),
                "request_draft",
                "W1",
                "P",
                "request:deadbeef",
                json.dumps({"source_tz": "TZ-1", "request_id": "deadbeef"}),
            ),
        )
    assert close_obsolete_requests(store) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='request:deadbeef'")["state"] == "ready"


def test_stale_frozen_generated_fix_returns_to_ready_when_root_is_still_submitted(tmp_path: Path):
    store = StubStore.open(tmp_path / "legacy-frozen-fix.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-1", state="submitted")
    add_task(store, "fix:TZ-1", state="frozen", kind="fix")
    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) VALUES(?,?,?,?,?,?)",
            (now(-120), "freeze", None, "P", "fix:TZ-1", json.dumps({"previous_state": "ready"})),
        )

    assert reactivate_stale_generated_fixes(store, after_min=60) == ["P/fix:TZ-1"]
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='fix:TZ-1'")["state"] == "ready"
        event = db.one(
            "SELECT kind,payload FROM events WHERE project='P' AND tz='fix:TZ-1' ORDER BY id DESC LIMIT 1"
        )
    assert event["kind"] == "auto_reactivate"
    assert json.loads(event["payload"])["root_tz"] == "TZ-1"


def test_fresh_or_blocked_frozen_fix_is_not_reactivated_by_legacy_cleanup(tmp_path: Path):
    store = StubStore.open(tmp_path / "fresh-frozen-fix.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-1", state="submitted")
    add_task(store, "fix:TZ-1", state="frozen", kind="fix")
    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) VALUES(?,?,?,?,?,?)",
            (now(-10), "freeze", None, "P", "fix:TZ-1", "{}"),
        )
    assert reactivate_stale_generated_fixes(store, after_min=60) == []

    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,worker_id,project,tz,payload) VALUES(?,?,?,?,?,?)",
            (now(-120), "block", "W1", "P", "fix:TZ-1", json.dumps({"reason": "blocked: external dependency"})),
        )
    assert reactivate_stale_generated_fixes(store, after_min=60) == []


def test_family_root():
    from dev_mcp.fleet.queue import family_root

    assert family_root("TZ-04") == "TZ-04"
    assert family_root("review:TZ-04") == "TZ-04"
    assert family_root("fix:TZ-04:3") == "TZ-04"
    assert family_root("review:fix:TZ-04:2") == "TZ-04"
    assert family_root("review:fix:TZ-04") == "TZ-04"
    assert family_root("request:88101bed") == "request:88101bed"
    # номер стыка — часть имени, а не раунд fix (07.10.2026: fix:styk:4 читался как fix задачи «styk»)
    assert family_root("fix:styk:4") == "styk:4"
    assert family_root("review:fix:styk:3:2:2") == "styk:3"
    assert family_root("review:request:12345678:2") == "request:12345678"


def test_started_work_goes_before_new_work_in_a_project(tmp_path: Path):
    store = StubStore.open(tmp_path / "finish-first.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-NEW", owns=("/repo/new.py",), priority=1)
    add_task(store, "fix:TZ-OLD", kind="fix", owns=("/repo/old.py",), priority=4)
    add_task(store, "review:TZ-MID", kind="review", priority=3)

    order = []
    for _ in range(3):
        task = take_next(store, "W1")
        order.append(task.tz)
        finish(store, "W1", "x")
        with store.tx() as db:
            db.execute("UPDATE tasks SET state='accepted' WHERE tz=?", (task.tz,))
    assert order == ["review:TZ-MID", "fix:TZ-OLD", "TZ-NEW"]


def test_finish_first_within_project_priority(tmp_path: Path):
    store = StubStore.open(tmp_path / "project-priority.sqlite")
    add_project(store, "A", priority=1)
    add_project(store, "B", priority=5)
    add_worker(store, "W1")
    add_task(store, "review:TZ-B", project="B", kind="review")
    add_task(store, "TZ-A", project="A")
    add_task(store, "review:TZ-A0", project="A", kind="review")
    assert take_next(store, "W1").tz == "review:TZ-A0"   # внутри A — начатое первым


def test_same_priority_work_prefers_dependency_that_immediately_unlocks_more_work(tmp_path: Path):
    store = StubStore.open(tmp_path / "dependency-fanout.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    # Старую независимую работу прежний FIFO выбирал первой.
    add_task(store, "TZ-OLD", owns=("/repo/old.py",), priority=5)
    add_task(store, "TZ-UNLOCK", owns=("/repo/unlock.py",), priority=5)
    add_task(store, "TZ-D1", needs=("TZ-UNLOCK",), owns=("/repo/d1.py",), priority=5)
    add_task(store, "TZ-D2", needs=("TZ-UNLOCK",), owns=("/repo/d2.py",), priority=5)

    assert take_next(store, "W1").tz == "TZ-UNLOCK"


def test_dependency_fanout_never_beats_explicit_task_priority(tmp_path: Path):
    store = StubStore.open(tmp_path / "dependency-fanout-priority.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-URGENT", owns=("/repo/urgent.py",), priority=1)
    add_task(store, "TZ-UNLOCK", owns=("/repo/unlock.py",), priority=5)
    add_task(store, "TZ-D1", needs=("TZ-UNLOCK",), owns=("/repo/d1.py",), priority=5)
    add_task(store, "TZ-D2", needs=("TZ-UNLOCK",), owns=("/repo/d2.py",), priority=5)

    assert take_next(store, "W1").tz == "TZ-URGENT"


def test_immediate_unlock_beats_larger_but_not_yet_actionable_fanout(tmp_path: Path):
    store = StubStore.open(tmp_path / "dependency-unlock-now.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-NOW", owns=("/repo/now.py",), priority=5)
    add_task(store, "TZ-LATER", owns=("/repo/later.py",), priority=5)
    add_task(store, "TZ-OTHER", owns=("/repo/other.py",), priority=5)
    add_task(store, "TZ-NOW-D", needs=("TZ-NOW",), owns=("/repo/now-d.py",), priority=5)
    for i in range(3):
        add_task(
            store,
            f"TZ-LATER-D{i}",
            needs=("TZ-LATER", "TZ-OTHER"),
            owns=(f"/repo/later-d{i}.py",),
            priority=5,
        )

    assert take_next(store, "W1").tz == "TZ-NOW"


def test_blocked_task_waits_for_named_dependency_instead_of_time_thaw(tmp_path: Path):
    store = StubStore.open(tmp_path / "thaw-dependency.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-123", state="ready", owns=("/repo/dep.py",))
    # A blocker dependency is structural; mentioning TZ-123 in prose is not enough.
    add_task(store, "TZ-BLOCK", state="frozen", needs=("tz-123",), owns=("/repo/x.py",))
    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            (now(-240), "block", "P", "TZ-BLOCK", json.dumps({"reason": "blocked: ждём TZ-123"})),
        )

    assert thaw_frozen(store, after_min=20) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-BLOCK'")["state"] == "frozen"
        db.execute("UPDATE tasks SET state='accepted' WHERE project='P' AND tz='TZ-123'")
        # Only an acceptance newer than the block is an observable unblocking fact.
        db.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            (now(), "accept", "P", "TZ-123", "{}"),
        )

    thawed = thaw_frozen(store, after_min=20)
    assert any("TZ-BLOCK" in item and "tz-123" in item for item in thawed)


def test_durable_external_block_never_reissues_by_timer(tmp_path: Path):
    store = StubStore.open(tmp_path / "thaw-external.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-BLOCK", state="frozen", owns=("/repo/x.py",))
    with store.tx() as db:
        db.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            (
                now(-600),
                "block",
                "P",
                "TZ-BLOCK",
                json.dumps({"reason": "blocked: provider-auth OpenRouter secret/spend is unavailable"}),
            ),
        )

    assert thaw_frozen(store, after_min=20) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-BLOCK'")["state"] == "frozen"


def test_blocked_task_has_backoff_after_three_thaws_but_never_permanent_gate(tmp_path: Path):
    store = StubStore.open(tmp_path / "thaw-backoff.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-BLOCK", state="frozen", owns=("/repo/x.py",))
    with store.tx() as db:
        # Three previous automatic thaw attempts, then a fresh block. The old contract
        # stopped forever here; the new contract waits 40 minutes and retries.
        for i in range(3):
            db.execute(
                "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
                (now(-100 + i), "thaw", "P", "TZ-BLOCK", "{}"),
            )
        db.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            (now(-41), "block", "P", "TZ-BLOCK", json.dumps({"reason": "blocked: external condition"})),
        )

    thawed = thaw_frozen(store, after_min=20, max_thaws=3)
    assert any("TZ-BLOCK" in item for item in thawed)
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-BLOCK'")["state"] == "ready"


def test_blocked_task_backoff_prevents_fast_churn_after_three_thaws(tmp_path: Path):
    store = StubStore.open(tmp_path / "thaw-backoff-hold.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-BLOCK", state="frozen", owns=("/repo/x.py",))
    with store.tx() as db:
        for i in range(3):
            db.execute(
                "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
                (now(-100 + i), "thaw", "P", "TZ-BLOCK", "{}"),
            )
        db.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,?,?,?,?)",
            (now(-30), "block", "P", "TZ-BLOCK", json.dumps({"reason": "blocked: external condition"})),
        )

    assert thaw_frozen(store, after_min=20, max_thaws=3) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-BLOCK'")["state"] == "frozen"


def test_revoked_worker_cannot_take_ready_work(tmp_path: Path):
    store = StubStore.open(tmp_path / "revoked-worker.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-READY", owns=("/repo/a.py",))
    with store.tx() as db:
        db.execute(
            "UPDATE workers SET revoked_at=?,revoke_reason=? WHERE id='W1'",
            (now(), "merged duplicate browser label"),
        )

    assert take_next(store, "W1") is None
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-READY'")["state"] == "ready"
        assert db.one("SELECT COUNT(*) AS n FROM claims WHERE worker_id='W1' AND state='active'")["n"] == 0


def test_blocked_fix_reserves_its_zone_from_new_work(tmp_path: Path):
    """fix, упёршийся в чужой захват, не отдаёт свою зону новой работе — иначе ждёт вечно."""
    store = StubStore.open(tmp_path / "reserve.sqlite")
    add_project(store, "P")
    for w in ("W1", "W2", "W3"):
        add_worker(store, w)
    add_task(store, "TZ-A", owns=("/P/a/one.py",), priority=1)
    assert take_next(store, "W1").tz == "TZ-A"
    add_task(store, "fix:TZ-F", kind="fix", owns=("/P/a/one.py", "/P/b"))
    add_task(store, "TZ-B", owns=("/P/b/two.py",))
    add_task(store, "TZ-C", owns=("/P/c",))

    assert take_next(store, "W2").tz == "TZ-C"  # TZ-B старше, но лезет в зону ждущего fix
    assert take_next(store, "W3") is None


def test_fix_with_whole_project_zone_does_not_stop_the_project(tmp_path: Path):
    store = StubStore.open(tmp_path / "reserve-root.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_worker(store, "W2")
    add_task(store, "TZ-A", owns=("/P/a",), priority=1)
    assert take_next(store, "W1").tz == "TZ-A"
    add_task(store, "fix:TZ-F", kind="fix", owns=("/P",))
    add_task(store, "TZ-B", owns=("/P/b",))

    assert take_next(store, "W2").tz == "TZ-B"


def test_styk_goes_with_fixes_before_new_work(tmp_path: Path):
    store = StubStore.open(tmp_path / "styk-rank.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_task(store, "TZ-1", owns=("/P/a",), priority=0)
    add_task(store, "styk:1", owns=("/P/b",), priority=5)

    assert take_next(store, "W1").tz == "styk:1"


def test_submitted_without_review_or_fix_gets_review_again(tmp_path: Path):
    store = StubStore.open(tmp_path / "orphan.sqlite")
    add_project(store, "P")
    add_task(store, "TZ-1", state="submitted")  # сирота
    add_task(store, "TZ-2", state="submitted")  # ждёт свой fix
    add_task(store, "fix:TZ-2", kind="fix", state="frozen")
    add_task(store, "TZ-3", state="submitted")  # ревью уже открыто
    add_task(store, "review:TZ-3", kind="review")
    add_task(store, "styk:4", state="submitted")  # fix стыка — fix:styk:4, не fix «styk»
    add_task(store, "fix:styk:4", kind="fix")

    assert reopen_orphan_reviews(store) == ["P/TZ-1"]
    assert reopen_orphan_reviews(store) == []  # идемпотентно: ревью теперь открыто
    with store.tx() as db:
        assert db.one("SELECT state,kind FROM tasks WHERE tz='review:TZ-1'")["state"] == "ready"
        kinds = [r["kind"] for r in db.query("SELECT kind FROM events WHERE tz='TZ-1' ORDER BY id")]
    assert kinds == ["review_reopen"]


def test_take_next_on_empty_queue_never_takes_the_write_lock(tmp_path: Path):
    store = StubStore.open(tmp_path / "empty.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")

    def no_write_lock():
        raise AssertionError("пустая очередь не должна брать замок записи")

    store.touch_worker = lambda worker_id: None  # type: ignore[method-assign]  # пульс — отдельная короткая запись
    @contextlib.contextmanager
    def plain_read():
        conn = sqlite3.connect(store.path)
        conn.row_factory = sqlite3.Row
        try:
            yield StubTxn(conn)
        finally:
            conn.close()

    store.read = plain_read  # type: ignore[method-assign]
    store.tx = no_write_lock  # type: ignore[method-assign]
    assert take_next(store, "W1") is None


def test_accepted_watch_is_terminal_across_legacy_requeue_block_and_tick(tmp_path: Path):
    from dev_mcp.fleet import judgment

    store = StubStore.open(tmp_path / "terminal-watch.sqlite")
    add_project(store, "P")
    for worker in ("W-author", "W-review", "W-legacy", "W-next"):
        add_worker(store, worker)
    old_watch = "watch:old-episode"
    add_task(store, old_watch, kind="triage")
    with store.tx() as db:
        db.execute("UPDATE tasks SET acceptance='[]' WHERE project='P' AND tz=?", (old_watch,))

    # Real submit -> independent review accept; only the file write is a
    # synthetic event, because this SQLite fixture has no live project files.
    assert take_next(store, "W-author").tz == old_watch
    with store.tx() as db:
        db.record_event(kind="write", worker_id="W-author", project="P",
                        tz=old_watch, path_norm="/P/notes/decision.txt")
    submit(store, "W-author", "decision recorded")
    review_tz = open_review(store, "P", old_watch)
    assert take_next(store, "W-review").tz == review_tz
    decide(store, "W-review", "accept", "decision verified")

    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (old_watch,))["state"] == "accepted"
        assert db.scalar("SELECT COUNT(*) FROM events WHERE project='P' AND tz=? AND kind='accept'",
                         (old_watch,)) == 1
        # Simulate the old judgment/apply path incorrectly returning an
        # already accepted episode to ready.
        db.execute("UPDATE tasks SET state='ready' WHERE project='P' AND tz=?", (old_watch,))
    assert take_next(store, "W-legacy") is None
    assert thaw_frozen(store, after_min=0) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (old_watch,))["state"] == "accepted"

    # A pre-upgrade worker may already hold a taken legacy claim. Repeated
    # attempts to block/finish it must restore accepted, not reopen it.
    for action in ("block", "finish"):
        with store.tx() as db:
            db.execute("UPDATE tasks SET state='taken' WHERE project='P' AND tz=?", (old_watch,))
            db.execute(
                "INSERT INTO claims(project,tz,worker_id,state,started_at) "
                "VALUES('P',?,'W-legacy','active',?)", (old_watch, now()),
            )
        if action == "block":
            block(store, "W-legacy", "blocked: old repair pending")
        else:
            assert finish(store, "W-legacy", "legacy handoff") == "accepted"
        with store.tx() as db:
            assert db.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (old_watch,))["state"] == "accepted"
            assert db.scalar(
                "SELECT COUNT(*) FROM claims WHERE project='P' AND tz=? AND state='active'",
                (old_watch,),
            ) == 0

    # A stale block + timer must not thaw a previously accepted episode.
    with store.tx() as db:
        db.execute("UPDATE tasks SET state='frozen' WHERE project='P' AND tz=?", (old_watch,))
        db.record_event(kind="block", project="P", tz=old_watch,
                        payload={"reason": "blocked: waiting on fix"})
    assert thaw_frozen(store, after_min=0) == []
    # The whole judgment maintenance tick must not resurrect this episode.
    judgment.tick(store, decisions=lambda: {})
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (old_watch,))["state"] == "accepted"
    assert take_next(store, "W-legacy") is None

    # A genuinely new watchdog episode has a distinct ID and remains issuable.
    add_task(store, "watch:new-episode", kind="triage")
    assert take_next(store, "W-next").tz == "watch:new-episode"


def test_blocked_watch_ignores_timer_but_unfinished_work_retries(tmp_path: Path):
    store = StubStore.open(tmp_path / "watch-vs-work-thaw.sqlite")
    add_project(store, "P")
    add_worker(store, "W1")
    add_worker(store, "W2")
    add_task(store, "watch:unresolved", kind="triage", state="frozen")
    add_task(store, "TZ-unfinished", kind="work", state="frozen")
    with store.tx() as db:
        for tz in ("watch:unresolved", "TZ-unfinished"):
            db.execute(
                "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,'block','P',?,?)",
                (now(-90), tz, json.dumps({"reason": "blocked: ordinary retry"})),
            )

    thawed = thaw_frozen(store, after_min=20)
    assert [entry.split(": ")[0] for entry in thawed] == ["P/TZ-unfinished"]
    with store.tx() as db:
        states = {row["tz"]: row["state"]
                  for row in db.query("SELECT tz,state FROM tasks WHERE project='P'")}
    assert states == {"watch:unresolved": "frozen", "TZ-unfinished": "ready"}

    add_task(store, "watch:independent-new", kind="triage", priority=1)
    issued = {take_next(store, worker).tz for worker in ("W1", "W2")}
    assert issued == {"TZ-unfinished", "watch:independent-new"}


def test_accepted_watch_survives_sqlite_reopen_and_dependency_thaw(tmp_path: Path):
    from dev_mcp.fleet.queue import peek_next

    path = tmp_path / "persisted-watch.sqlite"
    store = StubStore.open(path)
    add_project(store, "P")
    add_worker(store, "W-old")
    add_worker(store, "W-new")
    old = "watch:fingerprint1"
    add_task(store, old, kind="triage", state="accepted")
    with store.tx() as db:
        db.record_event(kind="accept", project="P", tz=old, worker_id="W-review",
                        payload={"note": "accepted in previous session"})
        # Simulate a stale pre-fix block after the accepted verdict.
        db.execute("UPDATE tasks SET state='frozen' WHERE project='P' AND tz=?", (old,))
        db.execute(
            "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,'block','P',?,?)",
            (now(-90), old, json.dumps({"reason": "blocked: legacy guard repair"})),
        )

    assert thaw_frozen(store, after_min=20) == []
    with store.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (old,))["state"] == "accepted"
    assert peek_next(store)[0] is None
    assert take_next(store, "W-old") is None

    # Open fresh SQLite connections, then simulate another legacy ready edge:
    # the historical accept event must remain authoritative across sessions.
    reopened = StubStore(path)
    with reopened.tx() as db:
        db.execute("UPDATE tasks SET state='ready' WHERE project='P' AND tz=?", (old,))
    assert peek_next(reopened)[0] is None
    assert take_next(reopened, "W-old") is None
    assert thaw_frozen(reopened, after_min=20) == []
    with reopened.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE project='P' AND tz=?", (old,))["state"] == "accepted"

    add_task(reopened, "watch:fingerprint2", kind="triage", priority=1)
    assert peek_next(reopened)[0].tz == "watch:fingerprint2"
    assert take_next(reopened, "W-new").tz == "watch:fingerprint2"

    # An unresolved watchdog can retry only after its *named* prerequisite
    # is newly accepted; an ordinary unfinished work item still times out.
    add_task(reopened, "TZ-dependency", state="ready")
    add_task(reopened, "watch:pending-dependency", kind="triage", state="frozen",
             needs=("TZ-dependency",))
    add_task(reopened, "TZ-timed-retry", state="frozen")
    with reopened.tx() as db:
        for tz in ("watch:pending-dependency", "TZ-timed-retry"):
            db.execute(
                "INSERT INTO events(ts,kind,project,tz,payload) VALUES(?,'block','P',?,?)",
                (now(-90), tz, json.dumps({"reason": "blocked: waiting on TZ-dependency"})),
            )
    thawed = thaw_frozen(reopened, after_min=20)
    assert len(thawed) == 1 and thawed[0].startswith("P/TZ-timed-retry:")
    with reopened.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE tz='watch:pending-dependency'")["state"] == "frozen"
        assert db.one("SELECT state FROM tasks WHERE tz='TZ-timed-retry'")["state"] == "ready"
        db.execute("UPDATE tasks SET state='accepted' WHERE project='P' AND tz='TZ-dependency'")
        db.record_event(kind="accept", project="P", tz="TZ-dependency", worker_id="W-review")
    assert any(item.startswith("P/watch:pending-dependency:") for item in thaw_frozen(reopened, after_min=20))
    with reopened.tx() as db:
        assert db.one("SELECT state FROM tasks WHERE tz='watch:pending-dependency'")["state"] == "ready"
