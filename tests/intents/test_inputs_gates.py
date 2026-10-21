"""TZ-312: referenced files are no-copy inputs, not a global policy exception."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dev_mcp.audit import Audit
from dev_mcp.fleet.store import Store
from dev_mcp.intents.inputs import _paths
from dev_mcp.intents.sources.model import IntentEnvelope
from dev_mcp.intents.spawn import spawn_project
from dev_mcp.policy import Policy
from dev_mcp.server import build_server


def tool(server, name):
    return asyncio.run(server.get_tool(name)).fn


@pytest.fixture
def rig(tmp_path):
    project_parent = tmp_path / "projects"
    project_parent.mkdir()
    outside = tmp_path / "Downloads"
    outside.mkdir()
    (outside / "notes with spaces.txt").write_text("referenced confidential draft", encoding="utf-8")
    (outside / "other.txt").write_text("unreferenced-private", encoding="utf-8")
    (outside / ".env").write_text("TOPSECRET=not-to-read", encoding="utf-8")
    (outside / "folder").mkdir()
    (outside / "folder" / "allowed.md").write_text("inside-folder", encoding="utf-8")
    (outside / "folder" / "secret.password").write_text("PASSWORD", encoding="utf-8")
    missing = outside / "missing.csv"
    text = (
        f"Изучи файл \"{(outside / 'notes with spaces.txt').as_posix()}\" "
        f"и папку `{(outside / 'folder').as_posix()}`. "
        f"Также позже: {missing.as_posix()}. "
        "Таблица https://docs.google.com/spreadsheets/d/abc/edit "
        "канал https://t.me/example и https://example.org/info."
    )
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{project_parent.as_posix()}']\n"
        f"write_roots = ['{project_parent.as_posix()}']\n"
        "deny_patterns = ['.env', '*.password', '.git']\n"
        "allow_write = true\n",
        encoding="utf-8",
    )
    policy = Policy.load(config)
    db = tmp_path / "fleet.sqlite"
    store = Store.open(str(db))
    envelope = IntentEnvelope(
        "files", "test-tz312", "r1", text,
        {"create_project": True, "project_roots": [str(project_parent)]},
        "2026-10-13T10:00:00+00:00",
    )
    result = spawn_project(store, envelope, parent=project_parent,
                           project_name="intended", size="small",
                           allowed_roots=[project_parent])
    stamp = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) "
                  "VALUES('other',?,1,1)", (str(project_parent / 'other'),),
                  event="test")
        tx.change("INSERT INTO tasks(project,tz,title,state,created_at,updated_at) "
                  "VALUES('other','TZ-1','other','taken',?,?)",
                  (stamp, stamp), event="test")
        for worker in ("WINT", "WOTHER", "WREVOKED"):
            tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) "
                      "VALUES(?,?,'chatgpt',?,?)",
                      (worker, worker, stamp, stamp), event="test")
        for project, tz, worker in (
            ("intended", result.tree.root_id + "-understood", "WINT"),
            ("other", "TZ-1", "WOTHER"),
            ("intended", result.tree.root_id + "-understood", "WREVOKED"),
        ):
            tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                      "VALUES(?,?,?,'active',?)",
                      (project, tz, worker, stamp), event="test")
        tx.change("UPDATE workers SET revoked_at=? WHERE id='WREVOKED'",
                  (stamp,), event="test")
    server = build_server(policy, Audit(tmp_path / "audit.jsonl"), fleet_db=db)
    yield server, store, result.directory, outside, missing, envelope
    store.close()


def test_inventory_no_copy_and_external_link_hints(rig):
    _, store, project, outside, missing, _ = rig
    report = (project / ".intent" / "inputs.md").read_text(encoding="utf-8")
    assert "notes with spaces.txt" in report
    assert "папка" in report
    assert "не найден" in report and missing.as_posix() in report
    assert "Google Sheets — коннектор core" in report
    assert "Telegram — браузер или коннектор" in report
    assert "Сайт — браузер или свой код" in report
    assert not (project / "notes with spaces.txt").exists()
    assert not (project / "Downloads").exists()
    with store.read() as tx:
        grants = tx.query("SELECT path FROM intent_input_grants WHERE project='intended'")
    assert len(grants) == 2, grants
    assert all(str(missing) not in r["path"] for r in grants)


def test_read_only_scoped_to_active_intent_claim_and_secret_denied(rig):
    server, _, project, outside, _, _ = rig
    read = tool(server, "read_file")
    target = str(outside / "notes with spaces.txt")
    assert "referenced confidential draft" in read(target, agent="WINT")
    for agent in ("WOTHER", "WREVOKED", ""):
        response = read(target, agent=agent)
        assert response.startswith("Отказано"), (agent, response)
        assert "confidential" not in response
    assert "unreferenced-private" not in read(str(outside / "other.txt"), agent="WINT")
    assert read(str(outside / "other.txt"), agent="WINT").startswith("Отказано")
    assert "TOPSECRET" not in read(str(outside / ".env"), agent="WINT")
    assert read(str(outside / "folder" / "secret.password"),
                agent="WINT").startswith("Отказано")
    assert "inside-folder" in read(str(outside / "folder" / "allowed.md"), agent="WINT")
    assert tool(server, "write_file")(target, "overwrite", True, "WINT").startswith("Отказано")
    assert (outside / "notes with spaces.txt").read_text() == "referenced confidential draft"

    # Agent-writable informational Markdown cannot escalate durable grants.
    (project / ".intent" / "inputs.md").write_text(str(outside / "other.txt"))
    assert read(str(outside / "other.txt"), agent="WINT").startswith("Отказано")


def test_missing_path_stays_closed_and_search_respects_mask(rig):
    server, _, _, outside, missing, _ = rig
    missing.write_text("appeared later", encoding="utf-8")
    assert tool(server, "read_file")(str(missing), agent="WINT").startswith("Отказано")
    search = tool(server, "search_files")
    out = search("inside-folder", path=str(outside / "folder"), agent="WINT")
    assert "allowed.md" in out
    out = search("PASSWORD", path=str(outside / "folder"), agent="WINT")
    assert "secret.password" not in out


def test_external_directory_listing_cannot_glob_outside_grant(rig):
    server, _, _, outside, _, _ = rig
    listing = tool(server, "list_dir")
    folder = str(outside / "folder")
    assert "allowed.md" in listing(folder, pattern="*.md", agent="WINT")
    for pattern in ("../other.txt", "..\\other.txt", "../notes with spaces.txt"):
        result = listing(folder, pattern=pattern, agent="WINT")
        assert result.startswith("Отказано"), (pattern, result)
        assert "other.txt" not in result and "notes with spaces.txt" not in result


def test_external_directory_listing_cannot_follow_symlink_to_sibling(rig):
    server, _, _, outside, _, _ = rig
    folder = outside / "folder"
    alias = folder / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    listing = tool(server, "list_dir")
    result = listing(str(folder), pattern="alias/*.txt", agent="WINT")
    assert "other.txt" not in result
    assert "notes with spaces.txt" not in result


def test_regenerating_intent_replaces_not_accumulates_grants(rig):
    _, store, project, outside, _, env = rig
    other = outside / "other.txt"
    next_env = IntentEnvelope(
        env.source_id, env.external_id, "r2",
        f"Новая ссылка: {other.as_posix()}", env.authority, env.timestamp,
    )
    spawn_project(store, next_env, parent=project.parent, project_name="intended",
                  size="small", allowed_roots=[project.parent])
    with store.read() as tx:
        grants = tx.query("SELECT path FROM intent_input_grants WHERE project='intended'")
    assert len(grants) == 1 and Path(grants[0]["path"]).name == "other.txt"


def test_path_parser_does_not_treat_links_as_local_paths():
    text = ('https://example.org/a/b and "C:\\Users\\dev\\Downloads\\My File.txt" '
            'plus /tmp/notes.csv')
    paths = _paths(text)
    assert len(paths) == 2, paths
    assert paths[0].endswith("My File.txt")
    assert paths[1] == "/tmp/notes.csv"


def test_unquoted_spaced_directory_is_discovered(tmp_path):
    source = tmp_path / "Моя база знаний"
    source.mkdir()
    text = f"Читай папку {source.as_posix()} для анализа"
    assert _paths(text) == (source.as_posix(),)
