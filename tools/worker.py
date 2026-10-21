#!/usr/bin/env python3
# TZ-08 local fleet worker adapter
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

IMPORT_LABEL = "импортирован, исполнитель неизвестен"


def _mods():
    from dev_mcp.fleet import inbox, model, queue, spec
    from dev_mcp.fleet.store import Store
    return Store, queue, inbox, spec, model


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _db_path(raw: str | None = None) -> pathlib.Path:
    explicit = raw or os.environ.get("DEV_MCP_FLEET_DB")
    if explicit:
        return pathlib.Path(explicit)
    from dev_mcp.fleet.location import fleet_db_path
    return fleet_db_path(ROOT)


def _state_dir() -> pathlib.Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return pathlib.Path(base) / "dev-mcp" if base else pathlib.Path.home() / ".local/state/dev-mcp"


def _slug(label: str) -> str:
    value = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "-", label.strip()).strip("-._")
    return value or "worker"


def _state_path(label: str) -> pathlib.Path:
    return _state_dir() / f"worker-{_slug(label)}.json"


def _write_state(label: str, worker_id: str, kind: str) -> pathlib.Path:
    path = _state_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"worker_id": worker_id, "label": label, "kind": kind, "updated_at": _now()},
        ensure_ascii=False, indent=2
    ), encoding="utf-8")
    return path


