"""Обязательства продукта в реестре: определения — у ведущего, состояние — из доказательств.

LEAD-SYSTEM-DESIGN-2026-11-06 §2 Р2, фаза 2.1 (07.11.2026). Модель одна:
- контракт (goal.md, сценарии, поручения) — у владельца;
- определения O-* — у ведущего в `ToDo/lead/LEDGER.json` (git): id, источник, обещание,
  требуемый уровень. Поля статуса в файле платформа не читает;
- состояние и доказательства — в реестре у платформы. Проекция односторонняя: каждое поле
  пишет один писатель.

Определения проецируются в `outcomes` отдельным поддеревом: корень `<проект>/product`,
по строке на O-* с id `<проект>/O-…`, `intent_id = product:<проект>`. Это поддерево НЕ видит
инвариант контроллера (`outcome.check_invariant`): у обязательства нет задач в `outcome_work`,
и контроллер иначе заводил бы на каждое «план» — ровно та автогенерация работы, которую
фазы 1.x убрали. Работу над O-* выбирает ведущий, а не контроллер.

Доказательства — квитанции свидетелей (`witness_result`) в `evidence`: `kind='measure'`,
`subject_version` = HEAD проекта при замере, `env_version` = `instrument:<sha прибора>`.
`works_live` → pass уровня ячейки (по умолчанию L3, поле), `broken` → fail, `unknown` —
не доказательство (L0: не измеряется).

Уровни: L0 не измеряется · L1 лаборатория · L2 интеграция · L3 поле · L4 выпуск.
Состояние O-* вычисляется (`state`), его не пишет никто:
- уровень — последний pass свидетеля, если его последний замер — pass;
- `instrument_changed` — sha прибора на этом pass отличается от его предыдущего замера:
  переход на изменённом приборе не засчитывается (06.11: 150 правок свидетеля за ночь);
- `stale` — pass снят на другом HEAD, чем сейчас, или старше STALE_AFTER_H.
"""
from __future__ import annotations

import hashlib
import re
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LEDGER = Path("ToDo") / "lead" / "LEDGER.json"
AUTHORITY = "lead-ledger"
LEVELS = ("L0", "L1", "L2", "L3", "L4")
DEFAULT_LIVE_LEVEL = "L3"
DEFAULT_REQUIRED = "L3"
STALE_AFTER_H = 72
_seen: dict[str, str] = {}  # путь книги → sha последней проекции


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def root_id(project: str) -> str:
    return f"{project}/product"


def outcome_id(project: str, obligation: str) -> str:
    return f"{project}/{obligation}"


def _level(value: Any, default: str) -> str:
    text = str(value or "").strip().upper()
    return text if text in LEVELS else default


def definitions(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """LEDGER.json → O-id → определение. Статусы и доказательства из файла не берутся."""
    items = ledger.get("obligations") if isinstance(ledger, dict) else None
    if isinstance(items, dict):
        items = [{"id": k, **v} for k, v in items.items() if isinstance(v, dict)]
    out: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("id") or "").strip()
        if not oid.startswith("O-") or len(oid) > 120:
            continue
        source = item.get("contract_refs") or item.get("source") or []
        refs = [str(s) for s in (source if isinstance(source, list) else [source]) if s]
        out[oid] = {
            "contract_refs": refs,
            "section": str(item.get("section") or ""),
            "promise": str(item.get("promise") or "")[:2000],
            "path": str(item.get("path") or item.get("life") or ""),
            "platform": str(item.get("platform") or ""),
            "required_level": _level(item.get("required_level"), DEFAULT_REQUIRED),
            "witnesses": [str(w) for w in item.get("witnesses") or [] if isinstance(w, str)],
        }
    return out


