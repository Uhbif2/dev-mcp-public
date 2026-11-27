from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.fleet.guard import check_exec, check_write  # noqa: E402
from dev_mcp.fleet.model import Decision, norm_path  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402
from dev_mcp.policy import Policy, PolicyError  # noqa: E402


_NOW = "2026-09-18T00:00:00+00:00"


def _store(tmp_path: Path) -> Store:
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    assert store.healthy()
    return store


def _seed_claim(
    store: Store,
    *,
    root: Path,
    worker_id: str,
    tz: str,
    owned: Path,
    label: str = "",
    revoked_reason: str = "",
) -> int:
    revoked_at = _NOW if revoked_reason else None
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO projects(name, root, priority, active) VALUES (?, ?, 1, 1)",
            ("dev-mcp", str(root)),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO tasks(
                project, tz, title, state, priority, kind, spec_path, spec_sha,
                needs, owns, resources, acceptance, author_worker, body,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'taken', 1, 'work', '', '', '[]', '[]', '[]', '[]',
                      NULL, '', ?, ?)
            """,
            ("dev-mcp", tz, tz, _NOW, _NOW),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO workers(
                id, label, kind, first_seen, last_seen, revoked_at, revoke_reason
            ) VALUES (?, ?, 'chatgpt', ?, ?, ?, ?)
            """,
            (worker_id, label, _NOW, _NOW, revoked_at, revoked_reason),
        )
        cur = conn.execute(
            """
            INSERT INTO claims(project, tz, worker_id, state, started_at, released_at)
            VALUES ('dev-mcp', ?, ?, 'active', ?, NULL)
            """,
            (tz, worker_id, _NOW),
        )
        claim_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO claim_paths(claim_id, path_norm) VALUES (?, ?)",
            (claim_id, norm_path(str(owned))),
        )
        conn.execute(
            """
            INSERT INTO events(ts, kind, worker_id, project, tz, tool, path_norm, rc, payload)
            VALUES (?, 'take', ?, 'dev-mcp', ?, '', '', NULL, '{}')
            """,
            (_NOW, worker_id, tz),
        )
        conn.commit()
    return claim_id


def test_degraded_store_allows_write() -> None:
    class DownStore:
        def healthy(self) -> bool:
            return False

    decision = check_write(DownStore(), "W1", r"C:\work\file.txt")
    assert decision.allow is True
    assert decision.degraded is True
    assert decision.worker_id == "W1"


def test_revoked_worker_wins_even_on_own_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "owned.py"
    _seed_claim(
        store,
        root=tmp_path,
        worker_id="W1",
        tz="TZ-03",
        owned=target,
        revoked_reason="остановлен владельцем",
    )

    decision = check_write(store, "W1", str(target))

    assert decision.allow is False
    assert decision.code == "FLEET_REVOKED"
    assert "остановлен владельцем" in decision.reason
    assert "request_note" in decision.reason


