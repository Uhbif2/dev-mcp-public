from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet.drafts import register_drafts, register_owner_approved_legacy
from dev_mcp.fleet.spec import sync_selected
from dev_mcp.fleet.store import Store


def setup(tmp_path: Path, name: str = "Example"):
    root = tmp_path / "folder-different-from-project-name"
    root.mkdir()
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES(?,?,1)",
                  (name, str(root)), event="seed")
    policy = {"roots": [str(root)], "write_roots": [str(root)],
              "allow_write": True}
    return store, root, policy


def draft(root: Path, tz: str, *, needs=(), owns=None, folder="tz-drafts"):
    path = root / "ToDo" / folder / f"{tz}-work.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    owns = owns or f"src/{tz.lower()}.py"
    path.write_text(
        "```yaml\n"
        f"tz: {tz}\n"
        f"owns: [{owns}]\n"
        f"needs: [{', '.join(needs)}]\n"
        # Registration now executes acceptance in a real isolated source snapshot.
        # These tests exercise draft routing, so use a launchable no-side-effect
        # command rather than relying on an unrelated PATH Python having pytest.
        "acceptance: [python -m compileall -q .]\n"
        "```\n\n# Work\n", encoding="utf-8",
    )
    return path


def records(store: Store, kind: str):
    with store.read() as tx:
        return [dict(r) for r in tx.query(
            "SELECT project,tz,payload FROM events WHERE kind=? ORDER BY id", (kind,)
        )]


def tick(store, policy, *, project_info=lambda p: {}, dry_runner=lambda argv, ws: 1):
    return register_drafts(store, policy, project_info=project_info,
                           dry_runner=dry_runner)


def test_valid_draft_promoted_and_registered_with_event(tmp_path):
    store, root, policy = setup(tmp_path)
    original = draft(root, "TZ-01")
    draft(root, "TZ-02", folder="ignore")  # not scanned
    (original.parent / "PLAN.md").write_text("# plan", encoding="utf-8")
    (original.parent / "notes.md").write_text("notes", encoding="utf-8")

    result = tick(store, policy)
    assert result["registered"] == ["Example/TZ-01"]
    assert result["invalid"] == {}
    assert not original.exists()
    assert (root / "ToDo/tz/TZ-01-work.md").is_file()
    assert (root / "ToDo/tz-drafts/PLAN.md").exists()
    assert (root / "ToDo/tz-drafts/notes.md").exists()
    with store.read() as tx:
        item = tx.one("SELECT state,spec_path FROM tasks WHERE project='Example' AND tz='TZ-01'")
    assert dict(item) == {"state": "ready", "spec_path": "ToDo/tz/TZ-01-work.md"}
    assert len(records(store, "draft_registered")) == 1


def test_concurrent_registry_change_defers_without_a_false_planner_fix(tmp_path):
    store, root, policy = setup(tmp_path)
    path = draft(root, 'TZ-01')
    def racing_probe(argv, workspace):
        with store.tx() as tx:
            tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) "
                      "VALUES ('Example','other','ready','now','now')", event='test.concurrent_progress')
        return 0
    first = tick(store, policy, dry_runner=racing_probe)
    assert first['deferred'] == {'Example/TZ-01': 'registry_changed_during_preflight'}
    assert first['invalid'] == {}
    assert path.exists()
    assert records(store, 'draft_invalid') == records(store, 'draft_fix_open') == []
    assert tick(store, policy)['registered'] == ['Example/TZ-01']
    assert tick(store, policy)["registered"] == []
    assert len(records(store, "draft_registered")) == 1


def test_closed_history_during_preflight_does_not_invalidate_live_ownership_snapshot(tmp_path):
    store,root,policy = setup(tmp_path)
    draft(root,'TZ-01')
    def closed_history(argv,workspace):
        with store.tx() as tx:
            tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) "
                      "VALUES ('Example','old','accepted','now','now')", event='test.closed_history')
        return 0
    result = tick(store,policy,dry_runner=closed_history)
    assert result['registered'] == ['Example/TZ-01']
    assert result['deferred'] == {} and result['invalid'] == {}


