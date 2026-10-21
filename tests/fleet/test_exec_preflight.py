"""TZ-50: executable changes are accepted only after a real trial run."""
from __future__ import annotations

import subprocess
import sys
import os
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_mcp.fleet.exec_preflight import (
    EXECUTABLE_SOURCES, ConfigActivation, PreflightResult,
    install_verified, inventory, parse_command, preflight_gate, run_command,
)
from dev_mcp.fleet.gate_snapshot import SnapshotStore
from dev_mcp.fleet import goals
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.integrate import Integrator
from dev_mcp.fleet.spec import sync_backlog


def test_inventory_all_execution_surfaces(tmp_path: Path) -> None:
    config = {
        "projects": {"P": {"gate": "python -m pytest -q"}},
        "resources": {"build": {"cwd": str(tmp_path), "command": "python build.py"}},
    }
    result = inventory(
        config, project_roots={"P": tmp_path},
        acceptances={"P/TZ-1": (tmp_path, ["python -m pytest tests/test_a.py"])},
        installations={"watchdog": (tmp_path, "schtasks /Create /TN test")},
        guard_entry=(tmp_path, "python guard/entry.py"),
    )
    assert [x.source for x in result] == list(EXECUTABLE_SOURCES)
    assert [x.identity for x in result] == [
        "P", "P/TZ-1:0", "build:command", "watchdog", "guard"
    ]
    with pytest.raises(ValueError, match="missing cwd"):
        inventory({"resources": {"bad": {"command": "python test.py"}}},
                  project_roots={})


def test_real_resource_reload_rejects_unlaunchable_command_keeps_old_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bite: old production goals._load_doc accepted any changed resource command."""
    root = tmp_path / "resource"
    root.mkdir()
    (root / "fixture.txt").write_text("resource sandbox\n", encoding="utf-8")
    config = tmp_path / "fleet-projects.toml"
    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    store = Store.open(str(db))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('Fleet Platform',?,1)",
                  (str(tmp_path),), event="seed")
    ok = f'"{sys.executable}" -c "print(1)"'
    bad = "executable-with-no-chance-of-existing-zzzzz"
    fixed = f'"{sys.executable}" -c "print(2)"'

    def propose(command: str) -> None:
        previous = config.stat().st_mtime if config.exists() else 0
        config.write_text("[resources.build]\n" +
                          "cwd = " + json.dumps(str(root)) + "\n" +
                          "command = " + json.dumps(command) + "\n", encoding="utf-8")
        if previous:
            os.utime(config, (previous + 3, previous + 3))

    monkeypatch.delenv("DEV_MCP_FLEET_PROJECTS", raising=False)
    monkeypatch.setattr(goals, "DEFAULT_PATH", config)
    monkeypatch.setattr(goals, "_cache", {"path": None, "mtime": None,
                                        "data": {}, "rejected_mtime": None})
    try:
        propose(ok)
        assert goals.resources_info()["build"]["command"] == ok
        propose(bad)
        assert goals.resources_info()["build"]["command"] == ok
        # Rejected content is not re-run or published on repeated reads.
        assert goals.resources_info()["build"]["command"] == ok
        with store.read() as tx:
            assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='exec_preflight_reject'") == 1
            assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='request'") == 1
        propose(fixed)
        observed = goals.resources_info()["build"]["command"]
        with store.read() as tx:
            events = [r["payload"] for r in tx.query(
                "SELECT payload FROM events WHERE kind='exec_preflight_reject' ORDER BY id"
            )]
        assert observed == fixed, events
    finally:
        store.close()


def test_resource_install_command_is_not_trial_run_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bite: an install command must not mutate the OS during a config reload."""
    root = tmp_path / "cwd"
    root.mkdir()
    (root / "marker.txt").write_text("sandbox\n", encoding="utf-8")
    output = tmp_path / "install_side_effect.txt"
    config = tmp_path / "fleet-projects.toml"
    config.write_text("[resources.service]\n", encoding="utf-8")
    monkeypatch.delenv("DEV_MCP_FLEET_PROJECTS", raising=False)
    monkeypatch.setattr(goals, "DEFAULT_PATH", config)
    monkeypatch.setattr(goals, "_cache", {"path": None, "mtime": None,
                                        "data": {}, "rejected_mtime": None})
    assert goals.resources_info() == {"service": {}}
    cmd = f'"{sys.executable}" -c "open({str(output)!r}, {chr(39)}w{chr(39)}).write({chr(39)}bad{chr(39)})"'
    previous = config.stat().st_mtime
    config.write_text(
        "[resources.service]\n"
        "cwd = " + json.dumps(str(root)) + "\n"
        "install_command = " + json.dumps(cmd) + "\n", encoding="utf-8",
    )
    os.utime(config, (previous + 3, previous + 3))
    assert goals.resources_info() == {"service": {}}
    assert not output.exists()


