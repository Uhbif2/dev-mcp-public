"""Read-only fleet deadlock diagnosis.

All edges come from registered tasks and durable gate/integrate/obstacle events.
This module never thaws claims or changes the review gate: integration belongs
to the owners of queue/controller/shadow.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from .model import norm_path, paths_overlap, task_root

_OPEN = frozenset({"draft", "ready", "taken", "submitted", "frozen"})


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _list(raw: Any) -> tuple[str, ...]:
    try:
        value = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ()
    return tuple(x for x in value if isinstance(x, str)) if isinstance(value, list) else ()


def _snapshot(tx: Any) -> tuple[dict[tuple[str, str], dict], dict[str, str], dict[str, dict], list[dict], list[dict]]:
    tasks = {(str(r["project"]), str(r["tz"])): dict(r) for r in tx.query(
        "SELECT project,tz,kind,state,owns,body,title FROM tasks")}
    roots = {str(r["name"]): str(r["root"]) for r in tx.query("SELECT name,root FROM projects")}
    gates: dict[str, dict] = {}
    integrations: dict[tuple[str, str], dict] = {}
    events = [dict(r) for r in tx.query(
        "SELECT id,project,tz,kind,rc,payload FROM events "
        "WHERE kind IN ('gate','styk_open','integrate','write') ORDER BY id DESC")]
    for row in events:
        project = str(row.get("project") or "")
        if row["kind"] == "gate" and project not in gates:
            gates[project] = row
        elif row["kind"] == "integrate":
            key = (project, str(row.get("tz") or ""))
            if key not in integrations:
                integrations[key] = row
    return tasks, roots, gates, events, list(integrations.values())


def _zone(tx: Any, project: str, tasks: Mapping[tuple[str, str], dict],
          roots: Mapping[str, str], gates: Mapping[str, dict]) -> tuple[str, ...]:
    gate = gates.get(project)
    if not gate or not gate.get("rc"):
        return ()
    root = roots.get(project, "")
    paths: set[str] = set()
    for rel in _list(_payload(gate["payload"]).get("failed")):
        try:
            paths.add(norm_path(rel, project_root=root))
        except (ValueError, OSError):
            continue
    for (proj, tz), task in tasks.items():
        if proj == project and tz.startswith("styk:") and task["state"] in _OPEN:
            paths.update(_list(task["owns"]))
    # A project-root claim in a historical styk is too broad for a gate.
    return tuple(sorted(p for p in paths if p.replace("\\", "/").rstrip("/").casefold()
                        != root.replace("\\", "/").rstrip("/").casefold()))


def _review_target(row: Mapping[str, Any]) -> tuple[str, str] | None:
    from .review import _review_target as parse
    try:
        return parse(str(row["tz"]), str(row.get("body") or ""), str(row["project"]))
    except ValueError:
        return None


def _resolvers(tasks: Mapping[tuple[str, str], dict], integrations: list[dict],
               tx: Any) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for e in integrations:
        info = _payload(e["payload"])
        if not info.get("deferred"):
            continue
        project = str(e["project"] or "")
        for tz in _list(info.get("waiting_for")):
            if (project, tz) in tasks:
                result.setdefault(project, set()).add(tz)
    # Typed obstacles may exist in databases upgraded beyond the base schema.
    present = tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='obstacles'")
    if present:
        for row in tx.query(
            "SELECT o.resolver_work,r.project FROM obstacles o JOIN outcomes r "
            "ON r.id=o.outcome_id WHERE o.state='open' AND r.state='open'"):
            project = str(row["project"])
            ref = str(row["resolver_work"] or "")
            prefix = project + "/"
            if ref.startswith(prefix):
                tz = ref[len(prefix):]
                if (project, tz) in tasks:
                    result.setdefault(project, set()).add(tz)
    return result


def wait_graph(tx: Any) -> dict[str, set[str]]:
    """Graph of *durable* waits; cycles indicate a stalled feedback path.

    Node prefixes are review:, trunk:, styk:, integrate:, work:.
    Do not manufacture a resolver from task prose or a failed acceptance.
    """
    tasks, roots, gates, events, integrations = _snapshot(tx)
    red = {p: _zone(tx, p, tasks, roots, gates) for p in gates}
    resolvers = _resolvers(tasks, integrations, tx)
    graph: dict[str, set[str]] = {}

    def edge(a: str, b: str) -> None:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set())

    reviews: dict[tuple[str, str], str] = {}
    for (project, tz), row in tasks.items():
        if row["kind"] != "review" or row["state"] not in _OPEN:
            continue
        node = f"review:{project}/{tz}"
        graph.setdefault(node, set())
        target = _review_target(row)
        if target is None:
            continue
        reviews[target] = node
        source = tasks.get(target)
        if source is not None and any(
            paths_overlap(owned, failing) for owned in _list(source["owns"])
            for failing in red.get(project, ())
        ):
            edge(node, f"trunk:{project}")

    for (project, tz), row in tasks.items():
        if not tz.startswith("styk:") or row["state"] not in _OPEN or not red.get(project):
            continue
        edge(f"trunk:{project}", f"styk:{project}/{tz}")

    for e in integrations:
        info = _payload(e["payload"])
        project, tz = str(e["project"] or ""), str(e["tz"] or "")
        if not red.get(project) or not info.get("deferred"):
            continue
        inode = f"integrate:{project}/{tz}"
        # A deferred integrate is evidence for the *project's* red styk;
        # waiting_for is the authoritative link, never guessed from text.
        for (proj, styk_tz), task in tasks.items():
            if proj == project and styk_tz.startswith("styk:") and task["state"] in _OPEN:
                edge(f"styk:{project}/{styk_tz}", inode)
        for dep in _list(info.get("waiting_for")):
            if (project, dep) in tasks:
                edge(inode, f"work:{project}/{dep}")

    for project, deps in resolvers.items():
        for dep in deps:
            work = f"work:{project}/{dep}"
            graph.setdefault(work, set())
            if (project, dep) in reviews:
                edge(work, reviews[(project, dep)])
            # A structured outcome obstacle may supply the resolver without
            # a matching integrate event; still express this gate wait.
            for (proj, styk_tz), row in tasks.items():
                if proj == project and styk_tz.startswith("styk:") and row["state"] in _OPEN and red.get(project):
                    edge(f"styk:{project}/{styk_tz}", work)
    return graph


def cycles(graph: Mapping[str, set[str]]) -> list[tuple[str, ...]]:
    """Stable strongly connected components, excluding acyclic singletons."""
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    active: set[str] = set()
    found: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in indices:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in active:
                low[node] = min(low[node], indices[nxt])
        if low[node] == indices[node]:
            members: list[str] = []
            while True:
                member = stack.pop()
                active.remove(member)
                members.append(member)
                if member == node:
                    break
            if len(members) > 1 or node in graph.get(node, ()):
                found.append(tuple(sorted(members)))

    for node in sorted(set(graph) | {n for values in graph.values() for n in values}):
        if node not in indices:
            visit(node)
    return sorted(found)


def exempt_reviews(tx: Any) -> set[tuple[str, str]]:
    """(project, review-tz) that must not wait for the red project styk.

    Two independent proofs suffice: the reviewed work is a recorded resolver,
    or its actual write-diff avoids the failing files. Without a write event,
    fall back to owns (fail closed on uncertain diff).
    """
    tasks, roots, gates, events, integrations = _snapshot(tx)
    resolvers = _resolvers(tasks, integrations, tx)
    zones = {p: _zone(tx, p, tasks, roots, gates) for p in gates}
    failures: dict[str, tuple[str, ...]] = {}
    for project, gate in gates.items():
        root = roots.get(project, "")
        paths = []
        for rel in _list(_payload(gate["payload"]).get("failed")):
            try:
                paths.append(norm_path(rel, project_root=root))
            except (OSError, ValueError):
                pass
        failures[project] = tuple(paths)
    writes: dict[tuple[str, str], set[str]] = {}
    for row in tx.query(
        "SELECT project,tz,path_norm FROM events WHERE kind='write' AND path_norm<>''"):
        writes.setdefault((str(row["project"]), str(row["tz"])), set()).add(str(row["path_norm"]))
    exempt: set[tuple[str, str]] = set()
    for (project, tz), task in tasks.items():
        if task["kind"] != "review" or task["state"] not in _OPEN or not zones.get(project):
            continue
        target = _review_target(task)
        if target is None:
            continue
        origin = tasks.get(target)
        if origin is None or origin["state"] not in _OPEN:
            continue
        owns = _list(origin["owns"])
        if not any(paths_overlap(a, b) for a in owns for b in zones[project]):
            continue  # queue already admits this review
        if target[1] in resolvers.get(project, ()):
            exempt.add((project, tz))
            continue
        changed = writes.get(target) or set(owns)
        if failures.get(project) and not any(
            paths_overlap(a, b) for a in changed for b in failures[project]
        ):
            exempt.add((project, tz))
    return exempt


@dataclass(frozen=True)
class Recovery:
    stale_styks: tuple[tuple[str, str], ...]
    repair_projects: tuple[str, ...]


def recovery_plan(tx: Any) -> Recovery:
    """Shadow-only advice: stale styk closure and one repair per stuck project.

    An obsolete styk requires a newer green gate, or both a changed recorded
    revision and a disjoint set of failures. No git/HEAD inference is made.
    """
    tasks, roots, gates, events, integrations = _snapshot(tx)
    resolvers = _resolvers(tasks, integrations, tx)
    exempt = exempt_reviews(tx)
    opened = {(str(e["project"]), str(e["tz"])): e for e in events if e["kind"] == "styk_open"}
    stale: set[tuple[str, str]] = set()
    repair: set[str] = set()
    for (project, tz), row in tasks.items():
        if not tz.startswith("styk:") or row["state"] not in _OPEN:
            continue
        gate = gates.get(project)
        opened_at = opened.get((project, tz))
        if not gate or not opened_at or int(gate["id"]) <= int(opened_at["id"]):
            continue
        if gate["rc"] == 0:
            stale.add((project, tz))
            continue
        earlier = next((e for e in events if e["kind"] == "gate" and e["project"] == project
                        and int(e["id"]) <= int(opened_at["id"])), None)
        old_commits = _list(_payload(earlier["payload"]).get("commits")) if earlier else ()
        new_commits = _list(_payload(gate["payload"]).get("commits"))
        old_failed = set(_list(_payload(earlier["payload"]).get("failed"))) if earlier else set()
        new_failed = set(_list(_payload(gate["payload"]).get("failed")))
        if old_commits and new_commits and old_commits[-1] != new_commits[-1] and old_failed and old_failed.isdisjoint(new_failed):
            stale.add((project, tz))
    for (project, tz), row in tasks.items():
        if not tz.startswith("styk:") or row["state"] not in _OPEN or (project, tz) in stale:
            continue
        # If no resolver can be reviewed independently, no runnable escape
        # exists; report a single repair request per project, not per styk.
        if resolvers.get(project) or any(p == project for p, _ in exempt):
            continue
        if any(p == project and r["state"] in _OPEN
               and (t == "TZ-53" or "[fleet-trunk-repair]" in str(r.get("title") or ""))
               for (p, t), r in tasks.items()):
            continue
        repair.add(project)
    return Recovery(tuple(sorted(stale)), tuple(sorted(repair)))
