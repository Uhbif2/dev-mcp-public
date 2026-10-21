"""Server-authoritative gallery for *taste*, not a product-quality approval gate.

An owner may select between already verified visual proofs. Unverified images,
non-taste obstacles, stale outcome versions, and concurrent duplicate choices
fail closed. Nothing in this module reads the private owner model.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store
from .decision_book import DecisionBook, Scope, SourceRef

_ID = re.compile(r"^[A-Za-z0-9_.:/#-]{1,256}$")
_MAX_VARIANTS = 6
_FRESH_UNTIL = "2099-12-31T23:59:59+00:00"


class TasteError(ValueError):
    """A proof, authorization, version, or concurrent-choice failure."""


@dataclass(frozen=True)
class TasteOption:
    option_id: str
    label: str
    image_evidence_id: str

    def validate(self) -> None:
        if not _ID.fullmatch(self.option_id):
            raise TasteError("option_id must be an opaque identifier")
        if not isinstance(self.label, str) or not self.label.strip() or len(self.label) > 90:
            raise TasteError("a bounded display label is required")
        if any(ord(char) < 32 for char in self.label):
            raise TasteError("control characters are not allowed")
        if not _ID.fullmatch(self.image_evidence_id):
            raise TasteError("a versioned screenshot evidence id is required")


def _install(store: Store) -> None:
    outcome.install_schema(store)
    # Record Decision in the same SQLite transaction as the selection, so make
    # sure the shared owner tables are present before acquiring the transaction.
    DecisionBook(store)
    with store.tx() as tx:
        tx.change(
            """CREATE TABLE IF NOT EXISTS owner_taste_galleries (
                outcome_id TEXT PRIMARY KEY REFERENCES outcomes(id),
                subject_version TEXT NOT NULL,
                revision INTEGER NOT NULL,
                choice_id TEXT,
                decision_id TEXT,
                updated_at TEXT NOT NULL
            )""", event="owner.taste_schema",
        )
        tx.change(
            """CREATE TABLE IF NOT EXISTS owner_taste_options (
                outcome_id TEXT NOT NULL REFERENCES owner_taste_galleries(outcome_id),
                option_id TEXT NOT NULL,
                label TEXT NOT NULL,
                image_evidence_id TEXT NOT NULL REFERENCES evidence(id),
                PRIMARY KEY(outcome_id,option_id)
            )""", event="owner.taste_schema",
        )


def _subject(row: Any) -> str:
    try:
        parsed = json.loads(row["evidence_spec"])
    except (TypeError, ValueError):
        parsed = {}
    value = parsed.get("subject_version") if isinstance(parsed, dict) else None
    if isinstance(value, str) and _ID.fullmatch(value):
        return value
    return f"rev:{row['rev']}"


def _outcome(tx: Any, oid: str) -> Any:
    row = tx.one("SELECT * FROM outcomes WHERE id=?", (oid,))
    if row is None:
        raise TasteError("result not found")
    if row["state"] != "open":
        raise TasteError("result is not open")
    if not tx.scalar(
        "SELECT COUNT(*) FROM obstacles WHERE outcome_id=? AND state='open' "
        "AND type='owner_action' AND cause_code='owner_taste'", (oid,),
    ):
        raise TasteError("only owner_taste obstacles may offer a gallery")
    return row


def _proof(tx: Any, oid: str, subject_version: str, evidence_id: str) -> bool:
    row = tx.one(
        "SELECT kind,verdict,subject_version,env_version FROM evidence "
        "WHERE id=? AND outcome_id=?", (evidence_id, oid),
    )
    return bool(row and row["kind"] == "screen" and row["verdict"] == "pass"
                and row["subject_version"] == subject_version and row["env_version"])


def publish_gallery(store: Store, *, outcome_id: str, options: Sequence[TasteOption],
                    expected_subject_version: str) -> int:
    """Trusted server publisher; only own-proven, version-matching options pass."""
    if not isinstance(outcome_id, str) or not _ID.fullmatch(outcome_id):
        raise TasteError("invalid result id")
    if not isinstance(expected_subject_version, str) or not _ID.fullmatch(expected_subject_version):
        raise TasteError("subject version required")
    choices = tuple(options)
    if not 2 <= len(choices) <= _MAX_VARIANTS:
        raise TasteError("provide 2..6 visual alternatives")
    for option in choices:
        if not isinstance(option, TasteOption):
            raise TasteError("invalid taste option")
        option.validate()
    if len({o.option_id for o in choices}) != len(choices):
        raise TasteError("duplicate option")
    if len({o.image_evidence_id for o in choices}) != len(choices):
        raise TasteError("a different image proof is required for each alternative")
    _install(store)
    with store.tx() as tx:
        row = _outcome(tx, outcome_id)
        if _subject(row) != expected_subject_version:
            raise TasteError("stale visual result")
        if any(not _proof(tx, outcome_id, expected_subject_version, o.image_evidence_id)
               for o in choices):
            raise TasteError("missing or unverified image proof")
        existing = tx.one(
            "SELECT subject_version,revision,choice_id FROM owner_taste_galleries WHERE outcome_id=?",
            (outcome_id,),
        )
        if (existing is not None and existing["choice_id"] is not None
                and existing["subject_version"] == expected_subject_version):
            raise TasteError("the owner has already selected this gallery version")
        if existing is not None and existing["subject_version"] == expected_subject_version:
            old = tx.query(
                "SELECT option_id,label,image_evidence_id FROM owner_taste_options WHERE outcome_id=? "
                "ORDER BY option_id", (outcome_id,),
            )
            normalized = sorted((o.option_id, o.label, o.image_evidence_id) for o in choices)
            if [(o["option_id"], o["label"], o["image_evidence_id"]) for o in old] == normalized:
                return int(existing["revision"])  # stable publication idempotency
        revision = int(existing["revision"]) + 1 if existing else 1
        if existing:
            tx.change("DELETE FROM owner_taste_options WHERE outcome_id=?", (outcome_id,),
                      event="owner.taste_publish")
            tx.change(
                "UPDATE owner_taste_galleries SET subject_version=?,revision=?,choice_id=NULL,"
                "decision_id=NULL,updated_at=? WHERE outcome_id=?",
                (expected_subject_version, revision, datetime.now(timezone.utc).isoformat(), outcome_id),
                event="owner.taste_publish",
            )
        else:
            tx.change(
                "INSERT INTO owner_taste_galleries "
                "(outcome_id,subject_version,revision,choice_id,decision_id,updated_at) "
                "VALUES(?,?,1,NULL,NULL,?)",
                (outcome_id, expected_subject_version, datetime.now(timezone.utc).isoformat()),
                event="owner.taste_publish",
            )
        tx.changes(
            (("INSERT INTO owner_taste_options "
              "(outcome_id,option_id,label,image_evidence_id) VALUES(?,?,?,?)",
              (outcome_id, opt.option_id, opt.label, opt.image_evidence_id)) for opt in choices),
            event={"kind": "owner.taste_publish", "payload": {"outcome_id": outcome_id, "revision": revision}},
        )
        return revision


def gallery_snapshot(store: Store, outcome_id: str) -> dict[str, Any]:
    """Never sends evidence.ref, image bytes/URL, raw scope or owner context."""
    if not isinstance(outcome_id, str) or not _ID.fullmatch(outcome_id):
        return {"status": "error", "reason": "invalid_result", "options": []}
    try:
        with store.read() as tx:
            tables = {r["name"] for r in tx.query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('owner_taste_galleries','owner_taste_options')"
            )}
            if len(tables) != 2:
                return {"status": "empty", "reason": "no_gallery", "options": []}
            gallery = tx.one("SELECT * FROM owner_taste_galleries WHERE outcome_id=?", (outcome_id,))
            if gallery is None:
                return {"status": "empty", "reason": "no_gallery", "options": []}
            row = tx.one("SELECT * FROM outcomes WHERE id=?", (outcome_id,))
            if row is None or row["state"] != "open" or _subject(row) != gallery["subject_version"]:
                return {"status": "error", "reason": "stale_gallery", "options": []}
            if not tx.scalar(
                "SELECT COUNT(*) FROM obstacles WHERE outcome_id=? AND state='open' "
                "AND type='owner_action' AND cause_code='owner_taste'", (outcome_id,),
            ):
                return {"status": "error", "reason": "taste_request_closed", "options": []}
            options = tx.query(
                "SELECT option_id,label,image_evidence_id FROM owner_taste_options "
                "WHERE outcome_id=? ORDER BY option_id", (outcome_id,),
            )
            if len(options) < 2 or any(
                not _proof(tx, outcome_id, gallery["subject_version"], o["image_evidence_id"])
                for o in options
            ):
                return {"status": "error", "reason": "image_proof_missing", "options": []}
            return {
                "status": "ready" if gallery["choice_id"] is None else "chosen",
                "outcome_id": outcome_id,
                "subject_version": str(gallery["subject_version"]),
                "revision": int(gallery["revision"]),
                "selected": gallery["choice_id"],
                "options": [
                    {"id": str(o["option_id"]), "label": str(o["label"]),
                     "image_proof_id": str(o["image_evidence_id"])}
                    for o in options
                ],
            }
    except Exception:
        return {"status": "error", "reason": "unavailable", "options": []}


def choose_taste(store: Store, *, outcome_id: str, option_id: str,
                 expected_revision: int, owner_authorized: bool = False) -> dict[str, Any]:
    """Owner-only write. The caller MUST authenticate the owner before setting
    owner_authorized=True; never export the bypass as an anonymous MCP tool.
    """
    if not owner_authorized:
        raise PermissionError("authenticated owner action required")
    if not isinstance(outcome_id, str) or not _ID.fullmatch(outcome_id):
        raise TasteError("invalid result id")
    if not isinstance(option_id, str) or not _ID.fullmatch(option_id):
        raise TasteError("invalid option id")
    if type(expected_revision) is not int or expected_revision < 1:
        raise TasteError("invalid revision")
    _install(store)
    with store.tx() as tx:
        row = _outcome(tx, outcome_id)
        gallery = tx.one(
            "SELECT * FROM owner_taste_galleries WHERE outcome_id=?", (outcome_id,),
        )
        if (gallery is None or gallery["choice_id"] is not None
                or gallery["revision"] != expected_revision
                or gallery["subject_version"] != _subject(row)):
            raise TasteError("stale or already selected taste gallery")
        option = tx.one(
            "SELECT image_evidence_id FROM owner_taste_options "
            "WHERE outcome_id=? AND option_id=?", (outcome_id, option_id),
        )
        if option is None or not _proof(tx, outcome_id, gallery["subject_version"],
                                        option["image_evidence_id"]):
            raise TasteError("unknown alternative or missing screenshot proof")
        project = tx.one("SELECT root FROM projects WHERE name=?", (row["project"],))
        if project is None:
            raise TasteError("project scope unavailable")
        # Scope.project accepts opaque identifiers, whereas real fleet project
        # names may contain spaces or punctuation. A project-specific digest
        # keeps the scope narrow without exposing or inventing a global '*'.
        project_scope = "fleet-" + hashlib.sha256(
            str(row["project"]).encode("utf-8")
        ).hexdigest()[:24]
        scope = Scope(str(project["root"]), project_scope, "owner_taste")
        source = SourceRef("owner_reply", "taste:" + outcome_id, str(gallery["subject_version"]))
        evidence = SourceRef("screen", str(option["image_evidence_id"]),
                             str(gallery["subject_version"]))
        decision_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        tx.changes([
            (
                "INSERT INTO decisions(id,scope,text,source,reversible,rollback,ts) "
                "VALUES(?,?,?,?,0,NULL,?)",
                (decision_id, json.dumps(vars(scope), sort_keys=True, ensure_ascii=False),
                 option_id, source.kind, now),
            ),
            (
                "INSERT INTO decision_provenance "
                "(id,decision_key,root,project,context,source_json,evidence_json,"
                "fresh_until,derived,revoked) VALUES (?,?,?,?,?,?,?,?,0,0)",
                (decision_id, "taste:" + outcome_id, scope.root, scope.project,
                 scope.context, json.dumps(vars(source), ensure_ascii=False),
                 json.dumps([vars(evidence)], ensure_ascii=False), _FRESH_UNTIL),
            ),
            (
                "UPDATE owner_taste_galleries SET choice_id=?,decision_id=?,revision=revision+1,"
                "updated_at=? WHERE outcome_id=? AND revision=? AND choice_id IS NULL",
                (option_id, decision_id, now, outcome_id, expected_revision),
            ),
        ], event={"kind": "owner.taste_decision", "project": str(row["project"]),
                  "payload": {"outcome_id": outcome_id, "decision_id": decision_id}})
        return {"status": "chosen", "decision_id": decision_id,
                "outcome_id": outcome_id, "subject_version": gallery["subject_version"],
                "revision": expected_revision + 1, "option_id": option_id}
