"""Runtime acceptance activation via actual registered spec entry point."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dev_mcp.fleet.spec import sync_backlog
from dev_mcp.fleet.store import Store


def _spec(root: Path, command: str) -> Path:
    path = root / "ToDo" / "tz" / "TZ-1-check.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "'''yaml\n".replace("'''", "```") +
        "tz: TZ-1\nowns: [src/sample.py]\nacceptance: [" +
        json.dumps(command) + "]\n```\n\n# Runtime candidate\n",
        encoding="utf-8",
    )
    return path


def _existing(store: Store) -> tuple[str, int, int]:
    with store.read() as tx:
        row = tx.one("SELECT acceptance FROM tasks WHERE project='P' AND tz='TZ-1'")
        failures = int(tx.scalar("SELECT COUNT(*) FROM events WHERE kind='exec_preflight_reject'") or 0)
        requests = int(tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='request'") or 0)
    assert row is not None
    return json.loads(row["acceptance"])[0], failures, requests


def _store(path: Path, root: Path) -> Store:
    store = Store.open(str(path))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('P',?,1)",
                  (str(root),), event="seed")
    return store


def test_backlog_never_replaces_registered_acceptance_without_owner_revision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "sample.py").write_text("result = 1\n", encoding="utf-8")
    old = f'\"{Path(sys.executable).as_posix()}\" -c \"print(1)\"'
    new = f'\"{Path(sys.executable).as_posix()}\" -c \"print(2)\"'
    _spec(root, old)
    store = _store(tmp_path / "fleet.sqlite", root)
    try:
        initial = sync_backlog(store, "P", str(root))
        assert initial["inserted"] == 1, initial

        _spec(root, "definitely-absent-acceptance-binary-xyz")
        scanned = sync_backlog(store, "P", str(root))
        assert scanned["updated"] == 1 and not scanned["errors"], scanned
        assert scanned["warnings"], "changed YAML must not silently replace registration"
        assert _existing(store) == (old, 0, 0)
        # Repeated rescans keep the original contract even if the file remains
        # invalid; unlike new registration, no runtime preflight or auto-request.
        sync_backlog(store, "P", str(root))
        assert _existing(store) == (old, 0, 0)

        _spec(root, new)
        rescanned = sync_backlog(store, "P", str(root))
        assert rescanned["updated"] == 1 and not rescanned["errors"], rescanned
        assert _existing(store) == (old, 0, 0)

        # Only explicit owner set-acceptance has authorization and old/new
        # provenance; the scanner must preserve this new registered revision.
        with store.tx() as tx:
            tx.change(
                "UPDATE tasks SET acceptance=? WHERE project='P' AND tz='TZ-1'",
                (json.dumps([new]),),
                event={"kind": "acceptance_update", "project": "P", "tz": "TZ-1",
                       "payload": {"old": [old], "new": [new]}},
            )
        sync_backlog(store, "P", str(root))
        assert _existing(store) == (new, 0, 0)
        with store.read() as tx:
            audit = tx.one("SELECT payload FROM events WHERE kind='acceptance_update'")
        assert json.loads(audit["payload"]) == {"old": [old], "new": [new]}
    finally:
        store.close()


def test_backlog_rejects_duplicate_pytest_module_collection_on_registration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "sample.py").write_text("result = 1\n", encoding="utf-8")
    old = f'\"{Path(sys.executable).as_posix()}\" -c \"print(1)\"'
    for directory in ("a", "b"):
        target = root / directory
        target.mkdir()
        (target / "test_same.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    _spec(root, old)
    store = _store(tmp_path / "fleet.sqlite", root)
    try:
        result = sync_backlog(store, "P", str(root))
        assert result["errors"] and result["inserted"] == 0
        assert "pytest_collection_failed" in next(iter(result["errors"].values()))
        with store.read() as tx:
            assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='P' AND tz='TZ-1'") == 0
    finally:
        store.close()


def test_registration_ignores_unrelated_collection_import_errors(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "sample.py").write_text("result = 1\n", encoding="utf-8")
    (root / "backend").mkdir()
    (root / "backend" / "test_api.py").write_text("import not_installed_pkg\n", encoding="utf-8")
    _spec(root, f'\"{Path(sys.executable).as_posix()}\" -c \"print(547)\"')
    store = _store(tmp_path / "fleet.sqlite", root)
    try:
        result = sync_backlog(store, "P", str(root))
        assert not result["errors"] and result["inserted"] == 1
    finally:
        store.close()
