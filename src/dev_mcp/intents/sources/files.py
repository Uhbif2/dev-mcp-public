"""Single-directory, read-only file source. No recursion or symlink traversal."""

from __future__ import annotations

from dev_mcp import machine

from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from .model import IntentEnvelope, PollResult, revision_for

DEFAULT_ROOT = machine.INTENT_INBOX
DEFAULT_SOURCE_ID = "file:" + machine.INTENT_INBOX.as_posix()
SUPPORTED_SUFFIXES = frozenset({"", ".txt", ".md", ".intent"})


class FileIntentAdapter:
    def __init__(
        self, root: Path = DEFAULT_ROOT, *, source_id: str | None = None,
        authority: Mapping[str, Any] | None = None, max_bytes: int = 1024 * 1024,
    ) -> None:
        root_path = Path(root)
        is_default_root = (
            os.path.normcase(str(root_path.resolve(strict=False)))
            == os.path.normcase(str(DEFAULT_ROOT.resolve(strict=False)))
        )
        if source_id is None:
            if not is_default_root:
                raise ValueError("source_id is required for custom file source root")
            source_id = DEFAULT_SOURCE_ID
        if not source_id or max_bytes < 1:
            raise ValueError("invalid source configuration")
        if not is_default_root and source_id == DEFAULT_SOURCE_ID:
            raise ValueError("source_id for a custom root cannot be the default source_id")
        self.root = root_path
        self.source_id = source_id
        self.authority = dict(authority or {})
        self.max_bytes = max_bytes

    def poll(self) -> PollResult:
        try:
            if self.root.is_symlink() or not self.root.is_dir():
                return PollResult(self.source_id, complete=False, errors=("source_directory_unavailable",))
            files = sorted(self.root.iterdir(), key=lambda p: p.name)
        except OSError:
            return PollResult(self.source_id, complete=False, errors=("source_directory_unavailable",))

        items: list[IntentEnvelope] = []
        errors: list[str] = []
        for path in files:
            if path.name.startswith(".") or path.name.endswith(("~", ".tmp", ".swp")):
                continue
            try:
                entry_stat = path.lstat()  # permission/stat errors must fail closed
                if stat.S_ISLNK(entry_stat.st_mode) or path.is_symlink():
                    errors.append(f"{path.name}:symlink_disallowed")
                    continue
                if path.suffix.lower() not in SUPPORTED_SUFFIXES or not stat.S_ISREG(entry_stat.st_mode):
                    continue
                before = path.stat()
                if before.st_size > self.max_bytes:
                    raise ValueError("intent_too_large")
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                # O_NOFOLLOW blocks rename-to-symlink races on platforms that support it.
                with os.fdopen(os.open(path, flags), "rb") as reader:
                    raw = reader.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise ValueError("intent_too_large")
                after = path.stat()
                if (before.st_mtime_ns, before.st_size, before.st_ino, before.st_dev) != (
                    after.st_mtime_ns, after.st_size, after.st_ino, after.st_dev,
                ):
                    raise ValueError("file_changed_during_poll")
                content = raw.decode("utf-8", errors="strict")
                if not content.strip():
                    raise ValueError("empty_intent")
                items.append(IntentEnvelope(
                    source_id=self.source_id, external_id=path.name,
                    revision=revision_for(content, self.authority), raw_intent=content,
                    authority=dict(self.authority),
                    timestamp=datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat(),
                ))
            except (OSError, UnicodeError, ValueError) as exc:
                errors.append(f"{path.name}:{type(exc).__name__}")
        return PollResult(self.source_id, tuple(items), not errors, tuple(errors))
