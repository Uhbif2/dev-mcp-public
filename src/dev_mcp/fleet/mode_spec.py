"""Fail-closed ChatGPT mode contract for fleet task YAML headers.

Mode is *metadata*, never executable browser code or an instruction to the
model.  Absence is backward-compatible; a malformed explicit value is fatal.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

MODES = frozenset({"default", "thinking", "deep-research"})
HEADER = re.compile(r"\A\s*```ya?ml[ \t]*\r?\n(.*?)\r?\n```[ \t]*(?:\r?\n|\Z)", re.S | re.I)
VERSION = "fleet-chat-mode/v1"


class ModeSpecError(ValueError):
    """Malformed mode metadata cannot quietly fall back to default."""


@dataclass(frozen=True)
class ModeDirective:
    requested: str
    stage: str | None
    spec_sha: str
    explicit: bool = False
    contract_version: str = VERSION

    def effective(self, available: set[str] | frozenset[str]) -> str:
        """Only 'понято' defaults to Research, and only on proven capability."""
        if self.requested == "default" and self.stage == "понято" and "deep-research" in available:
            return "deep-research"
        return self.requested


def validate_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in MODES:
        raise ModeSpecError(f"unsupported chat mode: {value!r}")
    return value


def mode_from_header(header: Mapping[str, Any], *, spec_sha: str = "") -> ModeDirective:
    if not isinstance(header, Mapping):
        raise ModeSpecError("task header must be a mapping")
    mode = validate_mode(header.get("mode", "default"))
    stage = header.get("outcome_stage", header.get("stage"))
    if stage is not None and (not isinstance(stage, str) or not stage.strip()):
        raise ModeSpecError("outcome_stage must be a non-empty string")
    return ModeDirective(requested=mode, stage=stage, spec_sha=spec_sha,
                         explicit="mode" in header)


def read_mode_spec(text: str) -> ModeDirective:
    if not isinstance(text, str):
        raise ModeSpecError("task spec must be text")
    match = HEADER.search(text)
    if not match:
        raise ModeSpecError("missing or displaced fenced YAML header")
    try:
        header = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ModeSpecError("malformed YAML header") from exc
    if not isinstance(header, Mapping):
        raise ModeSpecError("YAML header must be a mapping")
    return mode_from_header(
        header, spec_sha=hashlib.sha256(match.group(1).encode("utf-8")).hexdigest(),
    )


def read_mode_path(path: str | Path) -> ModeDirective:
    return read_mode_spec(Path(path).read_text(encoding="utf-8"))
