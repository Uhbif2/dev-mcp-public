from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.policy import Policy
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.spec import SpecError, _find_project_root, parse_spec, sync_selected, validate_spec
from dev_mcp.fleet.store import Store


def setup(tmp_path: Path):
    read_root = tmp_path / "safe"
    tree = read_root / "tree"
    project = tree / "Project"
    spec = project / "ToDo" / "tz" / "TZ-30-demo.md"
    spec.parent.mkdir(parents=True)
    (project / "src").mkdir()
    (project / "src" / "module.py").write_text("x = 1")
    denied = tree / "private"
    denied.mkdir()
    (denied / "src").mkdir()
    (tree / "owner.txt").write_text("owner file")
    spec.write_text(
        "```yaml\ntz: TZ-30\nowns: [src/module.py]\n"
        # Tree-policy assertions need an executable acceptance that is valid
        # in a minimal project without installing pytest into PATH Python.
        "acceptance: [python -m compileall -q .]\n```\n\n# demo\n",
        encoding="utf-8",
    )
    policy = Policy(
        roots=[read_root], write_roots=[], tree_roots=[tree],
        tree_deny=[denied], deny_patterns=["*.pem", ".git"],
    )
    mapping = {
        "roots": [str(read_root)],
        "write_roots": [],
        "tree_roots": [str(tree)],
        "tree_deny": [str(denied)],
        "deny_patterns": ["*.pem", ".git"],
    }
    task = parse_spec(str(spec))
    return read_root, tree, project, spec, policy, mapping, task


@pytest.mark.parametrize("as_mapping", [False, True])
def test_nested_tree_owns_allowed_and_project_root_discovered(tmp_path, as_mapping):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    used = mapping if as_mapping else policy
    assert not [e for e in validate_spec(task, used) if not e.startswith("warning:")]
    assert _find_project_root(task, used).resolve() == project.resolve()
    assert task.spec_path == "ToDo/tz/TZ-30-demo.md"


@pytest.mark.parametrize("as_mapping", [False, True])
def test_owner_only_root_file_denied(tmp_path, as_mapping):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    used = mapping if as_mapping else policy
    task = replace(task, owns=(norm_path(str(tree / "owner.txt")),))
    errors = validate_spec(task, used)
    assert any("файлы корня" in message for message in errors)


@pytest.mark.parametrize("as_mapping", [False, True])
def test_tree_deny_blocks_subtrees(tmp_path, as_mapping):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    used = mapping if as_mapping else policy
    task = replace(task, owns=(norm_path(str(tree / "private" / "src" / "a.py")),))
    errors = validate_spec(task, used)
    assert any("tree_deny" in message for message in errors)


@pytest.mark.parametrize("as_mapping", [False, True])
def test_denied_patterns_apply_to_tree_paths(tmp_path, as_mapping):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    used = mapping if as_mapping else policy
    task = replace(task, owns=(norm_path(str(project / "src" / "secret.pem")),))
    errors = validate_spec(task, used)
    assert any("маск" in message for message in errors)


def test_outside_tree_and_sibling_prefix_denied(tmp_path):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    (root / "tree-evil").mkdir()
    task = replace(task, owns=(norm_path(str(root / "tree-evil" / "evil.py")),))
    errors = validate_spec(task, policy)
    assert any("вне периметра" in message for message in errors)


def test_legacy_write_roots_still_apply(tmp_path):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    old = {
        "roots": [str(root)], "write_roots": [str(project / "src")],
    }
    task = replace(task, owns=(norm_path(str(project / "src" / "module.py")),))
    assert not [e for e in validate_spec(task, old) if not e.startswith("warning:")]
    task = replace(task, owns=(norm_path(str(tree / "owner.txt")),))
    assert any("вне периметра" in e for e in validate_spec(task, old))


def test_absolute_glob_directory_under_tree(tmp_path):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    task = replace(task, owns=(norm_path(str(project / "src")) + "/**",))
    assert not [e for e in validate_spec(task, policy) if not e.startswith("warning:")]


def test_device_unc_dotdot_and_mixed_separator_input(tmp_path):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    for invalid in (r"\\?\C:\secrets\file", r"\\.\C:\secrets\file"):
        spec.write_text(
            "```yaml\ntz: TZ-30\nowns: [" + "'" + invalid + "'" +
            "]\n```\n# rejected\n", encoding="utf-8",
        )
        with pytest.raises(SpecError):
            parse_spec(str(spec))
    spec.write_text(
        "```yaml\ntz: TZ-30\nowns: [../../../../escape.py]\n```\n",
        encoding="utf-8",
    )
    escaped = parse_spec(str(spec))
    assert any("вне периметра" in e for e in validate_spec(escaped, policy))
    spec.write_text(
        "```yaml\ntz: TZ-30\nowns: [src/../src/module.py]\n```\n",
        encoding="utf-8",
    )
    normalized = parse_spec(str(spec))
    assert not [e for e in validate_spec(normalized, policy) if not e.startswith("warning:")]
    # Valid mixed slashes and casing remain within the same tree.
    spec.write_text(
        "```yaml\ntz: TZ-30\nowns: ['src\\\\module.py']\n```\n# valid\n",
        encoding="utf-8",
    )
    got = parse_spec(str(spec))
    assert not [e for e in validate_spec(got, policy) if not e.startswith("warning:")]


def test_tree_root_folder_itself_not_writable(tmp_path):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    task = replace(task, owns=(norm_path(str(tree)) + "/**",))
    errors = validate_spec(task, policy)
    assert any("файлы корня" in message for message in errors)


def test_project_discovery_must_not_substitute_tree_parent(tmp_path):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    missing_spec_task = replace(task, spec_path="ToDo/tz/not-real.md",
                                owns=(norm_path(str(project / "src" / "module.py")),),
                                project="Nonexistent Project")
    assert _find_project_root(missing_spec_task, mapping) is None


@pytest.mark.parametrize("as_mapping", [False, True])
def test_checked_registration_under_tree_root(tmp_path, as_mapping):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    result = sync_selected(
        store, "Project", str(project), ["TZ-30"],
        policy=mapping if as_mapping else policy, strict_preflight=True,
        dry_runner=lambda argv, isolated: 0,
    )
    assert result["errors"] == {}
    assert result["inserted"] == 1
    assert result["preflight"]["TZ-30"]["action"] == "register"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE tz='TZ-30'") == 1


def test_case_insensitive_tree_root_for_owns(tmp_path):
    root, tree, project, spec, policy, mapping, task = setup(tmp_path)
    owned = norm_path(str(project / "src" / "module.py"))
    changed_case = owned.upper()
    assert not [e for e in validate_spec(replace(task, owns=(changed_case,)), policy)
                if not e.startswith("warning:")]
