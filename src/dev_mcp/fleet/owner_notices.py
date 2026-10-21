"""Durable owner attention, projected by the server and delivered by FleetChrome.

Transport receipts are deliberately incapable of approving an action or thawing work.
"""
from __future__ import annotations

from dev_mcp import machine

from datetime import datetime, timezone
import hashlib
import json
import subprocess
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
from typing import Any

_ID = re.compile(r"on-[a-f0-9]{24}$")


def ops_refinement_id(outcome_id: str) -> str:
    return 'request:'+hashlib.sha256((outcome_id+':concrete-ops-request-v1').encode()).hexdigest()[:8]


def attention(tx: Any) -> list[dict[str, str]]:
    alerts: list[dict[str,str]] = []
    if tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_attention'"):
        for row in tx.query("SELECT fingerprint,episode,summary FROM watchdog_attention WHERE state='active'"):
            alerts.append({'key':f"watchdog:{row['fingerprint']}:{row['episode']}",
                           'text':'Автоматика требует внимания оператора: '+str(row['summary'])})
    network = tx.one("SELECT id,payload FROM events WHERE kind='resource' "
                     "AND path_norm='resource:chatgpt-reachable' ORDER BY id DESC LIMIT 1")
    if network and json.loads(network['payload']).get('state') == 'down':
        alerts.append({'key':'network:'+str(network['id']), 'text':
                       'ChatGPT недоступен: новые агенты ожидают восстановления маршрута.'})
    disk = tx.one("SELECT ts,payload FROM events WHERE kind='storage_tick' ORDER BY id DESC LIMIT 1")
    if disk and json.loads(disk['payload']).get('state') == 'down':
        last_up = tx.one("SELECT id,ts FROM events WHERE kind='storage_tick' "
                         "AND json_extract(payload,'$.state')='up' ORDER BY id DESC LIMIT 1")
        start = tx.one("SELECT id,ts FROM events WHERE kind='storage_tick' AND id>? ORDER BY id LIMIT 1",
                       (int(last_up['id']) if last_up else 0,))
        if start and (datetime.now(timezone.utc)-datetime.fromisoformat(start['ts'])).total_seconds() >= 300:
            alerts.append({'key':'storage:'+str(start['id']), 'text':
                           'Недостаточно места для тяжёлых запусков. Автоочистка резерва не восстановила запас за 5 минут.'})
    return alerts


def install(store: Any) -> None:
    with store.tx() as tx:
        if tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_notices'"):
            return
        tx.change("CREATE TABLE owner_notices(id TEXT PRIMARY KEY,kind TEXT NOT NULL,"
                  "source_key TEXT NOT NULL,state TEXT NOT NULL,delivery_state TEXT NOT NULL,"
                  "updated_at TEXT NOT NULL)", event="owner_notices.schema")


def prepare_ops_requests(store: Any) -> int:
    """A proved document with no current approval request needs planning work.

    Queue one bounded refinement, not an approval and not execution. The stable
    task ID fences retries, including after acceptance, restart and manual freeze.
    """
    from dev_mcp.owner.overview import current_ops_approval_requested
    from . import review
    with store.read() as tx:
        if not tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcomes'"):
            return 0
        rows = tx.query("SELECT DISTINCT o.id,o.project,o.spec,p.root FROM outcomes o "
                        "JOIN projects p ON p.name=o.project JOIN obstacles b ON b.outcome_id=o.id "
                        "WHERE o.title='owner_approved' AND o.state='open' AND p.active=1 "
                        "AND b.type='owner_action' AND b.state='open' "
                        "AND b.owner_action LIKE 'Одобрить операционный план с откатом%'")
    made = 0
    for row in rows:
        dependencies = json.loads(row['spec']).get('depends_on',[])
        if not dependencies:
            continue
        with store.read() as tx:
            proven = True
            for dep in dependencies:
                supplier = tx.one("SELECT state,evidence_spec FROM outcomes WHERE id=?",(dep,))
                if not supplier or supplier['state'] != 'done':
                    proven = False
                    break
                expected = json.loads(supplier['evidence_spec'])
                if not tx.one("SELECT 1 FROM evidence WHERE outcome_id=? AND verdict='pass' "
                              "AND kind=? AND subject_version=?",(dep,expected.get('kind'),expected.get('subject_version'))):
                    proven = False
                    break
        if not proven:
            continue
        root = Path(row['root'])
        if current_ops_approval_requested(root):
            continue
        task_id = ops_refinement_id(row['id'])
        ident = task_id.partition(':')[2]
        with store.tx() as tx:
            if tx.one("SELECT 1 FROM tasks WHERE project=? AND tz=?",(row['project'],task_id)):
                continue
            view = SimpleNamespace(tx=lambda:nullcontext(tx))
            tz = review.open_request(view,ident,kind='other',to='queue',project=row['project'],
                path=str(root/'intent/rollback-plan.md'),
                summary='Подготовить конкретное решение по операционному плану',
                detail=('Этап owner_approved ожидает согласования, но сам проверенный план пишет '
                        '«для текущего документа ничего». Подготовь готовый к решению следующий шаг: '
                        'точные ограниченные действия, входы и доказательства, длительность и ресурсы, '
                        'стоп-пороги, откат, стоимость и допустимый вариант по умолчанию. '
                        'Раздели уже разрешённую read-only подготовку и будущие эффекты. '
                        'Техническую подготовку в существующих разрешённых границах выполняй сам; '
                        'не проси повторного общего разрешения. Пилот, нагрузку, установку и смену '
                        'маршрута не выполняй. В «Нужно от владельца» оставь один конкретный текущий '
                        'выбор с границами и причинами; если исполняемого предложения ещё нет, '
                        'зарегистрируй точного технического поставщика и заблокируйся по его ID. '
                        'План и уведомление не являются разрешением. Не изменяй owner-approval.md '
                        'или события согласования. Ревью проверяет конкретность и сохранение границ.'))
            tx.change("UPDATE tasks SET priority=0,acceptance=? WHERE project=? AND tz=?",
                      (json.dumps(['py -3 .intent/check.py rollback_plan']),row['project'],tz),
                      event={'kind':'owner_plan_refinement_queued','project':row['project'],'tz':tz,
                             'payload':{'outcome_id':row['id'],'reason':'approval not concrete yet'}})
            made += 1
    return made


