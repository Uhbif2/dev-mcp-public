from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from dev_mcp.fleet.spec import SpecError, parse_spec, spec_sha, sync_backlog, sync_selected, validate_spec


def _write_spec(
    path: Path,
    *,
    tz: str = "TZ-1",
    owns: str = "src/a.py",
    needs: str = "[]",
    acceptance: str = "[python -m py_compile src/a.py]",
    prose: str = "# Work\nbody",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Registry now probes *new* acceptance commands, not just updates. Supply
    # the input file that these schema/identity tests compile.
    if path.parent.name == "tz" and path.parent.parent.name == "ToDo":
        project = path.parent.parent.parent
        sample = project / "src" / "a.py"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text("value = 1\n", encoding="utf-8")
    path.write_text(
        "```yaml\n"
        f"tz: {tz}\n"
        f"owns: [{owns}]\n"
        f"needs: {needs}\n"
        f"acceptance: {acceptance}\n"
        "```\n\n"
        f"{prose}\n",
        encoding="utf-8",
    )


def test_standard_yaml_frontmatter_has_same_task_and_body_as_fenced_header(tmp_path):
    path = tmp_path / "ToDo" / "tz" / "TZ-1-work.md"
    _write_spec(path, prose="# Project task\nBody with --- separator")
    fenced = parse_spec(str(path))
    content = path.read_text(encoding="utf-8")
    path.write_text(content.replace("```yaml\n", "---\n", 1).replace("\n```\n", "\n---\n", 1),
                    encoding="utf-8")
    frontmatter = parse_spec(str(path))
    assert frontmatter == fenced
    assert spec_sha(str(path)) == fenced.spec_sha
    assert frontmatter.body.startswith("# Project task")


def test_frontmatter_requires_closing_delimiter_and_first_position(tmp_path):
    path = tmp_path / "ToDo" / "tz-drafts" / "TZ-4-work.md"
    path.parent.mkdir(parents=True)
    for value in ("---\ntz: TZ-4\nowns: [src/a.py]\n# no end",
                  "# Preface\n---\ntz: TZ-4\nowns: [src/a.py]\n---\n"):
        path.write_text(value, encoding="utf-8")
        with pytest.raises(SpecError, match="YAML-шапк"):
            parse_spec(str(path))


class Policy:
    def __init__(self, root: Path, write_roots: list[Path] | None = None):
        self.roots = [str(root)]
        self.write_roots = [str(p) for p in (write_roots or [root])]


SCHEMA = """
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
CREATE TABLE events(
    id INTEGER PRIMARY KEY,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    kind TEXT,
    worker_id TEXT,
    project TEXT,
    tz TEXT,
    tool TEXT DEFAULT '',
    path_norm TEXT DEFAULT '',
    rc INTEGER,
    payload TEXT DEFAULT '{}'
);
"""


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
        self.conn.execute(
            "INSERT INTO events(kind,worker_id,project,tz,tool,payload) VALUES(?,?,?,?,?,?)",
            (
                event.get("kind"),
                event.get("worker_id"),
                event.get("project"),
                event.get("tz"),
                event.get("tool", ""),
                event.get("payload", "{}"),
            ),
        )

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
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    @contextlib.contextmanager
    def tx(self):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield StubTxn(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def test_parse_correct_header(tmp_path: Path):
    root = tmp_path / "Project"
    path = root / "ToDo" / "tz" / "TZ-1-demo.md"
    _write_spec(path, owns="src/a.py, tests/a/**", needs="[TZ-0]")
    task = parse_spec(str(path.resolve()))
    assert task.project == "Project"
    assert task.tz == "TZ-1"
    assert task.needs == ("TZ-0",)
    assert len(task.owns) == 2
    assert task.owns[1].endswith("/tests/a/**")
    assert task.title == "Work"


def test_missing_or_late_header_is_clear(tmp_path: Path):
    missing = tmp_path / "missing.md"
    missing.write_text("# no header\n", encoding="utf-8")
    with pytest.raises(SpecError, match="нет машиночитаемой"):
        parse_spec(str(missing.resolve()))

    late = tmp_path / "late.md"
    late.write_text(
        "# title\n```yaml\ntz: TZ-1\nowns: [a.py]\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match="должна быть первой"):
        parse_spec(str(late.resolve()))


def test_spec_sha_only_tracks_header(tmp_path: Path):
    path = tmp_path / "TZ-1.md"
    _write_spec(path, prose="# First\nbody one")
    first = spec_sha(str(path.resolve()))

    _write_spec(path, prose="# First\nbody two")
    assert spec_sha(str(path.resolve())) == first

    _write_spec(path, owns="src/b.py", prose="# First\nbody two")
    assert spec_sha(str(path.resolve())) != first


def test_validate_spec_outside_write_root_and_cycle(tmp_path: Path):
    root = tmp_path / "Project"
    tzdir = root / "ToDo" / "tz"
    a = tzdir / "TZ-A.md"
    b = tzdir / "TZ-B.md"
    _write_spec(a, tz="TZ-A", owns="../outside.py", needs="[TZ-B]")
    _write_spec(b, tz="TZ-B", owns="src/b.py", needs="[TZ-A]")

    task = parse_spec(str(a.resolve()))
    issues = validate_spec(task, Policy(root, [root / "src"]))

    assert any("вне write_roots" in issue for issue in issues)
    assert any("цикл" in issue for issue in issues)


def test_sync_backlog_does_not_reset_taken_task(tmp_path: Path):
    root = tmp_path / "Project"
    path = root / "ToDo" / "tz" / "TZ-1-demo.md"
    _write_spec(path, owns="src/a.py")

    store = StubStore(tmp_path / "fleet.sqlite")
    with store.tx() as db:
        db.execute(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,5,1)",
            ("Project", str(root)),
        )

    first = sync_backlog(store, "Project", str(root))
    assert first["inserted"] == 1

    with store.tx() as db:
        db.execute(
            "UPDATE tasks SET state='taken' WHERE project='Project' AND tz='TZ-1'"
        )
        before = db.execute(
            "SELECT spec_sha FROM tasks WHERE project='Project' AND tz='TZ-1'"
        ).fetchone()[0]

    _write_spec(path, owns="src/changed.py")
    second = sync_backlog(store, "Project", str(root))
    assert second["updated"] == 1
    assert second["skipped_taken"] == 0

    with store.tx() as db:
        row = db.execute(
            "SELECT state,spec_sha FROM tasks WHERE project='Project' AND tz='TZ-1'"
        ).fetchone()

    assert row["state"] == "taken"
    assert row["spec_sha"] != before


def test_sync_backlog_refreshes_body_without_header_revision(tmp_path: Path):
    root = tmp_path / "Project"
    path = root / "ToDo" / "tz" / "TZ-1-demo.md"
    _write_spec(path, prose="# Original title\noriginal body")

    store = StubStore(tmp_path / "body-refresh.sqlite")
    with store.tx() as db:
        db.execute(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,5,1)",
            ("Project", str(root)),
        )

    assert sync_backlog(store, "Project", str(root))["inserted"] == 1
    with store.tx() as db:
        before = db.one(
            "SELECT spec_sha FROM tasks WHERE project='Project' AND tz='TZ-1'"
        )["spec_sha"]

    _write_spec(path, prose="# Updated title\nupdated body")
    result = sync_backlog(store, "Project", str(root))

    assert result["updated"] == 1
    assert result["unchanged"] == 0
    with store.tx() as db:
        row = db.one(
            "SELECT title,body,spec_sha FROM tasks WHERE project='Project' AND tz='TZ-1'"
        )
    assert row["title"] == "Updated title"
    assert "updated body" in row["body"]
    assert row["spec_sha"] == before


def test_sync_backlog_does_not_import_ambiguous_duplicate_tz(tmp_path: Path):
    root = tmp_path / "Project"
    tzdir = root / "ToDo" / "tz"
    _write_spec(tzdir / "TZ-A-first.md", tz="TZ-A", owns="src/a.py")
    _write_spec(tzdir / "TZ-A-second.md", tz="TZ-A", owns="src/b.py")

    store = StubStore(tmp_path / "dupe.sqlite")
    with store.tx() as db:
        db.execute(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,5,1)",
            ("Project", str(root)),
        )

    result = sync_backlog(store, "Project", str(root))
    assert result["inserted"] == 0
    assert len(result["errors"]) == 2
    with store.tx() as db:
        assert db.scalar("SELECT COUNT(*) FROM tasks WHERE project='Project'") == 0


