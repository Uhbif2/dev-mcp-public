"""Validate unregistered fleet TZ drafts before they enter the queue.

Uses the fleet's own parser and path overlap semantics; no file writes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dev_mcp.fleet.model import paths_overlap  # noqa: E402
from dev_mcp.fleet.spec import parse_spec  # noqa: E402


REQUIRED = ("Зачем", "Что есть сейчас", "Что сделать", "Чего не делать", "Приёмка", "Итог")
INVALID_COMMAND = re.compile(r"[|>'\"]|&&")
TZ_NAME = re.compile(r"^TZ-[0-9]+$")


def check(drafts: Path) -> list[str]:
    errors: list[str] = []
    if not drafts.is_dir():
        return [f"not a directory: {drafts}"]
    files = sorted(drafts.glob("TZ-*.md"))
    if not files:
        return [f"no TZ-*.md drafts in {drafts}"]

    registered = sorted((ROOT / "ToDo" / "tz").glob("TZ-*.md"))
    entries = []
    for path in [*registered, *files]:
        try:
            task = parse_spec(str(path.resolve()))
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: parse_spec: {exc}")
            continue
        if path in files:
            if not TZ_NAME.fullmatch(task.tz):
                errors.append(f"{path.name}: invalid tz id {task.tz}")
            if task.tz not in path.name:
                errors.append(f"{path.name}: tz id does not match filename")
            if not task.acceptance:
                errors.append(f"{path.name}: no acceptance commands")
            for command in task.acceptance:
                if INVALID_COMMAND.search(command):
                    errors.append(f"{path.name}: unsafe acceptance command: {command}")
            body = task.body
            for section in REQUIRED:
                if not re.search(rf"(?m)^## {re.escape(section)}\s*$", body):
                    errors.append(f"{path.name}: missing section {section}")
            if "враждебн" not in body.casefold():
                errors.append(f"{path.name}: missing hostile input cases")
        entries.append((path, task))

    ids = {task.tz for _, task in entries}
    for index, (path, task) in enumerate(entries):
        if any(other.tz == task.tz for other_path, other in entries[:index]):
            errors.append(f"{path.name}: duplicate id {task.tz}")
        if path not in files:
            continue
        for need in task.needs:
            base, *level = need.split("#", 1)
            if base not in ids or (level and not level[0]):
                errors.append(f"{path.name}: unknown/invalid need {need}")
            if base == task.tz:
                errors.append(f"{path.name}: self dependency {need}")

    for i, (left_path, left) in enumerate(entries):
        for right_path, right in entries[i + 1 :]:
            if left_path not in files and right_path not in files:
                continue
            for a in left.owns:
                for b in right.owns:
                    # TZ-07 owns the draft staging directory by design.
                    if (left.tz == "TZ-07" and a.rstrip("/").endswith("/todo/tz-drafts")
                            or right.tz == "TZ-07" and b.rstrip("/").endswith("/todo/tz-drafts")):
                        continue
                    if paths_overlap(a, b):
                        errors.append(f"owns collision {left.tz} / {right.tz}: {a} <> {b}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("drafts", type=Path)
    args = parser.parse_args()
    issues = check(args.drafts.resolve())
    for issue in issues:
        print("ERROR:", issue)
    if issues:
        print(f"{len(issues)} error(s)")
        return 1
    print(f"OK: {len(list(args.drafts.glob('TZ-*.md')))} drafts parsed, dependencies and owns checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
