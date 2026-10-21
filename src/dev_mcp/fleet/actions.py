"""Durable effect receipts: commit with the mutation, before any handoff."""
from __future__ import annotations

from typing import Any


def record_receipt(tx: Any, worker_id: str, claim_id: int,
                   receipt: dict[str, str] | None) -> None:
    if receipt is None:
        return
    tx.record_event(kind="claim_action_result", worker_id=worker_id,
                    payload={"claim_id": claim_id, **receipt})
