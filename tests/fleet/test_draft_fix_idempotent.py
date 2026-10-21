"""The planner's fixed YAML header must admit a no-op independent review."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import queue
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.store import Store

REL = "ToDo/tz-drafts/TZ-401-upgrade-ledger.md"
GOOD = (
    "```yaml\n"
    "tz: TZ-401\nproject: P\n"
    "owns: [src/ledger.py]\nneeds: []\nacceptance: [py -3 -m pytest -q tests/ledger.py]\n"
    "```\n# Ledger\n"
).encode()
BAD = GOOD.replace(b"```yaml", b"---").replace(b"```\n# Ledger", b"---\n# Ledger")
REASON = "TZ-401: нет машиночитаемой YAML-шапки"
FINGERPRINT = hashlib.sha256(BAD + b"\0" + REASON.encode()).hexdigest()
FIX = "fix:draft:" + hashlib.sha256(REL.casefold().encode()).hexdigest()[:16]


@pytest.fixture
def fixture(tmp_path: Path):
    root = tmp_path / "P"
    path = root / REL
    path.parent.mkdir(parents=True)
    path.write_bytes(GOOD)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)",
                  (str(root),), event={"kind": "test_setup"})
        tx.change("INSERT INTO workers(id,kind,first_seen,last_seen) "
                  "VALUES('W1','chatgpt','2026-01-01','2026-01-01')",
                  (), event={"kind": "test_setup"})
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('P',?,'correct draft','taken',1,'fix',?,?,'[]',?,'[]','[]','','2026-01-01','2026-01-01')",
            (FIX, REL, FINGERPRINT, json.dumps([norm_path(str(path))])),
            event={"kind": "test_setup"},
        )
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P',?,'W1','active','2026-01-01')",
            (FIX,), event={"kind": "test_setup"},
        )
        claim_id = int(tx.scalar("SELECT MAX(id) FROM claims"))
        tx.change("INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
                  (claim_id, norm_path(str(path))), event={"kind": "test_setup"})
        tx.record_event(kind="draft_invalid", project="P", tz="TZ-401",
                        payload={"draft": REL, "reason": REASON, "fingerprint": FINGERPRINT})
        tx.record_event(kind="draft_fix_open", project="P", tz=FIX,
                        payload={"draft": REL, "fingerprint": FINGERPRINT})
    claim = {"id": claim_id, "project": "P", "tz": FIX, "started_at": "2026-01-01"}
    return store, root, path, claim


def _verdict(fixture, monkeypatch) -> str:
    store, _root, _path, claim = fixture
    monkeypatch.setattr(queue, "_zone_untouched", lambda *_: True)
    with store.tx() as tx:
        return queue._empty_submit_reason(store, tx, "W1", claim)


def test_already_fixed_draft_enters_review_without_fake_write(fixture, monkeypatch):
    assert _verdict(fixture, monkeypatch) == ""
    store, _root, _path, claim = fixture
    queue.submit(store, "W1", "bite: n/a — valid header was repaired before this claim")
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='write'") == 0
        assert tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz=?", (FIX,)) == "submitted"
        assert tx.scalar("SELECT state FROM claims WHERE id=?", (claim["id"],)) == "released"


@pytest.mark.parametrize("bad_bytes", [
    BAD,
    GOOD.replace(b"acceptance:", b"checked:"),
    GOOD.replace(b"```yaml", b"---").replace(b"```\n# Ledger", b"---\n# Ledger"),
    GOOD.replace(b"tz: TZ-401", b"tz: TZ-402"),
])
def test_not_fixed_is_still_rejected(fixture, monkeypatch, bad_bytes):
    fixture[2].write_bytes(bad_bytes)
    assert _verdict(fixture, monkeypatch).startswith("FLEET_EMPTY_SUBMIT")


def test_unproven_draft_and_unrelated_fix_rejected(fixture, monkeypatch):
    store, _root, _path, claim = fixture
    with store.tx() as tx:
        tx.change("DELETE FROM events WHERE kind='draft_invalid'",
                  (), event={"kind": "test_setup"})
    assert _verdict(fixture, monkeypatch).startswith("FLEET_EMPTY_SUBMIT")
    with store.tx() as tx:
        tx.record_event(kind="draft_invalid", project="P", tz="TZ-401",
                        payload={"draft": REL, "reason": REASON, "fingerprint": FINGERPRINT})
    # A different fix can never borrow the planner's generated evidence.
    with store.read() as tx:
        assert not queue._corrected_generated_draft_header_fix(
            store, tx, "W1", {**claim, "tz": "fix:TZ-1"},
            [norm_path(str(fixture[2]))],
        )


def test_isolated_uses_sealed_claim_not_invalid_live_root(fixture, monkeypatch, tmp_path):
    store, root, path, claim = fixture
    private = tmp_path / "claim"
    (private / "ToDo" / "tz-drafts").mkdir(parents=True)
    (private / REL).write_bytes(GOOD)
    path.write_bytes(BAD)  # source is stale; sealed claim is independently corrected
    with store.tx() as tx:
        tx.record_event(kind="claim_submission", worker_id="W1", project="P",
                        tz=FIX, payload={"claim_id": claim["id"]})
    from dev_mcp.fleet import claim_review_bite
    monkeypatch.setattr(
        claim_review_bite, "_submission",
        lambda *_: (SimpleNamespace(author="W1", tree_sha="a" * 40),
                     SimpleNamespace(mode="isolated", tree_path=private), {}),
    )
    def git_show(args, **kwargs):
        assert args[-1] == "a" * 40 + ":" + REL
        return subprocess.CompletedProcess(args, 0, stdout=GOOD)
    monkeypatch.setattr(queue.subprocess, "run", git_show)
    assert _verdict(fixture, monkeypatch) == ""
    monkeypatch.setattr(queue.subprocess, "run",
                        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=BAD))
    assert _verdict(fixture, monkeypatch).startswith("FLEET_EMPTY_SUBMIT")