def test_sync_selected_ignores_non_spec_tombstone_but_refuses_two_valid_specs(tmp_path: Path):
    root = tmp_path / "Project"
    tzdir = root / "ToDo" / "tz"
    _write_spec(tzdir / "TZ-A-active.md", tz="TZ-A", owns="src/a.py")
    (tzdir / "TZ-A-old-name.md").write_text(
        "# Ошибочный номер — не ТЗ\n\nНомер не используется.\n",
        encoding="utf-8",
    )

    store = StubStore(tmp_path / "selected-tombstone.sqlite")
    first = sync_selected(store, "Project", str(root), ["TZ-A"])
    assert first["inserted"] == 1
    assert first["errors"] == {}

    _write_spec(tzdir / "TZ-A-second-valid.md", tz="TZ-A", owns="src/b.py")
    second = sync_selected(store, "Project", str(root), ["TZ-A"])
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert "TZ-A" in second["errors"]
    with store.tx() as db:
        row = db.one(
            "SELECT spec_path FROM tasks WHERE project='Project' AND tz='TZ-A'"
        )
    assert row["spec_path"].endswith("TZ-A-active.md")


def test_sync_selected_applies_policy_validation_without_manual_gate(tmp_path: Path):
    root = tmp_path / "Project"
    path = root / "ToDo" / "tz" / "TZ-A-active.md"
    _write_spec(path, tz="TZ-A", owns="../outside.py")

    store = StubStore(tmp_path / "selected-policy.sqlite")
    policy = Policy(root, [root / "src"])
    blocked = sync_selected(store, "Project", str(root), ["TZ-A"], policy=policy)

    assert blocked["inserted"] == 0
    assert any("вне write_roots" in message for message in blocked["errors"].values())
    with store.tx() as db:
        assert db.scalar("SELECT COUNT(*) FROM tasks WHERE project='Project'") == 0

    _write_spec(path, tz="TZ-A", owns="src/a.py", acceptance="[]")
    accepted = sync_selected(store, "Project", str(root), ["TZ-A"], policy=policy)
    assert accepted["inserted"] == 1
    assert accepted["errors"] == {}
    assert any(
        "acceptance пуст" in warning
        for warnings in accepted["warnings"].values()
        for warning in warnings
    )


