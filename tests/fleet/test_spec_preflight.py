"""Adversarial pre-registration probes. All workspaces are disposable tmp_path."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from dev_mcp.fleet.model import Task, norm_path
from dev_mcp.fleet.spec import sync_selected
from dev_mcp.fleet.spec_preflight import _command, preflight_task
from dev_mcp.fleet.store import Store


def task(root: Path, *, tz="TZ-A", owns=("src/a.py",),
         acceptance=("python -m pytest -q tests/test_a.py",), state="ready"):
    return Task("Example", tz, state, 3, "work", "ToDo/tz/x.md", "v1", (),
                tuple(norm_path(x, project_root=str(root)) for x in owns),
                (), acceptance, "Task", "# Task")


def test_ready_and_structured_version(tmp_path):
    item = task(tmp_path, owns=("src/**",))
    a = preflight_task(item, tmp_path, required_paths=["src/a.py"])
    b = preflight_task(item, tmp_path, required_paths=["src/a.py"])
    assert a.to_dict() == b.to_dict()
    assert a.ok and a.action == "register"
    assert a.subject_version and a.snapshot_version
    assert a.checks[0].admissible_as_evidence is False
    assert a.checks[0].dry_run == "not_executed"
    c = preflight_task(replace(item, body="# Changed"), tmp_path)
    assert c.subject_version != a.subject_version


@pytest.mark.parametrize("state", ["draft", "ready", "taken", "submitted", "frozen"])
def test_parent_tree_claim_from_other_task_becomes_dependency(tmp_path, state):
    candidate = task(tmp_path, owns=("src/utils/a.py",))
    other = task(tmp_path, tz="TZ-B", owns=("SRC/**",), state=state)
    report = preflight_task(candidate, tmp_path, existing_tasks=[other])
    assert report.action == "register_with_dependencies"
    assert report.dependencies == ("Example/TZ-B",)
    issue = report.obstacles[0]
    assert issue.cause_code == "needs_file_outside_owns"
    assert issue.predicate["condition"] == "foreign_owner_overlap"
    assert issue.change_source == "work:Example/TZ-B"
    assert issue.resolver_work == "Example/TZ-B"


def test_foreign_owner_can_be_completed_or_unowned(tmp_path):
    candidate = task(tmp_path)
    done = task(tmp_path, tz="TZ-B", owns=("src/**",), state="done")
    a = preflight_task(candidate, tmp_path, existing_tasks=[done])
    assert a.action == "register" and not a.obstacles  # урок 50: завершённое зону не держит
    b = preflight_task(candidate, tmp_path, required_paths=["lib/x.py"])
    assert b.action == "reject"
    assert b.obstacles[0].predicate["condition"] == "outside_declared_owns"


@pytest.mark.parametrize("state", ["review", "unknown"])
def test_terminal_or_nonexistent_owner_state_is_not_a_dependency(tmp_path, state):
    """Неизвестное состояние владельца — по-прежнему отказ (fail-closed); закрытые — урок 50."""
    candidate = task(tmp_path)
    other = task(tmp_path, tz="TZ-B", owns=("src/**",), state=state)
    report = preflight_task(candidate, tmp_path, existing_tasks=[other])
    assert report.action == "reject"
    assert report.dependencies == ()
    assert report.obstacles[0].resolver_work is None


def test_external_path_requires_explicit_authority(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    sibling = tmp_path / "second_project"
    sibling.mkdir()
    target = sibling / "file.py"
    item = task(root, owns=(str(target),))
    a = preflight_task(item, root)
    assert a.action == "reject"
    assert a.obstacles[0].predicate["condition"] == "outside_authorized_roots"
    b = preflight_task(
        item, root, required_paths=[str(target)], allowed_roots=[root, sibling]
    )
    assert b.action == "register"


def test_symlink_traversal_cannot_escape_root(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable for test account")
    candidate = task(tmp_path, owns=("src/a.py",))
    malicious = replace(candidate, owns=("link/**",))
    report = preflight_task(malicious, tmp_path, required_paths=["link/pwn.py"])
    assert report.action == "reject"
    assert any(o.predicate.get("condition") == "symlink_traversal" for o in report.obstacles)
    assert not (outside / "pwn.py").exists()


@pytest.mark.parametrize("acceptance,reason", [
    ((), "empty_acceptance"),
    (("unknown_missing_executable --version",), "unsupported_executable"),
    (("python -c \"print(1)\"",), "python_without_allowlisted_module"),
    (("python -m pytest && echo dangerous",), "shell_syntax_forbidden"),
])
def test_invalid_acceptance_rejected(tmp_path, acceptance, reason):
    report = preflight_task(task(tmp_path, acceptance=acceptance), tmp_path)
    assert report.action == "reject"
    assert report.obstacles[0].cause_code == "acceptance_impossible"
    assert report.obstacles[0].predicate["condition"] == reason


def test_npm_vitest_and_nested_no_emit_typecheck_preflight(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr('dev_mcp.fleet.spec_preflight.shutil.which', lambda exe: exe)
    (tmp_path/'package.json').write_text(json.dumps({'scripts':{
        'test':'vitest run', 'typecheck':'tsc --noEmit && npm run typecheck:hooks',
        'typecheck:hooks':'tsc -p tsconfig.hooks.json'}}),encoding='utf-8')
    (tmp_path/'tsconfig.hooks.json').write_text('{"compilerOptions":{"noEmit":true}}',encoding='utf-8')
    report = preflight_task(task(tmp_path, acceptance=('npm test','npm run typecheck')),tmp_path)
    assert report.action == 'register'
    assert all(c.dry_run=='not_executed' for c in report.checks)


@pytest.mark.parametrize('command,script',[
    ('npm install','vitest run'), ('npm --prefix ../outside test','vitest run'),
    ('npm run typecheck -- --project ../outside','tsc --noEmit'),
    ('npm test','vitest'), ('npm test','vitest run; echo no'),
    ('npm test','npm run test'), ('npm run typecheck','tsc'),
    ('npm test','node -e evil'), ('npm test','vitest run || echo success'),
])
def test_npm_cannot_install_escape_or_run_arbitrary_shell(tmp_path, monkeypatch, command, script):
    import json
    monkeypatch.setattr('dev_mcp.fleet.spec_preflight.shutil.which',lambda exe:exe)
    (tmp_path/'package.json').write_text(json.dumps({'scripts':{'test':script,'typecheck':script}}),encoding='utf-8')
    report = preflight_task(task(tmp_path,acceptance=(command,)),tmp_path)
    assert report.action=='reject'


def test_npm_lifecycle_hook_must_also_be_a_valid_check(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr('dev_mcp.fleet.spec_preflight.shutil.which',lambda exe:exe)
    (tmp_path/'package.json').write_text(json.dumps({'scripts':{'test':'vitest run','pretest':'curl https://example.test'}}),encoding='utf-8')
    report=preflight_task(task(tmp_path,acceptance=('npm test',)),tmp_path)
    assert report.action=='reject'


def test_npm_selected_test_files_keep_same_checked_package(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr('dev_mcp.fleet.spec_preflight.shutil.which', lambda exe: exe)
    (tmp_path/'package.json').write_text(json.dumps({'scripts': {'test': 'vitest run'}}))
    assert preflight_task(task(tmp_path, acceptance=(
        'npm test -- tests/transcript.test.ts tests/replay.test.ts',)), tmp_path).action == 'register'
    for suffix in ('--config outside.js', '../outside.ts', 'C:/outside.ts', '@args', '--watch'):
        assert preflight_task(task(tmp_path, acceptance=('npm test -- '+suffix,)),
                              tmp_path).action == 'reject'


def test_node_test_and_existing_project_script_have_narrow_safe_shapes(tmp_path, monkeypatch):
    monkeypatch.setattr("dev_mcp.fleet.spec_preflight.shutil.which", lambda exe: exe)
    script = tmp_path / "intent" / "scenarios.py"
    script.parent.mkdir()
    script.write_text("# verifier\n", encoding="utf-8")
    for command in ("node --test", "node --test test/upgrade-ledger.test.mjs",
                    "py -3 intent/scenarios.py"):
        report = preflight_task(task(tmp_path, acceptance=(command,)), tmp_path,
                                dry_runner=lambda argv, ws: 1)
        assert report.action == "register", (command, report.to_dict())
        assert not report.checks[0].admissible_as_evidence


@pytest.mark.parametrize("raw", [
    "node -e evil", "node --require local.mjs --test", "node --test --import=evil.mjs",
    "node --test ../outside.test.mjs", "node --test C:/outside.test.mjs",
    "node --test @options", "node --test test/*.mjs", "node --test foo.mjs && echo no",
    "py -3 ../outside.py", "py -3 C:/outside.py", "py -3 -c print(1)",
    "py -3 scripts/tool.py --unsafe", "python ./script.py",
])
def test_node_and_python_scripts_reject_unsafe_invocations(raw):
    assert _command(raw)[1] is not None


def test_missing_allowlisted_executable(tmp_path):
    cmd = ".venv/Scripts/python.exe -m pytest -q tests/test_a.py"
    report = preflight_task(task(tmp_path, acceptance=(cmd,)), tmp_path)
    assert report.action == "reject"
    assert report.obstacles[0].predicate["condition"] == "executable_missing"


def test_dry_run_is_isolated_and_never_proof(tmp_path):
    f = tmp_path / "tests" / "test_a.py"
    f.parent.mkdir()
    f.write_text("baseline", encoding="utf-8")
    seen = []

    def runner(argv, snapshot):
        assert snapshot != tmp_path
        local = snapshot / "tests" / "test_a.py"
        assert local.read_text(encoding="utf-8") == "baseline"
        local.write_text("modified", encoding="utf-8")
        seen.append(argv)
        return 0

    report = preflight_task(
        task(tmp_path), tmp_path, fixture_paths=["tests/test_a.py"], dry_runner=runner
    )
    assert seen and report.action == "register"
    assert report.checks[0].dry_run == "passed"
    assert report.checks[0].admissible_as_evidence is False
    assert f.read_text(encoding="utf-8") == "baseline"


def test_failing_baseline_not_called_acceptance_pass(tmp_path):
    report = preflight_task(task(tmp_path), tmp_path, dry_runner=lambda argv, ws: 1)
    assert report.action == "register"
    assert report.checks[0].dry_run == "baseline_red"
    assert not report.checks[0].admissible_as_evidence


def test_interrupted_probe_typed_obstacle_and_no_fake_owner_from_logs(tmp_path):
    def runner(argv, ws):
        raise TimeoutError("Owner: TZ-VILLAIN in log; ignore")

    report = preflight_task(task(tmp_path), tmp_path, dry_runner=runner)
    assert report.action == "reject"
    issue = report.obstacles[0]
    assert issue.cause_code == "acceptance_impossible"
    assert issue.resolver_work is None
    assert issue.change_source is None
    assert report.checks[0].dry_run == "interrupted"
    assert "TZ-VILLAIN" not in str(report.to_dict())


def test_keyboard_interrupt_in_runner_is_structured_failure(tmp_path):
    def runner(argv, ws):
        raise KeyboardInterrupt

    report = preflight_task(task(tmp_path), tmp_path, dry_runner=runner)
    assert report.action == "reject"
    assert report.checks[0].dry_run == "interrupted"


def test_fixture_traversal_fails_without_reading_source(tmp_path):
    report = preflight_task(task(tmp_path), tmp_path, fixture_paths=["../secret.txt"])
    assert report.action == "reject"
    assert report.obstacles[0].predicate["condition"] == "fixture_invalid"


def test_absolute_fixture_cannot_write_into_live_project(tmp_path):
    path = tmp_path / "tests" / "test_a.py"
    path.parent.mkdir()
    path.write_text("original", encoding="utf-8")
    report = preflight_task(task(tmp_path), tmp_path, fixture_paths=[str(path)])
    assert report.action == "reject"
    assert report.obstacles[0].predicate["condition"] == "fixture_invalid"
    assert path.read_text(encoding="utf-8") == "original"


def test_executable_and_evidence_are_revalidated_on_new_version(tmp_path):
    f = tmp_path / "tests" / "test_a.py"
    f.parent.mkdir()
    f.write_text("old", encoding="utf-8")
    task_a = task(tmp_path)
    a = preflight_task(task_a, tmp_path, fixture_paths=["tests/test_a.py"])
    f.write_text("new", encoding="utf-8")
    b = preflight_task(task_a, tmp_path, fixture_paths=["tests/test_a.py"])
    assert a.snapshot_version != b.snapshot_version
    assert a.checks[0].admissible_as_evidence is False


def _tz_file(root: Path, tz: str, owns: str, acceptance: str):
    folder = root / "ToDo" / "tz"
    folder.mkdir(parents=True, exist_ok=True)
    # Real initial registration now executes commands. Use an actual input
    # for these ownership/registry fixtures; the global test runner's plain
    # "python" installation may not include the pytest package.
    if acceptance == "python -m pytest -q":
        script = root / "src" / "a.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("value = 1\n", encoding="utf-8")
        acceptance = "python -m py_compile src/a.py"
    test = root / "tests" / "test_smoke.py"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text("def test_smoke(): assert True\n", encoding="utf-8")
    (folder / f"{tz}-check.md").write_text(
        chr(96) * 3 + "yaml\n"
        f"tz: {tz}\n"
        f"owns: [{owns}]\n"
        f"acceptance: [{acceptance}]\n"
        + chr(96) * 3 + "\n\n# Task\n",
        encoding="utf-8",
    )


def test_checked_registration_rejects_without_registry_side_effect(tmp_path):
    root = tmp_path / "Example"
    _tz_file(root, "TZ-A", "src/a.py", "")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    result = sync_selected(
        store, "Example", str(root), ["TZ-A"], strict_preflight=True
    )
    assert result["inserted"] == 0
    assert result["preflight"]["TZ-A"]["action"] == "reject"
    assert result["preflight"]["TZ-A"]["obstacles"][0]["cause_code"] == "acceptance_impossible"
    with store.tx() as db:
        assert db.scalar("SELECT COUNT(*) FROM projects") == 0
        assert db.scalar("SELECT COUNT(*) FROM tasks") == 0


def test_checked_registration_requires_isolated_runner(tmp_path):
    root = tmp_path / "Example"
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _tz_file(root, "TZ-A", "src/a.py", "python -m pytest -q")
    result = sync_selected(store, "Example", str(root), ["TZ-A"], strict_preflight=True)
    assert result["inserted"] == 0
    assert result["preflight"]["TZ-A"]["obstacles"][0]["predicate"]["condition"] == "dry_run_unavailable"
    with store.tx() as db:
        assert db.scalar("SELECT COUNT(*) FROM tasks") == 0


def test_checked_registration_creates_dependency_report_without_writing_conflict(tmp_path):
    root = tmp_path / "Example"
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _tz_file(root, "TZ-B", "src/**", "python -m pytest -q")
    first = sync_selected(
        store, "Example", str(root), ["TZ-B"], strict_preflight=True,
        dry_runner=lambda argv, snapshot: 1,
    )
    assert not first["errors"], first["errors"]
    assert first["inserted"] == 1, first
    _tz_file(root, "TZ-A", "src/a.py", "python -m pytest -q")
    second = sync_selected(
        store, "Example", str(root), ["TZ-A"], strict_preflight=True,
        dry_runner=lambda argv, snapshot: 1,
    )
    assert second["inserted"] == 0
    report = second["preflight"]["TZ-A"]
    assert report["action"] == "register_with_dependencies"
    assert report["dependencies"] == ("Example/TZ-B",)
    with store.tx() as db:
        assert db.scalar("SELECT COUNT(*) FROM tasks") == 1


def test_submitted_owner_in_registry_stays_dependency(tmp_path):
    root = tmp_path / "Example"
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _tz_file(root, "TZ-B", "src/**", "python -m pytest -q")
    assert sync_selected(
        store, "Example", str(root), ["TZ-B"], strict_preflight=True,
        dry_runner=lambda argv, snapshot: 1,
    )["inserted"] == 1
    with store.tx() as db:
        db.change(
            "UPDATE tasks SET state='submitted' WHERE project='Example' AND tz='TZ-B'",
            event={"kind": "run", "project": "Example", "tz": "TZ-B",
                   "tool": "simulated_submission"},
        )

    _tz_file(root, "TZ-A", "src/a.py", "python -m pytest -q")
    result = sync_selected(
        store, "Example", str(root), ["TZ-A"], strict_preflight=True,
        dry_runner=lambda argv, snapshot: 1,
    )
    assert result["inserted"] == 0
    report = result["preflight"]["TZ-A"]
    assert report["action"] == "register_with_dependencies"
    assert report["dependencies"] == ("Example/TZ-B",)
    assert report["obstacles"][0]["resolver_work"] == "Example/TZ-B"


def test_checked_registration_accepts_safe_task_and_recheck(tmp_path):
    root = tmp_path / "Example"
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _tz_file(root, "TZ-A", "src/a.py", "python -m pytest -q")
    a = sync_selected(
        store, "Example", str(root), ["TZ-A"], strict_preflight=True,
        dry_runner=lambda argv, snapshot: 1,
    )
    b = sync_selected(
        store, "Example", str(root), ["TZ-A"], strict_preflight=True,
        dry_runner=lambda argv, snapshot: 1,
    )
    assert a["inserted"] == 1
    assert b["unchanged"] == 1
    assert a["preflight"]["TZ-A"]["checks"][0]["admissible_as_evidence"] is False


def test_checked_registration_rechecks_owner_snapshot_before_write(tmp_path):
    root = tmp_path / "Example"
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    _tz_file(root, "TZ-B", "src/b.py", "python -m pytest -q")
    assert sync_selected(
        store, "Example", str(root), ["TZ-B"], strict_preflight=True,
        dry_runner=lambda argv, snapshot: 1,
    )["inserted"] == 1
    _tz_file(root, "TZ-A", "src/a.py", "python -m pytest -q")

    def concurrent_change(argv, workspace):
        with store.tx() as db:
            db.change(
                "UPDATE tasks SET state='taken' WHERE project='Example' AND tz='TZ-B'",
                event={"kind": "run", "project": "Example", "tz": "TZ-B",
                       "tool": "synthetic_owner_change"},
            )
        return 0

    report = sync_selected(
        store, "Example", str(root), ["TZ-A"],
        strict_preflight=True, dry_runner=concurrent_change
    )
    assert report["inserted"] == 0
    assert report["errors"].get("preflight") == "registry_changed_during_preflight", report
    with store.tx() as db:
        assert db.scalar("SELECT COUNT(*) FROM tasks") == 1


@pytest.mark.parametrize(('transferred','live_claim'), [(False,False),(True,False),(True,True)])
def test_proved_inactive_transfer_releases_owns_but_live_claim_or_pause_does_not(tmp_path, transferred, live_claim):
    import json
    root = tmp_path/'Example'
    store = Store.open(str(tmp_path/'fleet.sqlite'))
    _tz_file(root,'TZ-A','src/a.py','python -m pytest -q')
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('Old',?,0)",
                  (str(tmp_path/'staging'),),event='test.seed')
        tx.change("INSERT INTO tasks(project,tz,state,owns,created_at,updated_at) "
                  "VALUES('Old','TZ-B','frozen',?,'now','now')",
                  (json.dumps([norm_path(str(root/'src'))]),),event='test.seed')
        if transferred:
            tx.record_event(kind='intent.work_transferred',project='Old',payload={'replacement':'Example'})
        if live_claim:
            tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W1','now','now')",event='test.seed')
            tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                      "VALUES('Old','TZ-B','W1','active','now')",event='test.seed')
    result=sync_selected(store,'Example',str(root),['TZ-A'],strict_preflight=True,
                         dry_runner=lambda *_:1)
    assert result['inserted'] == int(transferred and not live_claim), result


@pytest.mark.parametrize("state", ["accepted", "rejected", "done"])
def test_closed_task_does_not_hold_its_owns(tmp_path, state):
    """Урок 50: принятая заявка с owns=tests/fleet не блокирует будущие черновики."""
    candidate = task(tmp_path, owns=("tests/fleet/test_model.py",))
    closed = task(tmp_path, tz="TZ-B", owns=("tests/fleet",), state=state)
    report = preflight_task(candidate, tmp_path, existing_tasks=[closed])
    assert not report.obstacles
    assert report.dependencies == ()
