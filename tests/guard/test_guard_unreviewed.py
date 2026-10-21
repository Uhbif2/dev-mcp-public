"""Ствол = только принятая работа: страж не выкатывает пути задач, не прошедших ревью."""
import json
import sqlite3

from guard import metrics


def _db(tmp_path, rows):
    db = tmp_path / "fleet.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (project TEXT, tz TEXT, state TEXT, owns TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def test_path_of_submitted_task_is_unreviewed(tmp_path):
    root = tmp_path / "work"
    own = str(root / "src" / "mod.py").replace("\\", "/").lower()
    db = _db(tmp_path, [("Fleet Platform", "TZ-10", "submitted", json.dumps([own]))])
    hits = metrics.unreviewed_owners(db, "Fleet Platform", root, ("src/mod.py", "src/other.py"))
    assert hits == ["src/mod.py <- TZ-10"]


def test_accepted_task_and_glob_owns(tmp_path):
    root = tmp_path / "work"
    base = str(root).replace("\\", "/").lower()
    db = _db(tmp_path, [
        ("Fleet Platform", "TZ-01", "accepted", json.dumps([base + "/src/a.py"])),
        ("Fleet Platform", "TZ-02", "taken", json.dumps([base + "/guard/"])),
        ("Fleet Platform", "review:TZ-03", "ready", json.dumps([base + "/src/b.py"])),
        ("Other", "TZ-04", "taken", json.dumps([base + "/src/c.py"])),
    ])
    hits = metrics.unreviewed_owners(db, "Fleet Platform", root, ("src/a.py", "guard/x.py", "src/b.py", "src/c.py"))
    assert hits == ["guard/x.py <- TZ-02"]


def test_missing_db_fails_closed(tmp_path):
    try:
        metrics.unreviewed_owners(tmp_path / "none.sqlite", "Fleet Platform", tmp_path, ("a.py",))
    except FileNotFoundError:
        return
    raise AssertionError("ожидался отказ")