def project(store: Any, project_name: str, root: str | Path, *, force: bool = False) -> dict[str, int]:
    """Односторонняя проекция определений книги ведущего в `outcomes`. Идемпотентна."""
    path = Path(root) / LEDGER
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    digest = hashlib.sha256(raw).hexdigest()
    key = str(path).casefold()
    if not force and _seen.get(key) == digest:
        return {}
    try:
        defs = definitions(json.loads(raw.decode("utf-8")))
    except (UnicodeError, ValueError):
        return {"invalid": 1}
    now, rid = _now(), root_id(project_name)
    stats = {"added": 0, "changed": 0, "dropped": 0}
    statements: list[tuple[str, tuple[Any, ...]]] = []
    insert = ("INSERT INTO outcomes(id,parent_id,project,title,intent_id,spec,evidence_spec,authority,"
              "state,rev,created_ts) VALUES(?,?,?,?,?,?,?,?,'open',1,?)")
    with store.tx() as tx:
        if tx.one("SELECT 1 FROM outcomes WHERE id=?", (rid,)) is None:
            statements.append((insert, (
                rid, None, project_name, "Обязательства продукта", f"product:{project_name}",
                json.dumps({"ledger": str(LEDGER).replace("\\", "/")}), "{}", AUTHORITY, now)))
        current = {r["id"]: r for r in tx.query(
            "SELECT id,spec,evidence_spec,state,rev FROM outcomes WHERE parent_id=?", (rid,))}
        for oid, spec in defs.items():
            ident = outcome_id(project_name, oid)
            body = json.dumps(spec, ensure_ascii=False, sort_keys=True)
            want = json.dumps({"kind": "measure", "required_level": spec["required_level"]},
                              sort_keys=True)
            row = current.get(ident)
            if row is None:
                statements.append((insert, (ident, rid, project_name, oid, f"product:{project_name}",
                                            body, want, AUTHORITY, now)))
                stats["added"] += 1
            elif row["spec"] != body or row["evidence_spec"] != want or row["state"] != "open":
                statements.append(("UPDATE outcomes SET spec=?,evidence_spec=?,state='open',"
                                   "closed_ts=NULL,rev=rev+1 WHERE id=?", (body, want, ident)))
                stats["changed"] += 1
        for ident, row in current.items():
            if row["state"] == "open" and ident.split("/", 1)[1] not in defs:
                statements.append(("UPDATE outcomes SET state='dropped',closed_ts=?,rev=rev+1 "
                                   "WHERE id=?", (now, ident)))
                stats["dropped"] += 1
        if statements:
            tx.changes(statements, event={
                "kind": "product_projection", "project": project_name,
                "payload": {**stats, "ledger_sha": digest, "obligations": len(defs)}})
    _seen[key] = digest
    return stats


def _evidence_row(tx: Any, project_name: str, event_id: int, result: dict[str, Any],
                  level: str, ts: str) -> tuple[str, tuple[Any, ...]] | None:
    obligation = str(result.get("obligation") or "")
    verdict = str(result.get("verdict") or "")
    head = str(result.get("head") or "")
    if not obligation.startswith("O-") or verdict not in ("works_live", "broken") or not head:
        return None
    ident = outcome_id(project_name, obligation)
    if tx.one("SELECT 1 FROM outcomes WHERE id=?", (ident,)) is None:
        return None
    lvl = _level(result.get("level") or level, DEFAULT_LIVE_LEVEL)
    witness = str(result.get("id") or "?")
    return ("INSERT OR IGNORE INTO evidence(id,outcome_id,kind,ref,subject_version,env_version,"
            "verdict,producer,ts) VALUES(?,?,'measure',?,?,?,?,'witness',?)",
            (f"witness:{event_id}", ident, f"witness:{witness}@{lvl}", head,
             f"instrument:{result.get('instrument_sha') or ''}",
             "pass" if verdict == "works_live" else "fail", ts))


def record_witness(tx: Any, project_name: str, event_id: int, result: dict[str, Any],
                   level: str = "") -> bool:
    """Квитанция свидетеля → строка `evidence` (в той же транзакции, что и событие)."""
    row = _evidence_row(tx, project_name, event_id, result, level, _now())
    if row is None:
        return False
    tx.changes([row], event={"kind": "product_evidence", "project": project_name,
                             "payload": {"witness_event": event_id}})
    return True


