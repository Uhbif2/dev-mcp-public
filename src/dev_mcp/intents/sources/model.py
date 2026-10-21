"""Provider-neutral contracts: content is verbatim, authority is trusted configuration."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Protocol


def authority_json(authority: Mapping[str, Any]) -> str:
    """Canonical authority, supplied by the operator, never parsed from intent text."""
    return json.dumps(dict(authority), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def revision_for(text: str, authority: Mapping[str, Any], provider_revision: str = "") -> str:
    """Change if bytes, trusted permissions, or provider revision change."""
    payload = text.encode("utf-8", errors="strict")
    authority_bytes = authority_json(authority).encode("utf-8")
    provider_bytes = provider_revision.encode("utf-8")
    return sha256(
        len(payload).to_bytes(8, "big") + payload
        + len(authority_bytes).to_bytes(8, "big") + authority_bytes
        + len(provider_bytes).to_bytes(8, "big") + provider_bytes
    ).hexdigest()


@dataclass(frozen=True)
class IntentEnvelope:
    source_id: str
    external_id: str
    revision: str
    raw_intent: str
    authority: Mapping[str, Any]
    timestamp: str  # ISO 8601, UTC preferred; metadata from source

    def __post_init__(self) -> None:
        if not self.source_id or not self.external_id or not self.revision:
            raise ValueError("source_id, external_id and revision are required")
        if not isinstance(self.raw_intent, str):
            raise TypeError("raw_intent must be text")
        if not isinstance(self.timestamp, str) or not self.timestamp:
            raise ValueError("timestamp is required")
        authority_json(self.authority)  # fail early on non-serializable authority

    @property
    def key(self) -> tuple[str, str]:
        return self.source_id, self.external_id


@dataclass(frozen=True)
class PollResult:
    source_id: str
    envelopes: tuple[IntentEnvelope, ...] = ()
    complete: bool = True  # false => NEVER infer deletions or commit partial state
    errors: tuple[str, ...] = ()


class SourceAdapter(Protocol):
    source_id: str

    def poll(self) -> PollResult: ...
