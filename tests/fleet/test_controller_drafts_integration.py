"""Real controller → draft registry wiring, using only temporary projects."""
from __future__ import annotations

from pathlib import Path

import pytest

from dev_mcp.fleet.controller import Controller, _draft_fixture_probe
from dev_mcp.fleet.store import Store


def test_background_preflight_does_not_hold_controller_heartbeat(tmp_path, monkeypatch):
    import threading
    from dev_mcp.fleet.controller import run_draft_registration_worker
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    def slow_registration(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'registered': [], 'invalid': {}, 'deferred': {}}
    monkeypatch.setattr('dev_mcp.fleet.drafts.register_drafts', slow_registration)
    worker = threading.Thread(target=run_draft_registration_worker,
                              args=(store, {}, lambda *_: 0, stop))
    worker.start()
    try:
        assert entered.wait(2)
        controller = Controller(store, policy={}, maintenance=lambda: None,
                                register_drafts_inline=False)
        assert controller.tick() == 0
        assert not release.is_set()  # the independent slow probe is still running
        with store.read() as tx:
            assert tx.one("SELECT id FROM events WHERE kind='controller_tick'")
    finally:
        stop.set()
        release.set()
        worker.join(timeout=5)
        store.close()


def _draft(root: Path) -> Path:
    path = root / "ToDo" / "tz-drafts" / "TZ-901-work.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "```yaml\n"
        "tz: TZ-901\n"
        "owns: [src/new_file.py]\n"
        "needs: []\n"
        # Exercise the actual executable preflight with the trusted Python
        # launcher instead of the unrelated PATH `python` without pytest.
        "acceptance: [py -3 -m pytest -q]\n"
        "```\n\n# Integration fixture\n",
        encoding="utf-8",
    )
    smoke = root / "tests" / "test_smoke.py"
    smoke.parent.mkdir(parents=True, exist_ok=True)
    smoke.write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    return path


def test_controller_tick_registers_draft_once_with_real_fixture_probe(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = _draft(root)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        with store.tx() as tx:
            tx.change("INSERT INTO projects(name,root,active) VALUES(?,?,1)",
                      ("Sandbox", str(root)), event="seed")
        policy = {"roots": [str(root)], "write_roots": [str(root)],
                  "allow_write": True}
        # A temporary Store has no right to execute a pytest subprocess by
        # default. The trusted fixture probe is deliberately injected here.
        controller = Controller(store, policy=policy, draft_runner=_draft_fixture_probe)

        assert controller.tick() == 1
        assert controller.tick() == 0
        with store.read() as tx:
            task = tx.one("SELECT state,spec_path FROM tasks "
                          "WHERE project='Sandbox' AND tz='TZ-901'")
            registered = tx.scalar("SELECT COUNT(*) FROM events "
                                   "WHERE project='Sandbox' AND tz='TZ-901' "
                                   "AND kind='draft_registered'")
            ticks = tx.scalar("SELECT COUNT(*) FROM events WHERE kind='controller_tick'")
        assert task is not None and task["state"] == "ready"
        assert task["spec_path"] == "ToDo/tz/TZ-901-work.md"
        assert registered == 1 and ticks == 2
        assert not source.exists()
        assert (root / "ToDo" / "tz" / source.name).is_file()
    finally:
        store.close()


@pytest.mark.parametrize("with_policy", [False, True])
def test_controller_without_trusted_draft_runner_does_not_touch_drafts(tmp_path, with_policy, monkeypatch):
    probe_calls = []

    def unexpected_probe(argv, fixture):
        probe_calls.append((argv, fixture))
        return 0

    # A policy alone must never silently promote the local fixture runner into
    # process-execution authority for a foreign Store, even if its probe passes.
    monkeypatch.setattr("dev_mcp.fleet.controller._draft_fixture_probe", unexpected_probe)
    root = tmp_path / "project"
    root.mkdir()
    source = _draft(root)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        with store.tx() as tx:
            tx.change("INSERT INTO projects(name,root,active) VALUES(?,?,1)",
                      ("Sandbox", str(root)), event="seed")
        # Even a permitted project root is insufficient to authorize running
        # task-provided pytest commands from an arbitrary test Store.
        policy = ({"roots": [str(root)], "write_roots": [str(root)],
                   "allow_write": True} if with_policy else None)
        assert Controller(store, policy=policy).tick() == 0
        assert probe_calls == [], "untrusted Store invoked fixture process runner"
        with store.read() as tx:
            assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz='TZ-901'") == 0
            assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='draft_registered'") == 0
        assert source.is_file()
    finally:
        store.close()


@pytest.mark.parametrize("argument", [
    "C:/owner/secret.py", "../escape.py", "--override-ini=addopts=-p evil",
    "-p", "--pyargs", "@response.txt", "//server/share/test.py",
])
def test_fixture_probe_denies_unsafe_acceptance_arguments(tmp_path, argument):
    with pytest.raises(ValueError):
        _draft_fixture_probe(("python", "-m", "pytest", argument), tmp_path)


def test_fixture_probe_never_runs_arbitrary_module(tmp_path):
    with pytest.raises(ValueError, match="module_not_allowed"):
        _draft_fixture_probe(("python", "-m", "os", "--help"), tmp_path)
