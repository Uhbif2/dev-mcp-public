"""Privacy-aware, scoped preference aggregation; private observations never persist/log."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Sequence

from dev_mcp.fleet.store import Store

from .decision_book import KnowledgeHit, Scope, SourceRef, _identifier


@dataclass(frozen=True)
class Preference:
    key: str
    choice: str = field(repr=False)
    scope: Scope
    source: SourceRef
    confidence: float
    fresh_until: str
    private: bool = True


class OwnerModel:
    """Keeps only explicitly non-private, versioned observations in memory.

    Sources are identified by opaque refs, never by copied note text. A duplicate
    event cannot increase confidence; conflicts block proposals.
    """

    def __init__(self, store: Store | None = None) -> None:
        self.store = store
        self._observations: dict[tuple[str, ...], Preference] = {}
        if store is not None:
            with store.tx() as tx:
                tx.change("""CREATE TABLE IF NOT EXISTS owner_preferences (
                    id TEXT PRIMARY KEY,
                    pref_key TEXT NOT NULL,
                    choice TEXT NOT NULL,
                    root TEXT NOT NULL,
                    project TEXT NOT NULL,
                    context TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    fresh_until TEXT NOT NULL
                )""", event="owner.preferences_schema")
            with store.read() as tx:
                for row in tx.query("SELECT * FROM owner_preferences"):
                    preference = Preference(
                        key=row["pref_key"], choice=row["choice"],
                        scope=Scope(row["root"], row["project"], row["context"]),
                        source=SourceRef(row["source_kind"], row["source_ref"], row["source_version"]),
                        confidence=row["confidence"], fresh_until=row["fresh_until"],
                        private=False,
                    )
                    self._observations[self._identity(preference)] = preference

    @staticmethod
    def _identity(preference: Preference) -> tuple[str, ...]:
        return (preference.key, preference.scope.root, preference.scope.project,
                preference.scope.context, preference.source.kind,
                preference.source.ref, preference.source.version)

    @staticmethod
    def _row_id(identity: tuple[str, ...]) -> str:
        return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()

    def observe(self, preference: Preference) -> bool:
        _identifier(preference.key, "preference key")
        if preference.private:
            return False
        if not 0 <= preference.confidence <= 1:
            raise ValueError("confidence outside [0,1]")
        ident = self._identity(preference)
        if self.store is not None:
            with self.store.tx() as tx:
                tx.change("""INSERT INTO owner_preferences
                    (id,pref_key,choice,root,project,context,source_kind,source_ref,source_version,
                     confidence,fresh_until) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET choice=excluded.choice,
                     confidence=excluded.confidence,fresh_until=excluded.fresh_until""",
                    (self._row_id(ident), preference.key, preference.choice, preference.scope.root,
                     preference.scope.project, preference.scope.context, preference.source.kind,
                     preference.source.ref, preference.source.version, preference.confidence,
                     preference.fresh_until),
                    event={"kind": "owner.preference", "payload": {"id": self._row_id(ident)}},
                )
        self._observations[ident] = preference
        return True

    def revoke(self, source: SourceRef) -> None:
        if self.store is not None:
            with self.store.tx() as tx:
                tx.change("DELETE FROM owner_preferences WHERE source_kind=? AND source_ref=? AND source_version=?",
                          (source.kind, source.ref, source.version),
                          event="owner.preference_revoked")
        self._observations = {
            k: v for k, v in self._observations.items()
            if v.source != source
        }

    def propose(
        self, *, key: str, scope: Scope, choices: Sequence[str],
        now: str, minimum_confidence: float = 0.85,
    ) -> KnowledgeHit | None:
        from .decision_book import _when

        relevant = [p for p in self._observations.values()
                    if p.key == key and p.scope.matches(scope)
                    and p.choice in choices and p.confidence >= minimum_confidence
                    and _when(p.fresh_until) > _when(now)]
        if not relevant:
            return None
        specificity = max(p.scope.specificity for p in relevant)
        relevant = [p for p in relevant if p.scope.specificity == specificity]
        if len({p.choice for p in relevant}) > 1:
            return None
        winner = max(relevant, key=lambda p: p.confidence)
        return KnowledgeHit(
            key=key, choice=winner.choice, scope=winner.scope,
            source=SourceRef("previous_choice", winner.source.ref, winner.source.version),
            confidence=winner.confidence, broker="local", access_granted=True,
            observed_until=winner.fresh_until,
        )