def test_broken_dependency_one_event_and_one_planner_fix_per_fingerprint(tmp_path):
    store, root, policy = setup(tmp_path)
    path = draft(root, "TZ-03", needs=["TZ-99"])
    for _ in range(3):
        result = tick(store, policy)
        assert "tz-99" in result["invalid"]["Example/TZ-03-work.md"].lower()
    assert path.exists()
    assert len(records(store, "draft_invalid")) == 1
    with store.read() as tx:
        fixes = tx.query("SELECT kind,state,author_worker FROM tasks WHERE tz LIKE 'fix:draft:%'")
    assert len(fixes) == 1
    assert tuple(fixes[0]) == ("fix", "ready", "planner")
    path.write_text(path.read_text(encoding="utf-8") + "\n# Changed\n", encoding="utf-8")
    tick(store, policy)
    assert len(records(store, "draft_invalid")) == 2
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'fix:draft:%'") == 1


def test_present_but_invalid_upstream_drafts_keep_dependants_waiting(tmp_path):
    """An invalid draft is pending, not absent, for every downstream level."""
    store, root, policy = setup(tmp_path)
    # The files for all four TZs exist, but the first two cannot yet parse.
    first = draft(root, "TZ-701")
    second = draft(root, "TZ-702")
    for path in (first, second):
        path.write_text("broken YAML header\n", encoding="utf-8")
    third = draft(root, "TZ-703", needs=("TZ-701", "TZ-702"))
    fourth = draft(root, "TZ-704", needs=("TZ-703",))

    for _ in range(2):
        outcome = tick(store, policy)
        assert outcome["registered"] == []
        assert set(outcome["invalid"]) == {
            "Example/TZ-701-work.md", "Example/TZ-702-work.md",
        }
        assert outcome["waiting"] == {
            "Example/TZ-703": ["TZ-701", "TZ-702"],
            "Example/TZ-704": ["TZ-703"],
        }
        assert third.is_file() and fourth.is_file()
    assert len(records(store, "draft_invalid")) == 2
    with store.read() as tx:
        fixes = tx.query("SELECT tz FROM tasks WHERE tz LIKE 'fix:draft:%'")
    assert len(fixes) == 2  # No bogus downstream draft-fixes.

    draft(root, "TZ-701")
    draft(root, "TZ-702")
    promoted = tick(store, policy)
    assert promoted["registered"] == [
        "Example/TZ-701", "Example/TZ-702", "Example/TZ-703", "Example/TZ-704",
    ]
    assert promoted["invalid"] == {} and promoted["waiting"] == {}
    assert not third.exists() and not fourth.exists()


def test_genuinely_missing_draft_dependency_remains_invalid(tmp_path):
    store, root, policy = setup(tmp_path)
    orphan = draft(root, "TZ-704", needs=("TZ-703",))

    outcome = tick(store, policy)

    assert outcome["waiting"] == {} and outcome["registered"] == []
    assert "нет ТЗ tz-703" in outcome["invalid"]["Example/TZ-704-work.md"]
    assert orphan.is_file()
    assert len(records(store, "draft_invalid")) == 1


def test_two_dependent_drafts_register_in_topological_order(tmp_path):
    store, root, policy = setup(tmp_path)
    draft(root, "TZ-01", needs=["TZ-02"])
    draft(root, "TZ-02")
    result = tick(store, policy)
    assert result["registered"] == ["Example/TZ-02", "Example/TZ-01"]
    assert result["invalid"] == {}
    assert len(records(store, "draft_registered")) == 2


def test_six_mixed_frontmatter_and_fenced_node_drafts_register_with_dependencies(tmp_path, monkeypatch):
    # Reproduces the real TZ-401..406 plan without launching the owner's relay,
    # Node test files or any external API. The fixture probe is not proof.
    store, root, policy = setup(tmp_path, "Proxy intent")
    monkeypatch.setattr("dev_mcp.fleet.spec_preflight._executable_exists",
                        lambda argv, root: True)
    cases = (
        ("TZ-401", (), "node --test test/ledger.test.mjs", True),
        ("TZ-402", (), "node --test test/scheduler.test.mjs", True),
        ("TZ-403", (), "node --test test/audit.test.mjs", False),
        ("TZ-404", ("TZ-401", "TZ-402", "TZ-403"), "node --test", True),
        ("TZ-405", ("TZ-404",), "py -3 intent/scenarios.py", True),
        ("TZ-406", ("TZ-405",), "node --test", False),
    )
    for tz, needs, acceptance, frontmatter in cases:
        source = root / "ToDo" / "tz-drafts" / f"{tz}-upgrade.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        border = "---" if frontmatter else "```yaml"
        end = "---" if frontmatter else "```"
        source.write_text(
            f"{border}\ntz: {tz}\nowns: [lib/{tz.lower()}.mjs]\n"
            f"needs: [{', '.join(needs)}]\nacceptance: [{acceptance}]\n{end}\n"
            f"# {tz} work\n", encoding="utf-8",
        )
    outcome = tick(store, policy)
    assert outcome["registered"] == [f"Proxy intent/TZ-{n}" for n in range(401, 407)]
    assert outcome["invalid"] == {} and outcome["waiting"] == {}
    assert len(records(store, "draft_registered")) == 6
    assert len(list((root / "ToDo" / "tz").glob("TZ-*.md"))) == 6
    assert tick(store, policy)["registered"] == []