TICKTICK_PROJECT = machine.OWNER_PROJECT
TICKTICK_RECEIPTS = machine.HUB_FLEET / 'owner-ticktick'
TICKTICK_CHECK = subprocess.list2cmdline(['py', '-3', str(
    machine.HOME / 'src' / 'dev_mcp' / 'owner' / 'ticktick_receipt.py')]) + ' '


def queue_ticktick(store: Any, needs: dict[str, dict]) -> int:
    """Each active «нужно от владельца» becomes one fleet task: an agent in ChatGPT puts it into
    TickTick through its official OAuth connector and leaves a receipt (owner, 19.10.2026).

    The server holds no TickTick credential and never calls TickTick. One task per notice id;
    a notice resolved before delivery freezes its undelivered task. The receipt checks form only —
    the blind reviewer confirms the task in TickTick with the same connector.
    """
    from . import review
    made = 0
    with store.read() as tx:
        if not tx.one("SELECT 1 FROM projects WHERE name=? AND active=1", (TICKTICK_PROJECT,)):
            return 0
    for ident, action in needs.items():
        receipt = TICKTICK_RECEIPTS / f'{ident}.json'
        if receipt.exists():
            continue
        title = ' '.join(str(action.get('action') or 'Нужно действие владельца').split())[:200]
        name = ' '.join(str(action.get('title') or '').split())
        if not name or len(name) > 80:  # intent titles may be whole source paragraphs
            name = str(action.get('project') or name[:80])
        lines = [f"Проект: {name}",
                 f"Что нужно: {title}"]
        if action.get('default'):
            lines.append(f"По умолчанию, если не ответить: {action['default']}")
        if action.get('reason'):
            lines.append(f"Почему: {action['reason']}")
        with store.tx() as tx:
            if tx.one("SELECT 1 FROM events WHERE kind='owner_ticktick_queued' "
                      "AND json_extract(payload,'$.notice')=?", (ident,)):
                continue
            view = SimpleNamespace(tx=lambda: nullcontext(tx))
            tz = review.open_request(view, 'tt-' + ident[3:15], kind='other', to='queue',
                project=TICKTICK_PROJECT, path=str(receipt),
                summary='Поставить владельцу задачу в TickTick: ' + title[:120],
                detail=('Создай задачу в TickTick ВЛАДЕЛЬЦА через подключённый коннектор TickTick '
                        '(официальный MCP, create_task) — в «Входящие», без даты, приоритет высокий. '
                        'Заголовок: «Флот: ' + title[:120] + '». Описание — дословно:\n'
                        + '\n'.join(lines) + '\n'
                        'Затем запиши квитанцию ' + str(receipt) + ' — JSON с полями notice="' + ident
                        + '", ticktick_task_id, ticktick_project_id (из ответа create_task), title. '
                        'Без коннектора TickTick — fleet_block с причиной, не выдумывай id. '
                        'Ревью: открой задачу get_task_by_id по квитанции и сверь заголовок.'))
            if not tz:
                continue
            tx.change("UPDATE tasks SET priority=0,acceptance=? WHERE project=? AND tz=?",
                      (json.dumps([TICKTICK_CHECK + subprocess.list2cmdline([str(receipt)])]),
                       TICKTICK_PROJECT, tz),
                      event={'kind': 'owner_ticktick_queued', 'project': TICKTICK_PROJECT, 'tz': tz,
                             'payload': {'notice': ident}})
            made += 1
    return made


