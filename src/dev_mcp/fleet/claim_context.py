"""Trusted, durable identity for a fleet claim's checkout.

The queue records this value in the *same* transaction as its take event.
Consumers must resolve it by worker and claim id, never accept one supplied
by an agent as authority to choose a different filesystem root.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping


class ClaimRoutingError(RuntimeError):
    """Unsafe or unavailable claim checkout; never fall back to the trunk."""


@dataclass(frozen=True)
class ClaimContext:
    claim_id: int
    worker_id: str
    project: str
    tz: str
    project_root: Path
    tree_path: Path
    base_sha: str
    branch: str
    mode: Literal["shared", "isolated"]

    def as_event(self) -> dict[str, str | int]:
        """Serialize only canonical trusted values to durable take-event payload."""
        return {
            "claim_id": self.claim_id,
            "worker_id": self.worker_id,
            "project": self.project,
            "tz": self.tz,
            "project_root": str(self.project_root),
            "tree_path": str(self.tree_path),
            "base_sha": self.base_sha,
            "branch": self.branch,
            "mode": self.mode,
        }

    @classmethod
    def from_event(cls, data: Mapping[str, object]) -> "ClaimContext":
        try:
            mode = str(data["mode"])
            if mode not in {"isolated", "shared"}:
                raise ValueError("invalid checkout mode")
            claim_id = int(data["claim_id"])
            worker_id = str(data["worker_id"])
            project = str(data["project"])
            tz = str(data["tz"])
            root = Path(str(data["project_root"])).resolve(strict=True)
            tree = Path(str(data["tree_path"])).resolve(strict=True)
            sha = str(data["base_sha"])
            branch = str(data["branch"])
            if claim_id <= 0 or not worker_id or not project or not tz:
                raise ValueError("incomplete claim identity")
            if mode == "shared" and tree != root:
                raise ValueError("shared checkout points away from project")
            if mode == "isolated" and (len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha)):
                raise ValueError("invalid pinned commit SHA")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise ClaimRoutingError(f"invalid durable claim context: {exc}") from exc
        return cls(claim_id, worker_id, project, tz, root, tree, sha, branch, mode)