def test_busy_owner_defers_draft_without_planner_fix_then_retries(tmp_path):
    """A dependency is not an invalid spec; retry after its owner finishes."""
    store, root, policy = setup(tmp_path)
    draft(root, "TZ-78", owns="src/integrate.py", folder="tz")
    previous = sync_selected(
        store, "Example", str(root), ["TZ-78"], policy=policy,
        strict_preflight=True, dry_runner=lambda argv, snapshot: 1,
    )
    assert previous["inserted"] == 1, previous
    source = draft(root, "TZ-195", needs=("TZ-78",), owns="src/integrate.py")

    for _ in range(2):
        pending = tick(store, policy)
        assert pending["registered"] == []
        assert pending["invalid"] == {}
        assert pending["waiting"] == {"Example/TZ-195": ["Example/TZ-78"]}
        assert source.is_file()
        assert not (root / "ToDo/tz/TZ-195-work.md").exists()
    assert records(store, "draft_invalid") == []
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz LIKE 'fix:draft:%'") == 0

    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='Example' AND tz='TZ-78'",
                  event={"kind": "seed"})
    ready = tick(store, policy)
    assert ready["registered"] == ["Example/TZ-195"]
    assert ready["waiting"] == {} and ready["invalid"] == {}
    assert not source.exists()
    assert (root / "ToDo/tz/TZ-195-work.md").exists()


def test_occupied_number_rejected_even_with_different_filename(tmp_path):
    store, root, policy = setup(tmp_path)
    draft(root, "TZ-07", folder="tz")
    candidate = draft(root, "TZ-07")
    result = tick(store, policy)
    assert result["registered"] == []
    assert "занят" in result["invalid"]["Example/TZ-07-work.md"]
    assert candidate.exists()


def test_named_project_differs_from_root_and_dependency_resolves(tmp_path):
    store, root, policy = setup(tmp_path, "Fleet Platform")
    draft(root, "TZ-10", folder="tz")
    candidate = draft(root, "TZ-11", needs=["TZ-10"])
    result = tick(store, policy)
    assert result["registered"] == ["Fleet Platform/TZ-11"]
    assert not candidate.exists()
    with store.read() as tx:
        assert tx.scalar("SELECT needs FROM tasks WHERE project='Fleet Platform' AND tz='TZ-11'") == '["TZ-10"]'


def test_cycle_rejected_without_moves_and_not_repeated(tmp_path):
    store, root, policy = setup(tmp_path)
    one = draft(root, "TZ-20", needs=["TZ-21"])
    two = draft(root, "TZ-21", needs=["TZ-20"])
    a = tick(store, policy)
    assert a["registered"] == []
    assert all("цикл" in text for text in a["invalid"].values())
    tick(store, policy)
    assert one.exists() and two.exists()
    assert len(records(store, "draft_invalid")) == 2


def test_strict_preflight_denies_missing_runner(tmp_path):
    store, root, policy = setup(tmp_path)
    original = draft(root, "TZ-08")
    result = register_drafts(store, policy)
    assert result["registered"] == []
    assert "dry_run_unavailable" in result["invalid"]["Example/TZ-08-work.md"]
    assert original.exists()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz='TZ-08'") == 0