def test_sync_selected_only_imports_explicit_active_backlog(tmp_path: Path):
    root = tmp_path / "Project"
    tzdir = root / "ToDo" / "tz"
    _write_spec(tzdir / "TZ-OLD-history.md", tz="TZ-OLD", owns="src/old.py")
    _write_spec(tzdir / "TZ-A-active.md", tz="TZ-A", owns="src/a.py")
    _write_spec(tzdir / "TZ-B-active.md", tz="TZ-B", owns="src/b.py")

    store = StubStore(tmp_path / "selected.sqlite")
    result = sync_selected(store, "Project", str(root), ["TZ-A", "TZ-B"])

    assert result["requested"] == 2
    assert result["inserted"] == 2
    assert result["errors"] == {}
    with store.tx() as db:
        rows = db.query(
            "SELECT tz,state FROM tasks WHERE project='Project' ORDER BY tz"
        )
    assert [(row["tz"], row["state"]) for row in rows] == [
        ("TZ-A", "ready"),
        ("TZ-B", "ready"),
    ]

    # Re-running the selective cutover is idempotent and still never resurrects
    # an omitted historical file.
    again = sync_selected(store, "Project", str(root), ["TZ-A", "TZ-B"])
    assert again["inserted"] == 0
    assert again["unchanged"] == 2
    with store.tx() as db:
        assert db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE project='Project' AND tz='TZ-OLD'"
        ) == 0


