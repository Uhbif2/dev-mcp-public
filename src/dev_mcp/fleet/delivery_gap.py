"""Durable operator handoff and evidence-linked release conversion.

An integration commit is L2, not permission to sign/publish and not native
proof. Operator fronts use the existing manual-platform outbox. The only L3/L4
credit is an explicit, SHA-linked delivery_gate receipt; generic accept or
witness events cannot prove a release was installed on a device.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STAGES = ("accepted", "integrated", "signed", "installed", "works_live")
_SHA = re.compile(r"^[0-9a-f]{40}$", re.I)
_ARTIFACT = re.compile(r"^[0-9a-f]{64}$", re.I)
_OBLIGATION = re.compile(r"\bO-[A-Za-z0-9_.-]+\b")
WINDOW_DAYS = 30


def _rows(db: Any, sql: str, args: tuple = ()) -> list[Any]:
    return list(db.query(sql, args) if hasattr(db, "query") else db.execute(sql, args).fetchall())


def _data(value: Any) -> dict[str, Any]:
    try:
        item = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return item if isinstance(item, dict) else {}


def _datetime(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _full_sha(root: str, commit: str) -> str:
    """Resolve the integrated commit, not an unrelated later HEAD."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit, re.I):
        return ""
    if _SHA.fullmatch(commit):
        return commit.lower()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
            cwd=root, capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        sha = result.stdout.strip().lower()
        return sha if result.returncode == 0 and _SHA.fullmatch(sha) and sha.startswith(commit.lower()) else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _data_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
        return [str(item) for item in value] if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def on_integration(store: Any, project: str, tz: str, payload: dict[str, Any]) -> str:
    """One idempotent manual operator successor per accepted integrated revision.

    Invoked after the integrate event is durable; never signs, builds, publishes,
    installs or changes the task queue. Non-delivery projects are ignored.
    """
    from . import goals, obstacles, platform_manual

    config = goals.project_info(project)
    if not isinstance(config, dict) or not config.get("lead_continuous") or not config.get("commit"):
        return ""
    commit = str(payload.get("commit") or "").strip()
    accept_id = payload.get("accept_id")
    if not commit or type(accept_id) is not int or accept_id <= 0:
        return ""
    with store.read() as tx:
        accepted = tx.one("SELECT id FROM events WHERE id=? AND project=? AND tz=? AND kind='accept'",
                          (accept_id, project, tz))
        row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        task = tx.one("SELECT body,resources,owns FROM tasks WHERE project=? AND tz=?", (project, tz))
    if accepted is None or row is None or not row["root"]:
        return ""
    # A documentation-only L2 commit is not an executable candidate for an
    # installation. A missing/legacy owns list stays visible rather than
    # silently dropping an integration for which provenance is incomplete.
    declared_paths = _data_list(task["owns"] if task else "[]")
    runtime_prefixes = ("/app/", "/client/", "/shared/", "/deploy/",
                        "/contracts/", "/ops/pilot/", "/ops/witness/")
    if declared_paths and not any(
        any(prefix in ("/" + p.replace("\\", "/").strip("/").casefold() + "/")
            for prefix in runtime_prefixes)
        for p in declared_paths
    ):
        return ""
    sha = _full_sha(str(row["root"]), commit)
    obligations = sorted(set(_OBLIGATION.findall(str(task["body"] or ""))))[:12] if task else []
    resources = _data_list(task["resources"] if task else "[]")
    exact = sha or f"НЕ УСТАНОВЛЕН (интеграционная квитанция: {commit})"
    fingerprint = f"delivery-gap:{project}:{accept_id}:{commit}"
    detail = (
        f"Поставка после интеграции {project}/{tz}, accept #{accept_id}. "
        f"Exact integrated SHA: {exact}. O-ID: {', '.join(obligations) or 'нужна адресная привязка'}. "
        "Ответственный: оператор релиза, публикация только после отдельного разрешённого gate. "
        "Последовательность: (1) подтвердить exact SHA в чистом main; "
        "(2) signed build на ЭТОМ SHA с artifact SHA-256, подписью и адресом пакета; "
        "(3) проверить publish gate/разрешение владельца и только затем вручную опубликовать; "
        "(4) зафиксировать installed version, устройство, обновление по штатному каналу; "
        "(5) native positive И negative control, связанный с тем же артефактом. "
        "Не засчитывать unit tests/HEAD/accepted как installed или works_live. "
        f"Ресурс: signing/release operator + native device; дорожка: {', '.join(resources) or 'release-gate, native-device'}. "
        "Пробуждение ведущего: оператор записал проверяемые delivery_gate receipts signed → "
        "installed → works_live с accept_id, commit, artifact_sha, evidence и native negative_evidence; "
        "либо сообщил конкретный блокер/ресурс. До этого карточка остаётся открытой. "
        + ("STOP: сначала разрешить точный SHA из git, публикация недопустима. " if not sha else "")
    )
    acceptance = [
        "Подтвердить SHA integrated commit (40 hex) и успешный release/publish gate; "
        "записать типизированные delivery_gate receipts для accept_id и commit с доказательствами "
        "signed (artifact_sha, url, evidence), installed (artifact_sha, device, version, evidence), "
        "works_live (artifact_sha, verdict=works_live, evidence, negative_evidence); "
        "проверить живое устройство и отрицательный контроль. Оператор вручную закрывает "
        "manual-platform front только по этим квитанциям либо с точным блокером."
    ]
    # Where the accepted work names ledger obligations, use the *existing*
    # typed lead obstacle. It projects to the same manual operator board and
    # its owner_answer predicate has a real unblock/wakeup path for TZ-LEAD.
    known = {o["id"] for o in obstacles.ledger_obligations(Path(str(row["root"]))) or []}
    linked = [o for o in obligations if o in known]
    if linked:
        ask = (f"Оператор: довести {project}/{tz} accept #{accept_id}, SHA {exact} "
               "до signed → publish gate → installed native works_live.")
        oid = "ob-" + hashlib.sha256(f"{project}/owner_action/{ask}".encode("utf-8")).hexdigest()[:8]
        with store.read() as tx:
            if tx.one("SELECT 1 FROM events WHERE project=? AND kind='obstacle_open' "
                      "AND json_extract(payload,'$.id')=? LIMIT 1", (project, oid)):
                return oid
        return obstacles.declare(store, "integrator", project, {
            "type": "owner_action", "actor": "operator",
            "obligations": linked, "ask": ask, "option": detail,
            "acceptance": acceptance[0],
            "predicate": {"kind": "owner_answer"},
        })
    # Legacy or unlinked tasks retain a durable operator front without
    # fabricating an O-ID or falsely parking an unrelated obligation.
    with store.tx() as tx:
        ident = platform_manual.enqueue(
            tx, fingerprint, title=f"Поставка L2→L4: {project}/{tz} #{accept_id}",
            detail=detail, source_project=project, source_tz=tz, acceptance=acceptance,
        )
    # The existing controller projects this durable event to the operator backlog.
    return ident


