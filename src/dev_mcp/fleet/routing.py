"""Opt-in work-lane planner. Pure decisions; live queue integration is separate."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .model import _overlap_segments, paths_overlap


# Admission control for the shared platform spine is distinct from take-time
# lane selection. Only a task registrar (not this pure module) can hold ready.
SPINE_FILES = frozenset({
    "controller.py", "tools.py", "integrate.py", "server.py",
    "queue.py", "review.py",
})


@dataclass(frozen=True)
class Lane:
    name: str
    project: str
    tzs: tuple[str, ...]


@dataclass(frozen=True)
class LaneState:
    lane: Lane
    ready: tuple[Any, ...] = ()
    leader: str | None = None
    leader_alive: bool = True


@dataclass(frozen=True)
class RoutingObservation:
    actual: str | None
    proposed: str | None
    would_change: bool


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _many(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return ()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(part) for part in raw if isinstance(part, str) and part)


def _paths(task: Any) -> tuple[str, ...]:
    return tuple(path.replace("\\", "/").casefold().rstrip("/")
                 for path in _many(_field(task, "owns")))


def _domain(path: str) -> str:
    """Conservative code-domain heuristic; ignore generic docs and tests."""
    parts = path.replace("\\", "/").casefold().removesuffix("/**").split("/")
    for marker in ("src", "app", "ops", "deploy", "backend", "frontend", "product"):
        if marker not in parts:
            continue
        index = parts.index(marker)
        tail = [x for x in parts[index:] if x]
        if not tail:
            continue
        width = 3 if marker == "src" and len(tail) >= 3 else 2
        return "/".join(tail[:width])
    return ""


def _domains(task: Any) -> frozenset[str]:
    explicit = _field(task, "subsystem")
    if isinstance(explicit, str) and explicit.strip():
        return frozenset({explicit.strip().casefold()})
    return frozenset(domain for path in _paths(task) if (domain := _domain(path)))


def lanes(tasks: Iterable[Any]) -> list[Lane]:
    """Stable connected components per project, by owned paths or local needs.

    A direct need connects only within the same code subsystem; task order
    and historical review claims never influence the component graph.
    """
    items: dict[tuple[str, str], Any] = {}
    for task in tasks:
        project = str(_field(task, "project") or "")
        tz = str(_field(task, "tz") or "")
        if not project or not tz:
            raise ValueError("every task needs a project and tz")
        kind = str(_field(task, "kind", "work") or "work").casefold()
        if kind in ("review", "triage") or tz.startswith("review:"):
            continue
        key = (project, tz)
        if key in items:
            raise ValueError(f"duplicate task: {project}/{tz}")
        items[key] = task

    keys = sorted(items, key=lambda key: (key[0].casefold(), key[1].casefold(), key))
    parent = {key: key for key in keys}

    def find(key: tuple[str, str]) -> tuple[str, str]:
        while key != parent[key]:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    owns = {key: _paths(items[key]) for key in keys}
    needs = {key: set(_many(_field(items[key], "needs"))) for key in keys}
    domains = {key: _domains(items[key]) for key in keys}
    # Segment-prefix overlaps form intervals in sorted path order. One ancestor
    # representative joins all descendants; equal paths join through that same
    # representative. This preserves the pairwise graph's components without
    # comparing thousands of historical tasks under the registry writer.
    entries = sorted((key[0], tuple(_overlap_segments(path)), key)
                     for key in keys for path in owns[key])
    stack: list[tuple[tuple[str, ...], tuple[str, str]]] = []
    previous_project = None
    for project, segments, key in entries:
        if project != previous_project:
            stack.clear()
            previous_project = project
        while stack and segments[:len(stack[-1][0])] != stack[-1][0]:
            stack.pop()
        if stack:
            union(stack[-1][1], key)
        stack.append((segments, key))
    # Dependencies are sparse edges; cross-project and cross-domain references
    # retain the original exclusion rules.
    for left in keys:
        for tz in needs[left]:
            right = (left[0], tz)
            if right in items and domains[left] & domains[right]:
                union(left, right)

    clusters: dict[tuple[str, str], list[str]] = {}
    for key in keys:
        clusters.setdefault(find(key), []).append(key[1])
    result = []
    for root, tzs in clusters.items():
        ordered = tuple(sorted(tzs, key=lambda tz: (_tz_order(tz), tz.casefold(), tz)))
        result.append(Lane(f"{root[0]}/{ordered[0]}", root[0], ordered))
    return sorted(result, key=lambda lane: (
        lane.project.casefold(), _tz_order(lane.tzs[0]), lane.name.casefold()
    ))


def _ready_tz(task: Any) -> str | None:
    if isinstance(task, str):
        tz, kind = task, "work"
    else:
        tz = str(_field(task, "tz") or "")
        kind = str(_field(task, "kind", "work") or "work")
    if not tz or kind.casefold() in ("review", "triage") or tz.startswith("review:"):
        return None
    return tz


def _first(state: LaneState) -> str | None:
    allowed = set(state.lane.tzs)
    return next((tz for task in state.ready
                 if (tz := _ready_tz(task)) is not None and tz in allowed), None)


def pick_for(worker: str, lane_state: Iterable[LaneState] | LaneState) -> str | None:
    """Warm leader first; never steal an active foreign lane; no side effects."""
    if not worker:
        return None
    states = (lane_state,) if isinstance(lane_state, LaneState) else tuple(lane_state)
    active_own = [state for state in states if state.leader == worker and state.leader_alive]
    unassigned = [state for state in states if not state.leader or not state.leader_alive]
    for state in (*active_own, *unassigned):
        if (tz := _first(state)) is not None:
            return tz
    return None


def shadow_pick(worker: str, states: Iterable[LaneState], actual: str | None) -> RoutingObservation:
    """Record a would-change observation without changing the queue result."""
    proposed = pick_for(worker, states)
    return RoutingObservation(actual=actual, proposed=proposed, would_change=actual != proposed)


def _tz_order(tz: str) -> tuple[tuple[int, Any], ...]:
    """Natural deterministic order for fallback when creation dates are absent."""
    import re
    return tuple((0, int(bit)) if bit.isdecimal() else (1, bit.casefold())
                 for bit in re.split(r"(\d+)", tz))


def _preceded_by(other: Any, candidate: Any) -> bool:
    before = str(_field(other, "created_at") or "")
    after = str(_field(candidate, "created_at") or "")
    if before and after and before != after:
        return before < after
    return _tz_order(str(_field(other, "tz") or "")) < _tz_order(
        str(_field(candidate, "tz") or "")
    )


def _shared_spine(first: Any, second: Any) -> bool:
    """True for a physically overlapping hot file, including parent globs."""
    for a in _paths(first):
        for b in _paths(second):
            if not paths_overlap(a, b):
                continue
            hot = (a.removesuffix("/**").rsplit("/", 1)[-1] in SPINE_FILES or
                   b.removesuffix("/**").rsplit("/", 1)[-1] in SPINE_FILES)
            # Both broad scopes can contain hot source files even if neither
            # explicitly names one. Restrict this case to the source tree.
            broad = (a.endswith("/**") and b.endswith("/**") and
                     ("/src/" in a or a.startswith("src/")) and
                     ("/src/" in b or b.startswith("src/")))
            if hot or broad:
                return True
    return False


def spine_blockers(candidate: Any, registered: Iterable[Any]) -> tuple[str, ...]:
    """Earlier unaccepted writers of the same spine file in this project.

    The registrar must NOT promote a candidate to ready while this result is
    nonempty. Checking only at fleet_take cannot prevent competing ready work.
    `created_at` establishes first writer if present; fallback is natural TZ
    order. Reviews never occupy writing lanes or admission slots.
    """
    project = str(_field(candidate, "project") or "")
    tz = str(_field(candidate, "tz") or "")
    kind = str(_field(candidate, "kind") or "work")
    if kind in {"review", "cross_review", "triage"} or tz.startswith("review:"):
        return ()
    if str(_field(candidate, "state") or "") == "accepted":
        return ()
    blockers = set()
    for other in registered:
        if str(_field(other, "project") or "") != project:
            continue
        other_tz = str(_field(other, "tz") or "")
        if other_tz == tz or not other_tz:
            continue
        if str(_field(other, "state") or "") == "accepted":
            continue
        other_kind = str(_field(other, "kind") or "work")
        if other_kind in {"review", "cross_review", "triage"} or other_tz.startswith("review:"):
            continue
        if _preceded_by(other, candidate) and _shared_spine(candidate, other):
            blockers.add(other_tz)
    return tuple(sorted(blockers, key=_tz_order))