def test_sync_selected_refreshes_body_without_header_revision(tmp_path: Path):
    root = tmp_path / "Project"
    path = root / "ToDo" / "tz" / "TZ-A-active.md"
    _write_spec(path, tz="TZ-A", prose="# First\nold body")

    store = StubStore(tmp_path / "selected-body-refresh.sqlite")
    assert sync_selected(store, "Project", str(root), ["TZ-A"])["inserted"] == 1
    with store.tx() as db:
        before = db.one(
            "SELECT spec_sha FROM tasks WHERE project='Project' AND tz='TZ-A'"
        )["spec_sha"]

    _write_spec(path, tz="TZ-A", prose="# Second\nnew body")
    result = sync_selected(store, "Project", str(root), ["TZ-A"])

    assert result["updated"] == 1
    assert result["unchanged"] == 0
    with store.tx() as db:
        row = db.one(
            "SELECT title,body,spec_sha FROM tasks WHERE project='Project' AND tz='TZ-A'"
        )
    assert row["title"] == "Second"
    assert "new body" in row["body"]
    assert row["spec_sha"] == before


def test_sync_selected_preserves_registered_acceptance_until_owner_revision(tmp_path: Path):
    root = tmp_path / "Project"
    path = root / "ToDo" / "tz" / "TZ-A-active.md"
    original = "python -m py_compile src/a.py"
    _write_spec(path, tz="TZ-A", acceptance=f"[{original}]")
    store = StubStore(tmp_path / "selected-frozen-acceptance.sqlite")
    assert sync_selected(store, "Project", str(root), ["TZ-A"])["inserted"] == 1

    # Editing the on-disk YAML is not an owner approval of a weaker contract.
    _write_spec(path, tz="TZ-A", acceptance="[python -m py_compile src/missing.py]")
    rescan = sync_selected(store, "Project", str(root), ["TZ-A"])
    assert rescan["updated"] == 1 and not rescan["errors"]
    assert any(
        "registered acceptance preserved" in warning
        for messages in rescan["warnings"].values() for warning in messages
    )
    with store.tx() as db:
        assert json.loads(db.one(
            "SELECT acceptance FROM tasks WHERE project='Project' AND tz='TZ-A'"
        )["acceptance"]) == [original]

    # An explicit owner-side migration becomes the new trusted registration;
    # another YAML rescan cannot roll it back.
    approved = [original, 'python -c "print(1)"']
    with store.tx() as db:
        db.change(
            "UPDATE tasks SET acceptance=? WHERE project='Project' AND tz='TZ-A'",
            (json.dumps(approved),),
            event={"kind": "acceptance_update", "project": "Project", "tz": "TZ-A"},
        )
    assert not sync_selected(store, "Project", str(root), ["TZ-A"])["errors"]
    with store.tx() as db:
        assert json.loads(db.one(
            "SELECT acceptance FROM tasks WHERE project='Project' AND tz='TZ-A'"
        )["acceptance"]) == approved


def test_sync_backlog_uses_tz_as_stable_identity_across_rename(tmp_path: Path):
    root = tmp_path / "Project"
    old_path = root / "ToDo" / "tz" / "TZ-1-old.md"
    new_path = root / "ToDo" / "tz" / "TZ-1-new.md"
    _write_spec(old_path, owns="src/a.py")

    store = StubStore(tmp_path / "rename.sqlite")
    with store.tx() as db:
        db.execute(
            "INSERT INTO projects(name,root,priority,active) VALUES(?,?,5,1)",
            ("Project", str(root)),
        )

    assert sync_backlog(store, "Project", str(root))["inserted"] == 1
    old_path.rename(new_path)
    result = sync_backlog(store, "Project", str(root))

    assert result["inserted"] == 0
    assert result["updated"] == 1
    with store.tx() as db:
        rows = db.execute(
            "SELECT tz,spec_path,state FROM tasks WHERE project='Project'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["tz"] == "TZ-1"
    assert rows[0]["spec_path"].endswith("TZ-1-new.md")
    assert rows[0]["state"] == "ready"
