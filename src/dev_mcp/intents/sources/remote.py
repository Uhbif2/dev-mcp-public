"""Remote adapters never request the body of an unmarked note/task."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .model import IntentEnvelope, PollResult, revision_for


@dataclass(frozen=True)
class MarkedItem:
    """Metadata only. The provider's list endpoint must not include private bodies."""
    external_id: str
    revision: str
    timestamp: str
    tags: tuple[str, ...]


class _MarkedRemoteAdapter:
    def __init__(
        self, source_id: str, *,
        list_marked: Callable[[str], Sequence[MarkedItem]] | None,
        read_marked: Callable[[str], str] | None,
        tag: str = "owner-intent", authority: Mapping[str, Any] | None = None,
        max_bytes: int = 1024 * 1024, enabled: bool = True,
    ) -> None:
        if not source_id or not tag or max_bytes < 1:
            raise ValueError("invalid remote source configuration")
        self.source_id = source_id
        self.list_marked = list_marked
        self.read_marked = read_marked
        self.tag = tag.lstrip("#")
        self.authority = dict(authority or {})
        self.max_bytes = max_bytes
        self.enabled = enabled

    def poll(self) -> PollResult:
        if not self.enabled:
            return PollResult(self.source_id, complete=False, errors=("source_disabled",))
        if self.list_marked is None or self.read_marked is None:
            return PollResult(self.source_id, complete=False, errors=("source_unconfigured",))
        try:
            # The adapter does not perform a global search or fetch bodies for tag discovery.
            headers = self.list_marked(self.tag)
        except Exception:
            return PollResult(self.source_id, complete=False, errors=("metadata_unavailable",))
        items: list[IntentEnvelope] = []
        errors: list[str] = []
        seen: set[str] = set()
        for header in headers:
            if not isinstance(header, MarkedItem):
                errors.append("invalid_metadata")
                continue
            if self.tag not in {item.lstrip("#") for item in header.tags}:
                continue
            if not header.external_id or header.external_id in seen:
                errors.append("duplicate_or_empty_id")
                continue
            seen.add(header.external_id)
            try:
                raw = self.read_marked(header.external_id)
                if not isinstance(raw, str):
                    raise TypeError("body_must_be_text")
                size = len(raw.encode("utf-8", errors="strict"))
                if size > self.max_bytes or not raw.strip():
                    raise ValueError("intent_empty_or_too_large")
                items.append(IntentEnvelope(
                    source_id=self.source_id, external_id=header.external_id,
                    revision=revision_for(raw, self.authority, header.revision),
                    raw_intent=raw, authority=dict(self.authority),
                    timestamp=header.timestamp,
                ))
            except Exception:
                errors.append(f"{header.external_id}:body_unavailable_or_invalid")
        return PollResult(self.source_id, tuple(items), not errors, tuple(errors))


class ObsidianIntentAdapter(_MarkedRemoteAdapter):
    """Inject a provider that lists *tagged metadata*, not all vault contents."""

    def __init__(
        self, vault_id: str, *,
        list_marked: Callable[[str], Sequence[MarkedItem]] | None = None,
        read_marked: Callable[[str], str] | None = None,
        tag: str = "owner-intent", authority: Mapping[str, Any] | None = None,
        max_bytes: int = 1024 * 1024,
    ) -> None:
        if not vault_id:
            raise ValueError("vault_id required")
        super().__init__(f"obsidian:{vault_id}", list_marked=list_marked,
                         read_marked=read_marked, tag=tag, authority=authority,
                         max_bytes=max_bytes)


class TickTickIntentAdapter(_MarkedRemoteAdapter):
    """Closed by default; BOTH operator enablement and owner grant are mandatory."""

    def __init__(
        self, account_id: str, *,
        list_marked: Callable[[str], Sequence[MarkedItem]] | None = None,
        read_marked: Callable[[str], str] | None = None,
        enabled: bool = False, owner_granted: bool = False,
        tag: str = "owner-intent", authority: Mapping[str, Any] | None = None,
        max_bytes: int = 1024 * 1024,
    ) -> None:
        if not account_id:
            raise ValueError("account_id required")
        self.owner_granted = owner_granted
        super().__init__(f"ticktick:{account_id}", list_marked=list_marked,
                         read_marked=read_marked, tag=tag, authority=authority,
                         max_bytes=max_bytes, enabled=enabled and owner_granted)

    def poll(self) -> PollResult:
        if not self.owner_granted or not self.enabled:
            return PollResult(self.source_id, complete=False, errors=("owner_access_not_granted",))
        return super().poll()