def backfill(store: Any, project_name: str) -> int:
    """Квитанции `witness_result`, записанные до проекции, → `evidence` (идемпотентно)."""
    rows: list[tuple[str, tuple[Any, ...]]] = []
    with store.tx() as tx:
        for event in tx.query("SELECT id,ts,payload FROM events WHERE kind='witness_result' AND project=? "
                              "AND NOT EXISTS (SELECT 1 FROM evidence v WHERE v.id='witness:'||events.id) "
                              "ORDER BY id", (project_name,)):
            try:
                result = json.loads(event["payload"] or "{}")
            except ValueError:
                continue
            if isinstance(result, dict):
                row = _evidence_row(tx, project_name, int(event["id"]), result, "", str(event["ts"]))
                if row is not None:
                    rows.append(row)
        if rows:
            tx.changes(rows, event={"kind": "product_evidence", "project": project_name,
                                    "payload": {"backfill": len(rows)}})
    return len(rows)


def sync(store: Any) -> dict[str, dict[str, int]]:
    """Проекция книг всех активных проектов с ведущим + догон квитанций."""
    out: dict[str, dict[str, int]] = {}
    with store.read() as tx:
        projects = [(str(r["name"]), str(r["root"])) for r in tx.query(
            "SELECT name,root FROM projects WHERE active=1 AND root IS NOT NULL")]
    for name, root in projects:
        if not (Path(root) / LEDGER).is_file():
            continue
        stats = project(store, name, root)
        if stats:
            stats = {**stats, "evidence": backfill(store, name)}
            out[name] = stats
    return out


def _head(root: str) -> str:
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def state(store: Any, project_name: str, *, head: str | None = None,
          now: datetime | None = None) -> list[dict[str, Any]]:
    """Вычисленное состояние каждого открытого O-* проекта (никем не записывается)."""
    now = now or datetime.now(timezone.utc)
    with store.read() as tx:
        if head is None:
            root = tx.scalar("SELECT root FROM projects WHERE name=?", (project_name,))
            head = _head(str(root)) if root else ""
        rows = tx.query("SELECT id,title,evidence_spec FROM outcomes WHERE parent_id=? AND state='open' "
                        "ORDER BY id", (root_id(project_name),))
        proofs = tx.query("SELECT id,outcome_id,ref,subject_version,env_version,verdict,ts FROM evidence "
                          "WHERE kind='measure' AND outcome_id IN (SELECT id FROM outcomes WHERE "
                          "parent_id=?) ORDER BY ts,id", (root_id(project_name),))
        # Все замеры свидетелей (любой вердикт): sha прибора предыдущего замера.
        measured = tx.query("SELECT id,json_extract(payload,'$.id') AS w,"
                            "json_extract(payload,'$.instrument_sha') AS sha FROM events "
                            "WHERE kind='witness_result' AND project=? ORDER BY id", (project_name,))
        accepted: set[tuple[str, str]] = set()
        for r in tx.query("SELECT payload FROM events WHERE kind='instrument_accepted' AND project=?",
                          (project_name,)):
            try:
                receipt = json.loads(r["payload"] or "{}")
            except ValueError:
                continue
            if receipt.get("accepted"):
                accepted.add((str(receipt.get("cell")), str(receipt.get("sha"))))
    previous_sha: dict[int, str] = {}
    last_by_witness: dict[str, str] = {}
    for m in measured:
        previous_sha[int(m["id"])] = last_by_witness.get(str(m["w"]), "")
        last_by_witness[str(m["w"])] = str(m["sha"] or "")
    runs: dict[tuple[str, str], list[Any]] = {}
    for p in proofs:
        witness = str(p["ref"]).rsplit("@", 1)[0]
        runs.setdefault((p["outcome_id"], witness), []).append(p)
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            required = _level(json.loads(row["evidence_spec"] or "{}").get("required_level"),
                              DEFAULT_REQUIRED)
        except ValueError:
            required = DEFAULT_REQUIRED
        item: dict[str, Any] = {"id": row["title"], "required": required, "level": "L0",
                                "measured": False, "broken": False, "stale": False,
                                "instrument_changed": False, "unaccepted_instrument": False}
        best = best_accepted = -1
        for (oid, _w), seq in runs.items():
            if oid != row["id"]:
                continue
            item["measured"] = True
            last = seq[-1]
            if last["verdict"] != "pass":
                item["broken"] = True
                continue
            event_id = str(last["id"]).removeprefix("witness:")
            before = previous_sha.get(int(event_id)) if event_id.isdigit() else None
            if before and f"instrument:{before}" != last["env_version"]:
                item["instrument_changed"] = True
                continue  # переход на изменённом приборе не засчитывается
            lvl = LEVELS.index(str(last["ref"]).rsplit("@", 1)[-1]) if "@" in str(last["ref"]) else 3
            try:
                old = now - datetime.fromisoformat(str(last["ts"])) > timedelta(hours=STALE_AFTER_H)
            except ValueError:
                old = True
            if (head and last["subject_version"] != head) or old:
                item["stale"] = True
            best = max(best, lvl)
            # 2.3: требуемый уровень засчитывается только прибором, принятым с отрицательным
            # контролем (instrument_accepted с этим sha); иначе уровень виден, но не «достигнут».
            if (_w.removeprefix("witness:"), last["env_version"].removeprefix("instrument:")) in accepted:
                best_accepted = max(best_accepted, lvl)
            else:
                item["unaccepted_instrument"] = True
        if best >= 0:
            item["level"] = LEVELS[best]
        item["met"] = (
            best_accepted >= LEVELS.index(required) and not item["stale"] and not item["broken"]
        )
        out.append(item)
    return out


