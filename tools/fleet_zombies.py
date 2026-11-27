"""Standalone operator CLI for evidence-based zombie queue cleanup."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from dev_mcp.fleet.store import Store  # noqa: E402
from dev_mcp.fleet.zombies import retire  # noqa: E402


class ReadOnlyStore:
    """Read-only CLI adapter: no Store.open, migrations or WAL setup."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        # A WAL reader may otherwise create -wal/-shm when they are absent.
        # Immutable is safe only if there is no WAL to ignore.
        wal = path.with_name(path.name + "-wal")
        shm = path.with_name(path.name + "-shm")
        if wal.exists() != shm.exists():
            raise RuntimeError("refusing read-only WAL open with incomplete sidecars")
        mode = "?mode=ro" if wal.exists() else "?mode=ro&immutable=1"
        self.conn = sqlite3.connect(path.resolve().as_uri() + mode, uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")

    @contextmanager
    def read(self) -> Iterator["ReadOnlyStore"]:
        yield self

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        if len(rows) > 1:
            raise RuntimeError(f"expected one row, got {len(rows)}")
        return rows[0] if rows else None

    def close(self) -> None:
        self.conn.close()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Classify old submitted/frozen fleet tasks; dry by default")
    parser.add_argument("project", nargs="?")
    parser.add_argument("--all", action="store_true", help="count stale classes across all projects")
    parser.add_argument("--summary", action="store_true", help="omit per-task output")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", type=Path, default=REPO / "data" / "fleet" / "fleet.sqlite",
                        help="registry path (default: live registry)")
    args = parser.parse_args(argv)
    if args.days < 0:
        parser.error("--days must be >= 0")
    if args.all and (args.project or args.apply):
        parser.error("--all cannot be combined with project or --apply")
    if not args.all and not args.project:
        parser.error("project is required unless --all is specified")
    store = Store.open(str(args.db)) if args.apply else ReadOnlyStore(args.db)
    try:
        if args.all:
            with store.read() as tx:
                names = [row["name"] for row in tx.query("SELECT name FROM projects ORDER BY name")]
            results = {name: retire(store, name, days=args.days) for name in names}
            counts = {name: {"total": result["total"], "counts": result["counts"]}
                      for name, result in results.items()}
        else:
            result = retire(store, args.project, days=args.days, apply=args.apply)
    finally:
        store.close()
    if args.all:
        if args.json:
            print(json.dumps(counts, ensure_ascii=False, indent=2))
        else:
            for name, info in counts.items():
                print(f"{name}: {info['total']} stale; " +
                      " ".join(f"{key}={value}" for key, value in info["counts"].items()))
        return 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        mode = "APPLY" if args.apply else "DRY"
        print(f"{mode} {args.project}: {result['total']} stale; classes " +
              " ".join(f"{k}={v}" for k, v in result["counts"].items()))
        if not args.summary:
            for item in result["rows"]:
                print(f"{item['id']} | {item['age_days']}d | {item['class']} | {item['evidence']}")
        if args.apply:
            print(f"retired={result['applied']} preimage={result.get('preimage', 'none')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
