"""MCP endpoint for the transcript-pressure handoff, executed with FastMCP test server."""
from __future__ import annotations

import json
from pathlib import Path

from dev_mcp.fleet import quick, chat_handoff, guard
from test_tools import _make_server, _tool_fn

OLD = "https://chatgpt.com/c/6af9c383-1111-2222-3333-111111111111"


def test_mcp_handoff_preserves_quick_agent_and_deduplicates(tmp_path: Path):
    server, store, _, _ = _make_server(tmp_path)
    book = quick.Book(Path(store.path).parent)
    job = quick.create(book, owner="claude", prompt="check bug", project="dev-mcp",
                       scope=[str(tmp_path)])
    with store.tx() as tx:
        tx.change("""INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason)
                     VALUES (?,?,?,datetime('now'),datetime('now'),NULL,'')""",
                  (job["tag"], f"pult-{job['id']}", "quick"),
                  event={"kind":"test_quick_registered", "worker_id":job["tag"]})
    quick.report(book, {"id": job["id"], "url": OLD, "sent": True})
    fn = _tool_fn(server, "fleet_chat_handoff")
    req = json.loads(fn(job["tag"], "observe", OLD, 0.85, 170000, False, "", "", "", ""))
    assert req["action"] == "request"
    assert json.loads(fn(job["tag"], "observe", OLD, 0.9, 180000,
                         False, "", "", "", ""))["token"] == req["token"]
    saved = json.loads(fn(job["tag"], "save", OLD, 0, 0, False, "",
                          "Сохранить полную сессию: " + "контекст " * 50, "", ""))
    assert Path(saved["file"]).is_file()
    ready = json.loads(fn(job["tag"], "prepare", OLD, 0, 0, False,
                          req["token"], "", "", ""))
    assert ready["action"] == "prepared"
    assert ready["new_agent"] == job["tag"] + ".2"
    assert quick.get(book, job["tag"])["tag"] == ready["new_agent"]
    assert quick.get(book, job["tag"])["chat_handoff"]["old_url"] == OLD
    target = tmp_path / "workspace" / "rotated-write.txt"
    assert guard.check_write(store, ready["new_agent"], str(target)).allow
    quick_report = _tool_fn(server, "fleet_quick_report")
    successor = "https://chatgpt.com/c/7af9c383-1111-2222-3333-222222222222"
    assert json.loads(quick_report(json.dumps({"id":job["id"],"url":successor, "sent":True})))["ok"]
    assert guard.check_write(store, job["tag"], str(target)).code == "FLEET_REVOKED"
    assert guard.check_write(store, ready["new_agent"], str(target)).allow
    assert guard.check_exec(store, job["tag"], str(target.parent)).code == "FLEET_REVOKED"
    assert guard.check_exec(store, ready["new_agent"], str(target.parent)).allow
    # Exercise the actual write_file MCP boundary, not only the guard helper.
    from dev_mcp.audit import Audit
    from dev_mcp.policy import Policy
    from dev_mcp.server import build_server
    config = tmp_path / "policy.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "allow_write = false", "allow_write = true"), encoding="utf-8")
    writable = build_server(Policy.load(config), Audit(tmp_path / "logs" / "audit.jsonl"))
    write = _tool_fn(writable, "write_file")
    denied = write(path=str(target), content="stale", agent=job["tag"])
    assert "FLEET_REVOKED" in denied and not target.exists(), denied
    allowed = write(path=str(target), content="successor", agent=ready["new_agent"])
    assert "создан" in allowed and target.read_text(encoding="utf-8").strip() == "successor", allowed
    with store.read() as tx:
        old = tx.one("SELECT revoked_at FROM workers WHERE id=?", (job["tag"],))
        newer = tx.one("SELECT revoked_at FROM workers WHERE id=?", (ready["new_agent"],))
    assert old["revoked_at"] is not None and newer["revoked_at"] is None
    # Browser can repeat its activation acknowledgement after an MV3 restart.
    assert json.loads(quick_report(json.dumps({"id":job["id"],"url":successor})))["ok"]
    assert len(book.jobs()) == 1


def test_rotated_worker_claim_migrates_atomically(tmp_path: Path):
    from test_tools import _seed_task
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    assert "worker_id" in _tool_fn(server, "fleet_join")(label="handoff-test")
    assert "TZ-X" in _tool_fn(server, "fleet_take")(agent="W1")
    source = "W1"
    successor = chat_handoff.rotated_tag(source)
    owned = str(root / "owned.txt")
    assert guard.check_write(store, source, owned).allow
    chat_handoff.register_successor(store, source, successor)
    chat_handoff.transfer_rights(store, source, successor)
    chat_handoff.transfer_rights(store, source, successor)
    assert guard.check_write(store, source, owned).code == "FLEET_REVOKED"
    assert guard.check_write(store, successor, owned).allow
    assert guard.check_exec(store, source, str(root)).code == "FLEET_REVOKED"
    assert guard.check_exec(store, successor, str(root)).allow
    with store.read() as tx:
        assert tx.one("SELECT worker_id FROM claims WHERE state='active' LIMIT 1")["worker_id"] == successor
