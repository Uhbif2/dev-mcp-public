"""Independent pythonw supervisor: recover previous immutable guard on hard crash."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def _runtime_dir() -> Path:
    """Каталог guard-runtime. Урок 41: TZ-59 запускает копию лончера из папки
    версии и передаёт каталог через DEV_MCP_GUARD_RUNTIME; без этого лончер искал
    версии внутри папки версии и молча выходил."""
    env = os.environ.get("DEV_MCP_GUARD_RUNTIME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve().parent
    if (here / "guard" / "__main__.py").is_file() and (here.parent / "run_guard.pyw").is_file():
        return here.parent
    return here


HERE = _runtime_dir()
LIVE = HERE.parents[1]
STATE_DIR = (Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA")
             else Path.home() / ".local" / "state") / "dev-mcp-guard"
STATE_FILE = STATE_DIR / "state.json"
PULSE_FILE = LIVE / "data" / "guard-heartbeat.json"
JOURNAL = LIVE / "logs" / "platform-guard.jsonl"
TIMEOUT = 180


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _event(code: str, **details) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                              "code": code, "stage": "supervisor", **details}) + "\n")
        out.flush()
        os.fsync(out.fileno())


def _installed(version: str) -> bool:
    return bool(version and version.isalnum() and
                (HERE / version / "guard" / "__main__.py").is_file())


def _choose(state: dict) -> str:
    pending = state.get("self_update")
    if isinstance(pending, dict) and pending.get("candidate") != state.get("guard_blocked_candidate"):
        version = str(pending.get("version", ""))
        if _installed(version):
            return version
    known = str(state.get("guard_known_good", ""))
    if _installed(known):
        return known
    # Compatibility with the first legacy install, before version pinning.
    copies = [p for p in HERE.iterdir() if p.is_dir() and _installed(p.name)]
    return max(copies, key=lambda p: p.stat().st_mtime).name if copies and not known else ""


def _revert(state: dict, why: str) -> str:
    pending = state.get("self_update")
    if not isinstance(pending, dict):
        return _choose(state)
    previous = str(pending.get("previous", ""))
    if not _installed(previous):
        _event("self_update_reverted", reason="previous_missing")
        return ""
    state.update(self_update=None, guard_known_good=previous,
                 guard_blocked_candidate=str(pending.get("candidate", "")))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + f".{os.getpid()}.new")
    try:
        with tmp.open("w", encoding="utf-8") as out:
            json.dump(state, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, STATE_FILE)
    finally:
        tmp.unlink(missing_ok=True)
    _event("self_update_reverted", candidate=state["guard_blocked_candidate"],
           previous=previous, reason=why)
    return previous


def _launch(version: str, token: str):
    if not _installed(version):
        raise RuntimeError("guard candidate is not installed")
    binary = Path(sys.executable).resolve()
    if os.name == "nt":
        binary = binary.with_name("pythonw.exe")
    if not binary.is_file():
        raise RuntimeError("hidden pythonw missing")
    home = HERE / version
    code = ("import runpy,sys;sys.path.insert(0," + repr(str(home)) + ");"
            "sys.argv=['guard','watch'];runpy.run_module('guard',run_name='__main__')")
    env = dict(os.environ, DEV_MCP_GUARD_HANDOFF_TOKEN=token)
    with (home / "guard-stdout.log").open("a", encoding="utf-8") as out:
        return subprocess.Popen([str(binary), "-c", code], cwd=home, env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                close_fds=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


HANDED_OFF = "-handed-off"  # not alnum: never a version name


def _handed_off(pending: dict, version: str, child_pid: int) -> bool:
    """Урок 39: успешная передача — не авария. Преемник уже бьётся пульсом
    со своей версией и токеном из state; старый процесс вышел штатно."""
    pulse = _json(PULSE_FILE)
    token = str(pending.get("token", ""))
    return bool(token and pending.get("version") and pending.get("version") != version
                and pulse.get("version") == pending.get("version")
                and pulse.get("handoff_token") == token
                and isinstance(pulse.get("pid"), int) and pulse.get("pid") != child_pid)


def supervise_once(version: str, token: str = "", *,
                   launch=_launch, clock=time.monotonic, sleep=time.sleep) -> str:
    """Independent 3-minute watchdog from start throughout canary."""
    child = launch(version, token)
    last_seen = clock()
    last_stamp = ""
    while child.poll() is None:
        state = _json(STATE_FILE)
        pending = state.get("self_update")
        if isinstance(pending, dict) and pending.get("candidate") == state.get("guard_blocked_candidate"):
            child.terminate()
            return _revert(state, "candidate_blocked")
        pulse = _json(PULSE_FILE)
        # A previously installed (pre-TZ44) known-good guard has no
        # version/token fields in its heartbeat. It is only trusted as the
        # pinned rollback copy; a pending candidate must prove both fields.
        legacy_previous = (not token and not isinstance(pending, dict)
                           and state.get("guard_known_good") == version
                           and not pulse.get("version"))
        fresh = (pulse.get("pid") == child.pid and pulse.get("time")
                 and pulse.get("time") != last_stamp)
        identified = ((pulse.get("version") == version
                       and pulse.get("handoff_token") == token)
                      or legacy_previous)
        if fresh and identified:
            last_stamp = str(pulse["time"])
            last_seen = clock()
        elif clock() - last_seen >= TIMEOUT:
            child.terminate()
            if isinstance(pending, dict) and _handed_off(pending, version, child.pid):
                _event("handoff_exit", version=version, successor=str(pending.get("version")))
                return HANDED_OFF
            return _revert(state, "heartbeat_timeout")
        sleep(5)
    state = _json(STATE_FILE)
    pending = state.get("self_update")
    if isinstance(pending, dict):
        if _handed_off(pending, version, child.pid):
            _event("handoff_exit", version=version, successor=str(pending.get("version")))
            return HANDED_OFF
        return _revert(state, "child_exited")
    return _choose(state)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--handoff-version", default="")
    parser.add_argument("--handoff-token", default="")
    args = parser.parse_args(argv)
    version, token = args.handoff_version, args.handoff_token
    if version:
        pending = _json(STATE_FILE).get("self_update")
        if (not isinstance(pending, dict) or pending.get("version") != version
                or pending.get("token") != token or not token or not _installed(version)):
            return
    while True:
        try:
            if not version:
                state = _json(STATE_FILE)
                version = _choose(state)
                pending = state.get("self_update")
                token = (str(pending.get("token", "")) if isinstance(pending, dict)
                         and pending.get("version") == version else "")
            if version:
                version = supervise_once(version, token)
                token = ""
                if version == HANDED_OFF:
                    return  # преемник со своим супервизором уже работает
            else:
                _event("self_update_reverted", reason="no_safe_version")
            time.sleep(1 if version else 30)
        except Exception as exc:  # Persistent supervisor must survive a child crash.
            try:
                _event("self_update_reverted", reason=type(exc).__name__)
            except OSError:
                pass
            version, token = "", ""
            time.sleep(30)


if __name__ == "__main__":
    main()
