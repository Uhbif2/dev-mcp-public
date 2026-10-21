"""Guarded, reversible Fleet Platform deploys; independent from dev_mcp imports."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import metrics

PROTECTED = ("policy.toml", "guard/")
WATCH_SECONDS = 60
PROBE_DEADLINE_SECONDS = 120
SELF_UPDATE_HANDOFF_SECONDS = 180
CODES = frozenset({
    "start", "no_change", "dirty_live", "protected_path", "tests_red", "tests_detail", "watch_error", "unreviewed_paths", "unreviewed_detail",
    "not_ff", "merge_error", "validation_error", "restart_error",
    "probe_red", "canary_cycle", "canary_maintenance", "canary_skew",
    "canary_unavailable", "canary_unhealthy", "rollback_ok", "rollback_dirty",
    "rollback_failed", "safe_mode", "known_good_missing", "known_good",
    "deployed", "extension_reload", "watch_busy", "incident_written", "bootstrap_failed",
    "git_error", "lock_busy", "unexpected_error", "journal_error",
    "tests_ok", "merge", "restart", "probe", "canary_tick", "rollback",
    "self_update", "self_update_reverted", "alive_after_restart",
    "release_window_open", "release_window_pending", "release_window_success",
    "release_window_rollback", "release_blocked", "release_recovered",
    "release_window_stale",
    "restart_detail", "safe_mode_skipped",
})


class GuardFailure(RuntimeError):
    def __init__(self, code: str):
        if code not in CODES:
            raise ValueError("unknown guard event code")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GuardConfig:
    live: Path
    work: Path
    health_url: str = ""
    port: int = 8766
    state_root: Path | None = None

    def resolved_state(self) -> Path:
        if self.state_root is not None:
            return self.state_root
        value = os.environ.get("LOCALAPPDATA")
        return (Path(value) if value else Path.home() / ".local" / "state") / "dev-mcp-guard"


class DeployGuard:
    TEST_PULSE_S = 30.0  # 45-minute release guardian must stay alive during long candidate tests

    def __init__(
        self, config: GuardConfig, *,
        validator: Callable[[Path], bool] | None = None,
        restarter: Callable[[Path], bool] | None = None,
        prober: Callable[[Path], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        canary_seconds: int = metrics.CANARY_SECONDS,
        sample_seconds: int = metrics.SAMPLE_SECONDS,
        review_check: Callable[[str, str], list[str]] | None = None,
    ):
        self.config = config
        self.review_check = review_check or self._unreviewed
        self.validator = validator or self._validate
        # Injected restarters are one-shot test seams; production always uses
        # the real launcher plus an independent, side-effect-free spawn probe.
        self._injected_restarter = restarter is not None
        self.restarter = restarter or self._restart
        self._initial_restarter = self.restarter
        self.prober = prober or self._probe
        self.sleep = sleeper
        self.now = now
        self.canary_seconds = canary_seconds
        self.sample_seconds = sample_seconds
        self.state_dir = config.resolved_state()
        self.db = config.live / "data" / "fleet" / "fleet.sqlite"
        self.log = config.live / "logs" / "platform-guard.jsonl"
        self._candidate = ""
        self._phase = "idle"

    def event(self, code: str, **fields: str | int | float) -> None:
        if code not in CODES:
            raise ValueError("unregistered event code")
        payload = {"ts": datetime.now(timezone.utc).isoformat(), "code": code}
        payload.update(fields)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as out:
            out.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def heartbeat(self, phase: str, candidate: str | None = None) -> None:
        """Atomically publish the watchdog's minute pulse, including during canary."""
        self._phase = phase
        if candidate is not None:
            self._candidate = candidate
        path = self.config.live / "data" / "guard-heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "phase": phase, "candidate": self._candidate,
                   "version": self._running_guard_version(),
                   "handoff_token": os.environ.get("DEV_MCP_GUARD_HANDOFF_TOKEN", ""),
                   "time": datetime.now(timezone.utc).isoformat()}
        tmp = path.with_name(path.name + f".{os.getpid()}.new")
        try:
            with tmp.open("w", encoding="utf-8") as out:
                json.dump(payload, out, ensure_ascii=False, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def phase_event(self, code: str, stage: str, **fields: str | int | float) -> None:
        self.heartbeat(code)
        self.event(code, stage=stage, candidate=self._candidate, **fields)

    def _known_good_logged(self, candidate: str) -> bool:
        """Accept a current receipt or a provably completed legacy handoff.

        The pre-TZ37 guard wrote state.deployed and the known-good Git tag
        before two consecutive, stage-less known_good/deployed log entries.
        Those entries have no SHA: never attach them to a candidate without
        the matching state, tag and state-file write time. A partial legacy
        receipt or an interrupted later restart cannot attest a deployment.
        """
        try:
            with self.log.open("r", encoding="utf-8") as source:
                entries = [json.loads(line) for line in source if line.strip()]
            if any(not isinstance(entry, dict) for entry in entries):
                return False
            if any(entry.get("code") == "known_good"
                   and entry.get("candidate") == candidate
                   and entry.get("stage") == "after" for entry in entries):
                return True

            state_file = self.state_dir / "state.json"
            if (state_file.is_symlink() or not state_file.is_file()
                    or self._state().get("deployed") != candidate):
                return False
            tagged = self.git(self.config.live, "rev-parse", "--verify",
                              "refs/tags/platform/known-good", ok=False).strip()
            if tagged != candidate:
                return False
            state_written = state_file.stat().st_mtime
            # Only the final consecutive legacy success pair can qualify.
            # A subsequent merge/restart/rollback invalidates that proof; the
            # current controller's start-before/start-after events do not.
            disruptive = {"merge", "restart", "rollback", "rollback_ok",
                          "rollback_failed", "safe_mode", "known_good_missing"}
            for i in range(len(entries) - 2, -1, -1):
                good, deployed = entries[i], entries[i + 1]
                if (good.get("code") != "known_good"
                        or deployed.get("code") != "deployed"
                        or any(key in good or key in deployed
                               for key in ("candidate", "stage"))):
                    continue
                good_ts = datetime.fromisoformat(good["ts"]).timestamp()
                deployed_ts = datetime.fromisoformat(deployed["ts"]).timestamp()
                # NTFS write-time granularity can place a just-written state
                # slightly after the journal's wall-clock timestamp.
                if not (-2 <= good_ts - state_written <= 60
                        and 0 <= deployed_ts - good_ts <= 30):
                    continue
                if any(entry.get("code") in disruptive
                       for entry in entries[i + 2:]):
                    return False
                return True
            return False
        except (OSError, ValueError, TypeError, KeyError, GuardFailure):
            return False

    def _exec(self, argv: list[str], *, cwd: Path, timeout: int = 180,
              env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, cwd=cwd, timeout=timeout, env=env,
                              check=False, capture_output=True, text=True, encoding="utf-8",
                              errors="replace",
                              # страж живёт под pythonw без консоли: без флага каждый git/uv/
                              # pytest открывает у владельца видимое окно (жалоба 09.10)
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def git(self, repo: Path, *argv: str, ok: bool = True) -> str:
        result = self._exec(["git", "-C", str(repo), *argv], cwd=repo, timeout=90)
        if ok and result.returncode:
            raise GuardFailure("git_error")
        return result.stdout

    def git_rc(self, repo: Path, *argv: str) -> int:
        return int(self._exec(["git", "-C", str(repo), *argv], cwd=repo, timeout=90).returncode)

    def _clean(self) -> bool:
        status = self.git(self.config.live, "status", "--porcelain", "--untracked-files=all")
        # Our durable trace and pulse can be new files on a freshly provisioned
        # live checkout with no .gitignore yet. Do not mask ANY other untracked
        # or tracked modification (in particular never ignore all of data/).
        runtime = {"logs/platform-guard.jsonl", "data/guard-heartbeat.json"}
        return not any(
            line for line in status.splitlines()
            if not (line.startswith("?? ") and line[3:].replace("\\", "/") in runtime)
        )

    def _state(self) -> dict:
        try:
            value = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **changes: object) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        state = self._state()
        state.update(changes)
        path = self.state_dir / "state.json"
        tmp = path.with_name(path.name + f".{os.getpid()}.new")
        try:
            with tmp.open("w", encoding="utf-8") as out:
                json.dump(state, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _verified_good(self, sha: str) -> bool:
        """A HEAD is a rollback target only with matching durable good receipt."""
        state = self._state()
        if (not sha or state.get("deployed") != sha
                or state.get("reattest_pending") == sha):
            return False
        tagged = self.git(self.config.live, "rev-parse", "--verify",
                          "refs/tags/platform/known-good", ok=False).strip()
        return tagged == sha and self._known_good_logged(sha)

    def _reattest_live(self, sha: str, tagged: str) -> bool:
        """Independently attest a manually fast-forwarded live before moving good.

        An earlier journal entry is only a trigger, NEVER a sufficient proof.
        Revalidate the exact live tree, restart/probe the running server, and
        finish a fresh canary before advancing the Git tag. The durable pending
        flag closes the crash gap between publishing the tag and its receipt.
        """
        live = self.config.live
        state = self._state()
        if (not sha or state.get("deployed") != sha
                or not self._known_good_logged(sha)
                or (tagged == sha and state.get("reattest_pending") != sha)):
            return False
        if (tagged and self.git_rc(live, "merge-base", "--is-ancestor", tagged, sha)):
            return False
        if (not self._clean() or self.git(live, "rev-parse", "HEAD").strip() != sha):
            return False
        changed = self._paths(tagged, sha) if tagged else ()
        # A manual synchronization cannot silently attest policy or guard
        # self-updates: those require the separate protected handoff.
        if self._policy_paths(changed) or self._guard_paths(changed):
            return False
        if tagged and self.review_check(tagged, sha):
            return False

        pending_candidate = self._candidate
        self._save_state(reattest_pending=sha)
        try:
            self.phase_event("tests_ok", "before")
            with self._candidate_tree(sha) as isolated:
                if not self.validator(isolated):
                    return False
            self.phase_event("tests_ok", "after")
            baseline = metrics.snapshot(self.db)
            if not self._restart_preflight(live):
                return False
            # An injected restarter is a one-shot simulation; production runs
            # restart.py only after the independent spawn probe succeeds.
            if (not self._injected_restarter or self.restarter is not self._initial_restarter) and not self.restarter(live):
                return False
            self.heartbeat("probe", sha)
            self.phase_event("probe", "before")
            try:
                if not self.prober(live):
                    return False
            except Exception:
                return False
            self.phase_event("probe", "after")
            if not self._guard_restart_heartbeat_confirmed():
                return False
            if self._canary(baseline) is not None:
                return False
            if (not self._clean() or
                    self.git(live, "rev-parse", "HEAD").strip() != sha):
                return False
            if self.git_rc(live, "tag", "-f", "platform/known-good", sha):
                return False
            self._save_state(deployed=sha)
            self.event("known_good", candidate=sha, stage="after", attested="independent")
            self._save_state(reattest_pending=None)
            return self._verified_good(sha)
        finally:
            self.heartbeat("start", pending_candidate)

    def _window(self, candidate: str, restore: str, phase: str,
                *, window_id: str = "") -> dict:
        prior = self._state().get("release_window") or {}
        window = {
            "window_id": window_id or str(prior.get("window_id") or uuid.uuid4().hex),
            "candidate_sha": candidate, "restore_sha": restore,
            "phase": phase,
            "started_at": prior.get("started_at", self.now()),
            "canary_started_at": (self.now() if phase == "canary"
                                  and prior.get("canary_started_at") is None
                                  else prior.get("canary_started_at")),
        }
        self._save_state(release_window=window)
        return window

    def _failed_release(self, candidate: str, reason: str) -> None:
        """Durably suppress retries of identical broken bytes, not a fixed HEAD."""
        window = self._state().get("release_window") or {}
        pending = self.git(self.config.work, "rev-parse", "platform/work", ok=False).strip()
        self._save_state(release_blocked_sha=candidate, release_window=None)
        self.event("release_window_rollback", candidate=candidate, reason=reason,
                   window_id=str(window.get("window_id") or ""),
                   restore_head=str(window.get("restore_sha") or ""),
                   pending_head=pending)

    def _recover_release(self, live_head: str) -> str | None:
        """Recover an interrupted FF from the *previous proven* good SHA.

        No validator or canary is run on this path. A window whose disk state is
        inconsistent with either pinned SHA fails closed without rewriting Git.
        """
        window = self._state().get("release_window")
        if not isinstance(window, dict) or not window:
            return None
        if window.get("phase") in {"idle", "complete"}:
            return None
        candidate = str(window.get("candidate_sha") or "")
        restore = str(window.get("restore_sha") or "")
        phase = str(window.get("phase") or "")
        state = self._state()
        handoff = state.get("self_update")
        # TZ-44 intentionally returns *before* FF. Its version-bound new
        # supervisor is the one allowed to continue the pinned candidate.
        if phase == "validating" and live_head == restore:
            if (isinstance(handoff, dict)
                    and handoff.get("candidate") == candidate
                    and handoff.get("version") == self._running_guard_version()):
                self._save_state(release_window=None)
                return None
            if state.get("guard_blocked_candidate") == candidate:
                # Old supervisor already failed handoff; leave its permanent
                # SHA blacklist authoritative instead of retrying this window.
                self._save_state(release_window=None)
                return None
        if (live_head not in {candidate, restore} and self._verified_good(live_head)
                and all(sha and not self.git_rc(self.config.live, "merge-base",
                                                "--is-ancestor", sha, live_head)
                        for sha in (candidate, restore))):
            # Урок 45: окно осталось от прежней версии стража, а live с тех пор
            # ушёл вперёд и подтверждён (тег, state, журнал). Окно устарело —
            # закрыть с уликой; иначе страж навсегда стоит на known_good_missing.
            self._save_state(release_window=None)
            self.event("release_window_stale", candidate=candidate,
                       restore_head=restore, live_head=live_head)
            return None
        if not candidate or not restore or candidate == restore or (
            live_head not in {candidate, restore}
        ):
            raise GuardFailure("known_good_missing")
        if phase == "finalizing" and live_head == candidate:
            # The 45-minute canary finished, but Git tag, state.json, and
            # journal cannot be published as one filesystem transaction.
            # Restore the previously journal-proven good even when the tag
            # has already been advanced to the incomplete new candidate.
            if self._verified_good(candidate):
                # Promotion is already complete; only the final journal/window
                # cleanup was interrupted. Never rerun tests or the canary.
                self._save_state(release_window=None, release_blocked_sha="")
                self.event("release_window_success", candidate=candidate,
                           restore_head=restore, pending_head=candidate)
                self.event("deployed", candidate=candidate)
                return "no_change"
            if (self._state().get("deployed") == restore
                    and self._known_good_logged(restore)):
                self.git(self.config.live, "tag", "-f", "platform/known-good", restore)
            elif (self._state().get("deployed") == candidate
                    and self._known_good_logged(restore)):
                self.git(self.config.live, "tag", "-f", "platform/known-good", restore)
            else:
                raise GuardFailure("known_good_missing")
            if not self._rollback(restore):
                self._safe_mode()
                raise GuardFailure("rollback_failed")
            # Restore the *two* public good pointers along with the Git tree.
            # Keeping state.deployed=candidate after a rollback makes the
            # proof inconsistent and permanently wedges later release windows.
            self._save_state(deployed=restore)
            self._failed_release(candidate, "incomplete_good_promotion")
            self.event("release_recovered", candidate=candidate, restore_head=restore,
                       window_id=str(window.get("window_id") or ""))
            return "release_recovered"
        if not self._verified_good(restore):
            raise GuardFailure("known_good_missing")
        if live_head == candidate:
            if phase not in {"deploying", "probing", "canary"}:
                raise GuardFailure("known_good_missing")
            if not self._rollback(restore):
                self._safe_mode()
                raise GuardFailure("rollback_failed")
        self._failed_release(candidate, "crash_recovery")
        self.event("release_recovered", candidate=candidate, restore_head=restore,
                   window_id=str(window.get("window_id") or ""))
        return "release_recovered"

    def _running_guard_version(self) -> str:
        """Return a version only when executing an installed immutable package."""
        runtime = (self.config.live / "data" / "guard-runtime").resolve()
        source = Path(__file__).resolve()
        if source.parent.name == "guard" and source.parent.parent.parent == runtime:
            return source.parent.parent.name
        return ""

    @staticmethod
    def _guard_paths(paths: tuple[str, ...]) -> bool:
        return any((q := p.replace("\\", "/").casefold().lstrip("./")) == "guard"
                   or q.startswith("guard/") for p in paths)

    @staticmethod
    def _policy_paths(paths: tuple[str, ...]) -> bool:
        return any(p.replace("\\", "/").casefold().lstrip("./") == "policy.toml"
                   for p in paths)

    def _spawn_handoff(self, version: str, token: str) -> subprocess.Popen:
        runtime = self.config.live / "data" / "guard-runtime"
        # Execute the staged candidate supervisor WITHOUT replacing the
        # persistent autostart launcher. It locates the same runtime via env.
        launcher = runtime / version / "run_guard.pyw"
        binary = Path(sys.executable).resolve()
        if os.name == "nt":
            binary = binary.with_name("pythonw.exe")
        if not binary.is_file() or not launcher.is_file():
            raise GuardFailure("self_update_reverted")
        return subprocess.Popen(
            [str(binary), str(launcher), "--handoff-version", version,
             "--handoff-token", token], cwd=runtime,
            env={**os.environ, "DEV_MCP_GUARD_RUNTIME": str(runtime)},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _self_update(self, candidate: str, package: Path) -> str:
        """Tested candidate is installed beside the old guard, never over it.

        The new independent pythonw supervisor must prove a fresh heartbeat
        from the candidate version before the old watch loop exits. The old
        guard retains the kernel deploy lock during this handshake.
        """
        from .install import install, package_version

        state = self._state()
        if state.get("guard_blocked_candidate") == candidate:
            raise GuardFailure("self_update_reverted")
        # Running != proven good: a candidate may have launched but not passed
        # its 45-minute canary. Never elevate that copy to rollback target.
        previous = str(state.get("guard_known_good", ""))
        runtime = self.config.live / "data" / "guard-runtime"
        if not previous or not (runtime / previous / "guard" / "__main__.py").is_file():
            raise GuardFailure("known_good_missing")
        version = package_version(package)
        if version == previous:
            raise GuardFailure("self_update_reverted")
        target = install(package, base=runtime, register=False)
        if target.name != version:
            raise GuardFailure("self_update_reverted")
        token = uuid.uuid4().hex
        self._save_state(guard_known_good=previous, guard_blocked_candidate="",
                         self_update={"candidate": candidate, "version": version,
                                      "previous": previous, "token": token})
        self.event("self_update", candidate=candidate, version=version,
                   previous=previous)
        process = None
        try:
            process = self._spawn_handoff(version, token)
            start = self.now()
            pulse_path = self.config.live / "data" / "guard-heartbeat.json"
            while self.now() - start < SELF_UPDATE_HANDOFF_SECONDS:
                if process.poll() is not None:
                    break
                try:
                    pulse = json.loads(pulse_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pulse = {}
                if (isinstance(pulse, dict) and pulse.get("version") == version
                        and pulse.get("handoff_token") == token
                        and isinstance(pulse.get("pid"), int)
                        and pulse["pid"] > 0 and pulse["pid"] != os.getpid()):
                    return "self_update"
                self.sleep(1)
        except (OSError, ValueError):
            pass
        # The child may not have started at all. The old guard stays alive;
        # mark the SHA as blocked so no fresh copy can retry it every minute.
        # Handoff failed before FF: no release is in progress. Persist the SHA
        # blacklist, but do not leave a stale validating window to recover.
        self._save_state(self_update=None, guard_blocked_candidate=candidate,
                         release_window=None)
        self.event("self_update_reverted", candidate=candidate, previous=previous)
        if process is not None and process.poll() is None:
            process.terminate()
        return "self_update_reverted"

    @contextmanager
    def lock(self):
        """Crash-safe kernel lock rather than a stale O_EXCL file."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "deploy.lock"
        with path.open("a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError:
                        raise GuardFailure("lock_busy") from None
                else:
                    import fcntl
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        raise GuardFailure("lock_busy") from None
                acquired = True
                yield
            finally:
                if acquired:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reload_extension(self, paths: tuple[str, ...]) -> None:
        """Правки расширения вступают в силу только после его перезагрузки в FleetChrome.
        Сбой перезагрузки не валит выкатку: супервизор перезагрузит при пропуске тиков."""
        if not any(p.replace("\\", "/").startswith("extension/fleet-tabs/") for p in paths):
            return
        script = self.config.live / "extension" / "fleet-tabs" / "reload-extension.mjs"
        env = {**os.environ, "NO_PROXY": "*", "no_proxy": "*"}
        try:
            rc = self._exec(["node", str(script)], cwd=script.parent, timeout=60, env=env).returncode
        except Exception:  # noqa: BLE001
            rc = -1
        self.event("extension_reload", rc=rc)

    def _unreviewed(self, prior: str, candidate: str) -> list[str]:
        """Ствол = только принятая работа (решение 09.10). Проверяются КОММИТЫ диапазона, а не файлы:
        файлы ствола правятся постоянно, но в ствол их кладёт интегратор после приёмки (трейлер
        Fleet-Reviewer). Коммит мимо приёмки, затрагивающий не только ToDo/, выкатывается, лишь если
        его sha есть в data/guard-approved-commits.txt живого каталога (флоту закрыт tree_deny)."""
        try:
            approved_file = self.config.live / "data" / "guard-approved-commits.txt"
            approved = set()
            if approved_file.is_file():
                approved = {line.split()[0] for line in approved_file.read_text(encoding="utf-8").splitlines()
                            if line.strip() and not line.startswith("#")}
            work = self.config.work
            pending: list[str] = []
            for sha in self.git(work, "rev-list", "--no-merges", f"{prior}..{candidate}").split():
                if sha in approved or any(sha.startswith(a) for a in approved if len(a) >= 7):
                    continue
                if "Fleet-Reviewer:" in self.git(work, "log", "-1", "--format=%B", sha):
                    continue
                # Git quotes non-ASCII names with octal escapes by default; a quoted
                # "ToDo/..." then fails startswith("todo/") and blocks a docs-only commit.
                # NUL output is unquoted and also preserves newlines inside filenames.
                # Disable rename detection: both ends must pass the ToDo-only exception.
                files = [f for f in self.git(work, "show", "--no-renames",
                                              "--name-only", "-z", "--format=", sha).split("\0") if f]
                # Git tree paths use '/' on every OS. A literal backslash can
                # be part of a POSIX filename; treating it as '/' would exempt a
                # root-level "ToDo\\code.py" from the review gate.
                if files and all(f.casefold().startswith("todo/") for f in files):
                    continue
                pending.append(f"{sha[:9]} {files[:3]}")
            return pending
        except Exception as exc:  # noqa: BLE001 — fail closed
            return [f"review-check: {type(exc).__name__}"]

    def _paths(self, old: str, candidate: str) -> tuple[str, ...]:
        # Do not let rename detection hide the old protected path: Git's
        # --name-only reports only the destination of an R100 rename. Treat
        # renames as deletion + addition so both paths are checked.
        result = self._exec(
            ["git", "-C", str(self.config.live), "diff", "--no-renames",
             "--name-only", "-z", old, candidate],
            cwd=self.config.live, timeout=90,
        )
        if result.returncode:
            raise GuardFailure("git_error")
        return tuple(p for p in result.stdout.split("\0") if p)

    @staticmethod
    def _protected(paths: tuple[str, ...]) -> bool:
        for raw in paths:
            p = raw.replace("\\", "/").casefold().lstrip("./")
            if p == "policy.toml" or p == "guard" or p.startswith("guard/"):
                return True
        return False

    @contextmanager
    def _candidate_tree(self, sha: str):
        with tempfile.TemporaryDirectory(prefix="dev-mcp-guard-") as tmp:
            path = Path(tmp) / "candidate"
            self.git(self.config.work, "worktree", "add", "--detach", str(path), sha)
            try:
                yield path
            finally:
                self.git(self.config.work, "worktree", "remove", "--force", str(path), ok=False)

    def _validate(self, path: Path) -> bool:
        if self._exec(["uv", "sync", "--frozen", "--extra", "dev"], cwd=path,
                      timeout=900).returncode:
            return False
        python = (path / ".venv" / "Scripts" / "python.exe") if os.name == "nt" else (
            path / ".venv" / "bin" / "python")
        if not python.exists():
            return False
        # The independent supervisor expires a guard with no heartbeat after
        # 180 s; a full HEAD validation can run much longer. Keep the heartbeat
        # alive without interrupting the single, pinned candidate validation.
        stop = threading.Event()
        phase = getattr(self, "_phase", "tests")

        def pulse() -> None:
            while not stop.wait(self.TEST_PULSE_S):
                try:
                    self.heartbeat(phase)
                except Exception:
                    # Heartbeat errors must not strand the process; deploy()
                    # independently fails closed on required journal writes.
                    pass

        beat = threading.Thread(target=pulse, name="guard-test-pulse", daemon=True)
        beat.start()
        try:
            result = self._exec([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                                cwd=path, timeout=1200)
        finally:
            stop.set()
            beat.join(timeout=5)
        if result.returncode:
            # Ремонту нужна улика, а не только код: какие тесты упали (до 20 строк).
            failed = [line for line in result.stdout.splitlines()
                      if line.startswith(("FAILED ", "ERROR "))][:20]
            self.event("tests_detail", rc=result.returncode, failed=" | ".join(failed)[:4000])
        return result.returncode == 0

    def _restart_preflight(self, path: Path) -> bool:
        """Prove the launcher can spawn without running restart.py or changing live.

        Windows job restrictions are exercised with the same executable,
        environment and creation flags as the actual restart. A successful
        no-op child proves spawn viability, not the later restart's outcome.
        Injected restarters are test doubles: their single call represents
        the simulated restart and must also fail before any live Git write.
        """
        if self._injected_restarter:
            if self.restarter is not self._initial_restarter:
                # A later callback replacement has no dry-run contract;
                # invoke it only at the actual post-merge restart boundary.
                return True
            try:
                return bool(self.restarter(path))
            except Exception as exc:
                self.event("restart_detail", error=f"preflight: {exc}"[:500])
                return False
        python = path / ".venv" / "Scripts" / "python.exe"
        if not python.is_file() or not (path / "tools" / "restart.py").is_file():
            self.event("restart_detail", error="preflight_missing_launcher")
            return False
        flags = [{"start_new_session": True}]
        if os.name == "nt":
            base = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            flags = [{"creationflags": base | subprocess.CREATE_BREAKAWAY_FROM_JOB},
                     {"creationflags": base}]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(path / "src")
        for extra in flags:
            try:
                child = subprocess.Popen(
                    [str(python), "-I", "-c", "pass"], cwd=path, env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", close_fds=True, **extra)
            except OSError as exc:
                self.event("restart_detail", error=f"preflight: {exc}"[:500])
                continue
            with child:
                try:
                    child.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    self.event("restart_detail", error="preflight_timeout")
                    return False
            if child.returncode:
                self.event("restart_detail", error="preflight_exit", rc=child.returncode)
            return child.returncode == 0
        return False

    def _restart(self, path: Path) -> bool:
        self._restart_launch_failed = False
        python = path / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            self._restart_launch_failed = True
            self.event("restart_detail", error="no_venv_python")
            return False
        env = os.environ.copy()
        env["PYTHONPATH"] = str(path / "src")
        spawn = dict(cwd=path, env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, encoding="utf-8", errors="replace",
                     close_fds=True)
        # Урок 37: job автозапуска (Планировщик) может запрещать breakaway —
        # Popen падает мгновенно, выкатка откатывается. Отдельный job рестартеру
        # не нужен: tools/restart.py гасит только слушателей порта и щадит
        # стража (TZ-51). Поэтому breakaway — попытка, а не условие.
        flagsets = [{"start_new_session": True}]
        if os.name == "nt":
            base = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            flagsets = [{"creationflags": base | subprocess.CREATE_BREAKAWAY_FROM_JOB},
                        {"creationflags": base}]
        for extra in flagsets:
            try:
                child = subprocess.Popen([str(python), "tools/restart.py"], **spawn, **extra)
            except OSError as exc:
                self.event("restart_detail", error=f"{type(exc).__name__}: {exc}"[:500],
                           breakaway=int("creationflags" in extra and bool(
                               extra["creationflags"] & getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))))
                continue
            with child:
                try:
                    output, _ = child.communicate(timeout=180)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate()
                    self.event("restart_detail", error="timeout")
                    return False
            if child.returncode:
                tail = " | ".join((output or "").strip().splitlines()[-15:])
                self.event("restart_detail", rc=child.returncode, tail=tail[:2000])
            return child.returncode == 0
        self._restart_launch_failed = True
        return False

    def _guard_restart_heartbeat_confirmed(self) -> bool:
        """Read back the guard's *fresh* on-disk probe pulse, not a stale log."""
        pulse_path = self.config.live / "data" / "guard-heartbeat.json"
        try:
            pulse = json.loads(pulse_path.read_text(encoding="utf-8"))
            if not isinstance(pulse, dict):
                return False
            stamp = datetime.fromisoformat(pulse["time"])
            if stamp.tzinfo is None:
                return False
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            return (-5 <= age <= 30
                    and pulse.get("pid") == os.getpid()
                    and pulse.get("phase") == "probe"
                    and pulse.get("candidate") == self._candidate
                    and pulse.get("version") == self._running_guard_version()
                    and pulse.get("handoff_token")
                    == os.environ.get("DEV_MCP_GUARD_HANDOFF_TOKEN", ""))
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def _worker(self, path: Path, args: list[str]) -> str:
        worker = path / "worker.cmd"
        if os.name != "nt" or not worker.is_file():
            raise GuardFailure("probe_red")
        result = self._exec(["cmd.exe", "/d", "/c", str(worker), *args], cwd=path, timeout=25)
        if result.returncode:
            raise GuardFailure("probe_red")
        return result.stdout

    def _prepare_probe(self, path: Path) -> bool:
        """Register a fresh, isolated trivial task before each real worker cycle.

        Submitted tasks are immutable until review. A new probe ID avoids
        silently reactivating an already submitted task or rewriting fleet SQL.
        The isolated project lives under ignored runtime data and is deliberately
        absent from fleet-projects.toml (the browser-tab expansion configuration).
        """
        project = path / "data" / "probe-project"
        folder = project / "ToDo" / "tz"
        folder.mkdir(parents=True, exist_ok=True)
        tz = "TZ-P" + str(time.time_ns())
        spec = folder / (tz + "-health-check.md")
        spec.write_text(
            "```yaml\n"
            "tz: " + tz + "\n"
            "owns: []\nresources: []\nneeds: []\n"
            "priority: 99\nacceptance: []\n"
            "```\n\n# Fleet Probe synthetic no-op\n\nNo live source changes.\n",
            encoding="utf-8",
        )
        cli = path / "fleet.cmd"
        if not cli.is_file() or os.name != "nt":
            return False
        result = self._exec(
            ["cmd.exe", "/d", "/c", str(cli), "bootstrap",
             "Fleet Probe", str(project), tz],
            cwd=path, timeout=25,
        )
        return result.returncode == 0

    def _probe(self, path: Path) -> bool:
        """Port+public health and a real synthetic join/take/submit cycle within 120s."""
        deadline = time.monotonic() + PROBE_DEADLINE_SECONDS
        with socket.create_connection(("127.0.0.1", self.config.port), timeout=3):
            pass
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.config.health_url, timeout=10) as resp:
            if not 200 <= int(resp.status) < 300:
                return False
        if not self._prepare_probe(path):
            return False
        name = "guard-probe-" + str(int(time.time_ns()))
        if "—" not in self._worker(path, ["join", name, "--kind", "human"]):
            return False
        selector = ["--worker-name", name]
        taken = self._worker(path, [*selector, "take", "--project", "Fleet Probe"])
        if "Fleet Probe/" not in taken or "Нет доступных задач" in taken:
            return False
        if time.monotonic() > deadline:
            return False
        try:
            submitted = self._worker(path, [*selector, "submit", "guard synthetic smoke"])
            return "Сдано" in submitted and time.monotonic() <= deadline
        finally:
            self._worker(path, [*selector, "finish"])

    def _canary(self, baseline: metrics.Metrics) -> str | None:
        start = self.now()
        while self.now() - start < self.canary_seconds:
            self.phase_event("canary_tick", "before")
            until = min(self.sample_seconds, self.canary_seconds - (self.now() - start))
            while until > 0:
                step = min(float(WATCH_SECONDS), until)
                self.sleep(step)
                until -= step
                self.heartbeat("canary")
            if not self._health():
                return "canary_unhealthy"
            try:
                observed = metrics.snapshot(self.db, since_id=baseline.last_id)
            except Exception:
                # SQLite corruption/missing schema is a rollback trigger.
                return "canary_unavailable"
            regressed = metrics.regression(baseline, observed)
            self.phase_event("canary_tick", "after", errors=observed.errors,
                             takes=len(observed.takes), blocks=len(observed.blocks),
                             thaws=len(observed.thaws))
            if regressed:
                return regressed
        return None

    def _health(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.config.port), timeout=3):
                pass
            # A listening proxy is not proof that the public gateway still
            # routes to the dev instance. Never use the machine HTTP proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.config.health_url, timeout=5) as response:
                return 200 <= int(response.status) < 300
        except (OSError, TimeoutError, ValueError):
            return False

    def _rollback(self, head: str) -> bool:
        self.phase_event("rollback", "before")
        self._rollback_stage = "checkout"
        if not self._clean():
            self.event("rollback_dirty")
            return False
        if self.git_rc(self.config.live, "checkout", "-B", "master", head):
            self.event("rollback_failed")
            return False
        self._rollback_stage = "restart"
        if getattr(self, "_restart_launch_failed", False):
            # The identical Windows launcher cannot spawn. HEAD has already
            # been restored; do not attempt the same known-doomed spawn again.
            self.event("rollback_failed", stage="restart", reason="spawn_unavailable")
            return False
        if not self.restarter(self.config.live):
            self.event("rollback_failed", stage="restart")
            return False
        self._rollback_stage = "probe"
        try:
            success = bool(self.prober(self.config.live))
        except Exception:
            success = False
        self.event("rollback_ok" if success else "rollback_failed")
        self.phase_event("rollback", "after", success=int(success))
        return success

    def _safe_mode(self) -> bool:
        try:
            good = self.git(self.config.live, "rev-parse", "--verify", "refs/tags/platform/known-good").strip()
            if not good or self.git_rc(self.config.live, "cat-file", "-e", good + "^{commit}"):
                raise GuardFailure("known_good_missing")
            if not self._verified_good(good):
                raise GuardFailure("known_good_missing")
        except GuardFailure:
            self.event("known_good_missing")
            return False
        if not self._clean():
            self.event("rollback_dirty")
            return False
        # Manual safe-mode pointer switch is the sole allowed non-FF change.
        if self.git_rc(self.config.live, "checkout", "-B", "master", good):
            self.event("rollback_failed")
            return False
        success = bool(self.restarter(self.config.live))
        if success:
            try:
                success = bool(self.prober(self.config.live))
            except Exception:
                success = False
        self.event("safe_mode" if success else "rollback_failed")
        return success

    def _incident(self, candidate: str, prior: str, code: str, paths: tuple[str, ...]) -> None:
        """Repair tickets are descriptive artifacts, never execution from prose."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        name = "TZ-R" + stamp
        incidents = self.config.work / "ToDo" / "incidents"
        tz_folder = self.config.work / "ToDo" / "tz"
        incidents.mkdir(parents=True, exist_ok=True)
        tz_folder.mkdir(parents=True, exist_ok=True)
        (incidents / (stamp + "-deploy.md")).write_text(
            "# Platform deployment incident\n\n"
            + "code: " + code + "\nold: " + prior + "\nnew: " + candidate
            + "\npaths:\n" + "\n".join("- " + p for p in paths) + "\n",
            encoding="utf-8",
        )
        owns = "\n".join("  - " + json.dumps(p, ensure_ascii=False) for p in paths
                         if p and not self._protected((p,)))
        (tz_folder / (name + "-deploy-repair.md")).write_text(
            "```yaml\ntz: " + name + "\nowns:\n" + (owns or "  - docs/") +
            "\nresources: []\nneeds: []\nacceptance: []\n```\n\n"
            "# Repair platform deployment " + stamp + "\n\n"
            "Diagnose code " + code + " from structured guard log; fix and test.\n",
            encoding="utf-8",
        )
        self.event("incident_written")
        tool = self.config.work / "fleet.cmd"
        if tool.is_file() and os.name == "nt":
            result = self._exec(["cmd.exe", "/d", "/c", str(tool), "bootstrap",
                                  "Fleet Platform", str(self.config.work), name],
                                cwd=self.config.work, timeout=30)
            if result.returncode:
                self.event("bootstrap_failed")

    def deploy(self) -> str:
        """Single transaction-like attempt; every outcome is a code, not prose."""
        try:
            with self.lock():
                return self._deploy_locked()
        except GuardFailure as exc:
            try:
                self.event(exc.code)
            except OSError:
                return "journal_error"
            return exc.code
        except OSError:
            # Never continue a release if a required heartbeat/journal fsync
            # failed, including between merge and restart.
            return "journal_error"
        except Exception:
            try:
                self.event("unexpected_error")
            except OSError:
                return "journal_error"
            return "unexpected_error"

    def _deploy_locked(self) -> str:
        live, work = self.config.live, self.config.work
        self.phase_event("start", "before")
        if not self._clean():
            raise GuardFailure("dirty_live")
        prior = self.git(live, "rev-parse", "HEAD").strip()
        candidate = self.git(work, "rev-parse", "platform/work").strip()
        # Урок 47: преемник после передачи продолжает ЗАКРЕПЛЁННЫЙ кандидат, даже если
        # ствол за время тестов ушёл вперёд; иначе проверка ниже отвергает передачу.
        handoff = self._state().get("self_update")
        if (isinstance(handoff, dict) and handoff.get("version")
                and handoff.get("version") == self._running_guard_version()):
            pinned = str(handoff.get("candidate") or "")
            if pinned and pinned != candidate and not self.git_rc(
                    work, "merge-base", "--is-ancestor", pinned, candidate):
                candidate = pinned
        self.heartbeat("start", candidate)
        self.phase_event("start", "after")
        recovered = self._recover_release(prior)
        if recovered:
            return recovered
        if self._state().get("release_blocked_sha") == candidate:
            self.event("release_blocked", candidate=candidate)
            return "release_blocked"
        # An uncompleted handoff may continue *only* in the pinned new guard
        # version. The old supervisor and any unrelated copy stay out.
        handoff = self._state().get("self_update")
        if isinstance(handoff, dict) and not (
            handoff.get("candidate") == candidate
            and handoff.get("version") == self._running_guard_version()
        ):
            raise GuardFailure("self_update_reverted")
        # A lost guard process can leave the fast-forward on disk without ever
        # restarting/probing/canarying the running server. HEAD equality alone
        # is not a delivery receipt. Recover the same SHA only with a distinct,
        # verified known-good commit available for a safe rollback.
        recovering = candidate == prior
        restore_head = prior
        if recovering:
            tagged = self.git(live, "rev-parse", "--verify",
                              "refs/tags/platform/known-good", ok=False).strip()
            if self._verified_good(candidate):
                self.event("no_change")
                return "no_change"
            if self._reattest_live(prior, tagged):
                self.event("no_change")
                return "no_change"
            restore_head = tagged
            if (not restore_head or restore_head == candidate
                    or self.git_rc(live, "cat-file", "-e", restore_head + "^{commit}")):
                raise GuardFailure("known_good_missing")
        if not recovering and self.git_rc(live, "merge-base", "--is-ancestor", prior, candidate):
            raise GuardFailure("not_ff")
        if recovering:
            if not self._verified_good(restore_head):
                raise GuardFailure("known_good_missing")
            # No complete candidate receipt, despite the FF already on disk.
            # Do not rerun expensive validators or a second 45-minute canary.
            if not self._rollback(restore_head):
                self._safe_mode()
                raise GuardFailure("rollback_failed")
            self._failed_release(candidate, "unproven_fast_forward")
            self.event("release_recovered", candidate=candidate,
                       restore_head=restore_head)
            return "release_recovered"
        paths = self._paths(restore_head, candidate)
        # Policy grants access; it can NEVER ride a code self-update, even
        # if an interrupted earlier process already fast-forwarded HEAD.
        if self._policy_paths(paths):
            raise GuardFailure("protected_path")
        guard_changed = self._guard_paths(paths)
        if guard_changed and self._state().get("guard_blocked_candidate") == candidate:
            raise GuardFailure("self_update_reverted")
        # Ствол = только принятая работа: путь задачи, не прошедшей ревью, не выкатывается.
        pending = self.review_check(restore_head, candidate)
        if pending:
            self.event("unreviewed_detail", paths=" | ".join(pending[:20])[:2000])
            raise GuardFailure("unreviewed_paths")
        # This check comes after no-write policy/review refusals, but BEFORE
        # validation and any destructive FF. A HEAD without a signed-good
        # receipt must never become the target of a later rollback.
        if not self._verified_good(restore_head):
            tagged = self.git(live, "rev-parse", "--verify",
                              "refs/tags/platform/known-good", ok=False).strip()
            if not self._reattest_live(restore_head, tagged):
                raise GuardFailure("known_good_missing")
        opened = self._window(candidate, restore_head, "validating", window_id=uuid.uuid4().hex)
        self.event("release_window_open", candidate=candidate, restore_head=restore_head,
                   window_id=str(opened["window_id"]), pending_head=candidate)
        self.phase_event("tests_ok", "before")
        with self._candidate_tree(candidate) as isolated:
            if guard_changed:
                from .install import package_version
                package = isolated / "guard"
                try:
                    expected_version = package_version(package)
                except (OSError, ValueError, RuntimeError):
                    raise GuardFailure("protected_path") from None
                # Rejected/removed source must not bypass the old guarded
                # boundary merely because a similar new guard file exists.
                if not all((package / name).is_file()
                           for name in ("core.py", "install.py", "run_guard.pyw")):
                    raise GuardFailure("protected_path")
            if not self.validator(isolated):
                self._failed_release(candidate, "tests_red")
                raise GuardFailure("tests_red")
            if guard_changed and self._running_guard_version() != expected_version:
                self.phase_event("tests_ok", "after")
                return self._self_update(candidate, package)
        self.phase_event("tests_ok", "after")
        try:
            baseline = metrics.snapshot(self.db)
        except (OSError, ValueError, Exception):
            raise GuardFailure("canary_unavailable")
        if not self._clean():
            raise GuardFailure("dirty_live")
        if not self._restart_preflight(live):
            # A launcher which cannot even spawn must not advance live and
            # must not poison a candidate with a permanent failed-release SHA.
            self._save_state(release_window=None)
            self.event("safe_mode_skipped", reason="restarter_preflight_failed")
            raise GuardFailure("restart_error")
        self._window(candidate, restore_head, "deploying")
        self.phase_event("merge", "before", recovery=int(recovering))
        if not recovering and self.git_rc(live, "merge", "--ff-only", candidate):
            raise GuardFailure("merge_error")
        self.phase_event("merge", "after", recovery=int(recovering))
        result = None
        try:
            self._window(candidate, restore_head, "probing")
            self.phase_event("restart", "before")
            self._restart_launch_failed = False
            if (not self._injected_restarter or self.restarter is not self._initial_restarter) and not self.restarter(live):
                result = "restart_error"
            else:
                self.phase_event("restart", "after")
                self.phase_event("probe", "before")
                if not self.prober(live):
                    result = "probe_red"
                else:
                    self.phase_event("probe", "after")
                    if not self._guard_restart_heartbeat_confirmed():
                        result = "probe_red"
                    else:
                        self.event("alive_after_restart", candidate=self._candidate,
                                   pid=os.getpid(), rc=0)
                        self._reload_extension(paths)
                        self._window(candidate, restore_head, "canary")
                        result = self._canary(baseline)
        except Exception:
            result = "probe_red"
        if result is not None:
            self.event(result)
            recovered = self._rollback(restore_head)
            self._failed_release(candidate, result)
            count = int(self._state().get("rollbacks", 0)) + 1
            self._save_state(rollbacks=count)
            if count >= 2 or not recovered:
                if not recovered and getattr(self, "_rollback_stage", "") == "restart":
                    # Урок 37: safe mode идёт тем же рестартером и лишь перевёл бы
                    # live на старую метку, теряя коммиты. Ствол остаётся на HEAD
                    # до попытки; нужен человек/ремонт рестартера (инцидент ниже).
                    self.event("safe_mode_skipped", reason="restarter_failed")
                else:
                    self._safe_mode()
            self._incident(candidate, restore_head, result, paths)
            if (guard_changed and
                    (self._state().get("self_update") or {}).get("candidate") == candidate):
                self._save_state(self_update=None, guard_blocked_candidate=candidate)
                self.event("self_update_reverted", candidate=candidate)
                return "self_update_reverted"
            return result
        if guard_changed:
            # The new supervisor has now passed handoff, restart, probe and
            # the COMPLETE canary. Only now may autostart adopt its launcher.
            from .install import publish_launcher
            try:
                publish_launcher(live / "data" / "guard-runtime",
                                 self._running_guard_version())
            except (OSError, RuntimeError):
                self._rollback(restore_head)
                self._failed_release(candidate, "self_update_reverted")
                self._save_state(self_update=None, guard_blocked_candidate=candidate)
                self.event("self_update_reverted", candidate=candidate)
                return "self_update_reverted"
        self._window(candidate, restore_head, "finalizing")
        self.phase_event("known_good", "before")
        self.git(live, "tag", "-f", "platform/known-good", candidate)
        changes = {"rollbacks": 0, "deployed": candidate}
        running_guard = self._running_guard_version()
        if running_guard:
            changes["guard_known_good"] = running_guard
        if guard_changed:
            changes.update(self_update=None, guard_blocked_candidate="")
        self._save_state(**changes)
        self.phase_event("known_good", "after")
        pending_head = self.git(work, "rev-parse", "platform/work").strip()
        window_id = str((self._state().get("release_window") or {}).get("window_id") or "")
        if pending_head != candidate:
            self.event("release_window_pending", candidate=candidate,
                       pending_head=pending_head, restore_head=restore_head,
                       window_id=window_id)
        self._save_state(release_window=None, release_blocked_sha="")
        self.event("release_window_success", candidate=candidate,
                   pending_head=pending_head, restore_head=restore_head,
                   window_id=window_id)
        self.event("deployed")
        return "deployed"

    def watch_once(self) -> str:
        """A second caller sees lock_busy, never performs a concurrent merge."""
        return self.deploy()

    def watch(self) -> None:
        # Демон автозапуска не должен падать насовсем: неожиданная ошибка (таймаут git,
        # занятый файл, ОС) пишется событием, цикл продолжается.
        while True:
            try:
                self.heartbeat("watch")
                result = self.watch_once()
                if result == "self_update":
                    # The new independent supervisor has emitted a version- and
                    # token-bound heartbeat. Release the old process/lock.
                    return
                if (result == "self_update_reverted"
                        and self._running_guard_version()
                        != self._state().get("guard_known_good")):
                    # We are the failed new version. The supervisor restores
                    # the pinned previous guard, not another process here.
                    return
            except Exception as exc:  # noqa: BLE001
                try:
                    self.event("watch_error", error=f"{type(exc).__name__}: {exc}"[:500])
                except Exception:  # noqa: BLE001
                    pass
            self.sleep(WATCH_SECONDS)