def brief_lines(store: Any, project_name: str, *, head: str | None = None) -> list[str]:
    """Строка брифа ведущего: обязательства по уровням — из доказательств, не из книги."""
    items = state(store, project_name, head=head)
    if not items:
        return []
    with store.read() as tx:
        root = tx.scalar("SELECT root FROM projects WHERE name=?", (project_name,))
    contract = coverage_lines(str(root)) if root else []
    by_level: dict[str, int] = {}
    for item in items:
        by_level[item["level"]] = by_level.get(item["level"], 0) + 1
    levels = " ".join(f"{k}:{by_level[k]}" for k in LEVELS if k in by_level)
    flags = []
    for key, label in (("broken", "сломано"), ("stale", "устарело"),
                       ("instrument_changed", "прибор сменился")):
        ids = [i["id"] for i in items if i[key]]
        if ids:
            flags.append(f"{label} {len(ids)} ({', '.join(ids[:5])})")
    met = sum(1 for i in items if i["met"])
    lines = contract + [f"Обязательства (реестр, из доказательств): {len(items)}, достигли требуемого {met}; "
             f"уровни {levels}" + ("; " + "; ".join(flags) if flags else "")]
    from . import arbiter, instrument
    lines += instrument.brief_lines(store, project_name)
    lines += arbiter.brief_lines(store, project_name)
    orphans = orphan_witnesses(store, project_name)
    if orphans:
        # 07.11: 3 из 6 свидетелей Nova мерили O-*, которых нет в книге — их works_live
        # ни к чему не засчитывался. Ячейка или книга должны сойтись.
        lines.append("Свидетели без обязательства в книге: " + ", ".join(orphans[:8])
                     + " — добавь O-* в LEDGER или исправь obligation ячейки (через оператора)")
    return lines


CANON = Path("intent") / "goal.md"
_CONTRACT_HEADING = re.compile(r"^#{2,4}\s+.*(сценари|требован|критери|обязательн)", re.I)
_ITEM = re.compile(r"^\s{0,3}(\d{1,3})\.\s+(.*)$")
_SID = re.compile(r"\bS(\d{2,3})\b")


