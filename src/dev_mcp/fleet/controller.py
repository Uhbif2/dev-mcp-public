"""Autonomous reconciliation loop for durable fleet outcomes and queue hygiene.

Only the short reserve/apply transactions mutate SQLite.  The discovery snapshot,
legacy probes and all potentially slow checks run outside a write transaction.
"""

from __future__ import annotations

from dev_mcp import machine

import base64
import hmac
import json
import logging
import hashlib
import ntpath
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import outcome, world
from .guard_watch import guard_watch_tick
from .spec_preflight import ProbeNotApplicable
from .regate import run_regater
from .store import Store


INTERVAL_S = 30.0
STALE_TICK_S = 300.0
LOG = logging.getLogger(__name__)
_GUARD_LIVE_ROOT = machine.LIVE_HOME or machine.HOME
GUARD_DISABLED = True


_TRIPLE_WORDS = re.compile(r"тр[её]хкратн|трижды|три\s+независим|независимо\s+трижды|triple", re.I)


def wants_triple(raw_intent: str) -> bool:
    """Triple variants + consensus only where the owner asked for them.

    14.10.2026 night: every intent ran 3 variants + 3 reviews + consensus + Gemini on
    understood and approach_selected; small change-intents (fast-jev fixes, gemini
    handoff) did not pass a single stage in 12 h. deep-research asked for it explicitly.
    """
    return bool(_TRIPLE_WORDS.search(str(raw_intent or "")))


def _draft_fixture_probe(argv: tuple[str, ...], fixture: Path) -> int:
    """Probe a narrow pytest command in preflight's disposable empty snapshot.

    Do not execute a task-supplied binary, pytest plugin, host path or shell.
    The result is only a baseline probe, never task acceptance evidence.
    """
    if not fixture.is_dir() or fixture.is_symlink() or not argv:
        raise ValueError("draft_fixture_invalid")
    exe = ntpath.basename(argv[0].replace("/", "\\")).casefold()
    if exe in {"python", "python.exe", "py", "py.exe"}:
        prefix = 1
        if exe in {"py", "py.exe"} and len(argv) > 1 and argv[1] == "-3":
            prefix += 1
        if tuple(argv[prefix:prefix + 2]) != ("-m", "pytest"):
            raise ProbeNotApplicable("draft_probe_module_not_allowed")
        args = argv[prefix + 2:]
    elif exe in {"pytest", "pytest.exe"}:
        args = argv[1:]
    else:
        raise ProbeNotApplicable("draft_probe_executable_not_allowed")

    allowed_flags = {"-q", "-qq", "-v", "-vv", "-x", "--disable-warnings",
                     "--strict-markers", "--strict-config", "--maxfail=1"}
    checked: list[str] = []
    offset = 0
    while offset < len(args):
        part = args[offset]
        if part == "-p" and offset + 1 < len(args) and args[offset + 1] == "no:cacheprovider":
            offset += 2
            continue
        if part.startswith("-"):
            if part not in allowed_flags:
                raise ValueError("draft_probe_option_not_allowed")
            checked.append(part)
        else:
            # Paths are relative to the ephemeral fixture, not to the owner's
            # checkout. Deny Windows drives, UNC, .., response files and paths
            # that could make pytest inspect an external directory.
            source = part.split("::", 1)[0].replace("\\", "/")
            if (not source or source.startswith(("/", "@", "~")) or
                    ntpath.isabs(source) or ntpath.splitdrive(source)[0] or
                    ".." in Path(source).parts):
                raise ValueError("draft_probe_path_not_allowed")
            checked.append(part)
        offset += 1
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
           if key in os.environ}
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "--noconftest", "-p", "no:cacheprovider",
         *checked], cwd=str(fixture), env=env, capture_output=True, timeout=12,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
    )
    return int(result.returncode)


def _trusted_draft_runner(db_path: str | Path) -> Callable[[tuple[str, ...], Path], int] | None:
    """Only the installed server's own Store may execute isolated probes."""
    own_db = Path(__file__).resolve().parents[3] / "data" / "fleet" / "fleet.sqlite"
    return (_draft_fixture_probe if Path(db_path).resolve() == own_db.resolve()
            else None)


def _controller_guard_watch_step(store: Store, db_path: Path | str) -> str:
    """Only the installed server may restart the guard, never a test DB/agent."""
    # 13.10.2026, владелец: страж выкатки выключен (пульт _decisions/0025) — правки платформы
    # выкатывает Claude вручную. Контроллер не поднимает стража обратно.
    if GUARD_DISABLED:
        return "disabled"
    live = _GUARD_LIVE_ROOT
    if Path(db_path).resolve() != (live / "data/fleet/fleet.sqlite").resolve():
        return "disabled"
    return guard_watch_tick(store, live)


def _controller_flow_watchdog_step(
    store: Store, *, now: datetime | None = None,
) -> list[Any]:
    """Run TZ-47 using trusted registry/sensors in the existing controller loop."""
    from .watchdog import local_signals, watchdog_tick

    stamp = now or datetime.now(timezone.utc)
    return watchdog_tick(store, now=stamp, signals=local_signals(store, now=stamp))



@dataclass(frozen=True)
class InterventionRuntime:
    """Fixed, server-owned observation paths; never derived from task text."""
    draft_root: Path
    repositories: tuple[Path, ...]
    audit_path: Path
    approvals_path: Path
    guard_log: Path
    lessons_path: Path


_INTERVENTION_BASELINE = datetime(2026, 10, 9, tzinfo=timezone.utc)
_INTERVENTION_INTERVAL_S = 300.0


def _trusted_intervention_runtime(db_path: Path | str) -> InterventionRuntime | None:
    """Do not scan host git/logs or create drafts from an arbitrary test Store.

    Dormant since the platform lane closed (owner, 14.10.2026): dev-mcp-work was retired
    20.10.2026, so this returns None and the intervention loop stays off."""
    live = _GUARD_LIVE_ROOT
    if Path(db_path).resolve() != (live / "data/fleet/fleet.sqlite").resolve():
        return None
    work = machine.HUB / "dev-mcp-work"
    if (not live.is_dir() or live.is_symlink() or not work.is_dir()
            or work.is_symlink() or work.resolve() != work.absolute()):
        return None
    return InterventionRuntime(
        draft_root=work, repositories=(work, live),
        audit_path=live / "logs/audit.jsonl",
        approvals_path=live / "data/guard-approved-commits.txt",
        guard_log=live / "logs/platform-guard.jsonl",
        lessons_path=work / "docs/LESSONS-AUTONOMY-2026-10.md",
    )