def _symlink(link: Path, target: Path, *, directory: bool = False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this filesystem")


def test_external_draft_symlink_never_moved_or_registered(tmp_path):
    store, root, policy = setup(tmp_path)
    external = tmp_path / "outside"
    external.mkdir()
    source = draft(root, "TZ-50")
    source.unlink()
    victim = external / "TZ-50-work.md"
    victim.write_text(
        chr(96) * 3 + "yaml\ntz: TZ-50\nowns: ["
        + (root / "src/inside.py").as_posix()
        + "]\nacceptance: [python -m pytest -q]\n"
        + chr(96) * 3 + "\n# Outside\n", encoding="utf-8",
    )
    _symlink(source, victim)
    result = tick(store, policy)
    assert result["registered"] == []
    assert "unsafe_symlink_draft" in result["invalid"]["Example/TZ-50-work.md"]
    assert source.is_symlink() and victim.is_file()
    assert not (root / "ToDo/tz/TZ-50-work.md").exists()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz='TZ-50'") == 0


def test_symlinked_draft_and_target_directories_are_fail_closed(tmp_path):
    store, root, policy = setup(tmp_path)
    external = tmp_path / "outside"
    external.mkdir()
    drafts = root / "ToDo/tz-drafts"
    drafts.parent.mkdir(parents=True)
    _symlink(drafts, external, directory=True)
    first = tick(store, policy)
    assert first["registered"] == []
    assert first["invalid"]["Example/ToDo/tz-drafts"] == "unsafe_symlink_directory"
    drafts.unlink()

    original = draft(root, "TZ-51")
    target_dir = root / "ToDo/tz"
    _symlink(target_dir, external, directory=True)
    second = tick(store, policy)
    assert second["registered"] == []
    assert "unsafe_symlink_target" in second["invalid"]["Example/TZ-51-work.md"]
    assert original.is_file() and list(external.iterdir()) == []


def test_replaced_symlink_at_rename_cannot_pass_preflight(tmp_path, monkeypatch):
    store, root, policy = setup(tmp_path)
    original = draft(root, "TZ-52")
    victim = tmp_path / "outside.md"
    victim.write_text("outside", encoding="utf-8")
    rename = Path.rename
    replaced = False

    def raced_rename(path, to):
        nonlocal replaced
        if path == original and not replaced:
            replaced = True
            path.unlink()
            path.symlink_to(victim)
        return rename(path, to)

    monkeypatch.setattr(Path, "rename", raced_rename)
    result = tick(store, policy)
    assert replaced and not result["registered"]
    assert "draft_path_changed_during_move" in result["invalid"]["Example/TZ-52-work.md"]
    assert victim.read_text(encoding="utf-8") == "outside"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz='TZ-52'") == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_commit_true_commits_only_draft_rename(tmp_path):
    store, root, policy = setup(tmp_path)
    draft(root, "TZ-30")
    unrelated = root / "unrelated.txt"
    unrelated.write_text("unrelated", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, check=True)

    git("init", "-q")
    git("config", "user.email", "fleet-test@example.invalid")
    git("config", "user.name", "Fleet Tests")
    git("add", ".")
    git("commit", "-qm", "initial")
    unrelated.write_text("uncommitted change", encoding="utf-8")

    result = tick(store, policy, project_info=lambda name: {"commit": True})
    assert result["registered"] == ["Example/TZ-30"]
    assert result["commit_errors"] == {}
    changed = git("show", "--pretty=format:", "--name-status", "HEAD").stdout
    assert "ToDo/tz/TZ-30-work.md" in changed
    assert "ToDo/tz-drafts/TZ-30-work.md" in changed
    assert "unrelated.txt" not in changed
    assert git("status", "--porcelain").stdout.strip() == "M unrelated.txt"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_commit_true_registers_new_untracked_draft(tmp_path):
    store, root, policy = setup(tmp_path)

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True, check=True)

    git("init", "-q")
    git("config", "user.email", "fleet-test@example.invalid")
    git("config", "user.name", "Fleet Tests")
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-qm", "initial")
    source = draft(root, "TZ-31")  # planner-created file is not tracked yet

    result = tick(store, policy, project_info=lambda name: {"commit": True})
    assert result["registered"] == ["Example/TZ-31"]
    assert result["commit_errors"] == {}
    assert not source.exists()
    assert (root / "ToDo/tz/TZ-31-work.md").is_file()
    changed = git("show", "--pretty=format:", "--name-status", "HEAD").stdout
    assert "ToDo/tz/TZ-31-work.md" in changed
    assert git("status", "--porcelain").stdout.strip() == ""


def legacy_approval(store, root, path, *, tz="TZ-70", kind="owner_legacy_import_approved",
                    tool="fleet_owner", provenance="active_backlog", sha=None,
                    project="Example", worker_id=None, name=None):
    data = {
        "project": project, "tz": tz,
        "path": name if name is not None else path.relative_to(root).as_posix(),
        "sha256": sha if sha is not None else hashlib.sha256(path.read_bytes()).hexdigest(),
        "provenance": provenance, "action": "approve",
    }
    with store.tx() as tx:
        tx.record_event(kind=kind, tool=tool, worker_id=worker_id,
                        project=project, tz=tz, payload=data)
        return int(tx.scalar("SELECT MAX(id) FROM events"))


def legacy_import(store, policy, event_id, *, dry_runner=lambda argv, ws: 1):
    return register_owner_approved_legacy(
        store, policy, approval_event_id=event_id, dry_runner=dry_runner,
    )


