"""TZ-48. Evidence ledger for human actions outside the fleet integrator.

Consumers call tick from a trusted controller. This module never sends source
data to an LLM, executes user log contents, or changes a git working tree.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from dev_mcp.audit import iter_entries
from dev_mcp.fleet.store import Store

COMMANDS = frozenset({"reactivate", "freeze", "priority", "recover", "bootstrap", "tell"})
SHA = re.compile(r"^[0-9a-f]{40}$", re.I)
COMMIT_PREFIX = re.compile(r"^[0-9a-f]{7,40}$", re.I)
GIT_FORMAT = "%H%x1f%an%x1f%aI%x1f%s%x1e"


def _dt(value: str) -> datetime:
    at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("timezone required")
    return at.astimezone(timezone.utc)


def _clean(value: str, limit: int = 140) -> str:
    return re.sub(r"[^\w .@:/+#-]", "_", str(value), flags=re.UNICODE)[:limit]


@dataclass(frozen=True)
class Intervention:
    source: str
    identifier: str
    action: str
    actor: str
    ts: str
    reference: str
    resolution: str = ""

    def __post_init__(self) -> None:
        if not all((self.source, self.identifier, self.action, self.reference)):
            raise ValueError("source, identifier, action, reference required")
        _dt(self.ts)


def classify(item: Intervention) -> str:
    """Deterministic classification; a Gemini suggestion must remain advisory."""
    if item.source == "lesson":
        return item.action
    if item.source == "fleet-cli":
        return "manual-" + item.action
    return {
        "git": "manual-commit",
        "guard-approval": "manual-guard-approval",
        "guard-install": "manual-guard-install",
        "owner-action": "manual-owner-action",
    }.get(item.source, "manual-other")


def git_interventions(
    root: Path, *, refs: Iterable[str] = ("platform/work", "master"),
    integrator_shas: Iterable[str] = (), since: str = "",
    runner: Callable[..., Any] = subprocess.run,
) -> list[Intervention]:
    """Only actual integrator SHA evidence excludes a commit, not author/title."""
    # The integrator records `git rev-parse --short HEAD`, not a 40-byte
    # digest. A verified prefix is enough to identify its *own* full SHA.
    known = {sha.lower() for sha in integrator_shas if COMMIT_PREFIX.fullmatch(sha)}
    found: dict[str, Intervention] = {}
    for ref in refs:
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", ref) or ref.startswith("-"):
            raise ValueError("unsafe git ref")
        argv = ["git", "log", "--no-merges", "--no-show-signature",
                "--format=" + GIT_FORMAT, ref]
        if since:
            argv.append("--since=" + _dt(since).isoformat())
        response = runner(argv, cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30)
        if response.returncode:
            continue
        for record in response.stdout.split("\x1e"):
            fields = record.strip().split("\x1f", 3)
            if len(fields) != 4:
                continue
            sha, author, at, _subject = fields
            if not SHA.fullmatch(sha) or any(sha.lower().startswith(prefix) for prefix in known):
                continue
            try:
                ts = _dt(at).isoformat()
            except ValueError:
                continue
            found[sha] = Intervention("git", sha, "commit", _clean(author),
                                      ts, "git:" + root.name + "@" + sha)
    return list(found.values())


def integrator_commits(store: Store) -> set[str]:
    with store.read() as tx:
        events = tx.query("SELECT payload FROM events WHERE kind='integrate'")
    result = set()
    for event in events:
        try:
            payload = json.loads(event["payload"])
        except (TypeError, ValueError):
            continue
        sha = payload.get("commit") if isinstance(payload, dict) else None
        if isinstance(sha, str) and COMMIT_PREFIX.fullmatch(sha):
            result.add(sha.lower())
    return result


def audit_interventions(path: Path, *, since: str = "") -> list[Intervention]:
    """Rotated MCP audit. Only CLI actions without a fleet agent are manual."""
    items = []
    occurrences: Counter[str] = Counter()
    # The content fingerprint contains the event timestamp, so filtering by
    # `since` cannot exclude earlier *identical* records. This lets a periodic
    # scan read recent archives, not the entire historical audit on each tick.
    for entry in iter_entries(path, since):
        digest = hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False,
                                           default=str).encode("utf-8")).hexdigest()
        ordinal = occurrences[digest]
        occurrences[digest] += 1
        if entry.get("decision") not in {"allow", "allowed", "ok"}:
            continue
        if entry.get("agent") not in {None, "", "owner", "operator", "manual"}:
            continue
        tool = str(entry.get("tool") or "")
        args = entry.get("args")
        if not isinstance(args, dict):
            continue
        action = str(args.get("action") or "").casefold()
        argv = args.get("argv")
        if isinstance(argv, list) and len(argv) >= 2:
            binary = str(argv[0]).replace("\\", "/").split("/")[-1].casefold()
            if binary in {"fleet.cmd", "guard.cmd"}:
                tool = binary
                action = str(argv[1]).casefold()
        source = ("fleet-cli" if tool in {"fleet.cmd", "fleet_cmd"} and action in COMMANDS
                  else "guard-install" if tool in {"guard.cmd", "guard_cmd"} and action == "install"
                  else "")
        if not source:
            continue
        try:
            ts = _dt(str(entry["ts"])).isoformat()
        except (KeyError, ValueError):
            continue
        # No user arguments or private command body in identifier/record.
        identifier = hashlib.sha256(f"{digest}|{ordinal}".encode()).hexdigest()
        items.append(Intervention(source, identifier, action, "operator", ts,
                                  "audit:" + path.name + ":" + ts))
    return items


def guard_approvals(path: Path) -> list[Intervention]:
    if not path.is_file():
        return []
    ts = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    found = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*([0-9a-fA-F]{40})(?:\s|$)", line)
        if match:
            sha = match.group(1).lower()
            found.append(Intervention("guard-approval", sha, "approve", "operator",
                                      ts, "guard-approval:" + sha))
    return found


def guard_events(path: Path, *, since: str = "") -> list[Intervention]:
    """Structured JSONL only: never classify arbitrary prose as a command."""
    if not path.is_file():
        return []
    result = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        try:
            event = json.loads(line)
            ts = _dt(str(event["ts"])).isoformat()
        except (KeyError, ValueError, TypeError):
            continue
        if not isinstance(event, dict) or (since and ts < _dt(since).isoformat()):
            continue
        action = event.get("action")
        if event.get("origin") not in {"manual", "operator", "owner"} or action not in {"install", "approve"}:
            continue
        source = "guard-install" if action == "install" else "guard-approval"
        identifier = _clean(str(event.get("id") or f"{path.name}:{number}"))
        result.append(Intervention(source, identifier, str(action), "operator", ts,
                                   f"guard-log:{path.name}:{number}"))
    return result


def owner_events(store: Store, *, since: str = "") -> list[Intervention]:
    with store.read() as tx:
        rows = tx.query("SELECT id,ts,kind,worker_id FROM events "
                        "WHERE kind IN ('owner_action_manual','owner.action_manual','owner.action_override') "
                        "AND ts >= ? ORDER BY id", (since,))
    return [Intervention("owner-action", str(row["id"]), str(row["kind"]),
                         _clean(row["worker_id"] or "owner"), row["ts"],
                         "fleet-event:" + str(row["id"])) for row in rows]


def cli_events(store: Store, *, since: str = "") -> list[Intervention]:
    """Console mutations bypass MCP audit: use transaction-bound CLI receipts."""
    with store.read() as tx:
        rows = tx.query("SELECT id,ts,payload FROM events "
                        "WHERE kind='owner_cli_manual' AND ts>=? ORDER BY id", (since,))
    result: list[Intervention] = []
    for row in rows:
        try:
            info = json.loads(row["payload"])
            if not isinstance(info, dict):
                continue
            action = info.get("command")
            if action not in COMMANDS:
                continue
            result.append(Intervention("fleet-cli", str(row["id"]), str(action),
                                       "console", row["ts"], "fleet-event:" + str(row["id"])))
        except (TypeError, ValueError):
            continue
    return result


def lesson_interventions(path: Path) -> list[Intervention]:
    """Seed each numbered case; refer to lessons, never duplicate their body."""
    if not path.is_file():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [s.strip() for s in line.strip().strip("|").split("|")]
        if len(parts) != 5 or not parts[0].isdigit():
            continue
        number, _body, category, _invariant, links = parts
        # The lessons catalog is Russian. ASCII-only slugs collapsed almost
        # every independent failure class to the same `lesson-other` key.
        slug = re.sub(r"[^\w-]+", "-", category.casefold()).strip("-")[:55] or "other"
        resolution = re.search(r"TZ-\d+", links, flags=re.I)
        items.append(Intervention("lesson", number, "lesson-" + slug, "bootstrap",
                                  "2026-10-10T00:00:00+00:00",
                                  f"docs/LESSONS-AUTONOMY-2026-10.md#case-{number}",
                                  resolution.group(0).upper() if resolution else ""))
    return items


class InterventionLedger:
    def __init__(self, store: Store, project: str, root: Path):
        self.store, self.project, self.root = store, project, Path(root)
        with store.tx() as tx:
            if not tx.one("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name='intervention_log'"):
                tx.change("""CREATE TABLE intervention_log (
                    source TEXT NOT NULL, identifier TEXT NOT NULL, project TEXT NOT NULL,
                    class TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                    ts TEXT NOT NULL, reference TEXT NOT NULL,
                    PRIMARY KEY(source,identifier,project))""", event="intervention.schema")
            if not tx.one("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name='intervention_classes'"):
                tx.change("""CREATE TABLE intervention_classes (
                    project TEXT NOT NULL, class TEXT NOT NULL, resolution_tz TEXT NOT NULL DEFAULT '',
                    draft_path TEXT NOT NULL DEFAULT '', defect_path TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(project,class))""", event="intervention.schema")

    def _resolution(self, tz: str) -> tuple[str, str] | None:
        if not tz:
            return None
        with self.store.read() as tx:
            row = tx.one("SELECT state,updated_at FROM tasks WHERE project=? AND tz=?",
                         (self.project, tz))
            accepted = tx.one("SELECT MAX(ts) AS ts FROM events WHERE project=? AND tz=? AND kind='accept'",
                              (self.project, tz))
        if row is None:
            return None
        return str(row["state"]), str(accepted["ts"] or row["updated_at"])

    def _allocate_draft(self, category: str, *, defect: bool, resolution: str) -> str:
        drafts = self.root / "ToDo" / "tz-drafts"
        if drafts.is_symlink():
            raise ValueError("unsafe draft directory")
        drafts.mkdir(parents=True, exist_ok=True)
        if category in {"manual-guard-install", "manual-guard-approval"}:
            scope = "guard/core.py, guard/install.py, tests/guard"
            acceptance = "python -m pytest -q tests/guard"
        elif category == "manual-commit":
            scope = "src/dev_mcp/fleet/integrate.py, tests/fleet/test_integrate.py"
            acceptance = "python -m pytest -q tests/fleet/test_integrate.py"
        elif category == "manual-owner-action":
            scope = "src/dev_mcp/owner/actions.py, tests/owner"
            acceptance = "python -m pytest -q tests/owner"
        elif category == "manual-bootstrap":
            # A recurrence after TZ-68 must not reclaim shared tools.py paths.
            # Bind a fresh regression file to this exact draft number instead.
            scope = "tests/fleet/test_manual_bootstrap_recurrence_tz_{number}.py"
            acceptance = "python -m pytest -q {scope}"
        else:
            scope = "src/dev_mcp/fleet/tools.py, tests/fleet/test_tools.py"
            acceptance = "python -m pytest -q tests/fleet/test_tools.py"
        used = {50}
        for directory in (drafts, self.root / "ToDo" / "tz"):
            for item in directory.glob("TZ-*.md"):
                number = re.match(r"TZ-(\d+)", item.name, re.I)
                if number:
                    used.add(int(number.group(1)))
        with self.store.read() as tx:
            for row in tx.query("SELECT tz FROM tasks WHERE project=?", (self.project,)):
                match = re.fullmatch(r"TZ-(\d+)", str(row["tz"]), re.I)
                if match:
                    used.add(int(match.group(1)))
        slug = re.sub(r"[^\w-]+", "-", category.casefold()).strip("-")[:45]
        for number in range(max(used) + 1, max(used) + 2000):
            name = f"TZ-{number}-intervention-{slug}.md"
            path = drafts / name
            draft_scope = scope.format(number=number)
            draft_acceptance = acceptance.format(scope=draft_scope)
            draft_needs = "TZ-68" if category == "manual-bootstrap" else ""
            # TZ-38 receives a parseable candidate. Remediation still needs
            # a regression test that fails before, passes after the change.
            body = ("```yaml\n"
                    f"tz: TZ-{number}\n"
                    f"owns: [{draft_scope}]\n"
                    f"needs: [{draft_needs}]\n"
                    f"acceptance: [{draft_acceptance}]\n"
                    "```\n\n"
                    "<!-- Generated by intervention ledger -->\n\n"
                    f"# {'Дефект устранения' if defect else 'Устранить ручное вмешательство'}: {category}\n\n"
                    f"Класс: {category}. "
                    + (f"Принятое устранение {resolution} не предотвратило повтор.\n" if defect
                       else "Убрать необходимость в ручном шаге.\n")
                    + "Добавить регрессионный тест: до исправления воспроизводит ручное "
                      "действие, после исправления ручной оператор не нужен.\n")
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(body)
                return path.relative_to(self.root).as_posix()
            except FileExistsError:
                continue
        raise RuntimeError("draft ID space exhausted")

    def _draft(self, category: str, *, defect: bool, resolution: str = "") -> None:
        field = "defect_path" if defect else "draft_path"
        # Keep the check, allocation and class binding under the same write
        # gate: independent controller ticks must not allocate two draft IDs.
        with self.store.tx() as tx:
            row = tx.one(f"SELECT {field} FROM intervention_classes "
                         "WHERE project=? AND class=?", (self.project, category))
            if row is None or row[field]:
                return
            path = self._allocate_draft(category, defect=defect, resolution=resolution)
            tz = re.match(r"(TZ-\d+)", Path(path).name, re.I).group(1)
            tx.change(f"UPDATE intervention_classes SET {field}=? WHERE project=? AND class=? AND {field}=''",
                      (path, self.project, category),
                      event={"kind": "intervention.draft", "project": self.project,
                             "payload": {"class": category, "draft": path, "defect": defect}})
            if not defect:
                tx.change("UPDATE intervention_classes SET resolution_tz=? WHERE project=? AND class=?",
                          (tz, self.project, category),
                          event="intervention.remediation_link")

    def ingest(self, item: Intervention) -> bool:
        category = classify(item)
        identifier = _clean(item.identifier)
        actor, action, reference = (_clean(item.actor), _clean(item.action),
                                    _clean(item.reference, 240))
        occurred_at = _dt(item.ts).isoformat()
        with self.store.tx() as tx:
            prior = tx.one("SELECT 1 FROM intervention_log WHERE source=? AND identifier=? AND project=?",
                           (item.source, identifier, self.project))
            if prior:
                return False
            tx.change("INSERT INTO intervention_log VALUES (?,?,?,?,?,?,?,?)",
                      (item.source, identifier, self.project, category,
                       actor, action, occurred_at, reference),
                      event={"kind": "intervention", "project": self.project, "tool": "interventions",
                             "payload": {"class": category, "source": item.source,
                                         "identifier": identifier, "actor": actor,
                                         "action": action, "occurred_at": occurred_at,
                                         "reference": reference}})
            existing = tx.one("SELECT 1 FROM intervention_classes WHERE project=? AND class=?",
                              (self.project, category))
            if existing is None:
                tx.change("INSERT INTO intervention_classes(project,class,resolution_tz) VALUES(?,?,?)",
                          (self.project, category, item.resolution),
                          event="intervention.class")
        with self.store.read() as tx:
            row = tx.one("SELECT * FROM intervention_classes WHERE project=? AND class=?",
                         (self.project, category))
        resolution = str(row["resolution_tz"])
        status = self._resolution(resolution)
        if resolution and status and status[0] == "accepted":
            if item.source != "lesson" and _dt(item.ts) > _dt(status[1]) and not row["defect_path"]:
                self._draft(category, defect=True, resolution=resolution)
        elif (not resolution or status is None or status[0] in {"rejected", "cancelled"}) and not row["draft_path"]:
            self._draft(category, defect=False)
        return True

    def tick(
        self, *, repositories: Iterable[Path] = (), audit_path: Path | None = None,
        approvals_path: Path | None = None, guard_log: Path | None = None,
        lessons_path: Path | None = None, since: str = "",
        runner: Callable[..., Any] = subprocess.run,
    ) -> dict[str, Any]:
        items = []
        trusted = integrator_commits(self.store)
        for repo in repositories:
            items.extend(git_interventions(Path(repo), integrator_shas=trusted,
                                           since=since, runner=runner))
        if audit_path is not None:
            items.extend(audit_interventions(Path(audit_path), since=since))
        if approvals_path is not None:
            items.extend(guard_approvals(Path(approvals_path)))
        if guard_log is not None:
            items.extend(guard_events(Path(guard_log), since=since))
        if lessons_path is not None:
            items.extend(lesson_interventions(Path(lessons_path)))
        items.extend(owner_events(self.store, since=since))
        items.extend(cli_events(self.store, since=since))
        return {"scanned": len(items), "new": sum(self.ingest(item) for item in items),
                "metrics": self.metrics()}

    def metrics(self, *, now: datetime | None = None, days: int = 7) -> dict[str, Any]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self.store.read() as tx:
            rows = tx.query("SELECT ts,class FROM intervention_log "
                            "WHERE project=? AND ts>=? ORDER BY ts",
                            (self.project, (now - timedelta(days=days)).isoformat()))
        by_day: dict[str, Counter[str]] = defaultdict(Counter)
        for row in rows:
            by_day[_dt(row["ts"]).date().isoformat()][row["class"]] += 1
        series = {(now.date() - timedelta(days=d)).isoformat(): {}
                  for d in range(days - 1, -1, -1)}
        for day, counts in by_day.items():
            if day in series:
                series[day] = dict(sorted(counts.items()))
        today = now.date().isoformat()
        prev = (now.date() - timedelta(days=1)).isoformat()
        n, before = sum(series.get(today, {}).values()), sum(series.get(prev, {}).values())
        return {"goal": 0, "today": n, "yesterday": before, "rising": n > before,
                "by_day": series,
                "by_class": dict(sorted(Counter(row["class"] for row in rows).items()))}
