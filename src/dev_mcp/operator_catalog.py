"""Frozen allowlist and strict, deliberately small JSON-schema subset for fleet_op.

Parameters contain no command lines, credential values, custom paths to executables,
or budget overrides. Adapters may only receive a validated named operation.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


class CatalogError(ValueError):
    """Invalid or unknown public operation request."""


def _text(maximum: int = 128, *, pattern: str = r"[A-Za-z0-9][A-Za-z0-9_.:-]*",
          minimum: int = 1) -> dict[str, Any]:
    return {"type": "string", "minLength": minimum, "maxLength": maximum, "pattern": pattern}


def _schema(extra: dict[str, dict[str, Any]], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace", "target", *required],
        "properties": {
            "workspace": {"type": "string", "format": "scope-path", "minLength": 2, "maxLength": 1024},
            "target": _text(100),
            # A different execution_id permits a new observation with the
            # same parameters; retry of one ID remains strictly idempotent.
            "execution_id": _text(64, pattern=r"[0-9a-f]{16,64}"),
            **extra,
        },
    }


_SCHEMAS: dict[str, dict[str, Any]] = {
    "android.build_install": _schema({
        "artifact_sha256": _text(64, pattern=r"[0-9a-f]{64}"),
        "build_profile": {"type": "string", "enum": ["debug", "release"]},
    }, ("artifact_sha256",)),
    "device.screens": _schema({
        "scenario": _text(100),
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
    }, ("scenario",)),
    "cloud.deploy": _schema({
        "artifact_sha256": _text(64, pattern=r"[0-9a-f]{64}"),
        "release_id": _text(100),
        "rollback_release": _text(100),
    }, ("artifact_sha256", "release_id")),
    "eval.live": _schema({
        "corpus_sha256": _text(64, pattern=r"[0-9a-f]{64}"),
        "sample_count": {"type": "integer", "minimum": 1, "maximum": 200},
    }, ("corpus_sha256",)),
    "ci.run": _schema({
        "commit_sha": _text(64, pattern=r"(?:[0-9a-f]{40}|[0-9a-f]{64})"),
        "workflow": _text(100),
    }, ("commit_sha", "workflow")),
    "second_pc.run": _schema({
        "plan_id": _text(100),
        "artifact_sha256": _text(64, pattern=r"[0-9a-f]{64}"),
    }, ("plan_id", "artifact_sha256")),
    "browser.task": _schema({
        "task_id": _text(100),
        "url": _text(2048, pattern=r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~:/?&=%#-]*)?"),
        "mode": {"type": "string", "enum": ["inspect", "configure"]},
    }, ("task_id", "url", "mode")),
}
# No mutable schema reference is ever handed to a caller.
NAMES: frozenset[str] = frozenset(_SCHEMAS)
CATALOG_VERSION = 1


@dataclass(frozen=True)
class ValidatedOperation:
    name: str
    params: Mapping[str, Any]


def public_catalog() -> dict[str, Any]:
    """Detached JSON serializable schema snapshot; callers cannot replace specs."""
    return {"version": CATALOG_VERSION, "operations": copy.deepcopy(_SCHEMAS)}


def validate_operation(name: str, params: Any) -> ValidatedOperation:
    if not isinstance(name, str) or name not in NAMES:
        raise CatalogError("unknown_operation")
    if not isinstance(params, dict) or type(params) is not dict:
        raise CatalogError("invalid_parameters")
    # Prevent oversized, deeply nested or non-JSON data before further inspection.
    try:
        wire = json.dumps(params, allow_nan=False, ensure_ascii=True, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CatalogError("invalid_json") from exc
    if len(wire) > 8192:
        raise CatalogError("parameters_too_large")
    schema = _SCHEMAS[name]
    props = schema["properties"]
    if not set(schema["required"]).issubset(params):
        raise CatalogError("missing_parameter")
    if any(k not in props for k in params):
        raise CatalogError("unknown_parameter")
    for key, value in params.items():
        field = props[key]
        if field["type"] == "string":
            if type(value) is not str:
                raise CatalogError("wrong_type")
            if not field.get("minLength", 0) <= len(value) <= field.get("maxLength", 8192):
                raise CatalogError("invalid_length")
            if "\x00" in value or any(ord(c) < 32 for c in value):
                raise CatalogError("control_character")
            if "enum" in field and value not in field["enum"]:
                raise CatalogError("invalid_enum")
            if "pattern" in field and re.fullmatch(field["pattern"], value, flags=re.ASCII) is None:
                raise CatalogError("invalid_format")
        elif field["type"] == "integer":
            if type(value) is not int or not field["minimum"] <= value <= field["maximum"]:
                raise CatalogError("invalid_number")
        else:
            raise CatalogError("unsupported_schema")
    return ValidatedOperation(name=name, params=MappingProxyType(params.copy()))