def test_real_new_acceptance_registration_does_not_activate_unlaunchable_command(
    tmp_path: Path,
) -> None:
    """Bite: the previous runtime probe skipped an entirely new spec."""
    root = tmp_path / "project"
    spec = root / "ToDo" / "tz" / "TZ-1-check.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(
        ("&#96;" * 3 + "yaml\n"
         "tz: TZ-1\nowns: [src/sample.py]\n"
         "acceptance: [\"unlaunchable-initial-acceptance-zzzzz\"]\n" +
         "&#96;" * 3 + "\n\n# Candidate\n").replace("&#96;", chr(96)),
        encoding="utf-8",
    )
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        result = sync_backlog(store, "P", str(root))
        assert result["errors"], result
        with store.read() as tx:
            assert tx.one("SELECT tz FROM tasks WHERE project='P' AND tz='TZ-1'") is None
            assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='exec_preflight_reject'") == 1
    finally:
        store.close()


def test_real_guard_install_uses_shared_verified_installer_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bite: old Scheduler install directly used a private, unshared heartbeat check."""
    from guard.install import install
    from dev_mcp.fleet import exec_preflight
    from guard import verified

    calls = []
    real = verified.install_verified
    assert exec_preflight.install_verified is real

    def spy(*args, **kwargs):
        calls.append("verified")
        return real(*args, **kwargs)

    monkeypatch.setattr(verified, "install_verified", spy)
    history = []

    def fake_scheduler(argv, **_kwargs):
        history.append(list(argv))
        if argv[1] == "/Query":
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    clock = [0.0]
    source = Path(__file__).resolve().parents[2] / "guard"
    with pytest.raises(RuntimeError, match="changing heartbeat"):
        install(source, base=tmp_path / "runtime", register=True,
                runner=fake_scheduler, process_alive=lambda _: True,
                clock=lambda: clock[0], sleep=lambda delta: clock.__setitem__(0, clock[0] + delta),
                verification_s=0.4, poll_s=0.1)
    assert calls == ["verified"]
    assert [part[1] for part in history] == ["/Query", "/Create", "/Run", "/Delete"]


def test_installed_guard_registers_and_rolls_back_without_dev_mcp(
    tmp_path: Path,
) -> None:
    """Bite: rejected guard.install imported dev_mcp only for register=True.

    Import and invoke a copied standalone guard package under -I -S. Fake
    Scheduler never changes OS configuration; the absence of a heartbeat
    must still restore the previous task. dev_mcp is not on sys.path.
    """
    source = Path(__file__).resolve().parents[2] / "guard"
    bundle = tmp_path / "standalone"
    shutil.copytree(
        source, bundle / "guard",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    script = """
import sys
from pathlib import Path
from types import SimpleNamespace
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from guard.install import install
history = []
clock = [0.0]
def scheduler(argv, **kwargs):
    history.append(argv[1])
    if argv[1] == '/Query':
        return SimpleNamespace(returncode=1, stdout='', stderr='not found')
    return SimpleNamespace(returncode=0, stdout='', stderr='')
try:
    install(root / 'guard', base=root / 'data' / 'guard-runtime',
            register=True, runner=scheduler, process_alive=lambda pid: True,
            clock=lambda: clock[0],
            sleep=lambda amount: clock.__setitem__(0, clock[0] + amount),
            verification_s=0.5, poll_s=0.1)
except RuntimeError as exc:
    assert 'changing heartbeat' in str(exc), str(exc)
else:
    raise AssertionError('missing heartbeat wrongly accepted')
