"""Operator-only lead obstacles must reach the manual ledger and SessionStart."""
import json
import sqlite3

from dev_mcp.fleet import obstacles
from dev_mcp.fleet.store import Store


def _event(store, project, kind, payload):
    with store.tx() as tx:
        tx.record_event(kind=kind, project=project, tz="TZ-LEAD", payload=payload)


def test_operator_classifier_explicit_and_legacy():
    assert obstacles.operator_required({"actor": "operator", "ask": "x"})
    assert not obstacles.operator_required({"actor": "owner", "ask": "оператор исправляет Oracle MCP-маршрут"})
    assert obstacles.operator_required({"ask": "Только авторизованный оператор должен интегрировать isolated clean main"})
    assert obstacles.operator_required({"option": "Интеграция 59 файлов в clean Git"})
    assert obstacles.operator_required({"ask": "Оператор исправляет Oracle MCP-маршрут"})
    assert not obstacles.operator_required({"ask": "Ждём ответ владельца о бюджете"})


def test_manual_backlog_sync_is_deduplicated_and_closes(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('P',?)",
                  (str(tmp_path),), event="seed")
    obstacle = {
        "id": "ob-37fc2650", "type": "external_fact", "actor": "operator",
        "ask": "Интеграция проекта", "option": "Поместить ветку в isolated clean main и записать receipt",
        "acceptance": "В clean main есть commit и receipt",
        "predicate": {"kind": "file_exists", "path": "receipt.json"},
        "obligations": ["O-1"],
    }
    _event(store, "P", obstacles.OPEN, obstacle)
    assert obstacles.cards(store, "P")[0]["id"] == obstacle["id"]
    assert "оператор" in "\n".join(obstacles.lines(store, "P"))
    obstacles.write_cards(store)
    assert obstacle["id"] not in (tmp_path / "owner-cards" / "p.md").read_text(encoding="utf-8")
    obstacles.sync_operator_backlog(store)
    obstacles.sync_operator_backlog(store)
    with store.read() as tx:
        rows = tx.query("SELECT payload FROM events WHERE kind='manual_platform_required'")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["source_project"] == "P"
    assert payload["id"].startswith("manual-platform:lead-obstacle:P:ob-37fc2650:")
    assert payload["acceptance"] == ["В clean main есть commit и receipt"]
    report = json.loads((tmp_path / "manual-platform-backlog.json").read_text(encoding="utf-8"))
    assert any(f["id"] == payload["id"] for f in report["fronts"])
    _event(store, "P", obstacles.CLOSED, {"id": obstacle["id"]})
    obstacles.sync_operator_backlog(store)
    obstacles.sync_operator_backlog(store)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_resolved'") == 1
    _event(store, "P", obstacles.OPEN, obstacle)
    obstacles.sync_operator_backlog(store)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 2


def test_session_start_operator_first_and_close_removes_it(tmp_path):
    import importlib.util
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("fleet_session_brief_test",
                                                  repo / "tools" / "fleet_session_brief.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,project TEXT,tz TEXT,kind TEXT,payload TEXT)")
    conn.execute("CREATE TABLE tasks(project TEXT,tz TEXT,state TEXT)")
    for project, oid, ask in [
        ("Nova VPN", "ob-37fc2650", "Оператор помещает изменения в isolated clean main"),
        ("samplesec", "ob-int", "Интеграция 59 файлов в clean Git"),
        ("local-library", "ob-oracle", "Оператор исправляет Oracle MCP-маршрут"),
    ]:
        conn.execute("INSERT INTO events(project,kind,payload) VALUES(?,?,?)",
                     (project, obstacles.OPEN,
                      json.dumps({"id": oid, "ask": ask, "option": ask})))
    lines = module._operator_obstacles(conn, None)
    assert len(lines) == 3
    assert lines[0].startswith("ОПЕРАТОР: Nova VPN/ob-37fc2650")
    assert "samplesec/ob-int" in lines[1]
    assert "local-library/ob-oracle" in lines[2]
    assert len(module._operator_obstacles(conn, "samplesec")) == 1
    conn.execute("INSERT INTO events(project,kind,payload) VALUES(?,?,?)",
                 ("Nova VPN", obstacles.CLOSED, '{"id":"ob-37fc2650"}'))
    assert len(module._operator_obstacles(conn, None)) == 2


def test_legacy_frozen_lead_block_routes_and_thaws(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES('sample-security-project',?)",
                  (str(tmp_path),), event="seed")
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,created_at,updated_at) "
            "VALUES('sample-security-project','TZ-LEAD','Commander','frozen','2026-11-15','2026-11-15')",
            (), event="seed")
        tx.record_event(kind="block", project="sample-security-project", tz="TZ-LEAD",
                        payload={"reason": "Оператор интегрирует 59 файлов в clean Git; "
                                              "после независимого SHA возобновить TZ-LEAD"})
    with store.read() as tx:
        blocks = obstacles.operator_lead_blocks(tx)
    assert len(blocks) == 1 and "59 файлов" in blocks[0]["action"]
    obstacles.sync_operator_backlog(store)
    obstacles.sync_operator_backlog(store)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_required'") == 1
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE project='sample-security-project' AND tz='TZ-LEAD'",
                  (), event="test_thaw")
    obstacles.sync_operator_backlog(store)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='manual_platform_resolved'") == 1
