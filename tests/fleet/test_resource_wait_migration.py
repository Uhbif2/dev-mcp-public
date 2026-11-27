"""Safety gate for one-time resource-wait migration."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from migrate_resource_wait import candidates


def test_migration_only_selects_sensor_only_blocks() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE tasks(project TEXT, tz TEXT, state TEXT, resources TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY, project TEXT, tz TEXT,
                            kind TEXT, payload TEXT);
    """)
    cases = [
        ("eligible", "block", {"reason": "Physical phone resource became unavailable"}),
        ("owner", "freeze", {"reason": "нет ресурса «телефон»", "origin": "owner"}),
        ("operator", "block", {"reason": "нет ресурса «телефон»",
                              "reactivate_policy": {"kind": "operator_condition"}}),
        ("gallery", "block", {"reason": "нет ресурса «телефон», Gallery QA не готова"}),
        ("no_phone", "block", {"reason": "Physical phone resource became unavailable"}),
    ]
    for index, (tz, kind, payload) in enumerate(cases, 1):
        resources = [] if tz == "no_phone" else ["телефон"]
        conn.execute("INSERT INTO tasks VALUES ('P',?,'frozen',?)",
                     (tz, json.dumps(resources, ensure_ascii=False)))
        conn.execute("INSERT INTO events VALUES (?,'P',?,?,?)",
                     (index, tz, kind, json.dumps(payload, ensure_ascii=False)))
    result = candidates(conn)
    assert [(project, tz) for _, project, tz, _, _ in result] == [("P", "eligible")]
    assert json.loads(result[0][4])["resource_wait"] == ["телефон"]
