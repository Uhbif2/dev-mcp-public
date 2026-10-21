"""Strict, project-local delivery policy. No executable strings or secrets.

Untrusted deliver.toml is data: only registered Operator operations may run,
and every target workspace must be inside a server-authorized root.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dev_mcp.operator_catalog import CatalogError, validate_operation

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
_APPLY = frozenset({"cloud.deploy", "android.build_install", "second_pc.run"})
_VERIFY = frozenset({"ci.run", "eval.live", "device.screens", "browser.task"})


class DeliveryPolicyError(ValueError):
    """Policy absent, incomplete or attempting to leave the authorized perimeter."""


@dataclass(frozen=True)
class DeliveryStep:
    operation: str
    params: dict[str, Any]


@dataclass(frozen=True)
class DeliveryPolicy:
    target: str
    workspace: Path
    timeout_seconds: int
    known_good_revision: str
    apply: DeliveryStep
    verify: DeliveryStep
    rollback: DeliveryStep


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _scoped(path: Path, roots: tuple[Path, ...]) -> Path:
    """Reject symlinked path components *before* resolving them."""
    if not path.is_absolute():
        raise DeliveryPolicyError("workspace_not_absolute")
    absolute = path.absolute()
    if any(p.is_symlink() for p in (absolute, *absolute.parents)):
        raise DeliveryPolicyError("symlink_path")
    actual = absolute.resolve()
    if not any(_inside(actual, root) for root in roots):
        raise DeliveryPolicyError("path_outside_roots")
    if not actual.is_dir():
        raise DeliveryPolicyError("workspace_missing")
    return actual


def _step(name: str, raw: Any, *, allowed: frozenset[str],
          target: str, roots: tuple[Path, ...]) -> DeliveryStep:
    if type(raw) is not dict or set(raw) != {"operation", "params"}:
        raise DeliveryPolicyError(f"{name}_schema")
    op, params = raw["operation"], raw["params"]
    if not isinstance(op, str) or op not in allowed:
        raise DeliveryPolicyError(f"{name}_operation_not_allowed")
    try:
        validated = validate_operation(op, params)
    except CatalogError as exc:
        raise DeliveryPolicyError(f"{name}_{exc}") from exc
    values = dict(validated.params)
    if values.get("target") != target:
        raise DeliveryPolicyError("target_mismatch")
    values["workspace"] = str(_scoped(Path(values["workspace"]), roots))
    return DeliveryStep(op, values)


def load_delivery_policy(
    project_root: str | Path, *, allowed_roots: tuple[str | Path, ...],
) -> DeliveryPolicy:
    """Load ONLY project_root/deliver.toml. Missing policy disables delivery.

    The roots are supplied by the trusted server, never by the TOML author.
    Parsing and validation complete before any side effect.
    """
    if not allowed_roots:
        raise DeliveryPolicyError("no_authorized_roots")
    roots = tuple(Path(root).resolve(strict=True) for root in allowed_roots)
    if any(not root.is_dir() or root.is_symlink() for root in roots):
        raise DeliveryPolicyError("invalid_authorized_root")
    project = _scoped(Path(project_root), roots)
    config = project / "deliver.toml"
    if config.is_symlink():
        raise DeliveryPolicyError("symlink_policy")
    if not config.is_file():
        raise DeliveryPolicyError("deliver_toml_missing")
    if config.stat().st_size > 16384:
        raise DeliveryPolicyError("deliver_toml_oversize")
    try:
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DeliveryPolicyError("invalid_toml") from exc
    if type(raw) is not dict or set(raw) != {"delivery", "apply", "verify", "rollback"}:
        raise DeliveryPolicyError("policy_schema")
    d = raw["delivery"]
    if type(d) is not dict or set(d) != {
        "version", "target", "workspace", "timeout_seconds", "known_good_revision",
    }:
        raise DeliveryPolicyError("delivery_schema")
    if type(d["version"]) is not int or d["version"] != 1:
        raise DeliveryPolicyError("delivery_schema_version")
    if not isinstance(d["target"], str) or not _TARGET.fullmatch(d["target"]):
        raise DeliveryPolicyError("invalid_target")
    if type(d["timeout_seconds"]) is not int or not 1 <= d["timeout_seconds"] <= 1800:
        raise DeliveryPolicyError("invalid_timeout")
    if not isinstance(d["known_good_revision"], str) or not _SHA.fullmatch(d["known_good_revision"]):
        raise DeliveryPolicyError("invalid_known_good_revision")
    workspace = _scoped(Path(d["workspace"]), roots)
    target = d["target"]
    apply = _step("apply", raw["apply"], allowed=_APPLY, target=target, roots=roots)
    verify = _step("verify", raw["verify"], allowed=_VERIFY, target=target, roots=roots)
    rollback = _step("rollback", raw["rollback"], allowed=_APPLY, target=target, roots=roots)
    if any(Path(step.params["workspace"]) != workspace for step in (apply, verify, rollback)):
        raise DeliveryPolicyError("workspace_mismatch")
    if rollback.operation != apply.operation:
        raise DeliveryPolicyError("rollback_kind_mismatch")
    if rollback.params.get("artifact_sha256") != d["known_good_revision"]:
        raise DeliveryPolicyError("rollback_revision_mismatch")
    return DeliveryPolicy(
        target=target, workspace=workspace,
        timeout_seconds=d["timeout_seconds"], known_good_revision=d["known_good_revision"],
        apply=apply, verify=verify, rollback=rollback,
    )