def freeze_stale_ticktick(store: Any) -> int:
    """A notice resolved before an agent delivered it — its delivery task is no longer wanted."""
    frozen = 0
    with store.tx() as tx:
        for row in tx.query(
                "SELECT e.tz, json_extract(e.payload,'$.notice') notice FROM events e "
                "JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
                "JOIN owner_notices n ON n.id=json_extract(e.payload,'$.notice') "
                "WHERE e.kind='owner_ticktick_queued' AND t.state='ready' AND n.state='resolved'"):
            tx.change("UPDATE tasks SET state='frozen' WHERE project=? AND tz=? AND state='ready'",
                      (TICKTICK_PROJECT, row['tz']),
                      event={'kind': 'owner_ticktick_obsolete', 'project': TICKTICK_PROJECT,
                             'tz': row['tz'], 'payload': {'notice': row['notice']}})
            frozen += 1
    return frozen


def reconcile(store: Any) -> int:
    from dev_mcp.owner.overview import snapshot
    install(store)
    prepare_ops_requests(store)
    data = snapshot(store)
    if data.get("status") not in {"ready", "empty"}:
        return 0  # a failed projection cannot clear previously actionable notices
    sources: dict[str, tuple[str, str]] = {}
    actions_by_key: dict[str, dict] = {}
    for action in data.get("human", {}).get("actions", []):
        key = json.dumps([action.get("project"), action.get("action_id"),
                          action.get("default"), action.get("reason")], ensure_ascii=False)
        sources["on-" + hashlib.sha256(key.encode()).hexdigest()[:24]] = ("needs", key)
        actions_by_key[key] = action
    # Completion evidence is generated by the server's stage prover. The
    # transport receives only its opaque event ID, never its private text.
    with store.read() as tx:
        for alert in attention(tx):
            key = alert['key']
            sources['on-'+hashlib.sha256(key.encode()).hexdigest()[:24]] = ('attention',key)
        present = tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcomes'")
        for row in (tx.query("SELECT e.id FROM events e JOIN projects p ON p.name=e.project "
                            "WHERE e.kind='owner_channel.sent' AND p.active=1 "
                            "AND json_extract(e.payload,'$.kind')='done' "
                            "AND EXISTS (SELECT 1 FROM outcomes o JOIN evidence v ON v.outcome_id=o.id "
                            "WHERE o.project=e.project AND o.title='handed_over' AND o.state='done' "
                            "AND v.verdict='pass' AND v.subject_version=json_extract(o.evidence_spec,'$.subject_version')) "
                            "ORDER BY e.id DESC LIMIT 16") if present else []):
            key = "done:" + str(row["id"])
            sources["on-" + hashlib.sha256(key.encode()).hexdigest()[:24]] = ("done", key)
    stamp = datetime.now(timezone.utc).isoformat()
    from .windows_notify import gui_session_available
    native = gui_session_available()
    with store.tx() as tx:
        rows = {r["id"]: r for r in tx.query("SELECT * FROM owner_notices")}
        latest = {}
        for row in rows.values():
            if row['source_key'] not in latest or row['updated_at'] > latest[row['source_key']]['updated_at']:
                latest[row['source_key']] = row
        normalized = {}
        for ident,(kind,key) in sources.items():
            prior = latest.get(key)
            if prior and prior['state'] == 'active':
                ident = prior['id']
            elif prior:
                ident = 'on-'+hashlib.sha256((key+prior['id']+prior['updated_at']).encode()).hexdigest()[:24]
            normalized[ident] = (kind,key)
        sources = normalized
        for ident, (kind, key) in sources.items():
            old = rows.get(ident)
            if old is None:
                tx.change("INSERT INTO owner_notices VALUES(?,?,?,'active','pending',?)",
                          (ident, kind, key, stamp), event={"kind":"owner_notice.queued","payload":{"id":ident}})
            elif old["state"] != "active":
                tx.change("UPDATE owner_notices SET state='active',delivery_state='pending',updated_at=? WHERE id=?",
                          (stamp,ident), event={"kind":"owner_notice.reopened","payload":{"id":ident}})
            elif native is False and old["delivery_state"] == "api_accepted":
                # Windows may accept the API while globally hiding banners.
                # Reconcile this real channel change through browser attention;
                # a viewed/approved state is never inferred from API acceptance.
                tx.change("UPDATE owner_notices SET delivery_state='pending',updated_at=? WHERE id=?",
                          (stamp,ident), event={"kind":"owner_notice.fallback_needed","payload":{"id":ident}})
        for ident, old in rows.items():
            if ident not in sources and old["state"] == "active":
                tx.change("UPDATE owner_notices SET state='resolved',updated_at=? WHERE id=?", (stamp,ident),
                          event={"kind":"owner_notice.resolved","payload":{"id":ident}})
        tx.record_event(kind='owner_notice_projection',payload={'current':len(sources),'native_toasts':native})
    try:
        from dev_mcp.owner import ticktick_oauth
        # Once the server holds its own TickTick connection, the board (ticktick_board) asks the
        # owner directly; agent-delivered cards would duplicate every question.
        if not ticktick_oauth.status().get('connected'):
            queue_ticktick(store, {ident: actions_by_key[key] for ident, (kind, key) in sources.items()
                                   if kind == 'needs' and key in actions_by_key})
        freeze_stale_ticktick(store)
    except (OSError, ValueError) as exc:  # delivery is a convenience; projection already stands
        logging.getLogger(__name__).warning('owner TickTick queue skipped: %s', exc)
    return len(sources)


