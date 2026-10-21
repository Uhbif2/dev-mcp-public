"""Operator-only budgets and atomic durable idempotency reservations.

Budget configuration is a protected owner-maintained file, never a parameter of
fleet_op. All amounts are integer micro-USD internally. SQLite serializes concurrent
reservations with BEGIN IMMEDIATE; failed attempts still consume their reservation.
"""
from __future__ import annotations

import json
import sqlite3
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


class BudgetError(ValueError):
    """Denial code is safe to return; no raw configuration is exposed."""


def _micros(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BudgetError("invalid_budget_amount")
    try:
        amount = Decimal(str(value)) * Decimal(1_000_000)
    except InvalidOperation as exc:
        raise BudgetError("invalid_budget_amount") from exc
    if not amount.is_finite() or amount != amount.to_integral_value() or not 0 <= amount <= 10**15:
        raise BudgetError("invalid_budget_amount")
    return int(amount)


@dataclass(frozen=True)
class Grant:
    operation: str
    cost_micro_usd: int
    max_micro_usd: int
    max_calls: int
    period_seconds: int
    roots: tuple[Path, ...]
    targets: frozenset[str]
    hosts: frozenset[str]


@dataclass(frozen=True)
class BudgetBook:
    approval_id: str
    expiry: datetime
    grants: Mapping[str, Grant]

    @classmethod
    def load(cls, path: Path | str) -> "BudgetBook":
        try:
            document = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise BudgetError("budget_config_unavailable") from exc
        if document.get("version") != 1 or document.get("approved") is not True:
            raise BudgetError("owner_approval_required")
        approval = document.get("approval_id")
        if not isinstance(approval, str) or not 4 <= len(approval) <= 120:
            raise BudgetError("invalid_approval")
        try:
            expiry = datetime.fromisoformat(document["expires_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError) as exc:
            raise BudgetError("invalid_expiry") from exc
        if expiry.tzinfo is None:
            raise BudgetError("invalid_expiry")
        raw = document.get("operations")
        if not isinstance(raw, dict) or not raw:
            raise BudgetError("no_budget_grants")
        from .operator_catalog import NAMES
        grants: dict[str, Grant] = {}
        for op, rule in raw.items():
            if op not in NAMES or not isinstance(rule, dict):
                raise BudgetError("invalid_budget_operation")
            if rule.get("approved") is not True:
                continue
            maximum_calls = rule.get("max_calls")
            period = rule.get("period_seconds")
            if type(maximum_calls) is not int or not 1 <= maximum_calls <= 1_000_000:
                raise BudgetError("invalid_call_limit")
            if type(period) is not int or not 60 <= period <= 31_536_000:
                raise BudgetError("invalid_period")
            cost = _micros(rule.get("cost_usd"))
            cap = _micros(rule.get("max_cost_usd"))
            if cost <= 0 or cap < cost:
                raise BudgetError("invalid_budget_cap")
            roots = rule.get("roots")
            targets = rule.get("targets")
            hosts = rule.get("hosts", [])
            if (not isinstance(roots, list) or not roots
                    or not all(type(x) is str and x for x in roots)):
                raise BudgetError("invalid_roots")
            if any(not Path(x).is_absolute() or ".." in Path(x).parts for x in roots):
                raise BudgetError("invalid_roots")
            if (not isinstance(targets, list) or not targets
                    or not all(type(x) is str and x for x in targets)):
                raise BudgetError("invalid_targets")
            if (not isinstance(hosts, list)
                    or not all(type(x) is str and x and x == x.lower() for x in hosts)
                    or (op == "browser.task" and not hosts)):
                raise BudgetError("invalid_hosts")
            grants[op] = Grant(op, cost, cap, maximum_calls, period,
                               tuple(Path(x).resolve() for x in roots), frozenset(targets),
                               frozenset(hosts))
        return cls(approval, expiry, MappingProxyType(grants))

    def authorize(self, op: str, params: Mapping[str, Any], now: float) -> Grant:
        if datetime.fromtimestamp(now, timezone.utc) >= self.expiry.astimezone(timezone.utc):
            raise BudgetError("approval_expired")
        grant = self.grants.get(op)
        if grant is None:
            raise BudgetError("operation_not_approved")
        if params["target"] not in grant.targets:
            raise BudgetError("target_out_of_scope")
        if op == "browser.task" and urlsplit(params["url"]).hostname not in grant.hosts:
            raise BudgetError("host_out_of_scope")
        raw = params["workspace"]
        candidate = Path(raw)
        # Explicit absolute workspace only: never allow .., a drive-relative path,
        # or escape through symlink/junction; resolve on the operator machine.
        if not candidate.is_absolute() or ".." in candidate.parts or "\x00" in raw:
            raise BudgetError("invalid_workspace")
        resolved = candidate.resolve()
        if not any(resolved == root or resolved.is_relative_to(root) for root in grant.roots):
            raise BudgetError("workspace_out_of_scope")
        return grant


# Imported only after BudgetBook declaration for simple types.
from types import MappingProxyType


@dataclass(frozen=True)
class Reservation:
    cached: dict[str, Any] | None


class BudgetLedger:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        # Directory is provisioned by the trusted operator launcher with private ACLs.
        if not self.path.parent.is_dir():
            raise BudgetError("ledger_directory_missing")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS operations(
                fingerprint TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                period_start INTEGER NOT NULL,
                cost_micro_usd INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('running','done')),
                result_json TEXT,
                created_ts REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS op_budget_period ON operations(operation,approval_id,period_start)")

    def reserve(self, fingerprint: str, grant: Grant, book: BudgetBook, now: float) -> Reservation:
        start = int(now // grant.period_seconds) * grant.period_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT state,result_json FROM operations WHERE fingerprint=?",
                                    (fingerprint,)).fetchone()
            if existing:
                conn.execute("COMMIT")
                if existing[0] == "running":
                    raise BudgetError("operation_in_progress")
                return Reservation(json.loads(existing[1]))
            count, spent = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(cost_micro_usd),0) FROM operations "
                "WHERE operation=? AND approval_id=? AND period_start=?",
                (grant.operation, book.approval_id, start),
            ).fetchone()
            if count >= grant.max_calls or spent + grant.cost_micro_usd > grant.max_micro_usd:
                raise BudgetError("budget_exhausted")
            conn.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)",
                (fingerprint, grant.operation, book.approval_id, start,
                 grant.cost_micro_usd, "running", None, now),
            )
            conn.execute("COMMIT")
        return Reservation(None)

    def complete(self, fingerprint: str, result: dict[str, Any]) -> None:
        wire = json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if len(wire) > 8192:
            raise BudgetError("result_too_large")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE operations SET state='done',result_json=? WHERE fingerprint=? AND state='running'",
                (wire, fingerprint),
            ).rowcount
            if changed != 1:
                raise BudgetError("reservation_missing")
            conn.execute("COMMIT")