def test_legacy_owner_approval_is_exact_and_idempotent(tmp_path):
    store, root, policy = setup(tmp_path)
    source = draft(root, "TZ-70", folder="tz")
    approval = legacy_approval(store, root, source)
    outcome = legacy_import(store, policy, approval)
    assert outcome["status"] == "registered", outcome
    with store.read() as tx:
        task = tx.one("SELECT state,spec_path FROM tasks WHERE project='Example' AND tz='TZ-70'")
    assert (task["state"], task["spec_path"]) == ("ready", "ToDo/tz/TZ-70-work.md")
    assert len(records(store, "owner_legacy_import_registered")) == 1
    assert legacy_import(store, policy, approval)["status"] == "unchanged"
    assert len(records(store, "owner_legacy_import_registered")) == 1


def test_legacy_spec_is_never_scanned_without_owner_approval(tmp_path):
    store, root, policy = setup(tmp_path)
    source = draft(root, "TZ-70", folder="tz")
    assert tick(store, policy)["registered"] == []
    assert legacy_import(store, policy, 98765)["reason"] == "missing_trusted_owner_approval"
    assert source.exists()
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz='TZ-70'") == 0


@pytest.mark.parametrize("override", [
    {"tool": "untrusted_agent"},
    {"kind": "owner_cli_manual"},
    {"provenance": "archive"},
    {"provenance": "unknown"},
    {"name": "ToDo/archive/TZ-70-work.md"},
    {"name": "ToDo/tz/../TZ-70-work.md"},
    {"name": "ToDo/tz/old/TZ-70-work.md"},
    {"sha": "0" * 64},
])
def test_legacy_bad_approval_path_provenance_and_digest_blocked(tmp_path, override):
    store, root, policy = setup(tmp_path)
    source = draft(root, "TZ-70", folder="tz")
    approval = legacy_approval(store, root, source, **override)
    assert legacy_import(store, policy, approval)["status"] == "blocked"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz='TZ-70'") == 0
    assert records(store, "owner_legacy_import_registered") == []


def test_legacy_cannot_reuse_approval_after_spec_change(tmp_path):
    store, root, policy = setup(tmp_path)
    source = draft(root, "TZ-70", folder="tz")
    approval = legacy_approval(store, root, source)
    source.write_text(source.read_text(encoding="utf-8") + "\nupdated\n", encoding="utf-8")
    assert legacy_import(store, policy, approval)["reason"] == "approved_sha_mismatch"
    assert records(store, "owner_legacy_import_registered") == []


def test_legacy_requires_trusted_strict_preflight_and_rejects_bad_owns(tmp_path):
    store, root, policy = setup(tmp_path)
    source = draft(root, "TZ-70", folder="tz")
    approval = legacy_approval(store, root, source)
    assert legacy_import(store, policy, approval, dry_runner=None)["reason"] == "missing_trusted_strict_preflight"
    # A pre-existing incompatible active owner blocks registration.
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('Example','TZ-71','other','ready',1,'work','','','[]',"
            "?, '[]','[]','',datetime('now'),datetime('now'))",
            (json.dumps(["src/tz-70.py"]),),
            event={"kind": "seed"},
        )
    outcome = legacy_import(store, policy, approval)
    assert outcome["status"] == "blocked"
    assert outcome["reason"] == "strict_preflight_or_registry_rejected"
    assert records(store, "owner_legacy_import_registered") == []


def test_legacy_rejects_symlink_to_foreign_file(tmp_path):
    store, root, policy = setup(tmp_path)
    source = draft(root, "TZ-70", folder="tz")
    approval = legacy_approval(store, root, source)
    other = tmp_path / "outside.md"
    other.write_bytes(source.read_bytes())
    source.unlink()
    _symlink(source, other)
    outcome = legacy_import(store, policy, approval)
    assert outcome["reason"] == "missing_or_unsafe_spec"
    assert records(store, "owner_legacy_import_registered") == []


def test_legacy_does_not_modify_existing_frozen_task(tmp_path):
    store, root, policy = setup(tmp_path)
    source = draft(root, "TZ-70", folder="tz")
    approval = legacy_approval(store, root, source)
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,"
            "needs,owns,resources,acceptance,body,created_at,updated_at) "
            "VALUES('Example','TZ-70','frozen','frozen',1,'work','','','[]',"
            "'[]','[]','[]','',datetime('now'),datetime('now'))",
            event={"kind": "seed"},
        )
    assert legacy_import(store, policy, approval)["reason"] == "tz_already_registered"
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='TZ-70'")["state"] == "frozen"