def lines(store: Any) -> list[str]:
    with store.read() as tx:
        if not tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_notices'"):
            return []
        rows = tx.query("SELECT id,kind FROM owner_notices WHERE state='active' "
                        "AND delivery_state NOT IN ('api_accepted','clicked','browser_attention') "
                        "ORDER BY CASE kind WHEN 'needs' THEN 0 WHEN 'attention' THEN 1 ELSE 2 END,updated_at,id LIMIT 3")
        counts = {r['delivery_state']:r['n'] for r in tx.query(
            "SELECT delivery_state,COUNT(*) n FROM owner_notices WHERE state='active' GROUP BY delivery_state")}
        required = int(tx.scalar("SELECT COUNT(*) FROM owner_notices WHERE state='active' AND "
                                 "(kind IN ('needs','attention') OR delivery_state NOT IN ('api_accepted','clicked','browser_attention'))") or 0)
        heartbeat = tx.one("SELECT ts,payload FROM events WHERE kind='owner_notice_projection' ORDER BY id DESC LIMIT 1")
    native = 'allowed' if heartbeat and json.loads(heartbeat['payload']).get('native_toasts') is True else 'blocked'
    head = (f"owner-channel: FleetChrome; pending={sum(v for k,v in counts.items() if k not in {'api_accepted','clicked','browser_attention'})}; "
            f"accepted={counts.get('api_accepted',0)+counts.get('clicked',0)}; browser_signal={counts.get('browser_attention',0)}; "
            f"denied={counts.get('denied',0)}; attention={required}; native={native}; "
            f"observed={heartbeat['ts'] if heartbeat else 'unconfirmed'}")
    return [head,*["owner-notice: " + json.dumps({"id":r["id"],"kind":r["kind"]},separators=(",",":")) for r in rows]]


def record_transport(store: Any, report: dict[str, Any]) -> bool:
    if (not isinstance(report,dict) or set(report) != {"id","state"}
            or not isinstance(report["id"],str) or not _ID.fullmatch(report["id"])
            or report["state"] not in {"api_accepted","denied","error","clicked","browser_attention"}):
        raise ValueError("invalid owner-notice transport receipt")
    with store.tx() as tx:
        row = tx.one("SELECT state,delivery_state FROM owner_notices WHERE id=?", (report["id"],))
        if row is None or row["state"] != "active":
            return False  # obsolete receipt is consumed without touching a new episode
        if row["delivery_state"] == report["state"] or row["delivery_state"] == "clicked":
            return True
        tx.change("UPDATE owner_notices SET delivery_state=?,updated_at=? WHERE id=?",
                  (report["state"],datetime.now(timezone.utc).isoformat(),report["id"]),
                  event={"kind":"owner_notice.transport","payload":report})
    return True


def run_worker(store: Any, stop_event: Any) -> None:
    while not stop_event.is_set():
        try:
            reconcile(store)
        except Exception:
            logging.getLogger(__name__).exception("owner notice projection deferred")
        stop_event.wait(30)