def _bounded_intervention_git(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """No git log may stall the independent maintenance loop indefinitely."""
    kwargs["timeout"] = min(5.0, float(kwargs.get("timeout", 30)))
    return subprocess.run(argv, **kwargs)


def _intervention_cycle(store: Store, runtime: InterventionRuntime) -> dict[str, Any]:
    """One real scan; an event cursor advances only after successful ingestion.

    A 48-hour overlap catches delayed source events. Ledger primary keys make
    repeated scans safe after a crash between ingest and cursor advancement.
    """
    from .interventions import InterventionLedger
    from . import platform_manual
    if platform_manual.enabled():
        return {'scanned':0,'new':0,'mode':'manual'}

    with store.read() as tx:
        previous = tx.one(
            "SELECT ts FROM events WHERE kind='intervention_scan' "
            "ORDER BY id DESC LIMIT 1"
        )
    since = _INTERVENTION_BASELINE
    if previous is not None:
        try:
            stamp = datetime.fromisoformat(str(previous["ts"]).replace("Z", "+00:00"))
            since = max(since, stamp.astimezone(timezone.utc) - timedelta(days=2))
        except (TypeError, ValueError):
            pass  # Bad cursor: scan the full bounded baseline again, fail safely.

    started = time.monotonic()
    ledger = InterventionLedger(store, "Fleet Platform", runtime.draft_root)
    result = ledger.tick(
        repositories=runtime.repositories,
        audit_path=runtime.audit_path,
        approvals_path=runtime.approvals_path,
        guard_log=runtime.guard_log,
        lessons_path=runtime.lessons_path,
        since=since.isoformat(),
        runner=_bounded_intervention_git,
    )
    with store.tx() as tx:
        tx.record_event(kind="intervention_scan", project="Fleet Platform", payload={
            "since": since.isoformat(), "scanned": int(result["scanned"]),
            "new": int(result["new"]),
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        })
    return result


def run_intervention_worker(store: Store, stop_event: threading.Event,
                            runtime: InterventionRuntime, *,
                            interval_s: float = _INTERVENTION_INTERVAL_S) -> None:
    """Periodically ingest real interventions outside Controller.tick / SQLite tx."""
    while not stop_event.is_set():
        try:
            _intervention_cycle(store, runtime)
        except Exception as exc:
            LOG.exception("intervention ledger scan failed; retrying")
            try:
                with store.tx() as tx:
                    tx.record_event(kind="intervention_scan_error",
                                    project="Fleet Platform",
                                    payload={"error": type(exc).__name__})
            except Exception:
                LOG.exception("unable to record intervention scan error")
        stop_event.wait(max(60.0, interval_s))

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Action:
    outcome_id: str
    rev: int
    kind: str
    project: str
    obstacle_id: str = ""
    detail: str = ""


@dataclass(frozen=True)
class IntentProject:
    """Trusted server selection; never populated from the raw intent text."""

    parent: Path
    project_name: str
    size: str
    allowed_roots: tuple[Path, ...]


@dataclass(frozen=True)
class DeliveryRuntime:
    """Server-owned, opt-in post-accept route (never sourced from task text)."""

    allowed_roots: tuple[Path, ...]
    state_db: Path
    subject_version: str  # SHA-256 of independently attested accepted artifact
    env_version: str  # SHA-256 of environment attested at acceptance
    fleet_op: Callable[[str, dict[str, Any]], Any]  # separately authorized Operator
    probe_environment: Callable[[Path, str], str]
    operator_timeout_s: float = 60.0
    # Trusted, independently attested Git provenance of the artifact. Existing
    # deliveries have none and MUST NOT auto-upgrade to claim-tree authority.
    integrated_sha: str = ""


@dataclass(frozen=True)
class HandoffRuntime:
    """Owner-only presentation channel, supplied by trusted server wiring."""

    state_db: Path
    where_to_use: str
    overview_url: str
    overview_available: Callable[[str], bool]
    notifier: Callable[[str], bool] | None = None
    app_id: str | None = None  # Must already be registered by the owner.
    session_ready: Callable[[], bool] | None = None


@dataclass(frozen=True)
class ControllerProvisioning:
    """Trusted launcher-only routing. Never build this from an MCP/intent payload.

    Every missing capability remains disabled. In particular, an Operator route
    cannot be inferred from Policy.exec_roots or the agent's own environment.
    """

    intent_project: Callable[[Any], IntentProject] | None = None
    delivery_route: Callable[[str, str], DeliveryRuntime] | None = None
    handoff_route: Callable[[str, str], HandoffRuntime] | None = None


def load_trusted_delivery_provisioning(
    config_path: Path | str,
    fleet_db: Path | str,
) -> ControllerProvisioning:
    """Read only protected, server-owned delivery authority.

    An independent restricted-identity attestor must atomically publish an
    HMAC-signed, fresh target observation. The artifact is rehashed from pinned
    immutable bytes by this server, never inferred from an agent's manifest.
    No config/attestation => no delivery route. The launcher MUST ACL-protect
    this file, both keys, target observations and artifact paths against agents.

    Accepted TOML:
      [operator_delivery]
      ipc_config = "<absolute protected Operator IPC TOML>"
      state_db = "<absolute durable receipt SQLite>"
      attestation_key = "<absolute independent attestor HMAC key>"
      [[projects]]
      name = "Project"; root = "<absolute registered project root>"
      workspace = "<absolute deployment workspace>"; target = "staging"
      artifact = "<absolute immutable accepted artifact>"
      observation = "<absolute signed attestor JSON>"
      allowed_roots = ["<absolute server-authorized workspace root>"]
    """
    from dev_mcp.operator_cli import load_trusted_operator_ipc

    def pinned_file(value: object, *, limit: int) -> Path:
        if type(value) is not str or not value:
            raise ValueError("trusted_delivery_path_invalid")
        path = Path(value)
        if (not path.is_absolute() or path.is_symlink() or
                not path.is_file() or path.stat().st_size > limit):
            raise ValueError("trusted_delivery_file_unavailable")
        return path

    database = Path(fleet_db)
    if not database.is_absolute():
        raise ValueError("trusted_delivery_database_invalid")
    config = pinned_file(str(config_path), limit=16_384)
    raw = tomllib.loads(config.read_text(encoding="utf-8"))
    if type(raw) is not dict or set(raw) != {"operator_delivery", "projects"}:
        raise ValueError("trusted_delivery_config_invalid")
    common, entries = raw["operator_delivery"], raw["projects"]
    if (type(common) is not dict or
            set(common) != {"ipc_config", "state_db", "attestation_key"} or
            type(entries) is not list or not 0 < len(entries) <= 32):
        raise ValueError("trusted_delivery_config_invalid")
    client = load_trusted_operator_ipc(common["ipc_config"])
    state_db = Path(common["state_db"])
    if not state_db.is_absolute() or state_db.is_symlink() or not state_db.parent.is_dir():
        raise ValueError("trusted_delivery_state_db_invalid")
    key_path = pinned_file(common["attestation_key"], limit=128)
    key = key_path.read_bytes()
    if not 32 <= len(key) <= 128:
        raise ValueError("trusted_delivery_attestation_key_invalid")

    definitions: dict[str, dict[str, Any]] = {}
    for entry in entries:
        fields = {"name", "root", "workspace", "target", "artifact",
                  "observation", "allowed_roots"}
        if type(entry) is not dict or set(entry) != fields:
            raise ValueError("trusted_delivery_project_invalid")
        name, target = entry["name"], entry["target"]
        if (type(name) is not str or not 1 <= len(name) <= 128 or
                type(target) is not str or
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", target) is None or
                name in definitions):
            raise ValueError("trusted_delivery_project_invalid")
        roots = entry["allowed_roots"]
        if type(roots) is not list or not 0 < len(roots) <= 16:
            raise ValueError("trusted_delivery_roots_invalid")
        if (any(type(v) is not str for v in roots)
                or type(entry["root"]) is not str
                or type(entry["workspace"]) is not str):
            raise ValueError("trusted_delivery_roots_invalid")
        paths = [Path(v) for v in roots]
        root, workspace = Path(entry["root"]), Path(entry["workspace"])
        if (any(not p.is_absolute() or p.is_symlink() for p in paths)
                or not root.is_absolute() or root.is_symlink()
                or not workspace.is_absolute() or workspace.is_symlink()
                or not any(workspace == p or p in workspace.parents for p in paths)):
            raise ValueError("trusted_delivery_roots_invalid")
        artifact = pinned_file(entry["artifact"], limit=2_147_483_647)
        observation = pinned_file(entry["observation"], limit=4096)
        definitions[name] = dict(root=root, workspace=workspace, target=target,
                                 artifact=artifact, observation=observation,
                                 allowed_roots=tuple(paths))

    def verified_observation(spec: dict[str, Any], artifact_sha: str) -> str:
        path = pinned_file(str(spec["observation"]), limit=4096)
        value = json.loads(path.read_text(encoding="utf-8"))
        expected = {"project", "workspace", "target", "artifact_sha256",
                    "env_version", "observed_ts", "mac"}
        if type(value) is not dict or set(value) != expected:
            raise ValueError("trusted_delivery_observation_invalid")
        mac = value["mac"]
        body = {k: v for k, v in value.items() if k != "mac"}
        digest = hmac.new(key, b"operator-delivery-observation-v1\x00" +
                          json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("ascii"),
                          hashlib.sha256).hexdigest()
        if (type(mac) is not str or not hmac.compare_digest(mac, digest) or
                body["project"] != spec["name"] or
                body["workspace"] != str(spec["workspace"]) or
                body["target"] != spec["target"] or
                body["artifact_sha256"] != artifact_sha or
                type(body["env_version"]) is not str or
                re.fullmatch(r"[0-9a-f]{64}", body["env_version"]) is None or
                type(body["observed_ts"]) is not int or
                not 0 <= time.time() - body["observed_ts"] <= 60):
            raise ValueError("trusted_delivery_observation_invalid")
        return body["env_version"]

    def route(project: str, _tz: str) -> DeliveryRuntime:
        spec = dict(definitions[project])
        spec["name"] = project
        # The trusted route is bound to the server's registered project, not
        # merely to an attacker-controlled matching name in an event payload.
        with Store.open(str(database)).read() as tx:
            registered = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        if (registered is None or
                Path(registered["root"]).resolve() != spec["root"].resolve()):
            raise ValueError("trusted_delivery_project_mismatch")
        # Compute a digest of independent immutable artifact bytes; never use
        # a digest supplied by the agent, a task or a deploy TOML.
        artifact = pinned_file(str(spec["artifact"]), limit=2_147_483_647)
        digest = hashlib.sha256()
        with artifact.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        artifact_sha = digest.hexdigest()
        env_sha = verified_observation(spec, artifact_sha)

        def probe(workspace: Path, target: str) -> str:
            if workspace != spec["workspace"] or target != spec["target"]:
                raise ValueError("trusted_delivery_probe_scope_invalid")
            return verified_observation(spec, artifact_sha)

        return DeliveryRuntime(
            allowed_roots=spec["allowed_roots"], state_db=state_db,
            subject_version=artifact_sha, env_version=env_sha,
            fleet_op=client, probe_environment=probe,
            operator_timeout_s=client.timeout_s,
        )

    return ControllerProvisioning(delivery_route=route)


class Controller:
    def __init__(self, store: Store, *, policy: Any = None,
                 maintenance: Callable[[], Any] | None = None,
                 intent_project: Callable[[Any], IntentProject] | None = None,
                 delivery_route: Callable[[str, str], DeliveryRuntime] | None = None,
                 handoff_route: Callable[[str, str], HandoffRuntime] | None = None,
                 draft_runner: Callable[[tuple[str, ...], Path], int] | None = None,
                 register_drafts_inline: bool = True):
        self.store = store
        self.policy = policy
        self.maintenance = maintenance
        # Explicit opt-in: the production controller must not invent a project
        # path, name, size or creation authority from an untrusted source.
        self.intent_project = intent_project
        # No delivery until a trusted server integration supplies independent
        # artifact/environment attestation and a deadline-bound Operator IPC.
        self.delivery_route = delivery_route
        self.handoff_route = handoff_route
        # An arbitrary Store/Policy pair must not acquire subprocess authority.
        # Only trusted server wiring (or an explicit in-process test fixture)
        # may supply a narrow isolated acceptance probe.
        self.draft_runner = draft_runner
        self.register_drafts_inline = register_drafts_inline
        self._ready = False

    def route_status(self) -> dict[str, str]:
        """Unambiguous durable startup/heartbeat status, never inferred success."""
        return {
            "intents": "provisioned" if self.intent_project is not None else "unprovisioned",
            "delivery": "provisioned" if self.delivery_route is not None else "unprovisioned",
            "handoff": "provisioned" if self.handoff_route is not None else "unprovisioned",
        }

    def _schema(self) -> None:
        if self._ready:
            return
        outcome.install_schema(self.store)
        with self.store.tx() as tx:
            tx.change("""CREATE TABLE IF NOT EXISTS controller_actions (
                outcome_id TEXT NOT NULL, rev INTEGER NOT NULL, kind TEXT NOT NULL,
                project TEXT NOT NULL, obstacle_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                created_ts TEXT NOT NULL, completed_ts TEXT,
                PRIMARY KEY(outcome_id,rev,kind))""", event="controller.schema")
            tx.change("""CREATE TABLE IF NOT EXISTS controller_cursor (
                name TEXT PRIMARY KEY, last_event INTEGER NOT NULL)""", event="controller.schema")
            tx.change("""CREATE TABLE IF NOT EXISTS controller_delivery_receipts (
                event_id INTEGER PRIMARY KEY, project TEXT NOT NULL,
                tz TEXT NOT NULL, receipt TEXT NOT NULL, ts TEXT NOT NULL
            )""", event="controller.schema")
            tx.change("""CREATE TABLE IF NOT EXISTS controller_handoffs (
                event_id INTEGER PRIMARY KEY, outcome_id TEXT NOT NULL,
                notice_state TEXT NOT NULL, attempts INTEGER NOT NULL,
                updated_ts TEXT NOT NULL
            )""", event="controller.schema")
            tx.change("""CREATE TABLE IF NOT EXISTS controller_intent_retries (
                event_id INTEGER PRIMARY KEY, attempts INTEGER NOT NULL,
                retry_at TEXT NOT NULL, error TEXT NOT NULL
            )""", event="controller.schema")
        self._ready = True

    def _handoff_deliveries(self, errors: list[str]) -> int:
        """Only server-recorded delivered receipts for a proved DONE outcome.

        Unlike an event-only cursor, a delayed outcome closure or unavailable
        owner dashboard remains eligible on future controller ticks.
        """
        if self.handoff_route is None:
            return 0
        from dev_mcp.fleet.handoff import HandoffService, build_packet, validate_overview_url
        from dev_mcp.fleet.windows_notify import gui_session_available

        with self.store.read() as tx:
            deliveries = tx.query(
                "SELECT d.event_id,d.project,d.tz,d.receipt "
                "FROM controller_delivery_receipts d LEFT JOIN controller_handoffs h "
                "ON h.event_id=d.event_id "
                "WHERE h.event_id IS NULL OR "
                "(h.notice_state='pending' AND h.attempts=0) "
                "ORDER BY d.event_id DESC LIMIT 256"
            )
        count = 0
        for entry in deliveries:
            try:
                receipt = json.loads(entry["receipt"])
                if not isinstance(receipt, dict) or receipt.get("status") != "delivered":
                    continue
                with self.store.read() as tx:
                    linked = tx.query(
                        "SELECT o.id,o.evidence_spec FROM outcome_work w "
                        "JOIN outcomes o ON o.id=w.outcome_id "
                        "WHERE w.project=? AND w.tz=? AND o.project=? AND o.state='done'",
                        (entry["project"], entry["tz"], entry["project"]),
                    )
                    previous = tx.one(
                        "SELECT outcome_id FROM controller_handoffs WHERE event_id=?",
                        (entry["event_id"],),
                    )
                matched = []
                for row in linked:
                    try:
                        if json.loads(row["evidence_spec"]).get("subject_version") == receipt.get("subject_version"):
                            matched.append(str(row["id"]))
                    except (TypeError, ValueError, AttributeError):
                        continue
                if len(matched) != 1:
                    continue  # Not done yet, unlinked or ambiguous; never guess.
                oid = matched[0]
                if previous is not None and previous["outcome_id"] != oid:
                    raise ValueError("handoff outcome changed after publication")
                item = outcome.get_outcome(self.store, oid)
                runtime = self.handoff_route(entry["project"], entry["tz"])
                if not isinstance(runtime, HandoffRuntime):
                    raise TypeError("handoff_route must supply trusted HandoffRuntime")
                validate_overview_url(runtime.overview_url)
                # A valid URL is not proof the extension/owner-only dashboard
                # is installed or accessible. A failed probe leaves attempts=0.
                def ready() -> bool:
                    return bool(runtime.overview_available(runtime.overview_url)) and bool(
                        (runtime.session_ready or gui_session_available)()
                    )

                service = HandoffService(
                    runtime.state_db, notifier=runtime.notifier, app_id=runtime.app_id,
                    session_ready=ready,
                )
                if previous is None:
                    # Verify the complete A2 and Operator proof structure before
                    # publishing anything (including dashboard delivery evidence).
                    build_packet(
                        outcome=item, delivery_receipt=receipt,
                        use_location=runtime.where_to_use,
                        overview_url=runtime.overview_url,
                    )
                    digest = hashlib.sha256(
                        f"{entry['event_id']}|{oid}|{receipt['subject_version']}".encode("utf-8")
                    ).hexdigest()
                    with self.store.read() as tx:
                        present = tx.one("SELECT 1 FROM evidence WHERE id=?", (digest,))
                    if present is None:
                        try:
                            outcome.add_evidence(
                                self.store, writer=outcome.server_evidence_writer(self.store),
                                outcome_id=oid, kind="delivery", evidence_id=digest,
                                ref=f"delivery://{digest}",
                                subject_version=receipt["subject_version"],
                                env_version=receipt["env_version"], verdict="pass",
                                producer="trusted_delivery_controller",
                            )
                        except sqlite3.IntegrityError:
                            # Another controller inserted the same deterministic proof.
                            pass
                    handoff = service.publish(
                        outcome=item, delivery_receipt=receipt,
                        use_location=runtime.where_to_use,
                        overview_url=runtime.overview_url,
                    )
                else:
                    # retry_pending itself refuses attempts>0 and unknown
                    # toast results: no duplicate notification even after crash.
                    handoff = service.retry_pending(receipt["event_id"])
                with self.store.tx() as tx:
                    tx.change(
                        "INSERT INTO controller_handoffs(event_id,outcome_id,notice_state,"
                        "attempts,updated_ts) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(event_id) DO UPDATE SET "
                        "notice_state=excluded.notice_state,attempts=excluded.attempts,"
                        "updated_ts=excluded.updated_ts",
                        (entry["event_id"], oid, handoff.notice_state,
                         handoff.attempts, _now()), event="controller.handoff",
                    )
                count += 1
            except Exception as exc:
                # Never surface private packet/adapter contents in a heartbeat.
                errors.append(f"handoff:{entry['event_id']}: {type(exc).__name__}"[:300])
        return count

    def _delivery_defect(self, request: Any) -> str:
        """Durable, deduplicated repair task retaining verified earlier proof."""
        from dev_mcp.fleet.delivery import DefectRequest

        if not isinstance(request, DefectRequest):
            raise TypeError("trusted delivery defect required")
        fingerprint = hashlib.sha256(
            f"{request.project}|{request.acceptance_event}".encode("utf-8")
        ).hexdigest()[:20]
        tz = f"delivery-defect:{fingerprint}"
        with self.store.tx() as tx:
            project = tx.one("SELECT root FROM projects WHERE name=?", (request.project,))
            if project is None:
                raise ValueError("delivery project no longer registered")
            root = Path(project["root"])
            body = (
                f"[fleet-delivery] acceptance={request.acceptance_event}\n"
                "Нужно установить реальное состояние поставки, устранить дефект и "
                "доказать результат серверным Operator. При running не повторяй apply "
                "до независимой сверки целевого окружения.\n"
                + json.dumps({
                    "failure_code": request.failure_code,
                    "subject_version": request.subject_version,
                    "known_good_revision": request.known_good_revision,
                    "accepted_evidence": request.old_evidence,
                    "delivery_evidence": request.delivery_evidence,
                }, ensure_ascii=False, sort_keys=True)
            )
            stamp = _now()
            tx.change(
                "INSERT OR IGNORE INTO tasks "
                "(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,"
                "owns,resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES(?,?,'Проверить сбой поставки','ready',2,'triage','','','[]',"
                "?,'[]','[]',NULL,?,?,?)",
                (request.project, tz, json.dumps([
                    str(root / "ToDo" / "delivery" / f"{tz.replace(':', '-')}.md")
                ], ensure_ascii=False), body, stamp, stamp),
                event="controller.delivery_defect",
            )
        return tz

    def _delivery_events(self, errors: list[str]) -> int:
        """Consume only actual server review accept transitions, never agent prose."""
        if self.delivery_route is None:
            return 0
        from . import goals
        from .claim_accept_flow import release_ready
        from dev_mcp.fleet.delivery import AcceptedEvent, DefectRequest, on_accept
        from dev_mcp.operator_core import verify_public_result

        with self.store.read() as tx:
            last = int(tx.scalar(
                "SELECT last_event FROM controller_cursor WHERE name='delivery_accept'"
            ) or 0)
            events = tx.query(
                "SELECT id,project,tz,worker_id,payload FROM events "
                "WHERE id>? AND kind='accept' ORDER BY id LIMIT 32", (last,),
            )
            # A claim accept can arrive before the asynchronous Integrator
            # performs FF + full gate. A blocked receipt must remain retryable
            # without rewinding the global cursor or starving other projects.
            retries = tx.query(
                "SELECT e.id,e.project,e.tz,e.worker_id,e.payload "
                "FROM events e JOIN controller_delivery_receipts r ON r.event_id=e.id "
                "WHERE e.kind='accept' AND r.receipt "
                "LIKE '%\"status\": \"claim_trunk_not_ready\"%' "
                "ORDER BY e.id LIMIT 16"
            )
            retry_ids = {int(row["id"]) for row in retries}
            events = [*retries, *(row for row in events
                                  if int(row["id"]) not in retry_ids)]
            events.sort(key=lambda row: int(row["id"]))
        processed = 0
        for event in events:
            try:
                project, tz = event["project"], event["tz"]
                payload = json.loads(event["payload"])
                review_tz = payload.get("review_task")
                with self.store.read() as tx:
                    original = tx.one(
                        "SELECT state,acceptance FROM tasks WHERE project=? AND tz=?",
                        (project, tz),
                    )
                    review = tx.one(
                        "SELECT state,kind FROM tasks WHERE project=? AND tz=?",
                        (project, review_tz),
                    ) if isinstance(review_tz, str) else None
                    claim = tx.one(
                        "SELECT 1 FROM claims WHERE project=? AND tz=? AND worker_id=? "
                        "AND state='released' ORDER BY id DESC LIMIT 1",
                        (project, review_tz, event["worker_id"]),
                    ) if review is not None and event["worker_id"] else None
                    registered = tx.one("SELECT root FROM projects WHERE name=?", (project,))
                # Review.decide checked A2 runs and the current subject-tree
                # before emitting 'accept'. Task state alone is NOT an acceptance.
                verified = bool(original and original["state"] == "accepted"
                                and json.loads(original["acceptance"] or "[]")
                                and review and review["state"] == "accepted"
                                and review["kind"] == "review" and claim and registered)
                if verified:
                    route = self.delivery_route(project, tz)
                    if not isinstance(route, DeliveryRuntime):
                        raise TypeError("delivery_route must return trusted DeliveryRuntime")
                    if not 0 < route.operator_timeout_s <= 1800:
                        raise ValueError("unbounded delivery Operator callback")
                    flags = goals.project_info(str(project))
                    if flags.get("claim_trees") is True and flags.get("commit") is True:
                        # The normal server-issued AcceptedEvent is not proof
                        # of a promoted, gated Git revision. Never manufacture
                        # delivery authority from an author/reviewer narrative.
                        # Artifact provenance requires separate trusted route
                        # binding; old 64-digit artifact SHA alone is not a
                        # Git HEAD and cannot authorize this path.
                        with self.store.read() as tx:
                            attestations = tx.query(
                                "SELECT payload FROM events WHERE kind='claim_accept' "
                                "AND project=? AND tz=? AND worker_id=? AND id<? "
                                "ORDER BY id DESC LIMIT 32",
                                (project, tz, event["worker_id"], event["id"]),
                            )
                        permitted = False
                        if re.fullmatch(r"[0-9a-f]{40}", route.integrated_sha):
                            for witness in attestations:
                                try:
                                    candidate = json.loads(witness["payload"] or "{}")
                                except (ValueError, TypeError):
                                    continue
                                if (type(candidate.get("claim_id")) is int and
                                        isinstance(candidate.get("submission_revision"), str) and
                                        release_ready(self.store, candidate["claim_id"],
                                                      candidate["submission_revision"])):
                                    with self.store.read() as tx:
                                        gate = tx.one(
                                            "SELECT payload FROM events WHERE kind='claim_trunk_gate' "
                                            "AND project=? AND tz=? ORDER BY id DESC LIMIT 1",
                                            (project, tz),
                                        )
                                    # Evidence must also belong to the exact
                                    # attested HEAD pinned by the trusted route.
                                    if gate is not None:
                                        try:
                                            data = json.loads(gate["payload"])
                                        except (ValueError, TypeError):
                                            data = {}
                                        if (data.get("claim_id") == candidate["claim_id"]
                                                and data.get("submission_revision") ==
                                                candidate["submission_revision"]
                                                and data.get("integrated_sha") == route.integrated_sha
                                                and type(data.get("rc")) is int and data["rc"] == 0):
                                            permitted = True
                                            break
                        if not permitted:
                            receipt = {"status": "claim_trunk_not_ready",
                                       "event_id": event["id"]}
                            # Persisted below; replay after async gate success.
                            with self.store.tx() as tx:
                                tx.change(
                                    "INSERT INTO controller_delivery_receipts"
                                    "(event_id,project,tz,receipt,ts) VALUES(?,?,?,?,?) "
                                    "ON CONFLICT(event_id) DO UPDATE SET "
                                    "receipt=excluded.receipt,ts=excluded.ts",
                                    (event["id"], str(project or ""), str(tz or ""),
                                     json.dumps(receipt, sort_keys=True), _now()),
                                    event="controller.delivery_receipt",
                                )
                                tx.change(
                                    "INSERT INTO controller_cursor(name,last_event) "
                                    "VALUES('delivery_accept',?) ON CONFLICT(name) "
                                    "DO UPDATE SET last_event=max(last_event,excluded.last_event)",
                                    (event["id"],), event="controller.delivery_cursor",
                                )
                            processed += 1
                            continue
                    event_id = f"accept-{event['id']}"
                    accepted = AcceptedEvent(
                        event_id, project, route.subject_version, route.env_version,
                        {"kind": "review", "verdict": "pass", "producer": "server",
                         "ref": f"server-review-{event['id']}",
                         "subject_version": route.subject_version,
                         "env_version": route.env_version},
                    )

                    def op(name: str, params: dict[str, Any]) -> Any:
                        started = time.monotonic()
                        response = route.fleet_op(name, params)
                        if time.monotonic() - started > route.operator_timeout_s:
                            raise TimeoutError("Operator deadline exceeded")
                        return verify_public_result(response, name)

                    receipt = on_accept(
                        accepted, project_root=Path(registered["root"]),
                        allowed_roots=route.allowed_roots, state_db=route.state_db,
                        fleet_op=op, probe_environment=route.probe_environment,
                        create_defect=self._delivery_defect,
                    )
                    if receipt["status"] in {"running", "repair_pending"} or (
                        receipt["status"] in {"failed", "rollback_failed"}
                        and not receipt.get("repair_work")
                    ):
                        # A crashed 'running' receipt NEVER gets another apply.
                        # Schedule an independent investigation even on replay.
                        self._delivery_defect(DefectRequest(
                            project, event_id, route.subject_version, dict(accepted.evidence),
                            tuple(step["evidence"] for step in receipt.get("evidence", ())
                                  if isinstance(step, dict) and "evidence" in step),
                            ("receipt_running_uncertain" if receipt["status"] == "running"
                             else receipt.get("error_code") or "repair_pending"),
                            str(receipt.get("known_good_revision") or ""),
                        ))
                else:
                    receipt = {"status": "unverified_accept", "event_id": event["id"]}
                with self.store.tx() as tx:
                    tx.change(
                        "INSERT INTO controller_delivery_receipts"
                        "(event_id,project,tz,receipt,ts) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(event_id) DO UPDATE SET "
                        "receipt=excluded.receipt,ts=excluded.ts",
                        (event["id"], str(project or ""), str(tz or ""),
                         json.dumps(receipt, sort_keys=True), _now()),
                        event="controller.delivery_receipt",
                    )
                    tx.change(
                        "INSERT INTO controller_cursor(name,last_event) "
                        "VALUES('delivery_accept',?) "
                        "ON CONFLICT(name) DO UPDATE SET "
                        "last_event=max(last_event,excluded.last_event)",
                        (event["id"],), event="controller.delivery_cursor",
                    )
                processed += 1
            except Exception as exc:
                # Avoid logging external/Operator exception text (may contain secrets).
                errors.append(f"delivery:{event['id']}: {type(exc).__name__}"[:300])
                break  # No ACK when an attempt has no durable receipt.
        return processed

    def _consume_intents(self, errors: list[str]) -> int:
        """Consume TZ-15 created/updated events with durable, post-spawn ACK.

        A crash between materialization and ACK safely replays spawn_project's
        versioned idempotency key. Cancelled events remain for their own owner.
        """
        if self.intent_project is None:
            return 0
        from dev_mcp.intents.sources.model import IntentEnvelope
        from dev_mcp.intents.sources.registry import IntentChange, IntentRegistry
        from dev_mcp.intents.spawn import spawn_project

        registry = IntentRegistry(self.store._connection())
        with self.store.read() as tx:
            pending = tx.query(
                "SELECT event_id,kind,source_id,external_id,revision,"
                "previous_revision,raw_intent,authority,timestamp "
                "FROM intent_source_events WHERE acknowledged=0 "
                "AND kind IN ('created','updated') ORDER BY event_id LIMIT 64"
            )
        consumed = 0
        for row in pending:
            try:
                envelope = IntentEnvelope(
                    row["source_id"], row["external_id"], row["revision"],
                    row["raw_intent"], json.loads(row["authority"]), row["timestamp"],
                )
                change = IntentChange(row["kind"], envelope, row["previous_revision"],
                                      row["event_id"])
                destination = self.intent_project(change)
                if not isinstance(destination, IntentProject):
                    raise TypeError("intent_project must return trusted IntentProject")
                spawn_project(
                    self.store, change, parent=destination.parent,
                    project_name=destination.project_name, size=destination.size,
                    allowed_roots=destination.allowed_roots,
                    triple=wants_triple(row["raw_intent"]),
                )
                with self.store.tx():
                    # ACK uses this Store's SQLite connection, inside the same
                    # write transaction. Failed spawns are NEVER acknowledged.
                    registry.ack_change(row["event_id"])
                consumed += 1
            except Exception as exc:
                errors.append(f"intent:{row['event_id']}: {type(exc).__name__}: {exc}"[:300])
        return consumed

    def _intent_tree(self, outcome_id: str) -> Any:
        """Rehydrate only the exact versioned TZ-16 template shape from Store."""
        from dev_mcp.intents.template import (
            Node, OutcomeTree, KINDS, SIZES, selected_stages, stage_dependencies)

        with self.store.read() as tx:
            current = tx.one("SELECT id,parent_id FROM outcomes WHERE id=?", (outcome_id,))
            if current is None:
                return None
            root_id = current["parent_id"] or current["id"]
            root = tx.one("SELECT * FROM outcomes WHERE id=?", (root_id,))
            if root is None or root["state"] == "dropped":
                return None
            try:
                spec = json.loads(root["spec"])
                if "intent_ref" not in spec or spec.get("size") not in SIZES:
                    return None
                size, revision = spec["size"], spec["revision"]
                kind = spec.get("kind", "build")
                target = spec.get("target", "new")
                if kind not in KINDS:
                    return None
                expected = selected_stages(kind, size)
                records = tx.query("SELECT * FROM outcomes WHERE parent_id=?", (root_id,))
                by_stage = {r["title"]: r for r in records}
                if len(records) != len(expected) or set(by_stage) != set(expected):
                    return None
                nodes = [Node(root_id, "root", None, (), spec,
                              json.loads(root["evidence_spec"]),
                              json.loads(root["authority"]))]
                for stage in expected:
                    r = by_stage[stage]
                    data = json.loads(r["spec"])
                    proof = json.loads(r["evidence_spec"])
                    depends = tuple(data["depends_on"])
                    required = tuple(f"{root_id}:{name}" for name in
                                     stage_dependencies(kind, size, stage))
                    if (r["id"] != f"{root_id}:{stage}" or r["project"] != root["project"]
                            or data.get("revision") != revision or data.get("stage") != stage
                            or proof.get("subject_version") != revision
                            or depends != required):
                        return None
                    nodes.append(Node(r["id"], stage, root_id, depends,
                                      data, proof, json.loads(r["authority"])))
                tree = OutcomeTree(root["project"], root["intent_id"], revision, size,
                                   root_id, tuple(nodes), kind, target,
                                   bool(spec.get("triple")))
                tree.validate()
                return tree
            except (KeyError, TypeError, ValueError, RecursionError):
                return None

    def _reconcile_intent_events(self, errors: list[str]) -> int:
        """Advance independent events; persist failures before moving the cursor."""
        from dev_mcp.intents.spawn import reconcile_tree, route_proven_understood

        with self.store.read() as tx:
            last = int(tx.scalar(
                "SELECT last_event FROM controller_cursor WHERE name='intent_outcome'"
            ) or 0)
            fresh = tx.query(
                "SELECT id,kind,payload FROM events WHERE id>? "
                "AND kind IN ('outcome.closed','outcome.evidence') "
                "ORDER BY id LIMIT 128", (last,),
            )
            retries = tx.query(
                "SELECT e.id,e.kind,e.payload FROM controller_intent_retries r "
                "JOIN events e ON e.id=r.event_id WHERE r.retry_at<=? "
                "ORDER BY r.retry_at LIMIT 16", (_now(),))
            events = {int(event['id']): event for event in (*retries, *fresh)}
        advanced = 0
        for event in events.values():
            failure = None
            try:
                payload = json.loads(event["payload"])
                identifier = payload.get("outcome") if event["kind"] == "outcome.evidence" \
                    else payload.get("id")
                if isinstance(identifier, str):
                    tree = self._intent_tree(identifier)
                    if tree is not None:
                        tree = route_proven_understood(self.store, tree)
                        advanced += len(reconcile_tree(self.store, tree))
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"[:500]
                errors.append(f"intent_reconcile:{event['id']}: {failure}"[:600])
            # A crash before this transaction replays the idempotent effect.
            # A failed effect remains durable, but cannot starve other projects.
            with self.store.tx() as tx:
                if failure:
                    previous = tx.one('SELECT attempts FROM controller_intent_retries WHERE event_id=?',
                                      (event['id'],))
                    attempts = int(previous['attempts']) + 1 if previous else 1
                    retry_at = (datetime.now(timezone.utc) + timedelta(
                        seconds=min(1800, 30 * 2 ** min(attempts - 1, 6)))).isoformat()
                    tx.change('INSERT INTO controller_intent_retries(event_id,attempts,retry_at,error) '
                              'VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET '
                              'attempts=excluded.attempts,retry_at=excluded.retry_at,error=excluded.error',
                              (event['id'], attempts, retry_at, failure),
                              event={'kind': 'controller.intent_retry',
                                     'payload': {'event_id': event['id'], 'attempts': attempts,
                                                 'retry_at': retry_at, 'error': failure}})
                else:
                    tx.change('DELETE FROM controller_intent_retries WHERE event_id=?',
                              (event['id'],), event='controller.intent_retry_resolved')
                tx.change("INSERT INTO controller_cursor(name,last_event) "
                          "VALUES('intent_outcome',?) ON CONFLICT(name) "
                          "DO UPDATE SET last_event=max(last_event,excluded.last_event)",
                          (event['id'],), event='controller.intent_cursor')
        return advanced

    def _legacy(self) -> None:
        if self.maintenance is not None:
            self.maintenance()
            return
        # Import lazily: the facade must not need a controller at import time.
        from . import queue
        from .tools import FleetContext, _fleet_maintenance
        ctx = FleetContext(store=self.store, policy=self.policy)
        _fleet_maintenance(ctx, queue)
        # Wake/recovery belong to the autonomous loop, not fleet_list.
        from . import wake
        book = wake.ChatBook(Path(self.store.path).with_name("chats.json"))
        try:
            wake.give_up(self.store, book)
            wake.orders(self.store, book)
        except Exception:
            LOG.exception("controller wake check failed")

    def _source_actions(self, tx: Any, rows: dict[str, Any]) -> list[Action]:
        last = int(tx.scalar("SELECT last_event FROM controller_cursor WHERE name='source'") or 0)
        events = tx.query(
            "SELECT id,kind,path_norm,payload FROM events WHERE id>? "
            "AND kind IN ('resource','outcome.closed') ORDER BY id", (last,),
        )
        # Cursor advances only after actions are durably reserved, in the SAME tx.
        actions: list[Action] = []
        if events:
            obstacles = tx.query("""SELECT o.id,o.outcome_id,o.change_source,o.predicate
                FROM obstacles o JOIN outcomes r ON r.id=o.outcome_id
                WHERE o.state='open' AND r.state='open' AND o.change_source IS NOT NULL""")
            for event in events:
                source: str = ""
                if event["kind"] == "resource":
                    source = str(event["path_norm"] or "")
                else:
                    try:
                        source = str(json.loads(event["payload"])["id"])
                    except (ValueError, TypeError, KeyError):
                        continue
                for obstacle in obstacles:
                    oid = obstacle["outcome_id"]
                    raw = str(obstacle["change_source"])
                    if raw not in {source, f"outcome:{source}", f"world:{source}",
                                   source.removeprefix("world:"), source.removeprefix("resource:")}:
                        continue
                    row = rows.get(oid)
                    if row is not None:
                        actions.append(Action(oid, row["rev"], f"source:{obstacle['id']}:{event['id']}",
                                              row["project"], obstacle["id"], source))
            tx.change("INSERT INTO controller_cursor(name,last_event) VALUES('source',?) "
                      "ON CONFLICT(name) DO UPDATE SET last_event=excluded.last_event",
                      (events[-1]["id"],), event="controller.source_cursor")
        return actions

    def _discover(self) -> list[Action]:
        # Diagnose outside any write transaction; corrupt legacy JSON must not
        # take down the server or prevent unrelated queue maintenance.
        try:
            violations = outcome.check_invariant(self.store)
        except (ValueError, TypeError, RecursionError):
            violations = []
        with self.store.read() as tx:
            rows = {r["id"]: r for r in tx.query(
                "SELECT id,project,rev,state,evidence_spec,spec FROM outcomes WHERE state='open'"
            )}
            actions: list[Action] = []
            # A cycle is a single blockage.  One repair task for the earliest
            # named node avoids dispatching every participant to repair it.
            first_cycle_node = min((v.outcome_id for v in violations if v.code == "wait_cycle"),
                                   default="")
            for violation in violations:
                if violation.code == "wait_cycle" and violation.outcome_id != first_cycle_node:
                    continue
                if violation.code == "no_next_step" and tx.scalar(
                    "SELECT COUNT(*) FROM outcome_work w JOIN tasks t "
                    "ON t.project=w.project AND t.tz=w.tz "
                    "WHERE w.outcome_id=? AND t.state='accepted'",
                    (violation.outcome_id,),
                ):
                    # A completed step without proof needs a specific proof
                    # task below, not another generic plan.
                    continue
                row = rows.get(violation.outcome_id)
                if row is None:
                    continue
                if violation.code=='no_next_step':
                    try:
                        spec=json.loads(row['spec'])
                        dependencies=spec.get('depends_on',[])
                    except (ValueError,TypeError,AttributeError):
                        dependencies=[];spec={}
                    suppliers = [tx.one('SELECT state FROM outcomes WHERE id=?', (dep,))
                                 for dep in dependencies if isinstance(dep, str)] if isinstance(dependencies, list) else []
                    if (spec.get('stage') and suppliers and len(suppliers) == len(dependencies) and
                            all(dep and dep['state'] in ('open', 'done') for dep in suppliers) and
                            any(dep['state'] == 'open' for dep in suppliers)):
                        # Versioned stages already have a trusted dependency
                        # observer in reconcile_tree. Do not create a planner
                        # for every future phase while its supplier is pending.
                        continue
                kind = {"no_next_step": "plan", "self_wait": "research",
                        "prose_control": "research", "wait_cycle": "cycle"}[violation.code]
                obstacle = violation.detail.split(" ")[1] if violation.code in {"self_wait", "prose_control"} else ""
                actions.append(Action(row["id"], row["rev"], kind, row["project"], obstacle,
                                      violation.detail))
            # If every linked task was accepted, server evidence (not the agent's
            # note) determines whether the result can be closed.
            completed = tx.query("""SELECT w.outcome_id, COUNT(*) AS total,
                SUM(CASE WHEN t.state='accepted' THEN 1 ELSE 0 END) AS passed,
                SUM(CASE WHEN t.state!='accepted' AND NOT
                    (t.kind='triage' AND t.state IN ('ready','frozen')
                     AND t.body LIKE '%[fleet-controller] outcome=%')
                    THEN 1 ELSE 0 END) AS blocking
                FROM outcome_work w JOIN tasks t ON t.project=w.project AND t.tz=w.tz
                GROUP BY w.outcome_id""")
            for item in completed:
                row = rows.get(item["outcome_id"])
                if row is None or not item["passed"] or item["blocking"]:
                    continue
                if _intent_stage_pending(tx, row):
                    continue  # intents/stages.py proves it; plan:proof only once stuck
                try:
                    requirement = json.loads(row["evidence_spec"])
                except (TypeError, ValueError):
                    continue
                proof = tx.query("SELECT kind,subject_version FROM evidence WHERE outcome_id=? AND verdict='pass'",
                                 (row["id"],))
                expected_kind = requirement.get("kind") or requirement.get("evidence_kind")
                version = requirement.get("subject_version")
                credible = any((not expected_kind or p["kind"] == expected_kind)
                               and (not version or p["subject_version"] == version) for p in proof)
                actions.append(Action(row["id"], row["rev"], "verify_done" if credible else "plan:proof",
                                      row["project"], detail="linked works completed; verify evidence"))
            # An unparseable predicate is an unknown method, not a fatal exception.
            for r in tx.query("""SELECT o.id,o.outcome_id,o.predicate FROM obstacles o
                JOIN outcomes a ON a.id=o.outcome_id WHERE o.state='open' AND a.state='open'"""):
                try:
                    json.loads(r["predicate"])
                except (TypeError, ValueError):
                    row = rows.get(r["outcome_id"])
                    if row:
                        actions.append(Action(row["id"], row["rev"], "research", row["project"], r["id"],
                                              "invalid machine predicate"))
        return actions

    def _reserve(self, action: Action, tx: Any = None) -> bool:
        with (self.store.tx() if tx is None else nullcontext(tx)) as tx:
            current = tx.one("SELECT rev,state FROM outcomes WHERE id=?", (action.outcome_id,))
            if current is None or current["state"] != "open" or current["rev"] != action.rev:
                return False
            if tx.one("SELECT 1 FROM controller_actions WHERE outcome_id=? AND rev=? AND kind=?",
                      (action.outcome_id, action.rev, action.kind)) is not None:
                return False
            result = tx.change("""INSERT OR IGNORE INTO controller_actions
                (outcome_id,rev,kind,project,obstacle_id,detail,status,created_ts)
                VALUES(?,?,?,?,?,?,'reserved',?)""",
                (action.outcome_id, action.rev, action.kind, action.project, action.obstacle_id,
                 action.detail, _now()), event="controller.reserve")
            return result.rowcount > 0

    def _apply(self, action: Action, tx: Any = None) -> None:
        with (self.store.tx() if tx is None else nullcontext(tx)) as tx:
            row = tx.one("SELECT status FROM controller_actions WHERE outcome_id=? AND rev=? AND kind=?",
                         (action.outcome_id, action.rev, action.kind))
            if row is None or row["status"] == "complete":
                return
            item = tx.one("SELECT state,rev FROM outcomes WHERE id=?", (action.outcome_id,))
            if item is None or item["state"] != "open" or item["rev"] != action.rev:
                tx.change("UPDATE controller_actions SET status='complete',completed_ts=? "
                          "WHERE outcome_id=? AND rev=? AND kind=?", (_now(), action.outcome_id, action.rev,
                                                                     action.kind), event="controller.stale")
                return
            if action.kind == "verify_done":
                children = tx.scalar("SELECT COUNT(*) FROM outcomes WHERE parent_id=? AND state='open'",
                                     (action.outcome_id,))
                if not children:
                    # A no-longer-needed planning triage must not remain queued
                    # after independent server proof has closed its outcome.
                    tx.change(
                        "UPDATE tasks SET state='rejected',updated_at=? "
                        "WHERE state IN ('ready','frozen') AND kind='triage' "
                        "AND body LIKE '[fleet-triage] subject=%' "
                        "AND (project,tz) IN (SELECT project,tz FROM outcome_work WHERE outcome_id=?) "
                        "AND body LIKE '%[fleet-controller] outcome=%'",
                        (_now(), action.outcome_id),
                        event="controller.plan_superseded",
                    )
                    tx.change("UPDATE outcomes SET state='done',rev=rev+1,closed_ts=? WHERE id=?",
                              (_now(), action.outcome_id),
                              event={"kind": "outcome.closed", "project": action.project,
                                     "payload": {"id": action.outcome_id, "state": "done"}})
            elif action.kind.startswith("source:"):
                obstacle = tx.one("SELECT predicate,change_source,state FROM obstacles WHERE id=?",
                                  (action.obstacle_id,))
                if obstacle and obstacle["state"] == "open":
                    try:
                        predicate = json.loads(obstacle["predicate"])
                    except (TypeError, ValueError):
                        predicate = {}
                    supplier = predicate.get("requires_outcome") if isinstance(predicate, dict) else None
                    resolved = bool(supplier and tx.scalar(
                        "SELECT COUNT(*) FROM outcomes WHERE id=? AND state='done'", (supplier,)))
                    if resolved and predicate.get("requires_proof"):
                        expected = tx.one("SELECT evidence_spec FROM outcomes WHERE id=?", (supplier,))
                        try:
                            proof = json.loads(expected["evidence_spec"])
                            resolved = bool(tx.scalar(
                                "SELECT COUNT(*) FROM evidence WHERE outcome_id=? "
                                "AND verdict='pass' AND kind=? AND subject_version=?",
                                (supplier, proof["kind"], proof["subject_version"]),
                            ))
                        except (TypeError, ValueError, KeyError):
                            resolved = False
                    if not resolved and isinstance(predicate, dict):
                        resource = predicate.get("resource") or predicate.get("world_resource")
                        if resource:
                            resolved = world.last_state(tx, str(resource))[0] == world.UP
                    if resolved:
                        tx.change("UPDATE obstacles SET state='resolved' WHERE id=?", (action.obstacle_id,),
                                  event={"kind": "outcome.obstacle_resolved", "project": action.project})
            else:
                if action.kind == "research" and action.obstacle_id:
                    tx.change("UPDATE obstacles SET type='unknown_method',change_source=NULL," 
                              "predicate=? WHERE id=? AND state='open'",
                              (json.dumps({"investigate": action.detail}), action.obstacle_id),
                              event="controller.research_obstacle")
                if action.kind in {"plan", "plan:proof", "research", "cycle"}:
                    from . import judgment
                    project_root = tx.one(
                        "SELECT root FROM projects WHERE name=?", (action.project,)
                    )
                    if project_root is None:
                        raise ValueError(f"project {action.project!r} missing")
                    # Outcome IDs are untrusted input: avoid separators, ADS and
                    # collisions in the plan filename while keeping the original ID
                    # in the durable subject and link.
                    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", action.outcome_id)[:48].strip("-") or "outcome"
                    digest = hashlib.sha256(action.outcome_id.encode("utf-8")).hexdigest()[:10]
                    rel = f"ToDo/plans/{action.kind.replace(':', '-')}-{slug}-{digest}.md"
                    title = {"plan": "Спланировать остаток результата",
                             "plan:proof": "Получить проверяемое доказательство результата",
                             "research": "Исследовать препятствие",
                             "cycle": "Разорвать цикл ожидания"}[action.kind]
                    subject = f"{action.kind}:{action.outcome_id}"
                    body = (
                        f"[fleet-controller] outcome={action.outcome_id}\n"
                        f"Проблема: {action.detail}\n"
                        f"Создай исполнимый план в `{rel}`: следующий результат, "
                        "конкретная проверка и связь с исходным outcome. "
                        "Если нужна реализация вне owns, создай request_note, "
                        "не оставляй ожидание собственной приёмки.\n"
                        + judgment._RESULT_RULE
                    )
                    tz = judgment._insert(
                        tx, action.project, subject, title, body,
                        [judgment._abs(str(project_root["root"]), rel)], 2,
                        {"outcome": action.outcome_id, "controller_action": action.kind},
                    )
                    tx.change(
                        "INSERT OR IGNORE INTO outcome_work(outcome_id,project,tz) VALUES(?,?,?)",
                        (action.outcome_id, action.project, tz),
                        event={"kind": "outcome.work_linked", "project": action.project,
                               "tz": tz, "payload": {"outcome": action.outcome_id}},
                    )
            tx.change("UPDATE controller_actions SET status='complete',completed_ts=? "
                      "WHERE outcome_id=? AND rev=? AND kind=?",
                      (_now(), action.outcome_id, action.rev, action.kind), event="controller.complete")

    def tick(self) -> int:
        started = time.perf_counter()
        phase_started = started
        phases: dict[str, float] = {}
        def mark(name: str) -> None:
            nonlocal phase_started
            now = time.perf_counter()
            phases[name] = round((now-phase_started)*1000, 1)
            phase_started = now
        count = 0
        errors: list[str] = []
        try:
            self._schema()
            mark('schema')
            # TZ-39: ingest verified deployment receipts before any queue thaw.
            # Cursor + event + thaw are transactional; the legacy maintenance
            # hook may also scan them, but replay is idempotent.
            try:
                from . import deploy_events
                receipt = deploy_events.tick(self.store)
                count += int(receipt.get("thawed", 0))
            except Exception as exc:
                errors.append(f"deploy_events: {type(exc).__name__}: {exc}"[:300])
            mark('deploy_events')
            try:
                self._legacy()
            except Exception as exc:
                errors.append(f"maintenance: {type(exc).__name__}: {exc}"[:300])
            mark('maintenance')
            # TZ-38: convert planner drafts without requiring fleet_take or a
            # Claude/Codex session. Only the trusted launcher-supplied policy
            # enables writes; preflight probes an isolated fixture, never trunk.
            if self.policy is not None and self.register_drafts_inline:
                try:
                    from .drafts import register_drafts
                    registered = register_drafts(
                        self.store, self.policy, dry_runner=self.draft_runner,
                    )
                    count += len(registered["registered"])
                except Exception as exc:
                    errors.append(f"drafts: {type(exc).__name__}: {exc}"[:300])
            mark('drafts')
            count += self._consume_intents(errors)
            mark('intent_ingest')
            try:
                actions = self._discover()
            except Exception as exc:
                LOG.exception('outcome discovery failed; continuing independent reconciliation')
                errors.append(f'discover: {type(exc).__name__}: {exc}'[:300])
                actions = []
            mark('discover')
            with self.store.tx() as tx:
                rows = {r["id"]: r for r in tx.query(
                    "SELECT id,project,rev FROM outcomes WHERE state='open'")}
                actions += self._source_actions(tx, rows)
                # Reserve sources and cursor in the same durable transaction:
                # no event may be acknowledged before its effect is reserved.
                for action in dict.fromkeys(actions):
                    self._reserve(action, tx)
            # A pending action survives a crash after reservation; replay is safe
            # because every effect is INSERT OR IGNORE or a guarded UPDATE.
            with self.store.read() as tx:
                pending = [Action(r["outcome_id"], r["rev"], r["kind"], r["project"],
                                  r["obstacle_id"], r["detail"]) for r in tx.query(
                    "SELECT * FROM controller_actions WHERE status='reserved' ORDER BY created_ts")]
            # The apply path is database-only, so batch a bounded number of
            # atomic effects in one short tx instead of 1000s of commits.
            for start in range(0, len(pending), 64):
                with self.store.tx() as tx:
                    for action in pending[start:start + 64]:
                        try:
                            self._apply(action, tx)
                            count += 1
                        except Exception as exc:
                            errors.append(f"{action.kind}: {type(exc).__name__}: {exc}"[:300])
            mark('reserve_apply')
            count += self._reconcile_intent_events(errors)
            mark('intent_reconcile')
            count += self._delivery_events(errors)
            mark('delivery')
            count += self._handoff_deliveries(errors)
            mark('handoff')
        except Exception as exc:
            LOG.exception('controller tick failed; independent workers remain active')
            errors.append(f"tick: {type(exc).__name__}: {exc}"[:300])
            mark('interrupted')
        try:
            with self.store.tx() as tx:
                tx.record_event(kind="controller_tick", payload={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "actions": count, "errors": errors[:8],
                    "phase_ms": phases,
                    "routes": self.route_status(),
                })
        except Exception:
            LOG.exception("unable to record controller heartbeat")
        if errors:
            LOG.warning("controller errors: %s", errors)
        return count


def controller_health(store: Store, *, now: datetime | None = None) -> str:
    """Read-only health line suitable for the console's first line."""
    now = now or datetime.now(timezone.utc)
    with store.read() as tx:
        row = tx.one("SELECT ts,payload FROM events WHERE kind='controller_tick' ORDER BY id DESC LIMIT 1")
    if row is None:
        return "controller: нет тиков"
    try:
        age = (now - datetime.fromisoformat(row["ts"])).total_seconds()
        data = json.loads(row["payload"])
    except (TypeError, ValueError):
        return "controller: повреждён heartbeat"
    status = ("ПРОПУСК >5 мин" if age > STALE_TICK_S else
              "ошибки" if data.get('errors') else "ok")
    return f"controller: {status}, {max(0, int(age))} с, действий {data.get('actions', 0)}"


def run_daily_drill_scheduler(stop_event: threading.Event, *,
                              run_daily: Callable[..., dict[str, Any]] | None = None,
                              now: Callable[[], datetime] | None = None,
                              sandbox_root: Path | None = None) -> None:
    """Run synthetic drills on UTC days, isolated from the live controller Store.

    This is an in-process daemon, not a console command or a Windows scheduled
    task.  The drill contract owns its per-day atomic lease and verified report.
    The scheduler never receives the production DB, project root, or policy.
    """
    if run_daily is None:
        from .drills import run_daily
    if now is None:
        now = lambda: datetime.now(timezone.utc)
    sandbox = (sandbox_root if sandbox_root is not None else
               Path(tempfile.gettempdir()).resolve() / "fleet-drill-sandbox")
    while not stop_event.is_set():
        today = now().astimezone(timezone.utc)
        try:
            report = run_daily(sandbox, now=today)
            if report.get("status") == "recovered":
                LOG.info("daily synthetic drill %s: recovered (report persisted)", report.get("date"))
            else:
                LOG.warning("daily synthetic drill %s: capability_gap=%s",
                            report.get("date"), report.get("capability_gaps", []))
        except Exception:
            # No synthetic failure may kill the production controller loop.
            LOG.exception("daily synthetic drill failed; isolated sandbox retained for audit")
        next_day = datetime.combine((today + timedelta(days=1)).date(),
                                    datetime.min.time(), tzinfo=timezone.utc)
        remaining = max(1.0, (next_day - now().astimezone(timezone.utc)).total_seconds())
        stop_event.wait(remaining)


def _cross_review_followups(store: Store, reports: list[dict[str, Any]]) -> int:
    """Turn validated P0/P1 Gemini hypotheses into reviewable defect work.

    Never auto-accept or close a source task. A stale SHA cannot create work.
    """
    from . import evidence

    created = 0
    for report in reports:
        if report.get("state") != "issues":
            continue
        with store.read() as tx:
            request = tx.one(
                "SELECT project,tz,payload FROM events "
                "WHERE id=? AND kind='cross_review_requested'",
                (report.get("request_id"),),
            )
            if request is None:
                continue
            result = tx.one(
                "SELECT payload FROM events WHERE kind='cross_review_result' "
                "AND project=? AND tz=? AND id>? ORDER BY id DESC LIMIT 1",
                (request["project"], request["tz"], report["request_id"]),
            )
        if result is None:
            continue
        try:
            pinned = json.loads(request["payload"])
            model = json.loads(result["payload"])
            if (model["state"] != "issues" or model["tree_sha"] != pinned["tree_sha"]
                    or report["tree_sha"] != pinned["tree_sha"]
                    or not isinstance(model["obstacles"], list)):
                continue
            claim_id = int(pinned["claim_id"])
            current = lambda: evidence.tree_hash(evidence.claim_paths(store, claim_id))
            if current() != pinned["tree_sha"]:
                continue  # Stale source, cannot turn old model text into repair.
            for defect in model["obstacles"][:30]:
                if (not isinstance(defect, dict) or defect.get("kind") != "defect"
                        or defect.get("next_work") != "fix" or defect.get("source") != "model_hypothesis"
                        or defect.get("severity") not in {"P0", "P1"}
                        or defect.get("tree_sha") != pinned["tree_sha"]
                        or defect.get("snapshot_sha") != model["snapshot_sha"]
                        or defect.get("project") != request["project"]
                        or defect.get("task") != request["tz"]
                        or not isinstance(defect.get("finding_id"), str)
                        or not isinstance(defect.get("testcase"), str)
                        or not isinstance(defect.get("proof_refs"), list)
                        or not defect["proof_refs"]):
                    continue
                fingerprint = hashlib.sha256(json.dumps([
                    request["project"], request["tz"], model["snapshot_sha"],
                    defect["finding_id"],
                ], separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()[:20]
                tz = f"cross-defect:{fingerprint}"
                body = (
                    f"[fleet-cross-review] source={request['project']}/{request['tz']}\n"
                    f"Gemini hypothesis, not independent acceptance: {defect['severity']}\n"
                    f"Tree SHA: {pinned['tree_sha']}\n"
                    f"Snapshot SHA: {model['snapshot_sha']}\n"
                    f"Finding: {str(defect.get('explanation', ''))[:900]}\n"
                    f"Reproduction: {defect['testcase'][:900]}\n"
                    "References: " + json.dumps(defect["proof_refs"][:20], ensure_ascii=True)
                )
                if current() != pinned["tree_sha"]:
                    break
                stamp = _now()
                with store.tx() as tx:
                    inserted = tx.change(
                        "INSERT OR IGNORE INTO tasks(project,tz,title,state,priority,kind,"
                        "spec_path,spec_sha,needs,owns,resources,acceptance,author_worker,"
                        "body,created_at,updated_at) VALUES(?,?,?,'ready',2,'triage',"
                        "'','','[]','[]','[]','[]',NULL,?,?,?)",
                        (request["project"], tz,
                         f"Проверить {defect['severity']} Gemini: {request['tz']}",
                         body, stamp, stamp), event="controller.cross_review_defect",
                    )
                    created += int(inserted.rowcount > 0)
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return created


def _cross_review_independent_outcomes(store: Store, *, limit: int = 32) -> int:
    """Calibrate model hypotheses only from later server-issued reviewer verdicts."""
    from . import cross_review

    with store.read() as tx:
        reviews = tx.query(
            "SELECT e.id,e.project,e.tz,e.worker_id,e.kind FROM events e "
            "WHERE e.kind IN ('accept','reject') "
            "AND EXISTS (SELECT 1 FROM events r WHERE r.kind='cross_review_requested' "
            "AND r.project=e.project AND r.tz=e.tz AND r.id<e.id) "
            "ORDER BY e.id DESC LIMIT ?", (limit,),
        )
    updated = 0
    for event in reviews:
        proof_id = f"event:{event['id']}"
        with store.read() as tx:
            existing = tx.query("SELECT payload FROM events "
                                "WHERE kind='cross_review_outcome' AND project=? AND tz=?",
                                (event["project"], event["tz"]))
            requested = tx.one(
                "SELECT payload FROM events WHERE kind='cross_review_requested' "
                "AND project=? AND tz=? AND id<? ORDER BY id DESC LIMIT 1",
                (event["project"], event["tz"], event["id"]),
            )
            owner = tx.one("SELECT author_worker FROM tasks WHERE project=? AND tz=?",
                           (event["project"], event["tz"]))
        if requested is None or (owner is not None and owner["author_worker"] == event["worker_id"]):
            continue
        if any(json.loads(row["payload"]).get("proof_id") == proof_id for row in existing):
            continue
        try:
            snapshot = cross_review._submitted_snapshot(
                store, event["project"], event["tz"], json.loads(requested["payload"])
            )
            cross_review.record_outcome(
                store, snapshot, independent_clear=event["kind"] == "accept",
                source="fleet_review", proof_id=proof_id,
            )
            updated += 1
        except (ValueError, KeyError, TypeError, OSError):
            continue  # No current tree proof, no calibration.
    return updated


def run_cross_review_worker(store: Store, stop_event: threading.Event, *,
                            interval_s: float = 30,
                            ask: Callable[..., Any] | None = None) -> None:
    """One model attempt per wake, outside Controller.tick and all submit txs."""
    from . import cross_review

    while not stop_event.is_set():
        try:
            attempts = cross_review.process_pending(store, ask=ask, limit=1)
            _cross_review_followups(store, attempts)
            _cross_review_independent_outcomes(store)
        except Exception:
            LOG.exception("cross-review worker failed, retrying on next wake")
        stop_event.wait(max(1.0, interval_s))


def _production_handoff_route(
    db_path: Path, *, repository_root: Path | None = None,
    profile_root: Path | None = None,
    chrome_probe: Callable[[], bool] | None = None,
) -> Callable[[str, str], HandoffRuntime] | None:
    """Route to the *installed* owner-only FleetChrome dashboard, fail closed.

    A dashboard source file is not proof of an installed/enabled extension.
    Its stable ID comes from the locally packaged public manifest key, while
    the live availability probe requires the matching enabled unpacked Chrome
    extension and a running FleetChrome debugging endpoint. Neither an agent
    message nor an environment variable can select another dashboard/AUMID.
    An unavailable UI still gets a durable handoff packet with zero toasts.
    """
    from .handoff import validate_overview_url

    root = (repository_root or Path(__file__).resolve().parents[3]).resolve()
    if db_path.resolve() != (root / "data" / "fleet" / "fleet.sqlite").resolve():
        return None
    extension = (root / "extension" / "fleet-tabs").resolve()
    try:
        manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
        public_key = base64.b64decode(manifest["key"], validate=True)
        if len(public_key) < 128 or not (extension / "dashboard" / "index.html").is_file():
            return None
        digest = hashlib.sha256(public_key).digest()[:16]
        extension_id = "".join(chr(ord("a") + nibble) for byte in digest
                               for nibble in (byte >> 4, byte & 15))
        url = validate_overview_url(
            f"chrome-extension://{extension_id}/dashboard/index.html"
        )
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None

    chrome_profile = profile_root or (
        Path.home() / "AppData" / "Local" / "FleetChrome" / "Default"
    )

    def live_chrome() -> bool:
        try:
            # Explicitly bypass the system proxy: this is loopback only.
            # A running Chrome is not proof that *this* extension is running.
            # Require an actual DevTools target owned by the expected ID.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open("http://127.0.0.1:9223/json", timeout=0.5) as response:
                targets = json.loads(response.read(262144))
            return isinstance(targets, list) and any(
                isinstance(target, dict) and isinstance(target.get("url"), str)
                and target["url"].startswith(f"chrome-extension://{extension_id}/")
                and target.get("type") in {"service_worker", "page", "background_page"}
                for target in targets
            )
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def dashboard_available(requested_url: str) -> bool:
        if requested_url != url:
            return False
        try:
            saved = json.loads(
                (chrome_profile / "Secure Preferences").read_text(encoding="utf-8")
            )
            installed = saved["extensions"]["settings"][extension_id]
            # location=4 means unpacked; Chrome state=1 means enabled.
            # A disabled extension can retain location/path with no
            # disable_reasons, so missing/malformed state must fail closed.
            if (type(installed.get("state")) is not int or
                    installed["state"] != 1 or
                    installed.get("location") != 4 or
                    Path(installed["path"]).resolve() != extension or
                    installed.get("disable_reasons")):
                return False
            return (extension / "dashboard" / "app.js").is_file() and (
                chrome_probe() if chrome_probe is not None else live_chrome()
            )
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def registered_aumid() -> str | None:
        """No registration, no WinRT attempt. Never create a shortcut/HKCU key."""
        if sys.platform != "win32":
            return None
        # Both the pre-existing Start-menu shortcut and owner registration
        # must exist. A test-only AUMID string is not itself authorization.
        shortcut = (Path.home() / "AppData" / "Roaming" / "Microsoft" /
                    "Windows" / "Start Menu" / "Programs" / "Fleet Owner Notify.lnk")
        if not shortcut.is_file():
            return None
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Classes\AppUserModelId\Fleet.Owner.Notify",
            ):
                return "Fleet.Owner.Notify"
        except (ImportError, OSError):
            return None

    state_db = db_path.with_name("handoff.sqlite")

    def provision(_project: str, _tz: str) -> HandoffRuntime:
        return HandoffRuntime(
            state_db=state_db,
            where_to_use="Локальная страница «Результаты флота»",
            overview_url=url,
            overview_available=dashboard_available,
            app_id=registered_aumid(),
        )

    return provision


def run_claim_maintenance_worker(
        store: Store, stop_event: threading.Event, *,
        interval_s: float = 300.0) -> None:
    """TZ-43 periodic audit and conditional, explicit-retention cleanup.

    An independent thread bounds impact on Controller.tick. Flag-off projects
    are excluded; absent trusted retention only diagnostic records are created.
    """
    from datetime import timedelta
    from .claim_maintenance import ClaimMaintenance

    while not stop_event.is_set():
        started = time.perf_counter()
        try:
            with store.read() as tx:
                projects = [str(r["name"]) for r in tx.query(
                    "SELECT name FROM projects WHERE active=1 ORDER BY name LIMIT 64")]
            maintenance = ClaimMaintenance(store)
            for project in projects:
                if stop_event.is_set():
                    break
                if not maintenance._enabled(project):
                    continue
                # No retention setting means observation only. A trusted
                # project policy can authorize cleanup of fully proven,
                # previously enumerated candidates after a bounded cutoff.
                flags = maintenance.settings(project)
                retention = flags.get("claim_tree_retention_days")
                authorized = type(retention) is int and 1 <= retention <= 365
                cutoff = datetime.now(timezone.utc) - timedelta(
                    days=retention if authorized else 7)
                audit = maintenance.audit(project, cutoff=cutoff)
                plan = maintenance.plan(project, older_than=cutoff)
                repairs = maintenance.repair_findings(audit)
                with store.tx() as tx:
                    tx.record_event(kind="claim_maintenance_dry_run", project=project,
                                    payload={
                                        "trunk_sha": plan.trunk_sha,
                                        "cutoff": plan.cutoff,
                                        "authorized": authorized,
                                        "candidates": [{
                                            "claim_id": c.claim_id, "path": c.path,
                                            "branch": c.branch, "tree_head": c.head_sha}
                                            for c in plan.candidates[:50]],
                                        "total_candidates": len(plan.candidates),
                                    })
                cleaned = maintenance.execute(plan) if authorized else None
                with store.tx() as tx:
                    tx.record_event(kind="claim_maintenance_tick", project=project,
                                    payload={
                                        "enabled": audit.enabled,
                                        "error": audit.error[:200],
                                        "trunk_sha": audit.trunk_sha,
                                        "candidates": [c.claim_id for c in plan.candidates[:50]],
                                        "repairs": list(repairs[:50]),
                                        "findings": len(audit.findings),
                                        "cleanup_authorized": authorized,
                                        "removed": list(cleaned.removed) if cleaned else [],
                                        "held": list(cleaned.held) if cleaned else [],
                                        "cleanup_errors": list(cleaned.errors[:8]) if cleaned else [],
                                    })
        except Exception as exc:
            LOG.exception("claim maintenance worker failed closed")
            try:
                with store.tx() as tx:
                    tx.record_event(kind="claim_maintenance_error", payload={
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    })
            except Exception:
                LOG.exception("claim maintenance failure receipt unavailable")
        stop_event.wait(interval_s)



def _intent_stage_pending(tx: Any, row: Any) -> bool:
    """An intent stage awaiting the stage prover is not yet a 'find proof' problem."""
    try:
        stage = json.loads(row["spec"]).get("stage")
    except (TypeError, ValueError, AttributeError):
        return False
    if not stage:
        return False
    from dev_mcp.intents.stages import is_stuck
    # outcome_work is many-to-many: a stage can also have a linked resolver or
    # research task. Only the canonical stage task belongs to its stage prover.
    # Assuming one linked work crashed ALL project reconciliation after the
    # first resolver was accepted (live 15.10: expected one row, got 2).
    tz = str(row['id']).rsplit(':', 1)[0] + '-' + str(stage)
    work = tx.one("SELECT project,tz FROM outcome_work WHERE outcome_id=? AND tz=?",
                  (row['id'], tz))
    return work is not None and not is_stuck(tx, work["project"], work["tz"])


# Owner intent inbox (canon 00-final §7). The folder is closed to agents by
# policy tree_deny, so a file there is the owner's own grant to create one
# project beneath INTENT_PARENT. Name and roots come from this code, never
# from the intent text.
INTENT_INBOX = machine.INTENT_INBOX
INTENT_PARENT = machine.HUB
INTENT_ROOTS = machine.WORKSPACES
_INTENT_PREFIX = re.compile(r"^[0-9]+[-_. ]*")


def owner_intent_project(change: Any) -> IntentProject:
    stem = Path(change.envelope.external_id).stem
    name = _INTENT_PREFIX.sub("", stem).strip().lower().replace(" ", "-") or stem
    # Имя занято чужой папкой (компонентом) — не падать SpawnError каждые 30 с, а взять
    # «<имя>-intent» (13.10: интенция ai-relay.md против C:\hub\ai-relay).
    base = name
    for n in range(1, 50):
        folder = INTENT_PARENT / name
        if not folder.exists() or (folder / "component.json").is_file():
            break
        name = f"{base}-intent" if n == 1 else f"{base}-intent-{n}"
    return IntentProject(parent=INTENT_PARENT, project_name=name, size="large",
                         allowed_roots=INTENT_ROOTS)


def _poll_owner_intents(store: Store) -> None:
    from dev_mcp.intents.sources.files import DEFAULT_ROOT, FileIntentAdapter
    from dev_mcp.intents.sources.registry import IntentRegistry
    custom = os.path.normcase(str(INTENT_INBOX)) != os.path.normcase(str(DEFAULT_ROOT))
    adapter = FileIntentAdapter(INTENT_INBOX, source_id=(
        f"file:{INTENT_INBOX.as_posix()}" if custom else None), authority={
        "create_project": True,
        "project_roots": [str(root) for root in INTENT_ROOTS],
    })
    if not INTENT_INBOX.is_dir():
        return
    result = IntentRegistry(store._connection()).poll(adapter)
    if result.errors:
        # One unreadable file blocks the whole scan (no false cancellations);
        # make it visible instead of silently waiting.
        with store.tx() as tx:
            tx.record_event(kind="intent_poll_incomplete",
                            payload={"errors": list(result.errors)[:16]})


def run_draft_registration_worker(store: Store, policy: Any, runner: Any,
                                  stop_event: threading.Event, *, interval_s: float = 30) -> None:
    """Slow isolated preflights have their own lifecycle and observable heartbeat."""
    from .drafts import register_drafts
    while not stop_event.is_set():
        started = time.perf_counter()
        try:
            result = register_drafts(store, policy, dry_runner=runner)
            receipt = {'registered': len(result['registered']), 'invalid': len(result['invalid']),
                       'deferred': len(result.get('deferred', {})), 'status': 'completed'}
        except Exception as exc:
            LOG.exception('draft registration worker deferred')
            receipt = {'status': 'failed', 'error': type(exc).__name__}
        try:
            with store.tx() as tx:
                tx.record_event(kind='draft_registration_tick', payload={**receipt,
                    'duration_ms': round((time.perf_counter()-started)*1000, 1),
                })
        except Exception:
            LOG.exception('draft registration heartbeat unavailable; worker will retry')
        stop_event.wait(interval_s)


def run_controller(db_path: str | Path, stop_event: threading.Event, *,
                   interval_s: float = INTERVAL_S, policy: Any = None,
                   provisioning: ControllerProvisioning | None = None) -> None:
    """Real server entrypoint. No owner grants/Operator IPC => explicit fail-closed.

    A future trusted launcher may supply narrowly authorized routes; passing
    callbacks via an agent request, a project manifest or Windows env is forbidden.
    """
    if provisioning is not None and not isinstance(provisioning, ControllerProvisioning):
        raise TypeError("trusted_controller_provisioning_required")
    store = Store.open(str(db_path))
    routes = provisioning or ControllerProvisioning()
    # The installed launcher is allowed to probe drafts only against its OWN
    # registry. A foreign/test DB cannot turn Controller.tick into a process
    # launcher by providing a permissive Policy.
    draft_runner = _trusted_draft_runner(db_path)
    # The server's own production database and packaged extension are the
    # only implicit handoff authority. An arbitrary test database, an intent,
    # a project config or a caller-supplied URL must never grant a route.
    handoff_route = routes.handoff_route or _production_handoff_route(Path(db_path))
    for callback in (routes.intent_project, routes.delivery_route, routes.handoff_route):
        if callback is not None and not callable(callback):
            raise TypeError("invalid_controller_route")
    controller = Controller(
        store, policy=policy, intent_project=routes.intent_project,
        delivery_route=routes.delivery_route, handoff_route=handoff_route,
        draft_runner=draft_runner,
        register_drafts_inline=False,
    )
    route_state = controller.route_status()
    # The production __main__.py launcher supplies only policy today. Record the
    # missing trusted provisioning in the DB and logs rather than silently
    # reporting an autonomous delivery/handoff path that cannot execute.
    with store.tx() as tx:
        tx.record_event(kind="controller_provisioning", payload={
            "routes": route_state,
            "status": "ready" if all(x == "provisioned" for x in route_state.values())
            else "fail_closed",
            "reason": "trusted_server_routes_not_configured"
            if any(x == "unprovisioned" for x in route_state.values()) else "",
        })
    if any(x == "unprovisioned" for x in route_state.values()):
        LOG.warning("controller routes unprovisioned (fail closed): %s", route_state)
    threading.Thread(target=run_daily_drill_scheduler, args=(stop_event,),
                     daemon=True, name="dev-mcp-daily-synthetic-drills").start()
    if policy is not None and draft_runner is not None:
        from .owner_notices import run_worker as run_owner_notices
        threading.Thread(target=run_owner_notices, args=(store,stop_event), daemon=True,
                         name='dev-mcp-owner-notices').start()
        from dev_mcp.owner.ticktick_board import run_worker as run_ticktick_board
        threading.Thread(target=run_ticktick_board, args=(store,stop_event), daemon=True,
                         name='dev-mcp-ticktick-board').start()
        from .storage_guard import run_worker as run_storage_guard
        threading.Thread(target=run_storage_guard, args=(store,stop_event), daemon=True,
                         name='dev-mcp-storage-guard').start()
        from .net_sensor import run_worker as run_network_sensor
        threading.Thread(target=run_network_sensor, args=(store,stop_event), daemon=True,
                         name='dev-mcp-network-sensor').start()
        from .queue_intake import run_worker as run_queue_intake_worker
        threading.Thread(target=run_queue_intake_worker,
                         args=(store, policy, draft_runner, stop_event), daemon=True,
                         name='dev-mcp-queue-intake').start()
        threading.Thread(target=run_draft_registration_worker,
                         args=(store, policy, draft_runner, stop_event), daemon=True,
                         name='dev-mcp-draft-registration').start()
    threading.Thread(target=run_cross_review_worker, args=(store, stop_event),
                     daemon=True, name="dev-mcp-gemini-cross-review").start()
    # Share the production controller lifecycle and shutdown event. Regater uses
    # the same per-project RLock as Integrator._gate to avoid parallel gates.
    threading.Thread(target=run_regater, args=(db_path, stop_event),
                     daemon=True, name="dev-mcp-red-trunk-regate").start()
    # TZ-43: starts at boot and repeats independently of 30s controller ticks.
    # Explicitly read-only Git maintenance; no cleanup/prune or lock takeover.
    threading.Thread(target=run_claim_maintenance_worker, args=(store, stop_event),
                     daemon=True, name="dev-mcp-claim-maintenance-audit").start()
    # TZ-48: only the installed controller may read trusted host Git/audit
    # sources. A dedicated worker bounds git calls and never stalls 30s ticks.
    intervention_runtime = _trusted_intervention_runtime(db_path)
    if intervention_runtime is not None:
        threading.Thread(
            target=run_intervention_worker,
            args=(store, stop_event, intervention_runtime),
            daemon=True, name="dev-mcp-intervention-ledger",
        ).start()
    if routes.intent_project is owner_intent_project:
        from dev_mcp.intents.stages import run_stage_prover
        threading.Thread(target=run_stage_prover, args=(store, stop_event),
                         daemon=True, name="dev-mcp-intent-stage-prover").start()
    while not stop_event.is_set():
        if routes.intent_project is owner_intent_project:
            try:
                _poll_owner_intents(store)
            except Exception as exc:
                LOG.exception("owner intent poll failed")
                with store.tx() as tx:
                    tx.record_event(kind="intent_poll_error",
                                    payload={"error": type(exc).__name__})
        controller.tick()
        # TZ-47: stall detection is a bounded, registry-backed controller step,
        # not a Claude/session timer. Failures are observed without halting the
        # production controller; an optional independent service has its own
        # cross-process lease and the same SQLite deduplication.
        try:
            _controller_flow_watchdog_step(store)
        except Exception as exc:
            LOG.exception("fleet stall watchdog maintenance failed")
            try:
                with store.tx() as tx:
                    tx.record_event(kind="watchdog_tick_error", payload={
                        "error": type(exc).__name__, "source": "controller",
                    })
            except Exception:
                LOG.exception("stall watchdog error could not be recorded")
        try:
            _controller_guard_watch_step(store, db_path)
        except Exception as exc:
            LOG.exception("guard watchdog maintenance failed")
            try:
                with store.tx() as tx:
                    tx.record_event(kind="guard_watch_error", payload={
                        "error": type(exc).__name__,
                    })
            except Exception:
                LOG.exception("guard watchdog error could not be recorded")
        stop_event.wait(interval_s)
