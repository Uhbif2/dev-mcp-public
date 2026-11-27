"""Выкатка принятого кода проекта в его живую поверхность (06.11.2026).

Ведущий local-library сутки считал сбой `library_status` своего же сервера «блокером
оператора»: живой процесс жил под общим core-прокси, перезапустить его без memory_service и
obsidian было нельзя, и никто не знал, какую ревизию он исполняет. Принятый и
закоммиченный код до пользователя не доходил.

Проект объявляет поверхность в fleet-projects.toml:

    [projects."local-library".live]
    instance = "lib"            # экземпляр mcp_launcher.py, где живут только его серверы
    port = 8768
    probe = "local-library"  # named-server для проверки initialize
    every_min = 10              # не чаще: рестарт рвёт текущие вызовы библиотеки

Интегратор раз в проход сверяет HEAD проекта с последней выкатанной ревизией. Новая ревизия,
старше двух минут, → остановка ТОЛЬКО прокси этого экземпляра (PID из state.json лончера,
сверенный по командной строке) → лончер поднимает его с новым кодом → initialize →
событие `live_deployed` {instance, rev, ok, detail}. Ведущий видит его в брифе (`lead.live_lines`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROXY_STATE = Path(os.environ.get(
    "DEV_MCP_PROXY_STATE", r"C:\hub\gateway-host\mcp-proxy\state.json"))
SETTLE_S = 120
# 08.11: холодный старт lib при нехватке RAM — 3 мин 20 с (10:22), проба на 150 с дала ложный ok:false.
PORT_WAIT_S = 300


def _git_head(root: str) -> tuple[str, float]:
    out = subprocess.run(["git", "-C", root, "log", "-1", "--format=%H %ct"], capture_output=True,
                         text=True, timeout=20, check=False)
    parts = out.stdout.split() if out.returncode == 0 else []
    return (parts[0], float(parts[1])) if len(parts) == 2 else ("", 0.0)


def _proxy_pid(instance: str, port: int) -> int | None:
    """PID прокси экземпляра — только если его командная строка действительно этот порт."""
    import psutil
    try:
        record = json.loads(PROXY_STATE.read_text(encoding="utf-8"))["instances"][instance]
        pid = int(record["pid"])
        cmd = " ".join(psutil.Process(pid).cmdline())
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None
    return pid if "mcp_proxy" in cmd and f"--port {port}" in cmd else None


def _initialize(port: int, server: str) -> tuple[bool, str]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "live_deploy", "version": "0"}}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/servers/{server}/mcp", body, {
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=60) as resp:
            text = resp.read(4000).decode("utf-8", "replace")
        return ('"serverInfo"' in text, text[:160])
    except Exception as exc:  # noqa: BLE001 — результат проверки, не авария
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def deploy(instance: str, port: int, probe: str) -> dict[str, Any]:
    pid = _proxy_pid(instance, port)
    if pid is None:
        return {"ok": False, "detail": f"прокси экземпляра {instance} не найден в state.json лончера"}
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    started = time.monotonic()
    while time.monotonic() - started < PORT_WAIT_S:
        time.sleep(3)
        if _port_open(port) and _proxy_pid(instance, port) not in (None, pid):
            ok, detail = _initialize(port, probe)
            if ok:
                return {"ok": True, "detail": detail, "restart_s": round(time.monotonic() - started)}
    ok, detail = _initialize(port, probe)
    return {"ok": ok, "detail": detail, "restart_s": round(time.monotonic() - started)}


def _release_proof(store: Any, project: str, rev: str,
                   info: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Only a trusted, green gate for this exact trunk HEAD permits a restart."""
    if info.get("commit") is True and info.get("claim_trees") is True:
        from .claim_accept_flow import release_ready

        with store.read() as tx:
            integrated = tx.query(
                "SELECT payload FROM events WHERE kind='claim_integrated' AND project=? "
                "ORDER BY id DESC", (project,))
            gates = tx.query(
                "SELECT payload FROM events WHERE kind='claim_trunk_gate' AND project=? "
                "ORDER BY id DESC", (project,))
            trunk_gates = tx.query(
                "SELECT kind,payload FROM events WHERE kind IN ('gate','gate_wait') "
                "AND project=? ORDER BY id DESC", (project,))
        # A later failed verdict for the same HEAD revokes earlier green proof.
        # release_ready checks existence of a green receipt, not recency.
        for row in gates:
            try:
                data = json.loads(row["payload"] or "{}")
            except (ValueError, TypeError):
                continue
            if data.get("integrated_sha") == rev:
                if type(data.get("rc")) is not int or data["rc"] != 0:
                    return False, f"claim_trunk_gate красный (rc={data.get('rc')})", {}
                break
        # Re-gating after integration can revoke an earlier green claim gate.
        for row in trunk_gates:
            try:
                data = json.loads(row["payload"] or "{}")
            except (ValueError, TypeError):
                continue
            if data.get("tested_revision") == rev:
                if row["kind"] != "gate" or type(data.get("rc")) is not int or data["rc"] != 0:
                    return False, f"последний trunk gate красный (rc={data.get('rc')})", {}
                break
        for row in integrated:
            try:
                data = json.loads(row["payload"] or "{}")
            except (ValueError, TypeError):
                continue
            claim = data.get("claim_id")
            submission = data.get("submission_revision")
            if (data.get("integrated_sha") == rev and type(claim) is int
                    and claim > 0 and isinstance(submission, str)
                    and re.fullmatch(r"[0-9a-f]{40}", submission)
                    and release_ready(store, claim, submission)):
                return True, "", {"kind": "claim_trunk_gate", "claim_id": claim,
                                  "submission_revision": submission}
        return False, "нет release_ready для принятого claim и точного HEAD", {}

    # Shared/legacy projects do not have sealed claim receipts. Their trusted
    # integrator writes a full trunk gate with tested_revision, rc and command.
    command = str(info.get("gate") or "").strip()
    if not command:
        return False, "нет настроенного trunk gate", {}
    with store.read() as tx:
        gates = tx.query(
            "SELECT kind,payload FROM events WHERE kind IN ('gate','gate_wait') "
            "AND project=? ORDER BY id DESC", (project,))
    for row in gates:
        try:
            data = json.loads(row["payload"] or "{}")
        except (ValueError, TypeError):
            continue
        if data.get("tested_revision") != rev:
            continue
        if data.get("command") != command:
            return False, "gate для HEAD выполнен другой командой", {}
        if row["kind"] == "gate" and type(data.get("rc")) is int and data["rc"] == 0:
            return True, "", {"kind": "gate", "tested_revision": rev}
        return False, f"trunk gate для HEAD не зелёный (rc={data.get('rc')})", {}
    return False, "нет зелёного trunk gate для точного HEAD", {}