assert history == ['/Query', '/Create', '/Run', '/Delete'], history
assert 'dev_mcp' not in sys.modules
"""
    checked = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(bundle)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert (bundle / "data" / "guard-runtime").is_dir()


def test_parser_preserves_windows_paths_quotes_and_shell_is_never_used(tmp_path: Path) -> None:
    assert parse_command(r'"C:\Tools\Python 3\python.exe" -m pytest "tests\a b.py"',
                         windows=True) == (
        r"C:\Tools\Python 3\python.exe", "-m", "pytest", r"tests\a b.py"
    )
    called = []

    def runner(argv, **kw):
        called.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = run_command(r'.venv/Scripts/python.exe -V', cwd=tmp_path,
                         runner=runner, windows=True)
    assert result.ok
    assert called[0][0] == [".venv/Scripts/python.exe", "-V"]
    assert called[0][1]["shell"] is False
    assert called[0][1]["cwd"] == str(tmp_path)
    assert called[0][1]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_preflight_rejects_command_requiring_cmd_before_activation(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "src.py").write_text("ok = True\n", encoding="utf-8")
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        if argv[0].lower().endswith("cmd.exe"):
            return subprocess.CompletedProcess(argv, 0, "command ok", "")
        raise FileNotFoundError("batch file requires cmd.exe")

    snapshots = SnapshotStore(tmp_path / "snapshots")
    bad = preflight_gate("check.cmd", source_root=root, snapshots=snapshots,
                         runner=runner, windows=True)
    assert not bad.ok and bad.reason == "gate_launch_failed"
    assert bad.launch.returncode == 127
    good = preflight_gate("cmd.exe /d /c check.cmd", source_root=root,
                          snapshots=snapshots, runner=runner, windows=True)
    assert good.ok and good.launch.ok
    assert len(seen) == 2


def test_preflight_detects_duplicate_pytest_module_names_on_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "src"
    for directory in ("a", "b"):
        folder = root / directory
        folder.mkdir(parents=True)
        (folder / "test_duplicate.py").write_text(
            "def test_something(): assert True\n", encoding="utf-8"
        )
    gate = f'"{sys.executable}" -c "print(123)"'
    result = preflight_gate(gate, source_root=root,
                            snapshots=SnapshotStore(tmp_path / "snapshots"))
    assert not result.ok and result.reason == "pytest_collection_failed"
    assert result.launch.ok and result.collection.returncode != 0
    assert "import file mismatch" in (
        result.collection.stdout + result.collection.stderr
    ).lower()
    # A local bytecode cache cannot poison or fix the verified snapshot.
    assert not (root / "__pycache__").exists()


def test_preflight_accepts_working_change_and_verifies_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "src"
    folder = root / "tests"
    folder.mkdir(parents=True)
    (folder / "test_works.py").write_text("def test_passes(): assert 2 == 2\n",
                                          encoding="utf-8")
    snapshots = SnapshotStore(tmp_path / "snapshots")
    outcome = preflight_gate(f'"{sys.executable}" -c "print(123)"',
                             source_root=root, snapshots=snapshots)
    assert outcome.ok and outcome.reason == "ok", outcome
    assert outcome.launch.ok and outcome.collection.ok
    assert snapshots.verify(outcome.snapshot_id).snapshot_id == outcome.snapshot_id


def test_preflight_resolves_project_venv_executable_but_uses_snapshot_cwd(
    tmp_path: Path,
) -> None:
    root = tmp_path / "src"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    executable = root / ".venv" / "Scripts" / "python.exe"
    executable.write_text("", encoding="utf-8")
    (root / "module.py").write_text("x = 1", encoding="utf-8")
    seen = []

    def runner(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = preflight_gate(".venv/Scripts/python.exe -V",
                            source_root=root, snapshots=SnapshotStore(tmp_path / "snap"),
                            runner=runner, windows=True)
    assert result.ok
    assert seen[0][0][0] == str(executable.resolve())
    assert Path(seen[0][1]["cwd"]) != root
    assert (Path(seen[0][1]["cwd"]) / "module.py").exists() is False
    # Snapshot workspace is destroyed after preflight; the source is untouched.
    assert executable.exists()


def test_transactional_config_keeps_last_known_good_and_reports_failure() -> None:
    emitted = []
    tasks = []
    checked = []

    def verify(identity, command):
        checked.append((identity, command))
        return PreflightResult(command != "broken", "gate_launch_failed" if command == "broken" else "ok")

    activation = ConfigActivation({"A": "old", "B": "keep"}, verify=verify,
                                  event=lambda *parts: emitted.append(parts),
                                  author_task=lambda *parts: tasks.append(parts))
    assert not activation.activate({"A": "broken", "B": "keep"})
    assert activation.active == {"A": "old", "B": "keep"}
    assert emitted == tasks == [("A", "broken", "gate_launch_failed")]
    assert activation.activate({"A": "good", "B": "keep"})
    assert activation.active == {"A": "good", "B": "keep"}
    assert checked == [("A", "broken"), ("A", "good")]


def test_process_install_without_pulse_rolls_back() -> None:
    events = []
    t = [0.0]

    def sleep(delta):
        t[0] += delta

    ok = install_verified(lambda: events.append("installed"),
                          alive=lambda: True, pulse=lambda: None,
                          rollback=lambda: events.append("rollback"),
                          duration_s=0.5, poll_s=0.1, clock=lambda: t[0],
                          sleep=sleep)
    assert not ok and events == ["installed", "rollback"]


def test_process_install_which_dies_rolls_back() -> None:
    t = [0.0]
    rolled = []

    def sleep(delta):
        t[0] += delta

    ok = install_verified(lambda: None, alive=lambda: t[0] < 0.2,
                          pulse=lambda: int(t[0] * 10),
                          rollback=lambda: rolled.append(True),
                          duration_s=0.5, poll_s=0.1, clock=lambda: t[0],
                          sleep=sleep)
    assert not ok and rolled


def test_process_install_alive_with_repeated_pulses_succeeds() -> None:
    t = [0.0]
    rolled = []

    def sleep(delta):
        t[0] += delta

    ok = install_verified(lambda: None, alive=lambda: True,
                          pulse=lambda: int(t[0] * 10),
                          rollback=lambda: rolled.append(True),
                          duration_s=0.5, poll_s=0.1, clock=lambda: t[0],
                          sleep=sleep)
    assert ok and rolled == []


def test_real_config_reload_rejects_bad_gate_and_creates_repair_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "fleet-projects.toml"
    config.write_text('[projects.P]\ngate = "python -V"\n', encoding="utf-8")
    db = tmp_path / "data" / "fleet" / "fleet.sqlite"
    store = Store.open(str(db))
    workspace = tmp_path / "project"
    workspace.mkdir()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('P',?,1)",
                  (str(workspace),), event="seed")
        tx.change("INSERT INTO projects(name,root,active) VALUES('Fleet Platform',?,1)",
                  (str(tmp_path),), event="seed")
    monkeypatch.delenv("DEV_MCP_FLEET_PROJECTS", raising=False)
    monkeypatch.setattr(goals, "DEFAULT_PATH", config)
    monkeypatch.setattr(goals, "_cache", {
        "path": None, "mtime": None, "data": {}, "rejected_mtime": None,
    })
    checked = []

    def verify(project: str, command: str) -> PreflightResult:
        checked.append((project, command))
        return PreflightResult(command != "bad.exe", "gate_launch_failed")

    monkeypatch.setattr(goals, "_verify_gate_change", verify)
    assert goals.project_info("P")["gate"] == "python -V"
    original_mtime = config.stat().st_mtime
    config.write_text('[projects.P]\ngate = "bad.exe"\n', encoding="utf-8")
    os.utime(config, (original_mtime + 3, original_mtime + 3))
    assert goals.project_info("P")["gate"] == "python -V"
    assert checked == [("P", "bad.exe")]
    # Repeated reads at the rejected mtime never rerun a broken command or task.
    assert goals.project_info("P")["gate"] == "python -V"
    assert len(checked) == 1
    with store.read() as tx:
        events = tx.query("SELECT kind,payload FROM events WHERE kind='exec_preflight_reject'")
        requests = tx.query("SELECT tz,state FROM tasks WHERE kind='request'")
    assert len(events) == len(requests) == 1
    assert requests[0]["state"] == "ready"

    config.write_text('[projects.P]\ngate = "python -c ok"\n', encoding="utf-8")
    os.utime(config, (original_mtime + 6, original_mtime + 6))
    assert goals.project_info("P")["gate"] == "python -c ok"
    store.close()


def test_config_reload_cannot_remove_gate_or_drop_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[projects.P]\ngate = "python -V"\n', encoding="utf-8")
    monkeypatch.setenv("DEV_MCP_FLEET_PROJECTS", str(config))
    monkeypatch.setattr(goals, "_cache", {
        "path": None, "mtime": None, "data": {}, "rejected_mtime": None,
    })
    assert goals.project_info("P")["gate"] == "python -V"
    before = config.stat().st_mtime
    config.write_text('[projects.P]\npriority = 1\n', encoding="utf-8")
    os.utime(config, (before + 3, before + 3))
    assert goals.project_info("P")["gate"] == "python -V"
    config.write_text('[projects.Q]\npriority = 1\n', encoding="utf-8")
    os.utime(config, (before + 6, before + 6))
    assert goals.project_info("P")["gate"] == "python -V"


def test_integrator_candidate_preflights_gate_before_commit(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    def git(*parts: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *parts], cwd=root, check=True, capture_output=True,
                              text=True, encoding="utf-8")

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (root / "src").mkdir()
    source = root / "src" / "candidate.py"
    source.write_text("result = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "baseline")
    initial_sha = git("rev-parse", "HEAD").stdout.strip()
    source.write_text("result = 2\n", encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    try:
        failing = Integrator(
            store, tmp_path / "cursor.json",
            project_info=lambda _: {"gate": "definitely-not-installed-executable-xyz"},
        )
        ready, reason = failing._candidate(str(root), ["src/candidate.py"], "P")
        assert not ready and "exec_preflight: gate_launch_failed" in reason
        assert git("rev-parse", "HEAD").stdout.strip() == initial_sha
        assert source.read_text(encoding="utf-8") == "result = 2\n"

        passing = Integrator(
            store, tmp_path / "cursor.json",
            project_info=lambda _: {"gate": f'\"{sys.executable}\" -c \"print(123)\"'},
        )
        ready, reason = passing._candidate(str(root), ["src/candidate.py"], "P")
        assert ready, reason
        assert git("rev-parse", "HEAD").stdout.strip() == initial_sha
    finally:
        store.close()


def test_snapshot_files_follow_git_and_drop_media_only_when_oversized(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dev_mcp.fleet import exec_preflight

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "docs" / "shot.png").write_bytes(b"png")
    (root / "build.log").write_text("noise", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    files = exec_preflight._snapshot_files(root)
    assert sorted(files) == [".gitignore", "docs/shot.png", "src/app.py"]

    monkeypatch.setattr(exec_preflight, "_MAX_FILES", 2)
    assert sorted(exec_preflight._snapshot_files(root)) == [".gitignore", "src/app.py"]

    plain = tmp_path / "plain"
    plain.mkdir()
    assert exec_preflight._snapshot_files(plain) is None


def test_gate_still_reads_media_left_out_of_an_oversized_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Beta App 19.10.2026: site/build.py opens assets/beta_app.png; the
    oversized snapshot dropped all media and every integration failed to launch."""
    from dev_mcp.fleet import exec_preflight

    root = tmp_path / "proj"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "brand.png").write_bytes(b"\x89PNG-bytes")
    (root / "build.py").write_text(
        "from pathlib import Path\n"
        "assert (Path(__file__).parent / 'assets' / 'brand.png').read_bytes() == b'\\x89PNG-bytes'\n",
        encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(exec_preflight, "_MAX_FILES", 1)
    files, side = exec_preflight._snapshot_split(root)
    assert files == ["build.py"] and side == ["assets/brand.png"]

    store = SnapshotStore(tmp_path / "objects")
    result = preflight_gate(subprocess.list2cmdline([sys.executable, "build.py"]),
                            source_root=root, snapshots=store)
    assert result.ok, result


def test_preflight_links_ignored_deps_without_snapshotting_them(tmp_path: Path) -> None:
    from dev_mcp.fleet import exec_preflight

    root = tmp_path / "proj"
    (root / "vendor" / "libx").mkdir(parents=True)
    (root / "vendor" / "libx" / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")
    (root / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    (root / "app.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent / 'vendor'))\n"
        "import libx\nassert libx.VALUE == 7\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert "vendor/libx/__init__.py" not in exec_preflight._snapshot_files(root)

    deps = exec_preflight.project_deps(root, ["vendor", "../escape", "missing"])
    assert list(deps) == ["vendor"]
    store = SnapshotStore(tmp_path / "objects")
    command = subprocess.list2cmdline([sys.executable, "app.py"])
    without = preflight_gate(command, source_root=root, snapshots=store)
    assert without.reason == "gate_launch_failed"
    linked = preflight_gate(command, source_root=root, snapshots=store, deps=deps)
    assert linked.ok, linked
    # The link is gone and the dependency itself is intact.
    assert (root / "vendor" / "libx" / "__init__.py").is_file()