def _valid_receipt(stage: str, payload: dict[str, Any], artifact: str) -> bool:
    if not str(payload.get("evidence") or "").strip():
        return False
    sha = str(payload.get("artifact_sha") or "")
    if not _ARTIFACT.fullmatch(sha) or (artifact and sha.lower() != artifact.lower()):
        return False
    if stage == "signed":
        return bool(str(payload.get("url") or "").strip())
    if stage == "installed":
        return bool(str(payload.get("device") or "").strip() and str(payload.get("version") or "").strip())
    if stage == "works_live":
        return (payload.get("verdict") == "works_live" and
                bool(str(payload.get("negative_evidence") or "").strip()))
    return False


def conversion(db: Any, project: str | None = None, *, now: datetime | None = None,
               days: int = WINDOW_DAYS) -> dict[str, dict[str, Any]]:
    """Per-project exact acceptance chains during the latest days days.

    A stage counts only with a linked prior stage, same project, task, accept
    event and commit. Unlinked witness_result cannot prove an installed artifact.
    """
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    filter_project = " AND project=?" if project else ""
    args = (since, project) if project else (since,)
    accepts = _rows(db, "SELECT id,project,tz,ts,payload FROM events "
                    "WHERE kind='accept' AND ts>=?" + filter_project + " ORDER BY id", args)
    chains: dict[int, dict[str, Any]] = {}
    for row in accepts:
        data = _data(row["payload"])
        task_name = str(row["tz"] or "").upper()
        if (data.get("progress") or not task_name or task_name == "TZ-LEAD"
                or task_name.startswith(("FIX:TZ-LEAD", "REVIEW:", "FIX:REVIEW:"))):
            continue
        chains[int(row["id"])] = {
            "project": str(row["project"]), "tz": str(row["tz"]),
            "stage": 0, "time": _datetime(row["ts"]), "commit": "", "artifact": "",
        }
    if not chains:
        return {}
    events = _rows(db, "SELECT id,project,tz,kind,ts,payload FROM events "
                   "WHERE kind IN ('integrate','delivery_gate') AND ts>=?"
                   + filter_project + " ORDER BY id", args)
    for row in events:
        p = _data(row["payload"])
        aid = p.get("accept_id")
        if type(aid) is not int or aid not in chains or int(row["id"]) <= aid:
            continue
        current = chains[aid]
        if str(row["project"]) != current["project"] or str(row["tz"]) != current["tz"]:
            continue
        stamp = _datetime(row["ts"])
        # Event IDs provide causal ordering. Integrator timestamps have seconds
        # precision whereas acceptance timestamps can have microseconds.
        if stamp is None:
            continue
        if row["kind"] == "integrate":
            commit = str(p.get("commit") or "").lower()
            if current["stage"] == 0 and re.fullmatch(r"[0-9a-f]{7,40}", commit):
                current.update(stage=1, time=stamp, commit=str(p["commit"]).lower())
            continue
        stage = str(p.get("stage") or "")
        if stage not in STAGES[2:] or current["stage"] != STAGES.index(stage) - 1:
            continue
        commit = str(p.get("commit") or "").lower()
        if not _SHA.fullmatch(commit) or not commit.startswith(current["commit"]):
            continue
        if not _valid_receipt(stage, p, current["artifact"]):
            continue
        current.update(stage=STAGES.index(stage), time=stamp,
                       commit=commit, artifact=str(p["artifact_sha"]).lower())
    out: dict[str, dict[str, Any]] = {}
    for chain in chains.values():
        project_name = chain["project"]
        group = out.setdefault(project_name, {"counts": [0] * len(STAGES),
                                              "pending": [[] for _ in STAGES[:-1]]})
        stage_idx = chain["stage"]
        for idx in range(stage_idx + 1):
            group["counts"][idx] += 1
        if stage_idx < len(STAGES) - 1 and chain["time"]:
            group["pending"][stage_idx].append(
                max(0.0, (now - chain["time"]).total_seconds() / 3600))
    return out


def summary_lines(db: Any, project: str | None = None, *,
                  now: datetime | None = None, days: int = WINDOW_DAYS) -> list[str]:
    data = conversion(db, project, now=now, days=days)
    out: list[str] = []
    for name in sorted(data):
        group = data[name]
        counts = group["counts"]
        ratios = [f"{counts[i]} ({100 * counts[i] / counts[i - 1]:.0f}%)"
                  if counts[i - 1] else f"{counts[i]} (—)"
                  for i in range(1, len(STAGES))]
        out.append(f"Поставка {name}, {days} дн: accepted {counts[0]} → "
                   + " → ".join(f"{stage} {value}" for stage, value in zip(STAGES[1:], ratios))
                   + ". KPI: installed/works_live, не accepted.")
        pending = [f"{STAGES[i]}→{STAGES[i+1]}: {len(ages)} ждут, "
                   f"старейший {max(ages):.1f} ч"
                   for i, ages in enumerate(group["pending"]) if ages]
        if pending:
            out.append("  Возраст ворот: " + "; ".join(pending))
    return out
