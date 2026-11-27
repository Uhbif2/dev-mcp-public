"""Арбитр: суточный read-only аудит инвариантов системы ведущего (LEAD-SYSTEM-DESIGN 3.2).

Вывод — строки брифа ведущего и отчёт оператору (`data/fleet/arbiter-report.json`, событие
`arbiter_report`). **Заявок и задач арбитр не создаёт**: 27.10 генераторы ремонтных заявок
давали тысячи задач без адресата, поэтому находка здесь — наблюдение, а не работа.

Инварианты (по проекту с книгой ведущего, окно 24 ч):
- покрытие контракта: пункты канона без O-* и без declined;
- свежесть: O-* с устаревшим доказательством (другой HEAD или старше 72 ч);
- приборы: ячейки без принятого sha; sha без квитанции «красный на поломке»;
- готовность: открытые препятствия без обязательств (карточка ничего не разблокирует);
- переходы / сдачи: заявленные вехи, их LLM-ревью и сдачи ведущего — North Star и цена.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from dev_mcp.json_book import write_json
from pathlib import Path
from typing import Any

EVERY_S = 24 * 3600
KIND = "arbiter_report"
REPORT = Path(__file__).resolve().parents[3] / "data" / "fleet" / "arbiter-report.json"
LEAD_TZ = "(UPPER(tz) LIKE 'TZ-LEAD%' OR UPPER(tz) LIKE 'FIX:TZ-LEAD%')"


def audit_project(store: Any, project: str, root: str, *, now: datetime | None = None) -> dict[str, Any]:
    from . import instrument, obstacles, product
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    found: dict[str, Any] = {"project": project}
    cov = product.coverage(root)
    if cov:
        found["contract"] = {"items": len(cov["items"]), "covered": len(cov["covered"]),
                             "declined": len(cov["declined"]), "uncovered": cov["uncovered"]}
    items = product.state(store, project)
    found["obligations"] = {
        "total": len(items), "met": sum(1 for i in items if i["met"]),
        "stale": [i["id"] for i in items if i["stale"]],
        "broken": [i["id"] for i in items if i["broken"]],
        "unaccepted_instrument": [i["id"] for i in items if i["unaccepted_instrument"]],
    }
    found["instruments"] = instrument.audit(store, project)
    found["orphan_witnesses"] = product.orphan_witnesses(store, project)
    found["obstacles_without_obligation"] = [
        i["id"] for i in obstacles.open_items(store, project) if not i.get("obligations")]
    with store.read() as tx:
        submits = tx.scalar(f"SELECT COUNT(*) FROM events WHERE project=? AND kind='submit' "
                            f"AND {LEAD_TZ} AND ts>?", (project, since)) or 0
        claims = tx.scalar("SELECT COUNT(*) FROM events WHERE project=? AND kind='lead_milestone_claim' "
                           "AND ts>?", (project, since)) or 0
        reviews = tx.scalar("SELECT COUNT(DISTINCT tz) FROM events WHERE project=? AND kind='take' AND ts>? "
                            "AND (UPPER(tz) LIKE 'REVIEW:TZ-LEAD%' OR UPPER(tz) LIKE 'REVIEW:FIX:TZ-LEAD%')",
                            (project, since)) or 0
        # 08.11: холодное ревью — вердикт вехи с собственной оценкой до чтения отчёта ведущего.
        verdicts = tx.query("SELECT json_extract(payload,'$.note') AS note FROM events WHERE project=? "
                            "AND kind IN ('accept','reject') AND ts>? AND "
                            "UPPER(COALESCE(json_extract(payload,'$.review_task'),'')) LIKE 'REVIEW:%TZ-LEAD%'",
                            (project, since))
    cold = sum(1 for v in verdicts if "холодно:" in str(v["note"] or "").casefold())
    found["window_24h"] = {"lead_submits": int(submits), "milestone_claims": int(claims),
                           "milestone_reviews": int(reviews), "milestone_verdicts": len(verdicts),
                           "cold_verdicts": cold}
    return found


def findings(report: dict[str, Any]) -> list[str]:
    """Нарушения инвариантов одной строкой каждое (пусто — инварианты держатся)."""
    out: list[str] = []
    c = report.get("contract") or {}
    if c.get("uncovered"):
        out.append(f"покрытие: {len(c['uncovered'])} из {c['items']} пунктов канона без O-*")
    o = report.get("obligations") or {}
    if o.get("stale"):
        out.append(f"свежесть: доказательство устарело у {len(o['stale'])} O-*")
    i = report.get("instruments") or {}
    if i.get("unpinned") or i.get("unproven"):
        out.append(f"приборы: без приёмки {len(i.get('unpinned') or [])}, sha без квитанции "
                   f"{len(i.get('unproven') or [])}")
    if report.get("orphan_witnesses"):
        out.append(f"свидетели без O-* в книге: {len(report['orphan_witnesses'])}")
    if report.get("obstacles_without_obligation"):
        out.append(f"препятствия без обязательств: {len(report['obstacles_without_obligation'])}")
    w = report.get("window_24h") or {}
    if w.get("milestone_reviews", 0) > max(1, w.get("milestone_claims", 0)):
        out.append(f"ревью вех {w['milestone_reviews']} больше заявленных переходов "
                   f"{w.get('milestone_claims', 0)}")
    if w.get("milestone_verdicts", 0) > w.get("cold_verdicts", 0):
        out.append(f"холодное ревью: {w.get('cold_verdicts', 0)} из {w['milestone_verdicts']} вердиктов вех "
                   "со строкой «холодно:»")
    return out


def run(store: Any, *, force: bool = False, now: datetime | None = None) -> dict[str, Any] | None:
    """Раз в сутки (или force): отчёт по всем проектам с книгой ведущего."""
    from . import product
    now = now or datetime.now(timezone.utc)
    with store.read() as tx:
        last = tx.one("SELECT ts FROM events WHERE kind=? ORDER BY id DESC LIMIT 1", (KIND,))
        projects = [(str(r["name"]), str(r["root"])) for r in tx.query(
            "SELECT name,root FROM projects WHERE active=1 AND root IS NOT NULL")]
    if not force and last is not None:
        try:
            if now - datetime.fromisoformat(str(last["ts"])) < timedelta(seconds=EVERY_S):
                return None
        except ValueError:
            pass
    reports = []
    for name, root in projects:
        if (Path(root) / product.LEDGER).is_file():
            report = audit_project(store, name, root, now=now)
            report["findings"] = findings(report)
            reports.append(report)
    summary = {"ts": now.isoformat(), "projects": reports}
    try:
        write_json(REPORT, summary, indent=1)
    except OSError:
        pass
    with store.tx() as tx:
        tx.record_event(kind=KIND, payload={"projects": {r["project"]: r["findings"] for r in reports}})
    return summary


def brief_lines(store: Any, project: str) -> list[str]:
    """Последний суточный вердикт арбитра по проекту — строка брифа, не задача."""
    with store.read() as tx:
        row = tx.one("SELECT ts,payload FROM events WHERE kind=? ORDER BY id DESC LIMIT 1", (KIND,))
    if row is None:
        return []
    try:
        found = (json.loads(row["payload"] or "{}").get("projects") or {}).get(project)
    except ValueError:
        return []
    if not found:
        return []
    return [f"Арбитр ({str(row['ts'])[:16]}, суточный аудит, не задача): " + "; ".join(found)]


def main() -> int:
    from .store import Store
    db = Path(__file__).resolve().parents[3] / "data" / "fleet" / "fleet.sqlite"
    summary = run(Store.open(str(db)), force=True)
    for report in (summary or {}).get("projects", []):
        print(report["project"], "—", "; ".join(report["findings"]) or "инварианты держатся")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