def test_foreign_path_is_denied_with_holder_and_repair(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "foreign.py"
    _seed_claim(
        store,
        root=tmp_path,
        worker_id="W2",
        tz="TZ-42",
        owned=target,
        label="сосед",
    )

    decision = check_write(store, "W1", str(target))

    assert decision.allow is False
    assert decision.code == "FLEET_FOREIGN_PATH"
    assert "W2 (сосед)" in decision.reason
    assert "TZ-42" in decision.reason
    assert "fleet_join" in decision.reason


def test_single_matching_claim_infers_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "owned.py"
    _seed_claim(
        store,
        root=tmp_path,
        worker_id="W7",
        tz="TZ-07",
        owned=target,
    )

    decision = check_write(store, None, str(target))

    assert decision.allow is True
    assert decision.worker_id == "W7"
    assert decision.degraded is False
    assert "inferred" in decision.reason


def test_multiple_matching_claims_without_worker_are_denied(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "shared.py"
    _seed_claim(store, root=tmp_path, worker_id="W1", tz="TZ-01", owned=target)
    _seed_claim(store, root=tmp_path, worker_id="W2", tz="TZ-02", owned=target)

    decision = check_write(store, None, str(target))

    assert decision.allow is False
    assert decision.code == "FLEET_NO_WORKER"
    assert "W1" in decision.reason
    assert "W2" in decision.reason
    assert "fleet_join" in decision.reason


def test_out_of_claim_is_warning_not_block(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_claim(
        store,
        root=tmp_path,
        worker_id="W1",
        tz="TZ-03",
        owned=tmp_path / "owned.py",
    )

    decision = check_write(store, "W1", str(tmp_path / "scratch.txt"))

    assert decision.allow is True
    assert decision.code == "FLEET_OUT_OF_CLAIM"
    assert decision.worker_id == "W1"
    assert "fleet_take" in decision.reason


def test_exec_uses_same_revocation_guard(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cwd = tmp_path / "work"
    cwd.mkdir()
    _seed_claim(
        store,
        root=tmp_path,
        worker_id="W9",
        tz="TZ-09",
        owned=cwd,
        revoked_reason="kill switch",
    )

    decision = check_exec(store, "W9", str(cwd))

    assert decision.allow is False
    assert decision.code == "FLEET_REVOKED"


def test_exec_from_project_root_is_not_blocked_by_neighbour_claim(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_claim(
        store,
        root=tmp_path,
        worker_id="W2",
        tz="TZ-02",
        owned=tmp_path / "src" / "neighbour.py",
    )

    decision = check_exec(store, "W1", str(tmp_path))

    assert decision.allow is True
    assert decision.code == ""
    assert decision.worker_id == "W1"


def test_cached_snapshot_does_not_repeat_store_health_probe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "claimed.py"
    _seed_claim(
        store,
        root=tmp_path,
        worker_id="W1",
        tz="TZ-03",
        owned=target,
    )
    original_healthy = store.healthy
    calls = 0

    def counted_healthy() -> bool:
        nonlocal calls
        calls += 1
        return original_healthy()

    store.healthy = counted_healthy  # type: ignore[method-assign]

    assert check_write(store, "W1", str(target)).allow is True
    assert check_write(store, "W1", str(target)).allow is True
    assert calls == 1


def test_snapshot_invalidates_when_events_id_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "claimed.py"
    claim_id = _seed_claim(
        store,
        root=tmp_path,
        worker_id="W2",
        tz="TZ-02",
        owned=target,
    )

    first = check_write(store, "W1", str(target))
    assert first.code == "FLEET_FOREIGN_PATH"

    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE claims SET state='released', released_at=? WHERE id=?",
            (_NOW, claim_id),
        )
        conn.execute(
            """
            INSERT INTO events(ts, kind, worker_id, project, tz, tool, path_norm, rc, payload)
            VALUES (?, 'finish', 'W2', 'dev-mcp', 'TZ-02', '', '', NULL, '{}')
            """,
            (_NOW,),
        )
        conn.commit()

    second = check_write(store, "W1", str(target))
    assert second.allow is True
    assert second.code == "FLEET_OUT_OF_CLAIM"


def test_policy_resolve_without_hook_is_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    policy = Policy(roots=[root], write_roots=[root])
    target = root / "new.txt"

    assert policy.resolve(str(target), for_write=True) == target


def test_policy_hook_runs_after_perimeter_and_can_deny(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    policy = Policy(roots=[root], write_roots=[root])
    calls: list[str] = []

    def guard(path: str) -> Decision:
        calls.append(path)
        return Decision(False, "FLEET_FOREIGN_PATH", "held by W2", "W1", False)

    policy.set_write_guard(guard)

    with pytest.raises(PolicyError, match="вне разрешённых корней"):
        policy.resolve(str(outside), for_write=True)
    assert calls == []

    with pytest.raises(PolicyError, match="FLEET_FOREIGN_PATH: held by W2"):
        policy.resolve(str(root / "inside.txt"), for_write=True)
    assert calls == [str(root / "inside.txt")]


def test_policy_read_never_calls_write_guard(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    policy = Policy(roots=[root], write_roots=[root])
    calls: list[str] = []
    policy.set_write_guard(lambda path: calls.append(path) or Decision(False, "X", "no", None, False))

    assert policy.resolve(str(root), for_write=False) == root
    assert calls == []


def test_audit_always_has_agent_and_marks_inferred_degraded(tmp_path: Path) -> None:
    audit = Audit(tmp_path / "audit.jsonl")
    audit.record("read_file", {"path": "x"}, decision="allow")
    audit.record(
        "write_file",
        {"path": "y"},
        decision="allow",
        agent="W3",
        inferred=True,
        degraded=True,
    )

    rows = [
        json.loads(line)
        for line in audit.path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["agent"] == ""
    assert "inferred" not in rows[0]
    assert "degraded" not in rows[0]
    assert rows[1]["agent"] == "W3"
    assert rows[1]["inferred"] is True
    assert rows[1]["degraded"] is True


def test_degraded_decision_can_be_written_to_audit(tmp_path: Path) -> None:
    class DownStore:
        def healthy(self) -> bool:
            return False

    decision = check_write(DownStore(), "W4", str(tmp_path / "file.txt"))
    audit = Audit(tmp_path / "audit.jsonl")
    audit.record(
        "write_file",
        {"path": "file.txt"},
        decision="allow",
        agent=decision.worker_id or "",
        degraded=decision.degraded,
    )

    row = json.loads(audit.path.read_text(encoding="utf-8"))
    assert row["agent"] == "W4"
    assert row["degraded"] is True


def test_lead_zone_yields_to_specialized_claim(tmp_path: Path) -> None:
    # 03.11: TZ-LEAD держит client/ целиком, TZ-312 — client/win/field.
    store = _store(tmp_path)
    client = tmp_path / "client"
    field = client / "win" / "field"
    _seed_claim(store, root=tmp_path, worker_id="WL", tz="fix:TZ-LEAD", owned=client)
    _seed_claim(store, root=tmp_path, worker_id="WS", tz="TZ-312", owned=field)

    specialized = check_write(store, "WS", str(field / "recorder.py"))
    assert specialized.allow is True and specialized.code == ""

    lead_in_field = check_write(store, "WL", str(field / "recorder.py"))
    assert lead_in_field.allow is False and "профильной задачи" in lead_in_field.reason

    lead_elsewhere = check_write(store, "WL", str(client / "win" / "window.py"))
    assert lead_elsewhere.allow is True and lead_elsewhere.code == ""


def test_lead_book_is_reserved_for_the_lead(tmp_path: Path) -> None:
    # 05.11: LEDGER.json держали ревалидации — ведущий не мог исправить отклонённую книгу.
    store = _store(tmp_path)
    lead_dir = tmp_path / "ToDo" / "lead"
    _seed_claim(store, root=tmp_path, worker_id="WL", tz="fix:TZ-LEAD:7", owned=tmp_path / "ToDo")
    _seed_claim(store, root=tmp_path, worker_id="WR", tz="revalidate:abc", owned=lead_dir)

    lead_book = check_write(store, "WL", str(lead_dir / "LEDGER.json"))
    assert lead_book.allow is True and lead_book.code == ""

    other = check_write(store, "WR", str(lead_dir / "LEDGER.json"))
    assert other.allow is False and other.code == "FLEET_FOREIGN_PATH"


def test_independent_review_path_yields_to_its_claimant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lead_dir = tmp_path / "ToDo" / "lead"
    review = lead_dir / "reviews" / "s13-recovery"
    _seed_claim(store, root=tmp_path, worker_id="WL", tz="TZ-LEAD", owned=lead_dir)
    _seed_claim(store, root=tmp_path, worker_id="WR", tz="sub:LEAD-7", owned=review)

    assert check_write(store, "WR", str(review / "probe.py")).allow
    assert check_write(store, "WL", str(review / "probe.py")).code == "FLEET_FOREIGN_PATH"
    assert check_write(store, "WL", str(lead_dir / "LEDGER.json")).allow


def test_lead_cannot_write_instruments_but_worker_can(tmp_path: Path, monkeypatch) -> None:
    # 06.11 (LEAD-SYSTEM-DESIGN Р1, фаза 0.2): 150 правок свидетеля ведущим за ночь без продукта.
    from dev_mcp.fleet import goals, project_config
    store = _store(tmp_path)
    monkeypatch.setattr(project_config, "project_root", lambda p: str(tmp_path))
    monkeypatch.setattr(goals, "project_info", lambda p: {"witness": [
        {"id": "a", "cmd": ["py", "-3", "ops/pilot/android_witness.py"]}]})
    _seed_claim(store, root=tmp_path, worker_id="WL", tz="TZ-LEAD", owned=tmp_path)
    _seed_claim(store, root=tmp_path, worker_id="WS", tz="sub:LEAD-1", owned=tmp_path / "ops" / "witness")
    for rel in ("ops/witness/mcp_search_live.py", "ops/lead/ledger_check.py", "ops/pilot/android_witness.py"):
        decision = check_write(store, "WL", str(tmp_path / rel))
        assert decision.allow is False and decision.code == "FLEET_INSTRUMENT", rel
    assert check_write(store, "WL", str(tmp_path / "ops" / "lead" / "fleet.toml")).allow is True
    assert check_write(store, "WL", str(tmp_path / "client" / "app.py")).allow is True
    worker = check_write(store, "WS", str(tmp_path / "ops" / "witness" / "mcp_search_live.py"))
    assert worker.allow is True and worker.code == ""


def test_lead_cannot_write_owner_canon_or_fleet_service_files(tmp_path: Path) -> None:
    # 08.11 (LEAD-SYSTEM-DESIGN 3.1): зона ведущего — проект минус канон и служебный флот.
    store = _store(tmp_path)
    _seed_claim(store, root=tmp_path, worker_id="WL", tz="TZ-LEAD", owned=tmp_path)
    _seed_claim(store, root=tmp_path, worker_id="WT", tz="triage:1", owned=tmp_path / "ToDo" / "decisions")
    for rel in ("product/spec/A.md", "GOAL.md", "AGENTS.md", "CLAUDE.md", "intent/goal.md",
                "ToDo/tz/TZ-LEAD-x.md", "ToDo/fleet/results/r.md"):
        decision = check_write(store, "WL", str(tmp_path / rel))
        assert decision.allow is False and decision.code == "FLEET_LEAD_CANON", rel
    for rel in ("client/app.py", "ToDo/lead/STATE.md", "productivity/x.py", "docs/a.md"):
        assert check_write(store, "WL", str(tmp_path / rel)).allow is True, rel
    # Не ведущий — прежние правила.
    assert check_write(store, "WT", str(tmp_path / "ToDo" / "decisions" / "d.md")).allow is True