def contract_items(root: str | Path) -> list[dict[str, str]]:
    """Структурные пункты канона (LEAD-SYSTEM-DESIGN 2.2): нумерованные пункты под разделами
    «Сценарии» / «Требования» / «Критерии» в intent/goal.md. Id — явный `Sxx` в тексте пункта,
    иначе `S` + номер в разделе. Детерминированно, без LLM."""
    try:
        text = (Path(root) / CANON).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    items: list[dict[str, str]] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("#"):
            inside = bool(_CONTRACT_HEADING.match(line))
            continue
        match = _ITEM.match(line) if inside else None
        if match is None:
            continue
        explicit = _SID.search(match.group(2)[:80])
        sid = f"S{int(explicit.group(1)):02d}" if explicit else f"S{int(match.group(1)):02d}"
        title = re.sub(r"[*_`]", "", match.group(2)).split(".")[0].strip()
        title = re.sub(rf"^{sid}\s*[—–-]\s*", "", title)[:100]
        items.append({"id": sid, "title": title})
    return items


def coverage(root: str | Path) -> dict[str, Any]:
    """Пункт канона → O-* книги, `declined(reason)` или непокрыт. Читает канон и книгу."""
    items = contract_items(root)
    if not items:
        return {}
    try:
        ledger = json.loads((Path(root) / LEDGER).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        ledger = {}
    defs = definitions(ledger)
    declined = {}
    for entry in (ledger.get("declined") if isinstance(ledger, dict) else None) or []:
        if isinstance(entry, dict) and entry.get("ref") and entry.get("reason"):
            declined[str(entry["ref"]).upper()] = str(entry["reason"])
    table: dict[str, dict[str, Any]] = {}
    for item in items:
        sid = item["id"]
        covering = sorted(oid for oid, spec in defs.items()
                          if any(f"S{int(m):02d}" == sid for text in
                                 [*spec["contract_refs"], spec["section"], spec["promise"]]
                                 for m in _SID.findall(text)))
        table[sid] = {"title": item["title"], "obligations": covering,
                      "declined": declined.get(sid, "")}
    return {"items": table,
            "covered": sorted(k for k, v in table.items() if v["obligations"]),
            "declined": sorted(k for k, v in table.items() if v["declined"] and not v["obligations"]),
            "uncovered": sorted(k for k, v in table.items() if not v["obligations"] and not v["declined"])}


def coverage_lines(root: str | Path) -> list[str]:
    """Строка брифа (не задача): покрытие контракта обязательствами."""
    cov = coverage(root)
    if not cov:
        return []
    total = len(cov["items"])
    line = (f"Контракт (intent/goal.md): пунктов {total}, покрыто O-* {len(cov['covered'])}, "
            f"отклонено {len(cov['declined'])}")
    if cov["uncovered"]:
        names = [f"{k} «{cov['items'][k]['title'][:40]}»" for k in cov["uncovered"][:6]]
        line += (f"; непокрыто {len(cov['uncovered'])}: " + ", ".join(names)
                 + " — укажи Sxx в contract_refs своего O-* или declined[{ref, reason}] в книге")
    return [line]


def orphan_witnesses(store: Any, project_name: str, *, days: int = 7) -> list[str]:
    """Свидетели последних days суток, чьё obligation не спроецировано из книги."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store.read() as tx:
        seen = tx.query("SELECT DISTINCT json_extract(payload,'$.id') AS w,"
                        "json_extract(payload,'$.obligation') AS o FROM events "
                        "WHERE kind='witness_result' AND project=? AND ts>?", (project_name, since))
        known = {r["title"] for r in tx.query(
            "SELECT title FROM outcomes WHERE parent_id=? AND state='open'", (root_id(project_name),))}
    return sorted(f"{r['w']}→{r['o']}" for r in seen if r["o"] and r["o"] not in known)


def main(argv: list[str] | None = None) -> int:
    """`python -m dev_mcp.fleet.product <проект> [--sync]` — вычисленное состояние O-*."""
    from .store import Store
    args = list(sys.argv[1:] if argv is None else argv)
    db = Path(__file__).resolve().parents[3] / "data" / "fleet" / "fleet.sqlite"
    store = Store.open(str(db))
    if "--sync" in args:
        args.remove("--sync")
        print(json.dumps(sync(store), ensure_ascii=False))
    for name in args:
        for line in brief_lines(store, name):
            print(line)
        for item in state(store, name):
            print(json.dumps(item, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