def _read_state(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("worker_id") else None


def _current_state(name: str | None = None) -> dict[str, Any]:
    requested = name or os.environ.get("DEV_MCP_WORKER_NAME")
    if requested:
        state = _read_state(_state_path(requested))
        if state:
            return state
        raise SystemExit(f"Нет worker-{_slug(requested)}.json; сначала join.")
    root = _state_dir()
    candidates: list[dict[str, Any]] = []
    if root.exists():
        for path in root.glob("worker-*.json"):
            state = _read_state(path)
            if state:
                candidates.append(state)
    if not candidates:
        raise SystemExit("Нет сохранённого worker_id; сначала worker.cmd join <имя> --kind <kind>")
    return max(candidates, key=lambda x: str(x.get("updated_at") or ""))


def _connect(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _open_store(path: pathlib.Path):
    Store, *_ = _mods()
    path.parent.mkdir(parents=True, exist_ok=True)
    return Store.open(str(path))


def _event(
    conn: sqlite3.Connection,
    kind: str,
    worker_id: str | None = None,
    project: str | None = None,
    tz: str | None = None,
    payload: dict[str, Any] | None = None,
    *,
    tool: str = "",
    path_norm: str = "",
    rc: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events(ts,kind,worker_id,project,tz,tool,path_norm,rc,payload) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            _now(), kind, worker_id, project, tz, tool, path_norm, rc,
            json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
        ),
    )


def _next_worker_id(conn: sqlite3.Connection) -> str:
    maximum = 0
    for row in conn.execute("SELECT id FROM workers WHERE id LIKE 'W%'"):
        m = re.fullmatch(r"W(\d+)", str(row["id"]))
        if m:
            maximum = max(maximum, int(m.group(1)))
    return f"W{maximum + 1}"


def _insert_worker(conn: sqlite3.Connection, label: str, kind: str,
                   project: str | None = None, tz: str | None = None,
                   payload: dict[str, Any] | None = None) -> str:
    wid = _next_worker_id(conn)
    now = _now()
    conn.execute(
        "INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason) "
        "VALUES (?,?,?,?,?,NULL,'')", (wid, label, kind, now, now)
    )
    _event(conn, "join", wid, project, tz, payload)
    return wid


def _join(label: str, kind: str, db: pathlib.Path) -> str:
    _open_store(db)
    cached = _read_state(_state_path(label))
    with _connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if cached:
            row = conn.execute("SELECT * FROM workers WHERE id=?", (cached["worker_id"],)).fetchone()
            if row and not row["revoked_at"]:
                conn.execute("UPDATE workers SET last_seen=? WHERE id=?", (_now(), row["id"]))
                _event(conn, "join", row["id"], payload={"resumed": True})
                conn.commit()
                _write_state(label, str(row["id"]), kind)
                return str(row["id"])
            if row and row["revoked_at"]:
                raise SystemExit(f"{row['id']} отозван: {row['revoke_reason']}")
        wid = _insert_worker(conn, label, kind)
        conn.commit()
    _write_state(label, wid, kind)
    return wid


def _touch(store: Any, state: dict[str, Any]) -> str:
    wid = str(state["worker_id"])
    store.touch_worker(wid)
    _write_state(str(state.get("label") or wid), wid, str(state.get("kind") or "chatgpt"))
    return wid



def _project_root(db: pathlib.Path, project: str) -> pathlib.Path | None:
    with _connect(db) as conn:
        row = conn.execute("SELECT root FROM projects WHERE name=?", (project,)).fetchone()
    return pathlib.Path(row["root"]) if row else None


def _active_task(db: pathlib.Path, worker_id: str) -> sqlite3.Row | None:
    with _connect(db) as conn:
        return conn.execute(
            "SELECT c.id AS claim_id,t.project,t.tz,t.kind,t.spec_path,t.spec_sha,p.root FROM claims c "
            "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "JOIN projects p ON p.name=t.project "
            "WHERE c.worker_id=? AND c.state='active' ORDER BY c.id DESC LIMIT 1",
            (worker_id,),
        ).fetchone()


def _claim_checkout(db: pathlib.Path, worker_id: str):
    """Resolve a checkout from the durable take event, not from CLI input."""
    from dev_mcp.fleet.claim_take_routing import resolve_active_claim_context

    Store, *_ = _mods()
    store = Store.open(str(db))
    try:
        return resolve_active_claim_context(store, worker_id)
    finally:
        store.close()


def _submit_for_review(store: Any, queue: Any, worker_id: str, note: str,
                       *, checkout: Any = None) -> str:
    row = _active_task(pathlib.Path(store.path), worker_id)
    if row is None:
        raise ValueError(f"{worker_id}: нет активной задачи")
    if str(row["kind"]) == "review":
        raise ValueError("review-задачу заверши командой review accept|reject, а не submit")
    queue.submit(store, worker_id, note)
    from dev_mcp.fleet import review

    if checkout is not None:
        print(f"Дерево сдачи: {checkout.tree_path}")
    return review.open_review(store, str(row["project"]), str(row["tz"]))


def _run_evidence(worker_id: str, argv: list[str], db: pathlib.Path) -> int:
    command = list(argv)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("после run передай команду, например: worker.cmd run -- pytest tests -q")
    row = _active_task(db, worker_id)
    if row is None:
        raise ValueError(f"{worker_id}: нет активной задачи")
    from dev_mcp.fleet.claim_take_routing import (
        claim_command_environment, validate_claim_command_argv,
    )

    checkout = _claim_checkout(db, worker_id)
    route = claim_command_environment(checkout)
    real_command = list(command)
    options: dict[str, Any] = {}
    if checkout.mode == "isolated":
        validate_claim_command_argv(command, checkout)
        executable = command[0].replace("\\", "/").casefold()
        is_project_python = executable in {".venv/scripts/python.exe", ".venv/bin/python"}
        is_python = executable in {"python", "python.exe"}
        if is_project_python or is_python:
            if not route.python_executable.is_file():
                raise ValueError("isolated Python requires the shared project .venv")
            real_command[0] = str(route.python_executable)
        options["env"] = route.environment
    # Исполнитель живёт без консоли (wscript): без флага каждая команда флота открывала у владельца
    # своё окно консоли, мигающее на экране (09.10.2026).
    result = subprocess.run(real_command, cwd=route.cwd, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **options)
    from dev_mcp.fleet import evidence
    from dev_mcp.fleet.claim_take_routing import project_path_to_claim

    with _connect(db) as conn:
        paths = [str(item["path_norm"]) for item in conn.execute(
            "SELECT path_norm FROM claim_paths WHERE claim_id=?", (row["claim_id"],)
        )]
        if checkout.mode == "isolated":
            paths = [str(project_path_to_claim(path, checkout, read_only=True)) for path in paths]
        payload: dict[str, Any] = {"argv": command}
        if paths:
            payload["tree"] = evidence.tree_hash(paths)
        conn.execute("BEGIN IMMEDIATE")
        _event(
            conn,
            "run",
            worker_id,
            str(row["project"]),
            str(row["tz"]),
            payload,
            tool="worker.run",
            path_norm=str(route.cwd),
            rc=int(result.returncode),
        )
        conn.commit()
    return int(result.returncode)


def _print_task(task: Any, db: pathlib.Path) -> None:
    print(f"{task.project}/{task.tz} — {task.title}")
    print(f"state={task.state} kind={task.kind} priority={task.priority}")
    with _connect(db) as conn:
        claim = conn.execute(
            "SELECT worker_id FROM claims WHERE project=? AND tz=? AND state='active' "
            "ORDER BY id DESC LIMIT 1", (task.project, task.tz),
        ).fetchone()
    if claim is not None:
        checkout = _claim_checkout(db, str(claim["worker_id"]))
        print(f"Дерево захвата ({checkout.mode}): {checkout.tree_path}")
    for key in ("needs", "resources"):
        value = getattr(task, key)
        if value:
            print(f"{key}: " + ", ".join(value))
    for key in ("owns", "acceptance"):
        value = getattr(task, key)
        if value:
            print(f"{key}:")
            for item in value:
                print(f"  - {item}")
    print("\n--- задание целиком ---")
    root = _project_root(db, task.project)
    if root and task.spec_path:
        try:
            print((root / task.spec_path).read_text(encoding="utf-8"))
            return
        except OSError:
            pass
    print(task.body or "(тело задания пусто)")


def _sync(worker_name: str | None, db: pathlib.Path) -> int:
    Store, _, inbox, spec, _ = _mods()
    store = Store.open(str(db))
    state = _current_state(worker_name)
    wid = _touch(store, state)
    row = _active_task(db, wid)
    if not row:
        print("Границы задачи: активной задачи нет.")
    elif not row["spec_path"]:
        print("Границы задачи: у задачи нет spec_path; сравнивать нечего.")
    else:
        path = pathlib.Path(row["root"]) / row["spec_path"]
        try:
            current = spec.spec_sha(str(path))
        except Exception as exc:
            print(f"Границы задачи: не удалось перечитать spec_sha: {exc}")
        else:
            recorded = str(row["spec_sha"] or "")
            if current != recorded:
                print(
                    "ГРАНИЦЫ ЗАДАЧИ ИЗМЕНИЛИСЬ: "
                    f"{row['project']}/{row['tz']} {recorded or '<empty>'} -> {current}"
                )
            else:
                print("Границы задачи: без изменений.")
    msgs = inbox.pending_for(store, wid, max_items=3, max_bytes=2048)
    print(inbox.render_envelope(msgs, wid) if msgs else "Уведомлений нет.")
    return 0


def _cmd_inbox(worker_name: str | None, db: pathlib.Path) -> int:
    Store, _, inbox, _, _ = _mods()
    store = Store.open(str(db))
    state = _current_state(worker_name)
    wid = _touch(store, state)
    msgs = inbox.pending_for(store, wid, max_items=50, max_bytes=65536)
    print(inbox.render_envelope(msgs, wid) if msgs else "Уведомлений нет.")
    return 0


def _ensure_project(conn: sqlite3.Connection, project: str, root: pathlib.Path) -> None:
    from types import SimpleNamespace
    from dev_mcp.fleet.project_identity import require_available
    view = SimpleNamespace(
        query=lambda sql, params=(): conn.execute(sql, params).fetchall(),
        one=lambda sql, params=(): conn.execute(sql, params).fetchone())
    require_available(view, project, str(root.resolve()))
    conn.execute(
        "INSERT INTO projects(name,root,priority,active) VALUES (?,?,5,1) "
        "ON CONFLICT(name) DO UPDATE SET root=excluded.root",
        (project, str(root.resolve())),
    )


def _heading(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _find_tz(
    root: pathlib.Path,
    tz: str,
    head: dict[str, Any] | None = None,
) -> pathlib.Path | None:
    base = root / "ToDo" / "tz"
    matches = sorted(base.glob(f"{tz}-*.md")) + sorted(base.glob(f"{tz}.md"))
    if len(matches) <= 1:
        return matches[0] if matches else None

    # Legacy trees can temporarily contain duplicate TZ numbers. Never bind a
    # live claim to "the first filename": disambiguate by the claim's actual
    # ownership/resources, which are the safety boundary that matters.
    if head is not None:
        _, _, _, spec, _ = _mods()
        wanted_owns = set(_norm_owns(root, [str(x) for x in head.get("owns") or []]))
        wanted_resources = {str(x) for x in head.get("resources") or []}
        compatible: list[pathlib.Path] = []
        for candidate in matches:
            try:
                task = spec.parse_spec(str(candidate.resolve()))
            except Exception:
                continue
            if set(task.owns) == wanted_owns and set(task.resources) == wanted_resources:
                compatible.append(candidate)
        if len(compatible) == 1:
            return compatible[0]

    names = ", ".join(path.name for path in matches)
    raise ValueError(f"{tz}: неоднозначное ТЗ ({names}); номер нужно развести до импорта")


def _task_material(root: pathlib.Path, head: dict[str, Any]) -> dict[str, Any]:
    tz = str(head["tz"])
    path = _find_tz(root, tz, head)
    text = ""
    spec_path = ""
    sha = ""
    if path:
        try:
            text = path.read_text(encoding="utf-8")
            spec_path = path.relative_to(root).as_posix()
            _, _, _, spec, _ = _mods()
            sha = spec.spec_sha(str(path))
        except Exception:
            pass
    return {
        "tz": tz,
        "title": _heading(text, tz),
        "spec_path": spec_path,
        "spec_sha": sha,
        "needs": [str(x) for x in head.get("needs") or []],
        "owns": [str(x) for x in head.get("owns") or []],
        "resources": [str(x) for x in head.get("resources") or []],
        "acceptance": [str(x) for x in head.get("acceptance") or []],
        "priority": int(head.get("priority") or 5),
    }


def _norm_owns(root: pathlib.Path, owns: list[str]) -> list[str]:
    *_, model = _mods()
    return [model.norm_path(x, project_root=str(root)) for x in owns]


def _display_path(path_norm: str, root: pathlib.Path) -> str:
    value = path_norm.replace("\\", "/")
    prefix = str(root.resolve()).replace("\\", "/").casefold().rstrip("/")
    folded = value.casefold()
    if folded == prefix:
        return "."
    if folded.startswith(prefix + "/"):
        return value[len(prefix) + 1:]
    return value



def _legacy_zone(conn: sqlite3.Connection, worker_id: str, project: str, tz: str) -> str:
    rows = conn.execute(
        "SELECT payload FROM events WHERE worker_id=? AND project=? AND tz=? "
        "AND kind IN ('take','join') ORDER BY id DESC LIMIT 8",
        (worker_id, project, tz),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        if data.get("legacy_zone"):
            return str(data["legacy_zone"])
    return ""


def bridge_list(project: str, root: pathlib.Path, db: pathlib.Path) -> list[dict[str, Any]]:
    _open_store(db)
    out = []
    with _connect(db) as conn:
        rows = conn.execute(
            "SELECT c.project,c.tz,c.worker_id,c.started_at,t.owns,t.resources "
            "FROM claims c JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.project=? AND c.state='active' ORDER BY c.tz",
            (project,),
        ).fetchall()
        for row in rows:
            out.append({
                "tz": row["tz"],
                "zone": _legacy_zone(conn, row["worker_id"], row["project"], row["tz"]),
                "owns": [_display_path(str(x), root) for x in json.loads(row["owns"] or "[]")],
                "resources": [str(x) for x in json.loads(row["resources"] or "[]")],
                "started_at": row["started_at"],
            })
    return out


def bridge_start(project: str, root: pathlib.Path, head: dict[str, Any],
                 db: pathlib.Path) -> dict[str, Any]:
    _open_store(db)
    material = _task_material(root, head)
    owns_norm = _norm_owns(root, material["owns"])
    *_, model = _mods()
    with _connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_project(conn, project, root)
        conflicts = []
        rows = conn.execute(
            "SELECT c.project,c.tz,cp.path_norm FROM claims c "
            "JOIN claim_paths cp ON cp.claim_id=c.id WHERE c.state='active'"
        ).fetchall()
        for row in rows:
            if row["project"] == project and row["tz"] == material["tz"]:
                continue
            for mine_raw, mine_norm in zip(material["owns"], owns_norm):
                if model.paths_overlap(mine_norm, str(row["path_norm"])):
                    conflicts.append(
                        f"{row['tz']} уже пишет в {_display_path(str(row['path_norm']), root)} "
                        f"(твоё: {mine_raw})"
                    )
        for resource in material["resources"]:
            for row in conn.execute(
                "SELECT c.tz FROM claims c JOIN claim_resources cr ON cr.claim_id=c.id "
                "WHERE c.state='active' AND cr.resource=?", (resource,)
            ):
                if row["tz"] != material["tz"]:
                    conflicts.append(f"{row['tz']} держит эксклюзивный ресурс {resource}")
        if conflicts:
            conn.rollback()
            return {"ok": False, "conflicts": sorted(set(conflicts))}

        existing = conn.execute(
            "SELECT worker_id FROM claims WHERE project=? AND tz=? AND state='active' "
            "ORDER BY id DESC LIMIT 1", (project, material["tz"])
        ).fetchone()
        if existing:
            conn.rollback()
            return {"ok": True, "worker_id": existing["worker_id"], "neighbours": []}

        wid = _insert_worker(
            conn, f"Nova legacy {material['tz']}", "human", project, material["tz"],
            {"legacy_zone": str(head.get("zone") or "")},
        )
        now = _now()
        conn.execute(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'work',?,?,?,?,?,?,NULL,'',?,?) "
            "ON CONFLICT(project,tz) DO UPDATE SET title=excluded.title,state='taken',"
            "priority=excluded.priority,spec_path=excluded.spec_path,spec_sha=excluded.spec_sha,"
            "needs=excluded.needs,owns=excluded.owns,resources=excluded.resources,"
            "acceptance=excluded.acceptance,updated_at=excluded.updated_at",
            (
                project, material["tz"], material["title"], "taken", material["priority"],
                material["spec_path"], material["spec_sha"],
                json.dumps(material["needs"], ensure_ascii=False),
                json.dumps(owns_norm, ensure_ascii=False),
                json.dumps(material["resources"], ensure_ascii=False),
                json.dumps(material["acceptance"], ensure_ascii=False),
                now, now,
            ),
        )
        cur = conn.execute(
            "INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
            "VALUES (?,?,?,'active',?,NULL)", (project, material["tz"], wid, now)
        )
        cid = int(cur.lastrowid)
        conn.executemany("INSERT INTO claim_paths(claim_id,path_norm) VALUES (?,?)",
                         [(cid, x) for x in owns_norm])
        conn.executemany("INSERT INTO claim_resources(claim_id,resource) VALUES (?,?)",
                         [(cid, x) for x in material["resources"]])
        _event(conn, "take", wid, project, material["tz"],
               {"legacy_zone": str(head.get("zone") or ""), "legacy_specific": True})
        neighbours = []
        zone = str(head.get("zone") or "")
        if zone:
            for row in conn.execute(
                "SELECT worker_id,tz FROM claims WHERE project=? AND state='active' AND tz<>?",
                (project, material["tz"])
            ):
                if _legacy_zone(conn, row["worker_id"], project, row["tz"]) == zone:
                    neighbours.append(f"{row['tz']} тоже в зоне {zone}")
        conn.commit()
    return {"ok": True, "worker_id": wid, "neighbours": sorted(set(neighbours))}


def bridge_finish(project: str, tz: str, db: pathlib.Path) -> dict[str, Any]:
    Store, queue, _, _, _ = _mods()
    store = Store.open(str(db))
    with _connect(db) as conn:
        row = conn.execute(
            "SELECT worker_id FROM claims WHERE project=? AND tz=? AND state='active' "
            "ORDER BY id DESC LIMIT 1", (project, tz)
        ).fetchone()
    if not row:
        return {"ok": True, "existed": False}
    wid = str(row["worker_id"])
    review_tz = _submit_for_review(store, queue, wid, "legacy agent.py finish")
    queue.finish(store, wid, "legacy agent.py finish")
    return {"ok": True, "existed": True, "review_tz": review_tz}


def import_claims(claim_dir: pathlib.Path, root: pathlib.Path, project: str,
                  db: pathlib.Path) -> list[str]:
    _open_store(db)
    prepared = []
    for path in sorted(claim_dir.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            claim = json.loads(raw)
        except (OSError, ValueError):
            continue
        if claim.get("tz"):
            material = _task_material(root, claim)
            fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            prepared.append((claim, material, _norm_owns(root, material["owns"]), fingerprint))
    imported = []
    with _connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_project(conn, project, root)
        for claim, material, owns_norm, fingerprint in prepared:
            seen = False
            for row in conn.execute(
                "SELECT payload FROM events WHERE project=? AND tz=? AND kind='take' ORDER BY id DESC",
                (project, material["tz"]),
            ):
                try:
                    payload = json.loads(row["payload"] or "{}")
                except ValueError:
                    continue
                if payload.get("legacy_claim_fingerprint") == fingerprint:
                    seen = True
                    break
            if seen or conn.execute(
                "SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active' LIMIT 1",
                (project, material["tz"]),
            ).fetchone():
                continue
            wid = _insert_worker(
                conn, f"{IMPORT_LABEL} — {material['tz']}", "human", project, material["tz"],
                {"legacy_zone": str(claim.get("zone") or ""), "imported": True},
            )
            now = _now()
            started = str(claim.get("started_at") or now)
            conn.execute(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                "resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'work',?,?,?,?,?,?,NULL,'',?,?) "
                "ON CONFLICT(project,tz) DO UPDATE SET state='taken',title=excluded.title,"
                "priority=excluded.priority,spec_path=excluded.spec_path,spec_sha=excluded.spec_sha,"
                "needs=excluded.needs,owns=excluded.owns,resources=excluded.resources,"
                "acceptance=excluded.acceptance,updated_at=excluded.updated_at",
                (
                    project, material["tz"], material["title"], "taken", material["priority"],
                    material["spec_path"], material["spec_sha"],
                    json.dumps(material["needs"], ensure_ascii=False),
                    json.dumps(owns_norm, ensure_ascii=False),
                    json.dumps(material["resources"], ensure_ascii=False),
                    json.dumps(material["acceptance"], ensure_ascii=False),
                    started, now,
                ),
            )
            cur = conn.execute(
                "INSERT INTO claims(project,tz,worker_id,state,started_at,released_at) "
                "VALUES (?,?,?,'active',?,NULL)", (project, material["tz"], wid, started)
            )
            cid = int(cur.lastrowid)
            conn.executemany("INSERT INTO claim_paths(claim_id,path_norm) VALUES (?,?)",
                             [(cid, x) for x in owns_norm])
            conn.executemany("INSERT INTO claim_resources(claim_id,resource) VALUES (?,?)",
                             [(cid, x) for x in material["resources"]])
            _event(conn, "take", wid, project, material["tz"], {
                "legacy_zone": str(claim.get("zone") or ""),
                "imported": True,
                "legacy_claim_fingerprint": fingerprint,
            })
            imported.append(material["tz"])
        conn.commit()
    return imported



def import_requests(root: pathlib.Path, project: str, db: pathlib.Path) -> list[str]:
    _open_store(db)
    request_dir = root / "ToDo" / "requests"
    paths = sorted(request_dir.glob("*.md")) if request_dir.exists() else []
    imported = []
    with _connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_project(conn, project, root)
        for path in paths:
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            tz = "REQ-" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12].upper()
            if conn.execute("SELECT 1 FROM tasks WHERE project=? AND tz=?", (project, tz)).fetchone():
                continue
            title = _heading(body, path.stem)
            now = _now()
            conn.execute(
                "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                "resources,acceptance,author_worker,body,created_at,updated_at) "
                "VALUES (?,?,?,'draft',5,'request',?,'','[]','[]','[]','[]',NULL,?,?,?) "
                "ON CONFLICT(project,tz) DO UPDATE SET title=excluded.title,kind='request',"
                "spec_path=excluded.spec_path,body=excluded.body,updated_at=excluded.updated_at",
                (project, tz, title, rel, body, now, now),
            )
            _event(conn, "write", project=project, tz=tz, payload={"import_request": rel})
            imported.append(tz)
        conn.commit()
    return imported


def _bridge_main(args: argparse.Namespace, db: pathlib.Path) -> int:
    root = pathlib.Path(args.project_root).resolve()
    project = args.project or root.name
    if args.bridge_command == "start":
        result = bridge_start(project, root, json.loads(args.head_json), db)
    elif args.bridge_command == "finish":
        result = bridge_finish(project, args.tz, db)
    elif args.bridge_command == "list":
        result = {"ok": True, "claims": bridge_list(project, root, db)}
    elif args.bridge_command == "import-claims":
        result = {
            "ok": True,
            "imported": import_claims(pathlib.Path(args.claim_dir), root, project, db),
        }
    elif args.bridge_command == "import-requests":
        result = {"ok": True, "imported": import_requests(root, project, db)}
    else:
        raise SystemExit(f"unknown bridge command: {args.bridge_command}")
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _selftest() -> int:
    assert _slug("claude code") == "claude-code"
    assert _slug("   ") == "worker"
    with tempfile.TemporaryDirectory(prefix="dev-mcp-worker-selftest-") as td:
        previous = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = td
        try:
            _write_state("self test", "W42", "codex")
            state = _current_state()
            assert state["worker_id"] == "W42"
            assert state["label"] == "self test"
        finally:
            if previous is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = previous
    print("worker: UTF-8/state-cache/CLI helpers — selftest ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-name", default=None)
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")

    join = sub.add_parser("join")
    join.add_argument("label")
    join.add_argument("--kind", default="chatgpt",
                      choices=["chatgpt", "claude-code", "codex", "human"])

    take = sub.add_parser("take")
    take.add_argument("--project", default=None)

    sub.add_parser("resume", help="показать активную задачу и durable-сводку после перезапуска")
    sub.add_parser("sync")
    submit = sub.add_parser("submit")
    submit.add_argument("note")
    sub.add_parser("finish")
    run = sub.add_parser("run", help="запустить acceptance-команду и записать реальный rc в fleet journal")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    review = sub.add_parser("review", help="вердикт по активной review-задаче")
    review.add_argument("verdict", choices=["accept", "reject"])
    review.add_argument("note", nargs="?", default="")
    sub.add_parser("inbox")
    ack = sub.add_parser("ack")
    ack.add_argument("ids", nargs="+")

    bridge = sub.add_parser("bridge", help=argparse.SUPPRESS)
    bridge.add_argument("--project-root", required=True)
    bridge.add_argument("--project", default=None)
    bsub = bridge.add_subparsers(dest="bridge_command", required=True)
    start = bsub.add_parser("start")
    start.add_argument("--head-json", required=True)
    finish = bsub.add_parser("finish")
    finish.add_argument("--tz", required=True)
    bsub.add_parser("list")
    imp = bsub.add_parser("import-claims")
    imp.add_argument("--claim-dir", required=True)
    bsub.add_parser("import-requests")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.selftest:
        return _selftest()
    if not args.command:
        parser.print_help()
        return 2
    db = _db_path(args.db)
    if args.command == "bridge":
        return _bridge_main(args, db)
    if args.command == "join":
        wid = _join(args.label, args.kind, db)
        print(f"{wid} — {args.label} ({args.kind})")
        return 0

    Store, queue, inbox, _, _ = _mods()
    store = Store.open(str(db))
    state = _current_state(args.worker_name)
    wid = _touch(store, state)
    if args.command == "take":
        task = queue.take_next(store, wid, project=args.project)
        if task is None:
            print("Нет доступных задач.")
            return 0
        _print_task(task, db)
        return 0
    if args.command == "resume":
        from dev_mcp.fleet import briefing

        print(briefing.resume(store, wid))
        return 0
    if args.command == "sync":
        return _sync(args.worker_name, db)
    if args.command == "submit":
        checkout = _claim_checkout(db, wid)
        review_tz = _submit_for_review(store, queue, wid, args.note, checkout=checkout)
        print(f"Сдано; review={review_tz}. Теперь вызови finish, чтобы освободить claim/resources.")
        return 0
    if args.command == "finish":
        queue.finish(store, wid, "")
        print("Завершено: claim/resources освобождены.")
        return 0
    if args.command == "run":
        return _run_evidence(wid, list(args.argv), db)
    if args.command == "review":
        from dev_mcp.fleet import review

        review.decide(store, wid, args.verdict, args.note)
        print(f"Вердикт {args.verdict} сохранён.")
        return 0
    if args.command == "inbox":
        return _cmd_inbox(args.worker_name, db)
    if args.command == "ack":
        print(f"Подтверждено: {inbox.ack(store, wid, list(args.ids))}")
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())



