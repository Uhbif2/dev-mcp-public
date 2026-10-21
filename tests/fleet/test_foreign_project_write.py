"""13.10: a task of one fleet project may not write inside another project's root."""
from pathlib import Path

from dev_mcp.fleet import guard
from dev_mcp.fleet.store import Store


def _store(tmp_path: Path) -> Store:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.changes([
            ("INSERT INTO projects(name,root,priority,active) VALUES('Hub',?,5,1)", (str(tmp_path / "hub"),)),
            ("INSERT INTO projects(name,root,priority,active) VALUES('Platform',?,5,1)", (str(tmp_path / "plat"),)),
            ("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES('W1','w','chatgpt','t','t')", ()),
            ("INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,resources,acceptance,body,created_at,updated_at) "
             "VALUES('Hub','TZ-1','t','taken',5,'work','s','x','[]',?,'[]','[]','','t','t')", ('["%s"]' % (tmp_path / "hub" / "a.py").as_posix(),)),
            ("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('Hub','TZ-1','W1','active','t')", ()),
        ], event="test")
    return store


def test_write_into_other_project_root_is_refused(tmp_path: Path) -> None:
    d = guard.check_write(_store(tmp_path), "W1", str(tmp_path / "plat" / "src" / "x.py"))
    assert not d.allow and d.code == "FLEET_FOREIGN_PROJECT"


def test_write_inside_own_project_or_outside_projects_is_allowed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert guard.check_write(store, "W1", str(tmp_path / "hub" / "other.py")).allow
    assert guard.check_write(store, "W1", str(tmp_path / "elsewhere" / "x.py")).allow
