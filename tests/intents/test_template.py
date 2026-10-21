from __future__ import annotations


def test_clean_start_workspace_stays_in_owned_project_child(tmp_path):
    from dev_mcp.intents.stages import PLAYBOOK, stage_owns

    assert str(tmp_path / ".clean-start") in stage_owns(tmp_path, "clean_start")
    assert "--no-hardlinks" in PLAYBOOK["clean_start"]

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from dev_mcp.intents.sources.model import IntentEnvelope
from dev_mcp.intents.template import STAGES, SIZES, build_template


def envelope(text="Build a small application", revision="abc", authority=None):
    return IntentEnvelope("files", "task-1", revision, text,
                          authority or {"create_project": True, "project_roots": ["C:/hub"]},
                          "2026-10-09T09:00:00+00:00")


@pytest.mark.parametrize("size", ["small", "medium", "large"])
def test_size_selected_tree_has_versioned_spec_authority_evidence(size):
    e = envelope()
    tree = build_template(e, project="TestProject", size=size)
    assert len(tree.nodes) == len(SIZES[size]) + 1
    assert tree.nodes[0].stage == "root"
    assert tuple(node.stage for node in tree.nodes[1:]) == SIZES[size]
    assert len(set(node.identifier for node in tree.nodes)) == len(tree.nodes)
    assert all(node.spec and node.authority and node.evidence_spec for node in tree.nodes)
    assert all(node.evidence_spec["subject_version"] == e.revision for node in tree.nodes[1:])
    assert all(node.parent == tree.root_id for node in tree.nodes[1:])
    assert tree.nodes[0].spec["raw_intent_sha256"] != e.raw_intent
    assert e.raw_intent not in repr(tree)
    tree.validate()


def test_large_includes_clean_start_quality_and_approach():
    tree = build_template(envelope(), project="Example", size="large")
    assert tuple(node.stage for node in tree.nodes[1:]) == STAGES
    assert any(node.stage == "implemented" and node.depends_on for node in tree.nodes)
    assert build_template(envelope(), project="Example", size="large") == tree
    assert build_template(envelope(revision="changed"), project="Example", size="large").root_id != tree.root_id


def test_unknown_size_zero_intent_large_attachment_no_authority():
    with pytest.raises(ValueError, match="unknown"):
        build_template(envelope(), project="Example", size="extra-large")
    with pytest.raises(ValueError, match="empty"):
        build_template(envelope(" \t\n"), project="Example", size="small")
    with pytest.raises(ValueError, match="too large"):
        build_template(envelope("a" * (5 * 1024 * 1024 + 1)), project="Example", size="small")
    e = envelope()
    with pytest.raises(ValueError, match="authority"):
        build_template(dataclasses.replace(e, authority={}), project="Example", size="small")


def test_cycle_in_tree_rejected():
    tree = build_template(envelope(), project="Example", size="large")
    first = tree.nodes[1]
    other = tree.nodes[2]
    altered = dataclasses.replace(first, depends_on=(other.identifier,))
    # other already points to first; must catch actual directed wait cycle.
    broken = dataclasses.replace(tree, nodes=(tree.nodes[0], altered, *tree.nodes[2:]))
    with pytest.raises(ValueError, match="cycle"):
        broken.validate()
