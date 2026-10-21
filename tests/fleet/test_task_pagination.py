"""Synthetic, read-only, lossless fleet task-body pagination regression.

This is deliberately a facade test: no live fleet SQLite or real review text.
The 26,637-character fixture includes twenty unique review-note sentinels.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.audit import Audit
from dev_mcp.fleet.store import Store
from dev_mcp.policy import Policy
from dev_mcp.server import build_server


PAGE_RESPONSE_LIMIT = 12_000  # MCP envelope has an approximately 13K cap.


def _body() -> str:
    notes = [
        f"\n[REVIEW-NOTE-{i:02d}] Synthetic observation {i**3} — проверено; уникальная заметка.\n"
        for i in range(1, 21)
    ]
    remaining = 26_637 - sum(map(len, notes))
    assert remaining > 0
    quotient, remainder = divmod(remaining, len(notes))
    result = []
    for i, note in enumerate(notes):
        width = quotient + (i < remainder)
        # Distinct but deterministic filler, to expose dropped or duplicated
        # page boundaries instead of accidentally matching repeated bytes.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        shifted = alphabet[i % len(alphabet):] + alphabet[:i % len(alphabet)]
        result.extend(((shifted * (width // len(shifted) + 1))[:width], note))
    body = "".join(result)
    assert len(body) == 26_637
    assert len({f"REVIEW-NOTE-{i:02d}" for i in range(1, 21)}) == 20
    for i in range(1, 21):
        assert body.count(f"[REVIEW-NOTE-{i:02d}]") == 1
    return body


def _db_dump(path: Path) -> tuple[str, ...]:
    # Connection must never create the DB under test.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        return tuple(db.iterdump())


def _tool_fn(server, name: str):
    tool = asyncio.run(server.get_tool(name))
    assert tool is not None, f"official task body page tool {name} is not registered"
    if asyncio.iscoroutinefunction(tool.fn):
        return lambda **kwargs: asyncio.run(tool.fn(**kwargs))
    return tool.fn


def _page_body(response: str, remaining: str) -> str:
    """Read a JSON content field or strip a human-readable page envelope.

    The reassembled bytes are checked against the source.  We never normalize
    line endings or whitespace in the content itself.
    """
    try:
        packet = json.loads(response)
    except (ValueError, TypeError):
        packet = None
    if isinstance(packet, dict):
        for key in ("body", "chunk", "content", "text"):
            value = packet.get(key)
            if isinstance(value, str):
                return value
    # If the API presents the page with a heading/footer, locate the longest
    # exact, contiguous prefix of the expected remaining body within it.
    signature = remaining[: min(24, len(remaining))]
    best = ""
    offset = response.find(signature)
    while offset != -1:
        length = 0
        while (offset + length < len(response) and length < len(remaining)
               and response[offset + length] == remaining[length]):
            length += 1
        if length > len(best):
            best = response[offset: offset + length]
        offset = response.find(signature, offset + 1)
    return best


def test_task_body_pagination_is_lossless_bounded_validated_and_read_only(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{root.as_posix()}']\n"
        f"write_roots = ['{root.as_posix()}']\n"
        f"exec_roots = ['{root.as_posix()}']\n"
        "exec_allowed_bin = ['python']\n"
        "allow_write = false\n",
        encoding="utf-8",
    )
    server = build_server(Policy.load(config), Audit(tmp_path / "logs" / "audit.jsonl"))
    db_path = tmp_path / "data" / "fleet" / "fleet.sqlite"
    store = Store.open(str(db_path))
    body = _body()
    now = datetime.now(timezone.utc).isoformat()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Beta App", str(root)), event="seed")
        tx.change("""INSERT INTO tasks
            (project,tz,title,state,priority,kind,spec_path,spec_sha,
             needs,owns,resources,acceptance,author_worker,body,created_at,updated_at)
            VALUES(?,?,?,'ready',1,'triage','','','[]','[]','[]','[]',NULL,?,?,?)""",
                  ("Beta App", "triage:12", "Synthetic long reviewer body", body, now, now),
                  event="seed")
    baseline = _db_dump(db_path)
    page_tool = _tool_fn(server, "fleet_task_body")

    reconstructed = ""
    for page in range(1, 100):
        raw = page_tool(project="Beta App", tz="triage:12", page=page)
        assert isinstance(raw, str)
        assert len(raw.encode("utf-8")) <= PAGE_RESPONSE_LIMIT, (page, len(raw))
        part = _page_body(raw, body[len(reconstructed):])
        assert part, f"page {page} missing a contiguous body segment"
        reconstructed += part
        assert body.startswith(reconstructed), f"page {page} rewrote/reordered task body"
        if len(reconstructed) == len(body):
            break
    assert reconstructed.encode("utf-8") == body.encode("utf-8")
    for i in range(1, 21):
        assert reconstructed.count(f"[REVIEW-NOTE-{i:02d}]") == 1

    for query in (
        {"project": "Missing Project", "tz": "triage:12", "page": 1},
        {"project": "Beta App", "tz": "missing:task", "page": 1},
        {"project": "Beta App", "tz": "triage:12", "page": 0},
        {"project": "Beta App", "tz": "triage:12", "page": -1},
        {"project": "Beta App", "tz": "triage:12", "page": 9999},
    ):
        message = page_tool(**query)
        assert isinstance(message, str)
        assert len(message.encode("utf-8")) <= PAGE_RESPONSE_LIMIT
        assert ("ошиб" in message.lower() or "не найд" in message.lower()
                or "invalid" in message.lower() or "not found" in message.lower()
                or "out of range" in message.lower() or "недопуст" in message.lower()), query
        assert "REVIEW-NOTE-01" not in message

    assert _db_dump(db_path) == baseline, "read-only pagination mutated fleet.sqlite"
