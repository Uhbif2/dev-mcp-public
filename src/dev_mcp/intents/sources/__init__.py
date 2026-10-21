"""Explicit, opt-in intent-source adapters and durable revision reconciliation."""

from .files import FileIntentAdapter
from .model import IntentEnvelope, PollResult, SourceAdapter, revision_for
from .registry import IntentChange, IntentRegistry, ReconcileResult
from .remote import MarkedItem, ObsidianIntentAdapter, TickTickIntentAdapter

__all__ = [
    "FileIntentAdapter", "IntentChange", "IntentEnvelope", "IntentRegistry",
    "MarkedItem", "ObsidianIntentAdapter", "PollResult", "ReconcileResult",
    "SourceAdapter", "TickTickIntentAdapter", "revision_for",
]
