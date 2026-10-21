"""Versioned outcome-tree templates, independent of the execution pipeline."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from .sources.model import IntentEnvelope

StageName = Literal[
    "understood", "executable_goal", "approach_selected", "executable_plan",
    "implemented", "clean_start", "quality", "delivered", "handed_over",
    "hypotheses_plan", "experiments", "verified", "report",
    "rollback_plan", "owner_approved", "executed",
]
STAGES: tuple[StageName, ...] = (
    "understood", "executable_goal", "approach_selected", "executable_plan",
    "implemented", "clean_start", "quality", "delivered", "handed_over",
)
SIZES: dict[str, tuple[StageName, ...]] = {
    "small": ("understood", "executable_goal", "implemented", "delivered", "handed_over"),
    "medium": ("understood", "executable_goal", "approach_selected", "executable_plan",
               "implemented", "delivered", "handed_over"),
    "large": STAGES,
}
KINDS = ("build", "change", "investigate", "ops")
KIND_STAGES: dict[str, tuple[str, ...]] = {
    "change": ("understood", "executable_goal", "approach_selected",
               "executable_plan", "implemented", "quality", "delivered", "handed_over"),
    "investigate": ("understood", "hypotheses_plan", "experiments", "verified",
                    "report", "handed_over"),
    "ops": ("understood", "rollback_plan", "owner_approved", "executed",
            "verified", "handed_over"),
}


# 17.10.2026 (владелец): размер решается на стадии understood для любого вида, а не только
# build — мелкая правка не должна идти девятью стадиями с тройками. ops всегда полный:
# откат и согласие владельца не сокращаются.
KIND_SIZES: dict[str, dict[str, tuple[str, ...]]] = {
    "change": {"small": ("understood", "implemented", "handed_over"),
               "medium": ("understood", "executable_goal", "implemented", "quality",
                          "handed_over"),
               "large": KIND_STAGES["change"]},
    "investigate": {"small": ("understood", "experiments", "report", "handed_over"),
                    "medium": KIND_STAGES["investigate"],
                    "large": KIND_STAGES["investigate"]},
    "ops": {size: KIND_STAGES["ops"] for size in SIZES},
}


def selected_stages(kind: str, size: str) -> tuple[str, ...]:
    return SIZES[size] if kind == "build" else KIND_SIZES[kind][size]


def stage_dependencies(kind: str, size: str, stage: str) -> tuple[str, ...]:
    chosen = selected_stages(kind, size)
    if kind != "build":
        return (chosen[chosen.index(stage) - 1],) if stage != "understood" else ()
    deps = tuple(dep for dep in PREREQUISITES[stage] if dep in chosen)
    return ("executable_goal",) if stage == "implemented" and "executable_plan" not in chosen else deps
PREREQUISITES: dict[str, tuple[str, ...]] = {
    "understood": (),
    "executable_goal": ("understood",),
    "approach_selected": ("executable_goal",),
    "executable_plan": ("executable_goal", "approach_selected"),
    "implemented": ("executable_goal", "executable_plan"),
    "clean_start": ("implemented",),
    "quality": ("implemented",),
    "delivered": ("implemented", "clean_start", "quality"),
    "handed_over": ("delivered",),
}
PROOFS: dict[str, tuple[str, str, str]] = {
    "understood": ("review", "context-analysis", "source and requirements reconciled"),
    "executable_goal": ("review", "golden-scenarios", "scenario with red baseline exists"),
    "approach_selected": ("review", "tradeoff-decision", "approaches compared on scenarios"),
    "executable_plan": ("review", "preflight-plan", "work ownership and acceptance validated"),
    "implemented": ("run", "server-gate", "server-run test passes against a versioned snapshot"),
    "clean_start": ("run", "fresh-clone-check", "clean environment independently passes"),
    "quality": ("review", "lenses-check", "severity one defects reconciled"),
    "delivered": ("delivery", "deployed-attestation", "target version and rollback verified"),
    "handed_over": ("review", "handoff-receipt", "recipient saw versioned delivered outcome"),
    "hypotheses_plan": ("review", "experiment-plan", "hypotheses and repeatable experiments planned"),
    "experiments": ("run", "experiment-records", "reproducible experiments performed"),
    "verified": ("review", "independent-replication", "independent replication and critique"),
    "report": ("review", "research-report", "claims traced to experiments and confidence"),
    "rollback_plan": ("review", "ops-rollback-plan", "safe execution and rollback specified"),
    "owner_approved": ("external", "owner-action", "owner gave explicit approval"),
    "executed": ("run", "ops-execution", "approved operation performed with receipt"),
}
MAX_INTENT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class Node:
    identifier: str
    stage: str
    parent: str | None
    depends_on: tuple[str, ...]
    spec: dict[str, object]
    evidence_spec: dict[str, object]
    authority: dict[str, object]


@dataclass(frozen=True)
class OutcomeTree:
    project: str
    intent_id: str
    revision: str
    size: str
    root_id: str
    nodes: tuple[Node, ...]
    kind: str = "build"
    target: str = "new"
    triple: bool = False

    def validate(self) -> None:
        if self.kind not in KINDS or (self.kind == "build") != (self.target == "new"):
            raise ValueError("invalid intent route")
        if not self.nodes or self.nodes[0].identifier != self.root_id:
            raise ValueError("outcome tree has no root")
        known = {node.identifier: node for node in self.nodes}
        if len(known) != len(self.nodes):
            raise ValueError("duplicate outcome id")
        if sum(n.parent is None for n in self.nodes) != 1:
            raise ValueError("outcome tree requires one root")
        for node in self.nodes:
            if not node.spec or not node.evidence_spec or not node.authority:
                raise ValueError("outcome lacks spec, evidence or authority")
            if node.parent is not None and node.parent not in known:
                raise ValueError("missing parent")
            if any(dep not in known or dep == node.identifier for dep in node.depends_on):
                raise ValueError("unknown dependency or self-wait")
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(ident: str) -> None:
            if ident in visiting:
                raise ValueError("cycle in outcome tree")
            if ident in visited:
                return
            visiting.add(ident)
            node = known[ident]
            if node.parent is not None:
                visit(node.parent)
            for dependency in node.depends_on:
                visit(dependency)
            visiting.remove(ident)
            visited.add(ident)
        for ident in known:
            visit(ident)
        required = ("understood", "executable_goal", "implemented") if self.kind == "build" else (
            "understood", "handed_over")
        for stage in required:
            if stage not in {node.stage for node in self.nodes}:
                raise ValueError("missing non-negotiable pre-code stage")


def build_template(envelope: IntentEnvelope, *, project: str,
                   size: str, kind: str = "build", target: str = "new",
                   triple: bool = False) -> OutcomeTree:
    if size not in SIZES:
        raise ValueError("unknown intent size")
    if kind not in KINDS:
        raise ValueError("unknown intent kind")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("empty intent target")
    if (kind == "build") != (target == "new"):
        raise ValueError("build requires target=new; other kinds require existing target")
    if not isinstance(envelope.raw_intent, str) or not envelope.raw_intent.strip():
        raise ValueError("empty intent")
    raw = envelope.raw_intent.encode("utf-8")
    if len(raw) > MAX_INTENT_BYTES:
        raise ValueError("intent too large")
    key = json.dumps([envelope.source_id, envelope.external_id, envelope.revision,
                      *([kind, target] if kind != "build" else []),
                      # не-large размер даёт новое дерево рядом со старым (перестройка
                      # после understood); large сохраняет прежние id.
                      *([size] if size != "large" else [])],
                     separators=(",", ":"), ensure_ascii=False)
    root_id = "intent-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    body_digest = hashlib.sha256(raw).hexdigest()
    base = {
        "project": project,
        "source_id": envelope.source_id,
        "external_id": envelope.external_id,
        "revision": envelope.revision,
        "raw_intent_sha256": body_digest,
        "size": size,
        "kind": kind,
        "target": target,
        "triple": triple,
    }
    # Only the source adapter supplies trusted authority. Raw intent is DATA;
    # it is never parsed to obtain permission or force a done status.
    trusted = dict(envelope.authority)
    if not trusted:
        raise ValueError("explicit authority is required")
    root = Node(root_id, "root", None, (),
                {**base, "goal_sha256": body_digest,
                 "intent_ref": f"{envelope.source_id}:{envelope.external_id}"},
                {"kind": "review", "artifact": "tree-proof",
                 "required": "every required leaf independently proven"},
                trusted)
    chosen = selected_stages(kind, size)
    nodes = [root]
    for stage in chosen:
        proof_kind, artifact, requirement = PROOFS[stage]
        dep_stages = stage_dependencies(kind, size, stage)
        nodes.append(Node(
            f"{root_id}:{stage}", stage, root_id,
            tuple(f"{root_id}:{dep}" for dep in dep_stages),
            {**base, "stage": stage, "required": requirement,
             "depends_on": [f"{root_id}:{dep}" for dep in dep_stages]},
            {"kind": proof_kind, "artifact": artifact, "required": requirement,
             "subject_version": envelope.revision},
            trusted.copy(),
        ))
    tree = OutcomeTree(project, root_id, envelope.revision, size, root_id, tuple(nodes),
                       kind, target, triple)
    tree.validate()
    return tree
