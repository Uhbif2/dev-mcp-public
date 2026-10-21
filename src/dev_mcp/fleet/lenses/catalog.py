"""Intent-oriented, explicitly evidenced quality lens catalog.

A lens is complete only after two independent clean observations. Visual lenses
need actual pixels, an Operator capture proof and Gemini analysis.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    scenario: str
    kind: str
    criterion: str
    count: int = 1


@dataclass(frozen=True)
class Lens:
    key: str
    purpose: str
    cases: tuple[Case, ...]


CATALOG: dict[str, Lens] = {
    "visual_motion": Lens(
        "visual_motion", "Composition, legibility, motion and state transitions against intent",
        (Case("home", "screen", "Visual hierarchy and readable real content"),
         Case("transition", "screen", "Motion continuity, no jank or overlay collisions", 3)),
    ),
    "error_empty": Lens(
        "error_empty", "Failure and empty screens are meaningful and recoverable",
        (Case("error", "screen", "Error message, accessible recovery control"),
         Case("empty", "screen", "Empty state guidance, no fake success")),
    ),
    "first_run": Lens(
        "first_run", "First launch works without manual owner walkthrough",
        (Case("onboarding", "screen", "First-run flow and clear primary action", 2),),
    ),
    "performance": Lens(
        "performance", "Measured latency and resource budgets on a versioned build",
        (Case("startup_perf", "measure", "Measured launch budget from trusted profiler"),),
    ),
    "security": Lens(
        "security", "Negative permission paths and access boundaries tested",
        (Case("denied_access", "run", "Unauthorized access must be rejected"),),
    ),
    "documentation": Lens(
        "documentation", "Instructions independently reproduce the advertised path",
        (Case("getting_started", "review", "Clean-environment documented path verified"),),
    ),
    "recovery": Lens(
        "recovery", "A failed state can be restored and re-verified",
        (Case("restore", "run", "Recovery drill from a controlled failure"),),
    ),
}

VISUAL = frozenset(k for k, v in CATALOG.items() if any(c.kind == "screen" for c in v.cases))


def catalog() -> tuple[Lens, ...]:
    """Return the stable quality dimensions, not a user-selected subset of criteria."""
    return tuple(CATALOG.values())