def tick(store: Any, project_info: Any, *, now: float | None = None) -> list[dict[str, Any]]:
    """Один проход: выкатить только HEAD с проверенным зелёным trunk gate."""
    now = time.time() if now is None else now
    done: list[dict[str, Any]] = []
    with store.read() as tx:
        projects = [dict(r) for r in tx.query(
            "SELECT name,root FROM projects WHERE active=1 AND root IS NOT NULL")]
    for proj in projects:
        try:
            info = project_info(proj["name"]) or {}
            live = info.get("live")
        except Exception:  # noqa: BLE001
            info = {}
            live = None
        if not isinstance(live, dict) or not live.get("instance") or not live.get("port"):
            continue
        rev, committed = _git_head(str(proj["root"]))
        if not rev or now - committed < SETTLE_S:
            continue
        with store.read() as tx:
            last = tx.one("SELECT ts,payload FROM events WHERE kind='live_deployed' AND project=? "
                          "ORDER BY id DESC LIMIT 1", (proj["name"],))
        prev: dict[str, Any] = {}
        if last is not None:
            try:
                prev = json.loads(last["payload"] or "{}")
            except ValueError:
                prev = {}
            age = now - datetime.fromisoformat(str(last["ts"])).timestamp()
            if (prev.get("rev") == rev and prev.get("ok")) or (
                    not prev.get("blocked") and age < 60 * float(live.get("every_min") or 10)):
                continue
        ready, reason, proof = _release_proof(store, proj["name"], rev, info)
        # The evidence check itself may read Git. Reject an intervening FF:
        # never restart a proxy for a different (ungated) HEAD.
        current, _ = _git_head(str(proj["root"]))
        if current != rev:
            ready = False
            reason = "HEAD изменился после проверки release proof"
        if not ready:
            detail = f"выкатка запрещена: {reason}"
            if not (prev.get("blocked") and prev.get("rev") == rev
                    and prev.get("detail") == detail):
                payload = {"instance": live["instance"], "port": int(live["port"]),
                           "rev": rev, "ok": False, "blocked": True, "detail": detail,
                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                # The existing fleet.cmd live status reads live_deployed; a
                # blocked attempt must be visible there without calling deploy.
                with store.tx() as tx:
                    tx.record_event(kind="live_deployed", project=proj["name"], payload=payload)
            continue
        result = deploy(str(live["instance"]), int(live["port"]), str(live.get("probe") or proj["name"]))
        payload = {"instance": live["instance"], "port": int(live["port"]), "rev": rev,
                   "previous_rev": prev.get("rev"), "release_proof": proof, **result,
                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with store.tx() as tx:
            tx.record_event(kind="live_deployed", project=proj["name"], payload=payload)
        done.append({"project": proj["name"], **payload})
    return done
