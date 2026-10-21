from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER_PATH = ROOT / "tools" / "worker.py"
NOVA = ROOT.parent / "Nova VPN"
AGENT_PATH = NOVA / "ops" / "agents" / "agent.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("tz08_worker", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def db_rows(db: pathlib.Path, sql: str, params=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def seed_ready_task(worker, db: pathlib.Path, project_root: pathlib.Path) -> None:
    worker._open_store(db)
    with worker._connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        worker._ensure_project(conn, "P", project_root)
        now = worker._now()
        conn.execute(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES ('P','TZ-X','Adapter task','ready',1,'work','','','[]','[]','[]','[]',"
            "NULL,'BODY FROM SHARED REGISTRY',?,?)",
            (now, now),
        )
        conn.commit()


def test_cli_uses_shared_registry_and_worker_id_survives_process(tmp_path: pathlib.Path) -> None:
    worker = load_worker()
    db = tmp_path / "fleet.sqlite"
    project_root = tmp_path / "project"
    project_root.mkdir()
    seed_ready_task(worker, db, project_root)

    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    cmd = [sys.executable, str(WORKER_PATH), "--db", str(db)]

    first = subprocess.run(
        [*cmd, "join", "codex-local", "--kind", "codex"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    wid = first.stdout.split()[0]
    assert wid.startswith("W")

    taken = subprocess.run(
        [*cmd, "take", "--project", "P"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert "P/TZ-X" in taken.stdout
    assert "BODY FROM SHARED REGISTRY" in taken.stdout

    rows = db_rows(db, "SELECT state FROM tasks WHERE project='P' AND tz='TZ-X'")
    assert rows[0]["state"] == "taken"
    resumed = subprocess.run(
        [*cmd, "--worker-name", "codex-local", "resume"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert "Возобновление: P/TZ-X" in resumed.stdout
    claims = db_rows(db, "SELECT worker_id,state FROM claims WHERE project='P' AND tz='TZ-X'")
    assert claims[0]["worker_id"] == wid
    assert claims[0]["state"] == "active"

    second = subprocess.run(
        [*cmd, "join", "codex-local", "--kind", "codex"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert second.stdout.split()[0] == wid

    subprocess.run(
        [*cmd, "submit", "готово"], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    subprocess.run(
        [*cmd, "finish"], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert db_rows(db, "SELECT state FROM tasks WHERE tz='TZ-X'")[0]["state"] == "submitted"
    assert db_rows(db, "SELECT state FROM claims WHERE tz='TZ-X'")[0]["state"] == "released"


def test_cli_submit_run_review_cycle_uses_shared_evidence(tmp_path: pathlib.Path) -> None:
    worker = load_worker()
    db = tmp_path / "fleet.sqlite"
    project_root = tmp_path / "project"
    project_root.mkdir()
    seed_ready_task(worker, db, project_root)

    acceptance_argv = [sys.executable, "-c", "print('review-ok')"]
    acceptance = " ".join(acceptance_argv)
    with worker._connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET acceptance=? WHERE project='P' AND tz='TZ-X'",
            (json.dumps([acceptance]),),
        )
        conn.commit()

    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    cmd = [sys.executable, str(WORKER_PATH), "--db", str(db)]

    author = subprocess.run(
        [*cmd, "join", "author", "--kind", "codex"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.split()[0]
    subprocess.run(
        [*cmd, "--worker-name", "author", "take", "--project", "P"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    submitted = subprocess.run(
        [*cmd, "--worker-name", "author", "submit", "done"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert "review=review:TZ-X" in submitted.stdout
    assert "finish" in submitted.stdout
    subprocess.run(
        [*cmd, "--worker-name", "author", "finish"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )

    reviewer = subprocess.run(
        [*cmd, "join", "reviewer", "--kind", "claude-code"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.split()[0]
    assert reviewer != author
    taken = subprocess.run(
        [*cmd, "--worker-name", "reviewer", "take", "--project", "P"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert "P/review:TZ-X" in taken.stdout

    ran = subprocess.run(
        [*cmd, "--worker-name", "reviewer", "run", *acceptance_argv],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert ran.returncode == 0
    reviewed = subprocess.run(
        [*cmd, "--worker-name", "reviewer", "review", "accept", "verified"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert "Вердикт accept сохранён" in reviewed.stdout

    states = {
        row["tz"]: row["state"]
        for row in db_rows(db, "SELECT tz,state FROM tasks WHERE project='P'")
    }
    assert states["TZ-X"] == "accepted"
    assert states["review:TZ-X"] == "accepted"
    run_rows = db_rows(
        db,
        "SELECT worker_id,tool,rc,payload FROM events WHERE kind='run' ORDER BY id",
    )
    assert run_rows[-1]["worker_id"] == reviewer
    assert run_rows[-1]["tool"] == "worker.run"
    assert run_rows[-1]["rc"] == 0
    assert json.loads(run_rows[-1]["payload"])["argv"] == acceptance_argv


def test_sync_surfaces_inbox_and_spec_drift(tmp_path: pathlib.Path) -> None:
    worker = load_worker()
    Store, _, inbox, spec_mod, _ = worker._mods()
    db = tmp_path / "fleet.sqlite"
    project_root = tmp_path / "project"
    spec_dir = project_root / "ToDo" / "tz"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "TZ-SYNC.md"
    spec_path.write_text(
        "```yaml\ntz: TZ-SYNC\nproject: P\nzone: Z\nowns:\n  - a.py\n```\n# Sync task\n",
        encoding="utf-8",
    )

    worker._open_store(db)
    owns = worker._norm_owns(project_root, ["a.py"])
    with worker._connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        worker._ensure_project(conn, "P", project_root)
        now = worker._now()
        conn.execute(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES ('P','TZ-SYNC','Sync task','ready',1,'work','ToDo/tz/TZ-SYNC.md',?,'[]',"
            "?,'[]','[]',NULL,'',?,?)",
            (spec_mod.spec_sha(str(spec_path)), json.dumps(owns), now, now),
        )
        conn.commit()

    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    cmd = [sys.executable, str(WORKER_PATH), "--db", str(db)]
    joined = subprocess.run(
        [*cmd, "join", "sync-worker", "--kind", "claude-code"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    wid = joined.stdout.split()[0]
    subprocess.run(
        [*cmd, "take", "--project", "P"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )

    store = Store.open(str(db))
    inbox.send(
        store,
        to=wid,
        from_kind="system",
        from_id="",
        kind="note",
        body="проверь новый контракт",
        dedup_key="sync-test",
    )
    spec_path.write_text(
        "```yaml\ntz: TZ-SYNC\nproject: P\nzone: Z\nowns:\n  - a.py\n  - b.py\n```\n# Sync task\n",
        encoding="utf-8",
    )

    synced = subprocess.run(
        [*cmd, "sync"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert "ГРАНИЦЫ ЗАДАЧИ ИЗМЕНИЛИСЬ" in synced.stdout
    assert "проверь новый контракт" in synced.stdout


def test_import_three_claims_marks_unknown_worker(tmp_path: pathlib.Path) -> None:
    worker = load_worker()
    db = tmp_path / "fleet.sqlite"
    root = tmp_path / "Nova VPN"
    claims = tmp_path / "claims"
    claims.mkdir()
    root.mkdir()

    for index, tz in enumerate(("TZ-25", "TZ-47", "TZ-48"), start=1):
        (claims / f"{tz}.json").write_text(
            json.dumps(
                {
                    "tz": tz,
                    "zone": f"Z{index}",
                    "owns": [f"ops/example/{tz.lower()}.py"],
                    "resources": ["stand"] if tz == "TZ-48" else [],
                    "started_at": f"2026-09-18T0{index}:00:00+00:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    assert worker.import_claims(claims, root, "Nova VPN", db) == ["TZ-25", "TZ-47", "TZ-48"]
    assert worker.import_claims(claims, root, "Nova VPN", db) == []
    assert worker.bridge_finish("Nova VPN", "TZ-25", db)["existed"] is True
    assert worker.import_claims(claims, root, "Nova VPN", db) == [], "same legacy file must not resurrect a finished claim"

    tasks = db_rows(db, "SELECT tz,state FROM tasks ORDER BY tz")
    assert [(r["tz"], r["state"]) for r in tasks] == [
        ("TZ-25", "submitted"),
        ("TZ-47", "taken"),
        ("TZ-48", "taken"),
        ("review:TZ-25", "ready"),
    ]
    labels = db_rows(
        db,
        "SELECT w.label FROM claims c JOIN workers w ON w.id=c.worker_id ORDER BY c.tz",
    )
    assert all(worker.IMPORT_LABEL in row["label"] for row in labels)


def test_import_claim_disambiguates_duplicate_tz_by_ownership(tmp_path: pathlib.Path) -> None:
    worker = load_worker()
    db = tmp_path / "fleet.sqlite"
    root = tmp_path / "Project"
    tzdir = root / "ToDo" / "tz"
    tzdir.mkdir(parents=True)
    (tzdir / "TZ-52-first.md").write_text(
        "```yaml\n"
        "tz: TZ-52\n"
        "owns: [ops/pilot]\n"
        "resources: [node]\n"
        "```\n\n# Participants\n",
        encoding="utf-8",
    )
    (tzdir / "TZ-52-docs.md").write_text(
        "```yaml\n"
        "tz: TZ-52\n"
        "owns: [docs/GOTCHAS.md]\n"
        "resources: []\n"
        "```\n\n# Docs\n",
        encoding="utf-8",
    )
    claims = tmp_path / "claims"
    claims.mkdir()
    (claims / "TZ-52.json").write_text(
        json.dumps(
            {
                "tz": "TZ-52",
                "zone": "pilot",
                "owns": ["ops/pilot"],
                "resources": ["node"],
            }
        ),
        encoding="utf-8",
    )

    assert worker.import_claims(claims, root, "P", db) == ["TZ-52"]
    row = db_rows(
        db,
        "SELECT spec_path,title FROM tasks WHERE project='P' AND tz='TZ-52'",
    )[0]
    assert row["spec_path"].endswith("TZ-52-first.md")
    assert row["title"] == "Participants"


def test_request_import_is_draft_and_keeps_artifact(tmp_path: pathlib.Path) -> None:
    worker = load_worker()
    db = tmp_path / "fleet.sqlite"
    root = tmp_path / "Nova VPN"
    request_dir = root / "ToDo" / "requests"
    request_dir.mkdir(parents=True)
    source = request_dir / "2026-09-18-test.md"
    source.write_text("# Заявка: общий контракт\nТело заявки.\n", encoding="utf-8")

    imported = worker.import_requests(root, "Nova VPN", db)
    assert len(imported) == 1
    assert worker.import_requests(root, "Nova VPN", db) == []
    row = db_rows(db, "SELECT kind,state,title,body FROM tasks")[0]
    assert row["kind"] == "request"
    assert row["state"] == "draft"
    assert row["title"] == "Заявка: общий контракт"
    assert "Тело заявки." in row["body"]
    assert source.exists()


@pytest.mark.skipif(not AGENT_PATH.exists(), reason="Nova checkout is not present")
def test_nova_service_unavailable_falls_back(tmp_path: pathlib.Path) -> None:
    env = os.environ.copy()
    env["DEV_MCP_FLEET_MODE"] = "service"
    env["DEV_MCP_ROOT"] = str(tmp_path / "missing-dev-mcp")
    proc = subprocess.run(
        [sys.executable, str(AGENT_PATH), "list"],
        cwd=NOVA, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0
    assert proc.stdout.count("dev-mcp fleet недоступен — работаю по старым claim-файлам.") == 1


@pytest.mark.skipif(not AGENT_PATH.exists(), reason="Nova checkout is not present")
def test_nova_service_reads_shared_registry_on_temp_db(tmp_path: pathlib.Path) -> None:
    env = os.environ.copy()
    env["DEV_MCP_FLEET_MODE"] = "service"
    env["DEV_MCP_ROOT"] = str(ROOT)
    env["DEV_MCP_FLEET_DB"] = str(tmp_path / "fleet.sqlite")
    claim_dir = NOVA / "ops" / "agents" / "claims"
    before = {p.name: p.read_bytes() for p in claim_dir.glob("*.json")}

    proc = subprocess.run(
        [sys.executable, str(AGENT_PATH), "list"],
        cwd=NOVA, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0
    assert "dev-mcp fleet недоступен" not in proc.stdout
    real_claim_count = len(before)
    if real_claim_count:
        assert f"Активных заявок: {real_claim_count}" in proc.stdout

    started = subprocess.run(
        [sys.executable, str(AGENT_PATH), "start", "TZ-48"],
        cwd=NOVA, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert started.returncode == 0
    assert "OK — TZ-48 закреплено за тобой" in started.stdout

    listed = subprocess.run(
        [sys.executable, str(AGENT_PATH), "list"],
        cwd=NOVA, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert "TZ-48" in listed.stdout

    finished = subprocess.run(
        [sys.executable, str(AGENT_PATH), "finish", "TZ-48"],
        cwd=NOVA, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert finished.returncode == 0
    assert "TZ-48: освобождено" in finished.stdout
    assert {p.name: p.read_bytes() for p in claim_dir.glob("*.json")} == before
