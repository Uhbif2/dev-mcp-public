"""Regression coverage for remaining JSON book producers.

Each listed producer delegates publication to dev_mcp.json_book.write_json.
The parametrized durability tests cover the *same* helper for every migrated
book path, without touching the running fleet, its DB, or external accounts.
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import pytest

from dev_mcp import json_book
from dev_mcp.json_book import read_json, write_json


ROOT = Path(__file__).resolve().parents[1]

# (writer module, persistent JSON book, call-site text). Two producers of
# the same book are represented separately where they update it differently.
BOOKS = (
    ("src/dev_mcp/fleet/arbiter.py", "data/fleet/arbiter-report.json", "write_json(REPORT"),
    ("src/dev_mcp/fleet/browser_storage.py", "receipt.json", "write_json(folder / 'receipt.json'"),
    ("src/dev_mcp/fleet/evidence.py", "snapshots/claim-1.json", "write_json(manifest_path"),
    ("src/dev_mcp/fleet/jev.py", "data/fleet/jev-spend.json", "write_json(path"),
    ("src/dev_mcp/fleet/rudiments.py", "retired/retired-date.json", "write_json(pre"),
    ("src/dev_mcp/intents/spawn.py", "component.json", "write_json(component"),
    ("src/dev_mcp/intents/stages.py", ".intent/options.json", "write_json(target / \"options.json\""),
    ("src/dev_mcp/owner/ticktick_board.py", "ticktick-board-status.json", "write_json(STATUS_PATH"),
    ("tools/egress_proxy.py", "egress-state.json", "write_json(args.state"),
    ("tools/fleet_night_snapshot.py", "night/project.json", "write_json(args.output / filename"),
    ("tools/fleet_night_snapshot.py", "night/index.json", 'write_json(args.output / "index.json"'),
    ("tools/fleet_night_snapshot.py", "night/global-errors.json", 'write_json(args.output / "global-errors.json"'),
    ("tools/fleet_session_brief.py", "brief-seen.json", "write_json(SEEN_FILE"),
    ("tools/operator_audit.py", "operator-audit.json", "write_json(args.output"),
    ("tools/run_public_tests.py", "pytest-shards.json", "write_json(out / 'pytest-shards.json'"),
    ("tools/standalone.py", "autostart.json", "write_json(manifest"),
    ("tools/standalone.py", "data/standalone-process.json", "write_json(root / 'data/standalone-process.json'"),
    ("tools/worker.py", "worker-unit.json", "write_json(path"),
)


@pytest.mark.parametrize("source,name,call", BOOKS)
def test_migrated_book_durable_under_serializer_and_stage_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    source: str, name: str, call: str,
) -> None:
    # Prevent regression to the original direct-truncation call at this site.
    assert call in (ROOT / source).read_text(encoding="utf-8")
    path = tmp_path / name
    old, new = {"generation": 0}, {"generation": 1}
    write_json(path, old)
    original = path.read_bytes()

    # json.dumps can fail after serializing a prefix of a nested value.
    with monkeypatch.context() as patch:
        original_dumps = json_book.json.dumps

        def broken(value, **kwargs):
            if value is new:
                original_dumps({"prefix": [1] * 100}, **kwargs)
                raise ValueError("synthetic serializer failure")
            return original_dumps(value, **kwargs)

        patch.setattr(json_book.json, "dumps", broken)
        with pytest.raises(ValueError, match="serializer failure"):
            write_json(path, new, attempts=1, delay=0)
    assert path.read_bytes() == original

    # Another failure after staging the complete payload, before publication,
    # must still leave the old book in place and clean up the staged file.
    with monkeypatch.context() as patch:
        patch.setattr(json_book.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk error")))
        with pytest.raises(OSError, match="disk error"):
            write_json(path, new, attempts=1, delay=0)
    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


@pytest.mark.parametrize("source,name,call", BOOKS)
def test_migrated_book_concurrent_reader_sees_complete_snapshots(
    tmp_path: Path, source: str, name: str, call: str,
) -> None:
    path = tmp_path / name
    old = {"generation": 0, "payload": "x" * 100_000}
    new = {"generation": 1, "payload": "y" * 100_000}
    write_json(path, old)
    started = threading.Event()
    failures: list[BaseException] = []
    seen: set[int] = set()

    def writer() -> None:
        started.set()
        try:
            write_json(path, new, attempts=40, delay=0.01)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert started.wait(2)
    deadline = time.monotonic() + 3
    while thread.is_alive() and time.monotonic() < deadline:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data in (old, new)
            seen.add(data["generation"])
        except PermissionError:
            # Windows: открыть файл в момент os.replace нельзя — это не полуфайл;
            # настоящие читатели повторяют (read_json attempts). Проверяем только целостность.
            pass
        except BaseException as exc:
            failures.append(exc)
            break
        time.sleep(0.002)  # реальный читатель не держит файл непрерывно
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert not failures
    assert seen.issubset({0, 1})
    assert read_json(path, strict=True, attempts=1) == new
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def _load_tool(name: str):
    path = ROOT / "tools" / name
    spec = importlib.util.spec_from_file_location("atomic_rest_" + name.replace(".", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jev_corrupt_spend_book_disables_budget_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dev_mcp.fleet import jev

    path = tmp_path / "jev-spend.json"
    path.write_text("{", encoding="utf-8")
    before = path.read_bytes()
    (tmp_path / "jev-openrouter.json").write_text('{"enabled":true,"daily_usd":1}')
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-only")
    with pytest.raises(RuntimeError, match="unreadable"):
        jev._record_spend(0.1, tmp_path)
    assert path.read_bytes() == before
    assert jev._openrouter_budget(tmp_path) == 0

    path.unlink()
    jev._record_spend(0.1, tmp_path)
    assert jev.spent_today(tmp_path) == 0.1


def test_worker_state_is_atomic_and_invalid_existing_state_is_not_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _load_tool("worker.py")
    monkeypatch.setattr(worker, "_state_dir", lambda: tmp_path)
    path = worker._write_state("example", "W-TEST", "chatgpt")
    assert worker._read_state(path)["worker_id"] == "W-TEST"
    path.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        worker._read_state(path)
    assert path.read_text() == ""


def test_brief_seen_corruption_suppresses_duplicate_and_preserves_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = _load_tool("fleet_session_brief.py")
    path = tmp_path / "brief-seen.json"
    monkeypatch.setattr(brief, "SEEN_FILE", path)
    assert brief._already_shown("p", "sig") is False
    assert brief._already_shown("p", "sig") is True
    path.write_text("{", encoding="utf-8")
    assert brief._already_shown("p", "other") is True
    assert path.read_text() == "{"


def test_ticktick_status_writer_preserves_existing_status_on_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dev_mcp.owner import ticktick_board

    path = tmp_path / "ticktick-board-status.json"
    monkeypatch.setattr(ticktick_board, "STATUS_PATH", path)
    ticktick_board._status({"state": "old"})
    assert read_json(path, strict=True)["state"] == "old"
    with monkeypatch.context() as patch:
        patch.setattr(json_book.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError()))
        ticktick_board._status({"state": "new"})
    assert read_json(path, strict=True)["state"] == "old"
