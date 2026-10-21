from __future__ import annotations

import hashlib
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from dev_mcp.fleet.mode_spec import ModeSpecError, read_mode_spec
from dev_mcp.fleet.spec import SpecError, parse_spec


def spec(mode: str = "", stage: str = "") -> str:
    lines = ["tz: TZ-1", "owns: [file.py]"]
    if mode:
        lines.append(f"mode: {mode}")
    if stage:
        lines.append(f"outcome_stage: {stage}")
    return "```yaml\n" + "\n".join(lines) + "\n```\n\n# Example\n"


@pytest.mark.parametrize("mode", ["default", "thinking", "deep-research"])
def test_safe_header_exact_mode_and_evidence_version(mode: str) -> None:
    text = spec(mode)
    directive = read_mode_spec(text)
    assert directive.requested == mode
    assert directive.contract_version == "fleet-chat-mode/v1"
    assert directive.spec_sha == hashlib.sha256(
        ("tz: TZ-1\nowns: [file.py]\nmode: " + mode).encode()
    ).hexdigest()


def test_missing_mode_is_backward_compatible() -> None:
    assert read_mode_spec(spec()).requested == "default"


def test_only_poniato_prefers_proven_research() -> None:
    requested = read_mode_spec(spec("default", "понято"))
    assert requested.effective({"default", "deep-research"}) == "deep-research"
    assert requested.effective({"default"}) == "default"
    for stage in ["сделано", "проверено", "поставлено"]:
        assert read_mode_spec(spec("default", stage)).effective({"deep-research"}) == "default"
    assert read_mode_spec(spec("thinking", "понято")).effective({"deep-research"}) == "thinking"


@pytest.mark.parametrize("raw", [
    "super-model", "THINKING", "deep_research", "null", "true", "42",
    '"thinking;alert(1)"', "'(()=>{location.href=\"evil\"})()'",
    "[default]", "{script: alert(1)}",
])
def test_hostile_explicit_modes_fail_closed(raw: str, tmp_path: Path) -> None:
    text = spec(raw)
    with pytest.raises(ModeSpecError):
        read_mode_spec(text)
    source = tmp_path / "ToDo" / "tz" / "TZ-1.md"
    source.parent.mkdir(parents=True)
    source.write_text(text, encoding="utf-8")
    with pytest.raises(SpecError):
        parse_spec(str(source.resolve()))


@pytest.mark.parametrize("text", [
    "# displaced\n" + spec("thinking"),
    "```yaml\nmode: [default\n```\n",
    "```yaml\n- mode\n```\n",
    "```yaml\nmode: !!python/object/apply:os.system ['echo pwn']\n```\n",
    "mode: default",
])
def test_malformed_headers_never_issue_a_mode(text: str) -> None:
    with pytest.raises(ModeSpecError):
        read_mode_spec(text)


def test_actual_readonly_task_api_binds_mode_to_server_spec_sha(tmp_path: Path) -> None:
    from dev_mcp.audit import Audit
    from dev_mcp.fleet.store import Store
    from dev_mcp.policy import Policy
    from dev_mcp.server import build_server

    project = tmp_path / "project"
    path = project / "ToDo" / "tz" / "TZ-1.md"
    path.parent.mkdir(parents=True)
    path.write_text(spec("thinking", "понято"), encoding="utf-8")
    config = tmp_path / "policy.toml"
    config.write_text(
        "roots = ['" + project.as_posix() + "']\n"
        "write_roots = ['" + project.as_posix() + "']\n"
        "exec_roots = ['" + project.as_posix() + "']\n"
        "exec_allowed_bin = ['python']\n"
        "allow_write = false\n", encoding="utf-8",
    )
    server = build_server(Policy.load(config), Audit(tmp_path / "logs" / "audit.jsonl"))
    store = Store.open(str(tmp_path / "data" / "fleet" / "fleet.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    sha = read_mode_spec(path.read_text(encoding="utf-8")).spec_sha
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Fixture", str(project)), event="seed")
        tx.change(
            """INSERT INTO tasks
            (project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,
             resources,acceptance,author_worker,body,created_at,updated_at)
             VALUES(?,?,?,'ready',1,'work',?,?,'[]','[]','[]','[]',NULL,?,?,?)""",
            ("Fixture", "TZ-1", "fixture", "ToDo/tz/TZ-1.md", sha,
             "immutable task body", now, now), event="seed",
        )
    tool = asyncio.run(server.get_tool("fleet_task_body"))
    assert tool is not None
    before = json.loads(tool.fn(project="Fixture", tz="TZ-1", page=1))
    assert before["body"] == "immutable task body"
    assert (before["mode_status"], before["mode"], before["mode_explicit"]) == (
        "verified", "thinking", True,
    )
    assert before["mode_spec_sha"] == sha
    assert before["outcome_stage"] == "понято"
    assert before["mode_contract"] == "fleet-chat-mode/v1"
    path.write_text(spec("deep-research"), encoding="utf-8")
    after = json.loads(tool.fn(project="Fixture", tz="TZ-1", page=1))
    assert after["mode_status"] == "stale"
    assert "mode" not in after

    # Generated reports point to source files without claiming an authored spec.
    with store.tx() as tx:
        tx.change("UPDATE tasks SET kind='review',spec_sha='' WHERE project='Fixture' AND tz='TZ-1'",event='seed')
    generated=json.loads(tool.fn(project='Fixture',tz='TZ-1',page=1))
    assert generated['mode_status']=='implicit-default' and 'mode' not in generated
    with store.tx() as tx:
        tx.change("UPDATE tasks SET kind='work' WHERE project='Fixture' AND tz='TZ-1'",event='seed')
    assert json.loads(tool.fn(project='Fixture',tz='TZ-1',page=1))['mode_status']=='unavailable'
