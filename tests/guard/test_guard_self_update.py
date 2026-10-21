"""TZ-44: isolated Git handoff and independent pythonw rollback, no live install."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

import pytest

from guard.install import TASK_NAME, install, task_xml
import test_platform_guard as platform
from test_platform_guard import cmd, commit, head, log

# Imported pytest fixture must remain registered on this test module.
tree = platform.tree


class FakeChild:
    pid = 4567

    def __init__(self):
        self.killed = False
        self.exited = False

    def poll(self):
        return 1 if self.exited else None

    def terminate(self):
        self.killed = True
        self.exited = True


@pytest.fixture
def guard_candidate(request):
    """Install a known-good guard in a disposable runtime, commit an update."""
    tree = request.getfixturevalue("tree")
    source = Path(__file__).resolve().parents[2] / "guard"
    previous = install(source, base=tree.live / "data" / "guard-runtime")
    # Simulate the explicitly verified, already-installed autostart bootstrap.
    stable = tree.live / "data" / "guard-runtime" / "run_guard.pyw"
    stable.write_bytes((source / "run_guard.pyw").read_bytes())
    shutil.copytree(source, tree.work / "guard",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    old_code = (tree.work / "guard" / "core.py")
    old_code.write_text(old_code.read_text(encoding="utf-8") +
                        "\n# candidate version marker\n", encoding="utf-8")
    cmd("git", "-C", tree.work, "add", "guard")
    cmd("git", "-C", tree.work, "commit", "-m", "Fleet-Reviewer: synthetic guard candidate")
    candidate = head(tree.work)
    tree.guard._running_guard_version = lambda: previous.name
    tree.guard._save_state(guard_known_good=previous.name)
    tree.previous = previous
    tree.candidate = candidate
    return tree


def test_guard_update_stages_validated_copy_and_exits_only_after_new_heartbeat(guard_candidate):
    t = guard_candidate
    stable = t.live / "data/guard-runtime/run_guard.pyw"
    trusted_bytes = stable.read_bytes()
    candidate_entry = t.work / "guard/run_guard.pyw"
    updated_bytes = candidate_entry.read_bytes() + b"\n# tested supervisor version\n"
    candidate_entry.write_bytes(updated_bytes)
    cmd("git", "-C", t.work, "add", "guard/run_guard.pyw")
    cmd("git", "-C", t.work, "commit", "-m", "Fleet-Reviewer: tested launcher")
    t.candidate = head(t.work)
    stages = []
    t.guard.validator = lambda folder: stages.append(folder) or True
    spawned = FakeChild()

    def fake_spawn(version, token):
        assert (t.live / "data/guard-runtime" / version / "guard/core.py").is_file()
        pulse = t.live / "data/guard-heartbeat.json"
        pulse.write_text(json.dumps({"pid": spawned.pid, "version": version,
                                     "handoff_token": token}), encoding="utf-8")
        return spawned

    t.guard._spawn_handoff = fake_spawn
    assert t.guard.deploy() == "self_update"
    assert head(t.live) == t.original  # New child, not old, merges to live.
    state = t.guard._state()
    assert state["self_update"]["candidate"] == t.candidate
    assert state["guard_known_good"] == t.previous.name
    assert state["self_update"]["version"] != t.previous.name
    assert "self_update" in log(t)
    assert len(stages) == 1 and not stages[0].exists()
    assert stable.read_bytes() == trusted_bytes  # heartbeat alone cannot promote

    new_guard = type(t.guard)(
        t.guard.config, review_check=lambda *_: [],
        validator=lambda _: True, restarter=lambda _: True, prober=lambda _: True,
        sleeper=t.clock.sleep, now=t.clock.now, canary_seconds=2, sample_seconds=1)
    new_guard._health = lambda: True
    new_guard._running_guard_version = lambda: state["self_update"]["version"]
    assert new_guard.deploy() == "deployed"
    assert head(t.live) == t.candidate
    assert new_guard._state()["self_update"] is None
    assert new_guard._state()["guard_known_good"] == state["self_update"]["version"]
    assert stable.read_bytes() != trusted_bytes
    assert stable.read_bytes() == (stable.parent / state["self_update"]["version"] / "run_guard.pyw").read_bytes()  # only after validated canary


def test_handoff_executes_staged_supervisor_not_autostart(guard_candidate, monkeypatch):
    t = guard_candidate
    stable = t.live / "data/guard-runtime/run_guard.pyw"
    stable_before = stable.read_bytes()
    runtime = stable.parent
    version = t.previous.name
    calls = []
    monkeypatch.setattr("guard.core.subprocess.Popen",
                        lambda argv, **kwargs: calls.append((argv, kwargs)) or FakeChild())
    t.guard._spawn_handoff(version, "test-token")
    argv, kwargs = calls[0]
    assert Path(argv[1]) == runtime / version / "run_guard.pyw"
    assert kwargs["env"]["DEV_MCP_GUARD_RUNTIME"] == str(runtime)
    assert stable.read_bytes() == stable_before


def test_failed_handoff_cannot_replace_persistent_autostart_launcher(guard_candidate):
    """Hard crash after a failed candidate must boot the same known-good launcher."""
    t = guard_candidate
    stable = t.live / "data/guard-runtime/run_guard.pyw"
    trusted_bytes = stable.read_bytes()
    source = t.work / "guard/run_guard.pyw"
    updated_bytes = source.read_bytes() + b"\n# candidate supervisor change\n"
    source.write_bytes(updated_bytes)
    cmd("git", "-C", t.work, "add", "guard/run_guard.pyw")
    cmd("git", "-C", t.work, "commit", "-m", "Fleet-Reviewer: synthetic supervisor candidate")
    t.candidate = head(t.work)
    child = FakeChild()
    t.guard._spawn_handoff = lambda *_: child  # never writes a valid heartbeat

    assert t.guard.deploy() == "self_update_reverted"
    assert child.killed and head(t.live) == t.original
    assert stable.read_bytes() == trusted_bytes
    assert (t.live / "data/guard-runtime" / t.guard._state()["guard_known_good"] / "guard/core.py").is_file()
    assert (t.live / "data/guard-runtime/run_guard.pyw").read_bytes() != updated_bytes


def test_new_copy_without_heartbeat_blocks_sha_and_old_guard_stays(guard_candidate):
    t = guard_candidate
    child = FakeChild()
    t.guard._spawn_handoff = lambda _version, _token: child
    # No pulse: injected monotonic clock advances rather than actually sleeping.
    assert t.guard.deploy() == "self_update_reverted"
    assert child.killed and head(t.live) == t.original
    assert t.guard._state()["guard_blocked_candidate"] == t.candidate
    assert t.guard._state()["self_update"] is None
    # A failed pre-FF handoff must not leave a durable validating window.
    assert t.guard._state()["release_window"] is None
    assert "self_update_reverted" in log(t)
    assert t.guard.deploy() == "self_update_reverted"


def test_new_guard_probe_failure_restores_previous_version(guard_candidate):
    t = guard_candidate
    stable = t.live / "data/guard-runtime/run_guard.pyw"
    trusted_bytes = stable.read_bytes()
    candidate_entry = t.work / "guard/run_guard.pyw"
    candidate_entry.write_bytes(candidate_entry.read_bytes() + b"\n# failed candidate\n")
    cmd("git", "-C", t.work, "add", "guard/run_guard.pyw")
    cmd("git", "-C", t.work, "commit", "-m", "Fleet-Reviewer: failed candidate supervisor")
    t.candidate = head(t.work)
    child = FakeChild()

    def spawn(version, token):
        (t.live / "data/guard-heartbeat.json").write_text(json.dumps({
            "pid": child.pid, "version": version, "handoff_token": token,
        }), encoding="utf-8")
        return child

    t.guard._spawn_handoff = spawn
    assert t.guard.deploy() == "self_update"
    new_version = t.guard._state()["self_update"]["version"]
    new_guard = type(t.guard)(
        t.guard.config, review_check=lambda *_: [],
        validator=lambda _: True, restarter=lambda _: True, prober=lambda _: False,
        sleeper=t.clock.sleep, now=t.clock.now, canary_seconds=2, sample_seconds=1)
    new_guard._running_guard_version = lambda: new_version
    assert new_guard.deploy() == "self_update_reverted"
    assert head(t.live) == t.original
    state = new_guard._state()
    assert state["guard_known_good"] == t.previous.name
    assert state["guard_blocked_candidate"] == t.candidate
    assert state["self_update"] is None
    assert "self_update_reverted" in log(t)
    assert stable.read_bytes() == trusted_bytes  # probe failed: reboot uses old guard


def test_policy_is_still_protected_and_no_guard_install(guard_candidate):
    t = guard_candidate
    commit(t.work, "policy.toml", "allow_read = true\n")
    t.guard._spawn_handoff = lambda *_: pytest.fail("no guard self-update for policy")
    assert t.guard.deploy() == "protected_path"
    assert head(t.live) == t.original


def test_existing_deploy_lock_rejects_second_guard(guard_candidate):
    t = guard_candidate
    with t.guard.lock():
        assert t.guard.deploy() == "lock_busy"
    assert head(t.live) == t.original


def _load_supervisor():
    source = Path(__file__).resolve().parents[2] / "guard" / "run_guard.pyw"
    loader = importlib.machinery.SourceFileLoader("guard_self_update_supervisor", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_independent_supervisor_reverts_dead_child_after_three_minutes(tmp_path, monkeypatch):
    supervisor = _load_supervisor()
    runtime = tmp_path / "data" / "guard-runtime"
    for version in ("old", "new"):
        (runtime / version / "guard").mkdir(parents=True)
        (runtime / version / "guard/__main__.py").write_text("# immutable")
    state_file = tmp_path / "state.json"
    journal = tmp_path / "events.jsonl"
    pulse = tmp_path / "guard-heartbeat.json"
    monkeypatch.setattr(supervisor, "HERE", runtime)
    monkeypatch.setattr(supervisor, "STATE_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "STATE_FILE", state_file)
    monkeypatch.setattr(supervisor, "JOURNAL", journal)
    monkeypatch.setattr(supervisor, "PULSE_FILE", pulse)
    state_file.write_text(json.dumps({
        "guard_known_good": "old",
        "self_update": {"candidate": "deadSHA", "previous": "old",
                        "version": "new", "token": "unique"}}), encoding="utf-8")
    fake = FakeChild()
    ticks = [0]

    def clock():
        return ticks[0]

    def advance(seconds):
        ticks[0] += seconds

    result = supervisor.supervise_once(
        "new", "unique", launch=lambda *_: fake, clock=clock, sleep=advance)
    assert result == "old" and fake.killed
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["guard_known_good"] == "old"
    assert state["guard_blocked_candidate"] == "deadSHA"
    assert state["self_update"] is None
    assert "self_update_reverted" in journal.read_text(encoding="utf-8")
    assert supervisor._choose(state) == "old"


def test_supervisor_relaunches_previous_version_after_child_failure(tmp_path, monkeypatch):
    supervisor = _load_supervisor()
    runtime = tmp_path / "data" / "guard-runtime"
    for version in ("old", "new"):
        (runtime / version / "guard").mkdir(parents=True)
        (runtime / version / "guard/__main__.py").write_text("# immutable")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"self_update": {"candidate": "bad", "version": "new",
                                                 "previous": "old", "token": "fresh"},
                                 "guard_known_good": "old"}), encoding="utf-8")
    monkeypatch.setattr(supervisor, "HERE", runtime)
    monkeypatch.setattr(supervisor, "STATE_FILE", state)
    versions = []

    def next_version(version, token=""):
        versions.append(version)
        if version == "new":
            return "old"  # Watcher finished reverting to previous.
        raise SystemExit(0)

    monkeypatch.setattr(supervisor, "supervise_once", next_version)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _: None)
    with pytest.raises(SystemExit) as exc:
        supervisor.main(["--handoff-version", "new", "--handoff-token", "fresh"])
    assert exc.value.code == 0
    assert versions == ["new", "old"]


def test_pinned_legacy_previous_heartbeat_survives_more_than_three_minutes(tmp_path, monkeypatch):
    """A legacy known-good guard lacks new version/token heartbeat fields."""
    supervisor = _load_supervisor()
    runtime = tmp_path / "data" / "guard-runtime"
    (runtime / "old" / "guard").mkdir(parents=True)
    (runtime / "old" / "guard/__main__.py").write_text("# legacy")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"guard_known_good": "old",
                                      "self_update": None}), encoding="utf-8")
    pulse = tmp_path / "guard-heartbeat.json"
    monkeypatch.setattr(supervisor, "HERE", runtime)
    monkeypatch.setattr(supervisor, "STATE_FILE", state_file)
    monkeypatch.setattr(supervisor, "PULSE_FILE", pulse)
    ticks = [0]
    checks = [0]
    fake = FakeChild()

    def poll():
        checks[0] += 1
        return 0 if checks[0] >= 7 else None

    fake.poll = poll
    pulse.write_text(json.dumps({"pid": fake.pid, "time": "initial"}), encoding="utf-8")

    def advance(seconds):
        ticks[0] += seconds * 12
        pulse.write_text(json.dumps({"pid": fake.pid, "time": str(ticks[0])}), encoding="utf-8")

    assert supervisor.supervise_once("old", launch=lambda *_: fake,
                                   clock=lambda: ticks[0], sleep=advance) == "old"
    assert ticks[0] > 180 and not fake.killed


def test_staged_immutable_copy_detects_tamper(tmp_path):
    source = Path(__file__).resolve().parents[2] / "guard"
    installed = install(source, base=tmp_path / "runtime")
    victim = installed / "guard/core.py"
    victim.write_text("# corrupted copy\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid contents"):
        install(source, base=tmp_path / "runtime")


def test_tampered_staged_launcher_never_becomes_autostart(tmp_path):
    from guard.install import publish_launcher
    source = Path(__file__).resolve().parents[2] / "guard"
    runtime = tmp_path / "runtime"
    installed = install(source, base=runtime)
    stable = runtime / "run_guard.pyw"
    stable.write_bytes(b"# independently verified old launcher\n")
    (installed / "run_guard.pyw").write_bytes(b"# different unverified bytes\n")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        publish_launcher(runtime, installed.name)
    assert stable.read_bytes() == b"# independently verified old launcher\n"


def _fake_scheduler(*, old_task=None):
    commands = []
    restores = []

    def runner(argv, **_kwargs):
        commands.append(list(argv))
        if argv[1] == "/Query":
            return SimpleNamespace(returncode=0, stdout=old_task, stderr="") if old_task else (
                SimpleNamespace(returncode=1, stdout="",
                                stderr="ERROR: The system cannot find the file specified."))
        if argv[1] == "/Create" and Path(argv[-2]).name == "previous.xml":
            restores.append(Path(argv[-2]).read_text(encoding="utf-16"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return runner, commands, restores


def test_scheduled_guard_installer_rejects_created_task_without_heartbeat(tmp_path):
    """A successful /Create and /Run are NOT proof of a working guard."""
    source = Path(__file__).resolve().parents[2] / "guard"
    runner, commands, _ = _fake_scheduler()
    now = [0.0]
    base = tmp_path / "data" / "guard-runtime"

    def advance(seconds):
        now[0] += seconds

    with pytest.raises(RuntimeError, match="changing heartbeat"):
        install(source, base=base, runner=runner, register=True,
                process_alive=lambda _: True, clock=lambda: now[0],
                sleep=advance, verification_s=1, poll_s=0.1)
    assert [argv[1] for argv in commands] == ["/Query", "/Create", "/Run", "/Delete"]
    assert commands[-1] == ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    assert not (base / "run_guard.pyw").exists()  # stable launcher not switched


def test_scheduled_guard_installer_restores_previous_task_on_stale_heartbeat(tmp_path):
    source = Path(__file__).resolve().parents[2] / "guard"
    old_xml = task_xml(Path("C:/Old/pythonw.exe"), Path("C:/Old Guard"))
    runner, commands, restores = _fake_scheduler(old_task=old_xml)
    now = [0.0]
    base = tmp_path / "data" / "guard-runtime"
    pulse = base.parent / "guard-heartbeat.json"
    pulse.parent.mkdir(parents=True)

    def advance(seconds):
        now[0] += seconds

    # Stale heartbeat from an already running same-version process is insufficient.
    version = install(source, base=base).name
    pulse.write_text(json.dumps({"version": version, "pid": 9999,
                                "time": "pre-install"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="changing heartbeat"):
        install(source, base=base, runner=runner, register=True,
                process_alive=lambda pid: pid == 9999,
                clock=lambda: now[0], sleep=advance,
                verification_s=1, poll_s=0.1)
    assert [argv[1] for argv in commands] == ["/Query", "/Create", "/Run", "/Create"]
    assert restores == [old_xml]  # restore previous XML, not delete the task


def test_installer_rejects_reused_old_pid_despite_fresh_changing_beats(tmp_path):
    """Scheduler IgnoreNew must not validate an unrelated existing watcher."""
    source = Path(__file__).resolve().parents[2] / "guard"
    base = tmp_path / "data" / "guard-runtime"
    version = install(source, base=base).name
    pulse = base.parent / "guard-heartbeat.json"
    pulse.write_text(json.dumps({"pid": 4321, "version": version,
                                 "time": "old"}), encoding="utf-8")
    runner, commands, _ = _fake_scheduler()
    tick = [0.0]

    def advance(delta):
        tick[0] += delta
        pulse.write_text(json.dumps({"pid": 4321, "version": version,
                                     "time": str(tick[0])}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="changing heartbeat"):
        install(source, base=base, runner=runner, register=True,
                process_alive=lambda pid: pid == 4321, clock=lambda: tick[0],
                sleep=advance, verification_s=1, poll_s=0.1)
    assert commands[-1][1] == "/Delete"


def test_scheduled_guard_installer_accepts_only_live_changing_versioned_beats(tmp_path):
    source = Path(__file__).resolve().parents[2] / "guard"
    runner, commands, _ = _fake_scheduler()
    base = tmp_path / "data" / "guard-runtime"
    pulse = base.parent / "guard-heartbeat.json"
    pulse.parent.mkdir(parents=True)
    version = install(source, base=base).name
    now = [0.0]
    seen_pids = []

    def advance(seconds):
        now[0] += seconds
        pulse.write_text(json.dumps({"pid": 7654, "version": version,
                                     "time": str(now[0])}), encoding="utf-8")

    result = install(source, base=base, runner=runner, register=True,
                     process_alive=lambda pid: seen_pids.append(pid) or pid == 7654,
                     clock=lambda: now[0], sleep=advance,
                     verification_s=1, poll_s=0.1)
    assert result.name == version and seen_pids
    assert [argv[1] for argv in commands] == ["/Query", "/Create", "/Run"]
    assert (base / "run_guard.pyw").is_file()
    xml = (result / "guard-task.xml").read_bytes()
    assert xml.startswith(b"\xff\xfe")  # schtasks XML is UTF-16, not UTF-8


def test_scheduled_guard_installer_rejects_dead_pid_even_if_heartbeat_changes(tmp_path):
    source = Path(__file__).resolve().parents[2] / "guard"
    base = tmp_path / "data" / "guard-runtime"
    version = install(source, base=base).name
    pulse = base.parent / "guard-heartbeat.json"
    pulse.write_text(json.dumps({"pid": 7654, "version": version,
                                 "time": "new"}), encoding="utf-8")
    runner, commands, _ = _fake_scheduler()
    now = [0.0]
    with pytest.raises(RuntimeError, match="changing heartbeat"):
        install(source, base=base, runner=runner, register=True,
                process_alive=lambda _: False, clock=lambda: now[0],
                sleep=lambda delta: now.__setitem__(0, now[0] + delta),
                verification_s=1, poll_s=0.1)
    assert commands[-1][1] == "/Delete"


def test_scheduled_guard_installer_fails_closed_if_previous_task_cannot_be_queried(tmp_path):
    source = Path(__file__).resolve().parents[2] / "guard"
    commands = []

    def denied(argv, **_kwargs):
        commands.append(list(argv))
        return SimpleNamespace(returncode=5, stderr="Access denied", stdout="")

    with pytest.raises(RuntimeError, match="previous scheduled task"):
        install(source, base=tmp_path / "data" / "guard-runtime",
                runner=denied, register=True)
    assert [cmd[1] for cmd in commands] == ["/Query"]


def test_explicit_guard_install_cli_uses_verified_registration(monkeypatch):
    """A CLI exit code of zero cannot mean 'staged only, Scheduler untouched'."""
    from guard import __main__ as cli
    actions = []
    monkeypatch.setattr(cli, "install", lambda **options: actions.append(options))
    assert cli.main(["install"]) == 0
    assert actions == [{"register": True}]


def test_scheduler_xml_pins_installer_localappdata_without_working_directory(
    tmp_path, monkeypatch,
):
    import xml.etree.ElementTree as ET
    profile = str(tmp_path / "профиль владельца's data")
    monkeypatch.setenv("LOCALAPPDATA", profile)
    xml = task_xml(Path("C:/Python/pythonw.exe"), tmp_path / "candidate")
    root = ET.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:Exec/t:WorkingDirectory", ns) is None
    arguments = root.find(".//t:Exec/t:Arguments", ns).text
    match = re.search(r"os.environ\['LOCALAPPDATA'\] = bytes.fromhex\('([0-9a-f]+)'\)", arguments)
    assert match and bytes.fromhex(match.group(1)).decode("utf-8") == profile
    assert profile not in arguments  # shell/Python apostrophes cannot break it


def test_successor_continues_pinned_candidate_when_trunk_moved(guard_candidate):
    """Урок 47: пока шла передача, в ствол пришёл новый коммит — преемник выкатывает
    закреплённого кандидата, а не отвергает передачу."""
    t = guard_candidate
    spawned = FakeChild()

    def fake_spawn(version, token):
        (t.live / "data/guard-heartbeat.json").write_text(json.dumps(
            {"pid": spawned.pid, "version": version, "handoff_token": token}), encoding="utf-8")
        return spawned

    t.guard._spawn_handoff = fake_spawn
    assert t.guard.deploy() == "self_update"
    state = t.guard._state()
    newer = commit(t.work, "later.py", "later\n")
    new_guard = type(t.guard)(
        t.guard.config, review_check=lambda *_: [],
        validator=lambda _: True, restarter=lambda _: True, prober=lambda _: True,
        sleeper=t.clock.sleep, now=t.clock.now, canary_seconds=2, sample_seconds=1)
    new_guard._health = lambda: True
    new_guard._running_guard_version = lambda: state["self_update"]["version"]
    assert new_guard.deploy() == "deployed"
    assert head(t.live) == t.candidate != newer
