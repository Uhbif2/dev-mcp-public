import json

import pytest

from dev_mcp.operator_catalog import CatalogError, NAMES, public_catalog, validate_operation


DIGEST = "a" * 64
BASE = {"workspace": "/trusted/app", "target": "dev-1"}


def test_catalog_names_and_json_schema():
    assert NAMES == {
        "android.build_install", "device.screens", "cloud.deploy",
        "eval.live", "ci.run", "second_pc.run", "browser.task",
    }
    catalog = public_catalog()
    assert catalog["version"] == 1
    for schema in catalog["operations"].values():
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "workspace" in schema["required"]
        assert "target" in schema["required"]
    assert json.loads(json.dumps(catalog))


def test_public_catalog_tampering_cannot_register_new_commands():
    public_catalog()["operations"]["arbitrary.shell"] = {"type": "object"}
    public_catalog()["operations"]["ci.run"]["additionalProperties"] = True
    assert "arbitrary.shell" not in public_catalog()["operations"]
    with pytest.raises(CatalogError, match="unknown_operation"):
        validate_operation("arbitrary.shell", {})


@pytest.mark.parametrize("operation,extra", [
    ("android.build_install", {"artifact_sha256": DIGEST}),
    ("device.screens", {"scenario": "startup", "count": 2}),
    ("cloud.deploy", {"artifact_sha256": DIGEST, "release_id": "r1"}),
    ("eval.live", {"corpus_sha256": DIGEST, "sample_count": 20}),
    ("ci.run", {"commit_sha": "b" * 40, "workflow": "gate"}),
    ("second_pc.run", {"plan_id": "fresh", "artifact_sha256": DIGEST}),
    ("browser.task", {"task_id": "inspect", "url": "https://example.org/page", "mode": "inspect"}),
])
def test_named_operation_shapes(operation, extra):
    request = {**BASE, **extra}
    validated = validate_operation(operation, request)
    request["target"] = "swapped"
    assert validated.params["target"] == "dev-1"


@pytest.mark.parametrize("params", [
    {}, {**BASE, "artifact_sha256": ""},
    {**BASE, "artifact_sha256": DIGEST, "command": "cmd /c whoami"},
    {**BASE, "artifact_sha256": DIGEST, "target": ""},
    {**BASE, "artifact_sha256": DIGEST, "target": "x" * 10000},
    {**BASE, "artifact_sha256": DIGEST, "workspace": "x" * 10000},
    {**BASE, "artifact_sha256": DIGEST, "target": 100},
    {**BASE, "artifact_sha256": DIGEST, "target": ["x"]},
    {**BASE, "artifact_sha256": DIGEST, "target": "x\npassword=foo"},
])
def test_hostile_inputs_are_denied(params):
    with pytest.raises(CatalogError):
        validate_operation("android.build_install", params)


def test_no_shell_expression_or_opaque_nesting():
    with pytest.raises(CatalogError):
        validate_operation("ci.run", {**BASE, "workflow": "bash -c", "commit_sha": "f" * 40})
    with pytest.raises(CatalogError):
        validate_operation("device.screens", {**BASE, "scenario": "start", "count": True})
    with pytest.raises(CatalogError):
        validate_operation("browser.task", {
            **BASE, "task_id": "x", "url": "http://private.local", "mode": "inspect"
        })
